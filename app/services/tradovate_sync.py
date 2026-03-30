"""
Tradovate WebSocket sync — fetch historical orders and fills.

The REST API only returns current-session data. Historical fills
require connecting via WebSocket and sending a user/syncrequest.
"""
import json
import asyncio
import logging
from datetime import datetime, timezone

import websockets

logger = logging.getLogger(__name__)

# Tradovate WebSocket message format:
#   request:  "endpoint\nid\n\njson_body"
#   response: "a[\"endpoint\\nid\\n\\njson_body\"]" (SockJS framed)


def _parse_ws_messages(raw) -> list[dict]:
    """Parse SockJS-framed WebSocket messages from Tradovate."""
    messages = []
    raw_str = str(raw) if not isinstance(raw, str) else raw

    if raw_str.startswith("a["):
        try:
            outer = json.loads(raw_str[1:])  # strip 'a' prefix
            for item in outer:
                if isinstance(item, dict):
                    # Already parsed as dict
                    messages.append({"endpoint": "", "id": "", "body": item})
                elif isinstance(item, str):
                    # "endpoint\nid\n\njson_body"
                    parts = item.split("\n", 3)
                    if len(parts) >= 4:
                        try:
                            body = json.loads(parts[3])
                            messages.append({"endpoint": parts[0], "id": parts[1], "body": body})
                        except json.JSONDecodeError:
                            pass
                    elif len(parts) >= 1:
                        # Try parsing the whole thing as JSON
                        try:
                            body = json.loads(item)
                            messages.append({"endpoint": "", "id": "", "body": body})
                        except json.JSONDecodeError:
                            pass
        except (json.JSONDecodeError, TypeError):
            pass
    elif raw_str.startswith("o"):
        pass  # connection open frame
    elif raw_str.startswith("h"):
        pass  # heartbeat
    return messages


async def sync_fills(
    base_url: str,
    access_token: str,
    account_id: int,
    account_name: str,
) -> list[dict]:
    """
    Connect to Tradovate WebSocket, authenticate, request sync,
    and return all fill/order data for the account.

    Returns list of dicts with keys: action, symbol, price, qty, multiplier, timestamp, order_id
    """
    ws_url = base_url.replace("https://", "wss://").replace("/v1", "/v1/websocket")
    logger.info(f"Tradovate sync: connecting to {ws_url} for account {account_name}")

    fills = []
    orders_by_id = {}
    request_id = 1

    try:
        async with websockets.connect(ws_url, close_timeout=5) as ws:
            # Wait for open frame
            open_msg = await asyncio.wait_for(ws.recv(), timeout=10)
            logger.debug(f"WS open: {str(open_msg)[:100]}")

            # Authenticate — Tradovate expects the token as the body of an "authorize" request
            auth_body = json.dumps(access_token) if isinstance(access_token, str) else json.dumps(str(access_token))
            auth_msg = f"authorize\n{request_id}\n\n{access_token}"
            request_id += 1
            await ws.send(auth_msg)

            auth_resp = await asyncio.wait_for(ws.recv(), timeout=10)
            auth_str = str(auth_resp)
            logger.info(f"Tradovate WS auth response ({account_name}): {auth_str[:300]}")

            # Check for auth failure
            if "error" in auth_str.lower() or "unauthorized" in auth_str.lower():
                logger.warning(f"Tradovate WS auth failed for {account_name}: {auth_str[:200]}")
                return fills, orders_by_id

            # Request sync for this account
            sync_body = json.dumps({"accounts": [account_id]})
            sync_msg = f"user/syncrequest\n{request_id}\n\n{sync_body}"
            request_id += 1
            await ws.send(sync_msg)
            logger.info(f"Tradovate WS sync request sent for {account_name} (acct_id={account_id})")

            # Collect responses until we get no more data
            # Tradovate sends multiple frames with different entity types
            empty_count = 0
            max_messages = 200  # safety limit

            for _ in range(max_messages):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    empty_count += 1
                    if empty_count >= 2:
                        break
                    continue

                empty_count = 0
                raw_str = str(raw)
                if len(fills) == 0 and len(orders_by_id) == 0:
                    # Log first few messages for debugging
                    logger.debug(f"Tradovate WS [{account_name}]: {raw_str[:500]}")

                parsed = _parse_ws_messages(raw)

                for msg in parsed:
                    body = msg.get("body", {})

                    # Sync response contains multiple entity types
                    # Orders come as lists under various keys
                    if isinstance(body, dict):
                        # Look for orders
                        for key in ("orders", "d"):
                            if key in body and isinstance(body[key], list):
                                for order in body[key]:
                                    if isinstance(order, dict) and "id" in order:
                                        orders_by_id[order["id"]] = order

                        # Look for fills
                        for key in ("fills", ):
                            if key in body and isinstance(body[key], list):
                                fills.extend(body[key])

                        # Look for execution reports
                        for key in ("executionReports", ):
                            if key in body and isinstance(body[key], list):
                                for er in body[key]:
                                    if isinstance(er, dict) and er.get("execType") in ("Fill", "Trade"):
                                        fills.append(er)

                    # Sometimes data comes as a flat list
                    elif isinstance(body, list):
                        for item in body:
                            if isinstance(item, dict):
                                if "orderId" in item and "price" in item:
                                    fills.append(item)
                                elif "action" in item and "id" in item:
                                    orders_by_id[item["id"]] = item

        logger.info(
            f"Tradovate sync complete for {account_name}: "
            f"{len(fills)} fills, {len(orders_by_id)} orders"
        )

    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"Tradovate WS closed: {e}")
    except Exception as e:
        logger.exception(f"Tradovate sync error for {account_name}: {e}")

    return fills, orders_by_id
