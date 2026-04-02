"""
Server-Sent Events bus backed by Redis Pub/Sub.

Publishers (relay, worker) call push_delivery_event() which publishes to
Redis channel "sse:delivery". The SSE endpoint subscribes to the same channel
and fans out to connected browser clients.

Falls back to in-process fan-out if Redis is unavailable — preserves the
single-monolith behavior until the service split is complete.
"""
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# Local subscriber queues: connection_id -> asyncio.Queue
_subscribers: dict[str, asyncio.Queue] = {}

_HEARTBEAT_INTERVAL = 15  # seconds
_SSE_CHANNEL = "sse:delivery"

# Background task that relays Redis pub/sub -> local queues
_redis_listener_task: asyncio.Task | None = None


def _subscriber_count() -> int:
    return len(_subscribers)


async def _publish_to_redis(data: str) -> bool:
    """Publish to Redis. Returns True on success."""
    try:
        from app.redis import get_redis
        r = await get_redis()
        if r:
            await r.publish(_SSE_CHANNEL, data)
            return True
    except Exception:
        logger.debug("Redis SSE publish failed, using local fan-out")
    return False


def _fan_out_local(data: str) -> None:
    """Push data to all local subscriber queues."""
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


def push_delivery_event(event: dict) -> None:
    """
    Publish a delivery event to all SSE clients.
    Publishes to Redis (cross-service) and falls back to local fan-out.
    Called from order_processor after a webhook is handled.
    """
    data = json.dumps(event)

    # Try Redis publish (async, fire-and-forget from sync context)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_publish_to_redis(data))
    except RuntimeError:
        # No running loop — fall back to local
        _fan_out_local(data)
        return

    # If no Redis listener is running, also fan out locally as fallback
    if _redis_listener_task is None or _redis_listener_task.done():
        _fan_out_local(data)


async def _redis_sse_listener():
    """
    Background task: subscribe to Redis sse:delivery channel and
    fan out received messages to all local SSE connections.
    """
    import redis.asyncio as aioredis
    from app.config import get_settings

    while True:
        try:
            settings = get_settings()
            if not settings.redis_url:
                await asyncio.sleep(30)
                continue

            # Dedicated connection for pub/sub — no socket timeout
            # (pub/sub blocks indefinitely waiting for messages)
            r = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            pubsub = r.pubsub()
            await pubsub.subscribe(_SSE_CHANNEL)
            logger.info("SSE Redis subscriber connected")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    _fan_out_local(message["data"])

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"SSE Redis subscriber error: {e}, reconnecting in 2s")
            await asyncio.sleep(2)


def start_sse_listener() -> asyncio.Task | None:
    """Start the Redis SSE listener background task. Called from app lifespan."""
    global _redis_listener_task
    try:
        _redis_listener_task = asyncio.create_task(
            _redis_sse_listener(), name="sse_redis_listener"
        )
        return _redis_listener_task
    except Exception:
        logger.warning("Could not start SSE Redis listener")
        return None


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
        yield f"event: connected\ndata: {json.dumps({'conn_id': conn_id})}\n\n"

        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
                yield f"event: delivery\ndata: {data}\n\n"
            except asyncio.TimeoutError:
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
