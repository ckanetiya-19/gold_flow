"""
feed_hub.py - SHARED iTick real-spot feed hub (Option B).
============================================================================
Why: iTick allows ~1 connection per key. With a single key, 9060/9080/9100
cannot each open their own iTick socket. So this hub opens the ONE iTick
connection (real spot XAUUSD: real price + real per-trade volume) and
re-broadcasts it on a local WebSocket, exactly like the retired Port 9000 did
- but iTick-only (no PAXG) and with a staleness WATCHDOG so it can't silently
zombie (the failure mode that plagued Port 9000's Binance stream).

Downstream ports (9060/9080/9100) subscribe to ws://127.0.0.1:9200/ws with
FEED_MODE=hub and consume the same tick/depth/mark_price messages they always
did. Port 9900 stays independent on its own key.

When extra iTick keys arrive: flip each port to FEED_MODE=direct (its own
ITickFeed) and this hub is no longer needed - no code change, just env vars.

Run:
    set GOLDFLOW_HUB_ITICK_KEY=<key>
    python feed_hub.py                      # serves ws://127.0.0.1:9200/ws
"""
import asyncio
import json
import logging
import os
import time

import uvicorn
from fastapi import FastAPI, WebSocket

from itick_feed import ITickFeed

HOST = "127.0.0.1"
PORT = int(os.getenv("GOLDFLOW_HUB_PORT", "9200"))
KEY = os.getenv("GOLDFLOW_HUB_ITICK_KEY", "") or os.getenv("GOLDFLOW_9060_ITICK_KEY", "")
SYMBOL = os.getenv("GOLDFLOW_HUB_ITICK_SYMBOL", "XAUUSD$GB")
STALE_RECONNECT_SEC = int(os.getenv("GOLDFLOW_HUB_STALE_SEC", "120"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s INFO [feed-hub] %(message)s")
logger = logging.getLogger("feed_hub")
app = FastAPI(title="Goldflow iTick Feed Hub (Port 9200)")

clients = set()
state = {"last_price": None, "last_tick_ts": None}


async def _send(ws, raw):
    try:
        await ws.send_text(raw)
    except Exception:
        clients.discard(ws)


def on_message(raw):
    """Called from inside the event loop (ITickFeed runs as a task). Fan the
    raw message out to every subscriber, and track price/staleness."""
    try:
        msg = json.loads(raw)
        if msg.get("type") == "tick":
            state["last_price"] = msg.get("price")
            state["last_tick_ts"] = time.time()
        elif msg.get("type") == "mark_price" and state["last_price"] is None:
            state["last_price"] = msg.get("price")
    except Exception:
        pass
    for c in list(clients):
        asyncio.create_task(_send(c, raw))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    logger.info(f"Subscriber connected. Total: {len(clients)}")
    try:
        # init frame so subscribers start like they did with Port 9000
        await websocket.send_text(json.dumps({"type": "init", "candles": [],
                                              "last_price": state["last_price"]}))
        while True:
            await websocket.receive_text()      # keep-alive; ignore content
    except Exception:
        pass
    finally:
        clients.discard(websocket)
        logger.info(f"Subscriber disconnected. Total: {len(clients)}")


@app.get("/health")
async def health():
    ago = round(time.time() - state["last_tick_ts"], 1) if state["last_tick_ts"] else None
    return {"connected": _feed.connected if _feed else False,
            "subscribers": len(clients), "last_price": state["last_price"],
            "last_tick_sec_ago": ago, "reconnects": _feed.reconnects if _feed else 0}


_feed = None


async def _watchdog():
    """Force a reconnect if the feed is 'connected' but has gone silent past
    the threshold - guards against a zombie socket (Port 9000's old failure)."""
    while True:
        await asyncio.sleep(30)
        lt = state["last_tick_ts"]
        if _feed and _feed.connected and lt and (time.time() - lt) > STALE_RECONNECT_SEC:
            logger.warning(f"Watchdog: no ticks for >{STALE_RECONNECT_SEC}s while connected - forcing reconnect.")
            await _feed.force_reconnect()


@app.on_event("startup")
async def startup():
    global _feed
    _feed = ITickFeed(key=KEY, on_message=on_message, logger=logger, name="hub", symbol=SYMBOL)
    asyncio.create_task(_feed.run())
    asyncio.create_task(_watchdog())
    logger.info(f"iTick Feed Hub on {HOST}:{PORT} -> broadcasting real spot. "
                f"Key: {'set' if KEY else 'MISSING'} | stale-reconnect {STALE_RECONNECT_SEC}s")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
