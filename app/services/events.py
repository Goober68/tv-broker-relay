"""
In-process Server-Sent Events bus.

Maintains a set of per-connection asyncio queues. When a webhook is processed
order_processor calls push_delivery_event() to fan out to all connected clients.

Works correctly with --workers 1 (single uvicorn process). If you ever move to
multiple workers you'd need Redis pub/sub instead — but the interface here would
stay the same.
"""
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# Set of active subscriber queues: connection_id → asyncio.Queue
_subscribers: dict[str, asyncio.Queue] = {}

_HEARTBEAT_INTERVAL = 15  # seconds — keeps connections alive through proxies


def _subscriber_count() -> int:
    return len(_subscribers)


def push_delivery_event(event: dict) -> None:
    """
    Fan out a delivery event to all connected SSE clients.
    Called from order_processor after a webhook is handled.
    Safe to call from async context — uses put_nowait.
    """
    if not _subscribers:
        return
    data = json.dumps(event)
    dead = []
    for conn_id, q in _subscribers.items():
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(f"SSE queue full for connection {conn_id} — dropping event")
        except Exception:
            dead.append(conn_id)
    for conn_id in dead:
        _subscribers.pop(conn_id, None)
    if _subscribers:
        logger.debug(f"SSE: pushed delivery event to {len(_subscribers)} client(s)")


async def event_stream(tenant_id: uuid.UUID) -> AsyncGenerator[str, None]:
    """
    Async generator yielding SSE-formatted strings for one client connection.
    Registers a queue, yields events as they arrive, sends heartbeats,
    and cleans up on disconnect.
    """
    conn_id = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers[conn_id] = q
    logger.info(
        f"SSE client connected: tenant={tenant_id} conn={conn_id} "
        f"(total={_subscriber_count()})"
    )

    try:
        # Send an immediate connected confirmation
        yield f"event: connected\ndata: {json.dumps({'conn_id': conn_id})}\n\n"

        while True:
            try:
                # Wait for an event, timing out after HEARTBEAT_INTERVAL
                data = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
                yield f"event: delivery\ndata: {data}\n\n"
            except asyncio.TimeoutError:
                # Send a heartbeat comment to keep the connection alive
                yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break

    except GeneratorExit:
        pass
    finally:
        _subscribers.pop(conn_id, None)
        logger.info(
            f"SSE client disconnected: conn={conn_id} "
            f"(total={_subscriber_count()})"
        )
