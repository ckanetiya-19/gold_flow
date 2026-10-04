"""
PORT 9000 - Core WebSocket Data Engine V2 (Hedge-Fund-Grade Pilot Upgrade)
============================================================================
NEW file, NEW port (9000). PORT_8000_CORE_WEBSOCKET_ENGINE.py and its live
port 8000 are completely untouched and keep running independently.

Upgrades over the V1 engine (PORT_8000_CORE_WEBSOCKET_ENGINE.py):
  1. REAL order-book depth from Binance (paxgusdt@depth20@100ms) instead of
     no depth data at all in V1 (other V1 dashboards fabricate DOM with
     random.uniform()).
  2. REAL trade volume + real aggressor side from Binance aggTrade stream
     (V1 downstream dashboards use random.randint() for volume).
  3. Persistent, queryable time-series storage in QuestDB (ticks_v2 /
     depth_v2 tables) via the ILP fast-ingestion protocol, so every tick
     and depth snapshot can be replayed/backtested later. V1 only ever
     broadcasts data - nothing is durably stored.
  4. Exponential-backoff reconnect with structured logging instead of a
     silent `except: pass` that can leave the feed stale with no signal.
  5. /health endpoint reporting feed staleness, reconnect count, and
     ingestion counters - so a monitoring tool (or a human) can tell the
     feed is alive without reading logs.
  6. Zero hardcoded secrets - all connection settings come from environment
     variables with safe localhost defaults.
  7. Still binds to 127.0.0.1 only (same safe default V1 already used) -
     not reachable from the LAN.

Requires: QuestDB running locally (see questdb_v2/ folder - Docker
container `questdb_goldflow_v2`, ILP on 127.0.0.1:9009, HTTP console on
127.0.0.1:9010 with Basic Auth).

Run: python PORT_9000_CORE_WEBSOCKET_ENGINE_V2.py
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from questdb import Sender, TimestampNanos

# =============================================================================
# CONFIG - environment variables only, no hardcoded secrets/URLs
# =============================================================================
SYMBOL = os.getenv("GOLDFLOW_SYMBOL", "XAUUSD")
BINANCE_SYMBOL = os.getenv("GOLDFLOW_BINANCE_SYMBOL", "paxgusdt")
BINANCE_WS_URL = os.getenv(
    "GOLDFLOW_BINANCE_WS_URL",
    "wss://stream.binance.com:9443/stream?streams="
    f"{BINANCE_SYMBOL}@aggTrade/{BINANCE_SYMBOL}@depth20@100ms/{BINANCE_SYMBOL}@kline_1m",
)
QUESTDB_ILP_CONF = os.getenv("GOLDFLOW_QUESTDB_ILP_CONF", "tcp::addr=127.0.0.1:9009;")
ENGINE_HOST = os.getenv("GOLDFLOW_ENGINE_HOST", "127.0.0.1")
ENGINE_PORT = int(os.getenv("GOLDFLOW_ENGINE_PORT", "9000"))
MAX_BACKOFF_SECONDS = 30
STALE_FEED_THRESHOLD_SECONDS = 15

# Optional real XAUUSD spot price cross-reference (env var only, never hardcoded).
# Set once here and every downstream dashboard (9050-9090) gets it automatically
# via the existing WebSocket broadcast - no per-port API setup needed.
GOLDAPI_KEY = os.getenv("GOLDFLOW_GOLDAPI_KEY", "")
GOLDAPI_KEY_BACKUP = os.getenv("GOLDFLOW_GOLDAPI_KEY_BACKUP", "")
GOLDAPI_URL = "https://www.goldapi.io/api/XAU/USD"
# Conservative default: free-tier GoldAPI plans have small monthly quotas.
# Override with GOLDFLOW_GOLDAPI_POLL_SECONDS if your plan allows more frequent polling.
GOLDAPI_POLL_SECONDS = int(os.getenv("GOLDFLOW_GOLDAPI_POLL_SECONDS", "300"))

# Real spot XAUUSD tick feed (iTick) - becomes the PRIMARY price driver for
# every downstream chart's OHLC, so the price matches what a real forex/gold
# broker shows (Binance PAXG remains the volume/CVD source - spot forex has
# no consolidated real trade volume, see design discussion). Env var only.
ITICK_API_KEY = os.getenv("GOLDFLOW_ITICK_API_KEY", "")
ITICK_WS_URL = os.getenv("GOLDFLOW_ITICK_WS_URL", "wss://api-free.itick.org/forex")
ITICK_SYMBOL = os.getenv("GOLDFLOW_ITICK_SYMBOL", "XAUUSD$GB")
ITICK_STALE_FALLBACK_SECONDS = 5  # if no iTick tick in this long, Binance mid fills in

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [core-engine-v2] %(message)s",
)
logger = logging.getLogger("core_engine_v2")

app = FastAPI(title="Goldflow Core WebSocket Engine V2")

# =============================================================================
# SHARED STATE
# =============================================================================
state = {
    "last_price": None,
    "last_tick_time": None,
    "last_mark_time": None,
    "last_itick_time": None,
    "itick_connected": False,
    "itick_reconnect_count": 0,
    "connected": False,
    "reconnect_count": 0,
    "ticks_ingested": 0,
    "depth_snapshots_ingested": 0,
    "questdb_errors": 0,
    "started_at": datetime.now(timezone.utc),
}
real_spot_gold = {
    "price": None, "bid": None, "ask": None, "source": None, "last_update": None, "errors": 0,
}
_main_event_loop = None  # captured at startup so the sync GoldAPI poller thread can broadcast
recent_candles = deque(maxlen=500)
subscribers: set[WebSocket] = set()
_questdb_sender: "Sender | None" = None


# =============================================================================
# REAL SPOT GOLD (GoldAPI.io) - optional cross-reference on top of the
# Binance PAXG proxy, shared to every downstream port via the same broadcast.
# =============================================================================
def _fetch_goldapi_spot(api_key: str) -> dict | None:
    try:
        r = requests.get(
            GOLDAPI_URL,
            headers={"x-access-token": api_key, "Content-Type": "application/json"},
            timeout=5,
        )
        if r.status_code == 200:
            d = r.json()
            price = float(d.get("price", 0))
            if price > 0:
                return {
                    "price": price,
                    "bid": float(d.get("bid", price)),
                    "ask": float(d.get("ask", price)),
                }
        elif r.status_code == 401:
            logger.warning("GoldAPI key rejected (401) - check GOLDFLOW_GOLDAPI_KEY.")
        return None
    except Exception:
        logger.exception("GoldAPI request failed")
        return None


def goldapi_poller() -> None:
    """Runs in a plain thread (not asyncio) - polls infrequently to respect
    free-tier quotas, then hands the result to the event loop for broadcast."""
    if not GOLDAPI_KEY:
        logger.info("GOLDFLOW_GOLDAPI_KEY not set - real spot gold cross-reference disabled "
                     "(Binance PAXG proxy remains the primary/only price feed).")
        return

    logger.info(f"Real spot gold (GoldAPI.io) poller started - every {GOLDAPI_POLL_SECONDS}s.")
    while True:
        result = _fetch_goldapi_spot(GOLDAPI_KEY)
        if result is None and GOLDAPI_KEY_BACKUP:
            logger.info("Primary GoldAPI key failed; trying backup key.")
            result = _fetch_goldapi_spot(GOLDAPI_KEY_BACKUP)

        if result is not None:
            real_spot_gold.update(result)
            real_spot_gold["source"] = "GoldAPI.io"
            real_spot_gold["last_update"] = datetime.now(timezone.utc)
            real_spot_gold["errors"] = 0
            logger.info(f"Real spot XAUUSD: ${result['price']:.2f} (bid ${result['bid']:.2f} / ask ${result['ask']:.2f})")
            if _main_event_loop is not None:
                asyncio.run_coroutine_threadsafe(
                    broadcast({
                        "type": "real_spot",
                        "symbol": SYMBOL,
                        "price": result["price"],
                        "bid": result["bid"],
                        "ask": result["ask"],
                        "source": "GoldAPI.io",
                    }),
                    _main_event_loop,
                )
        else:
            real_spot_gold["errors"] += 1

        time.sleep(GOLDAPI_POLL_SECONDS)


# =============================================================================
# QUESTDB INGESTION (real, durable storage - runs off the event loop thread)
# =============================================================================
def _get_sender() -> Sender:
    global _questdb_sender
    if _questdb_sender is None:
        _questdb_sender = Sender.from_conf(QUESTDB_ILP_CONF)
        _questdb_sender.establish()
        logger.info("QuestDB ILP sender (re)established.")
    return _questdb_sender


def ingest_tick(price: float, volume: float, is_buy: bool, ts_ms: int) -> None:
    global _questdb_sender
    try:
        sender = _get_sender()
        sender.row(
            "ticks_v2",
            symbols={"symbol": SYMBOL, "side": "BUY" if is_buy else "SELL"},
            columns={"price": price, "volume": volume},
            at=TimestampNanos(ts_ms * 1_000_000),
        )
        sender.flush()
        state["ticks_ingested"] += 1
    except Exception:
        logger.exception("QuestDB tick ingest failed; will reconnect on next tick")
        state["questdb_errors"] += 1
        _questdb_sender = None


def ingest_depth(bids: list, asks: list, ts_ms: int) -> None:
    global _questdb_sender
    try:
        sender = _get_sender()
        ts = TimestampNanos(ts_ms * 1_000_000)
        for level, (p, q) in enumerate(bids[:10]):
            sender.row(
                "depth_v2",
                symbols={"symbol": SYMBOL, "side": "BID"},
                columns={"level": level, "price": float(p), "qty": float(q)},
                at=ts,
            )
        for level, (p, q) in enumerate(asks[:10]):
            sender.row(
                "depth_v2",
                symbols={"symbol": SYMBOL, "side": "ASK"},
                columns={"level": level, "price": float(p), "qty": float(q)},
                at=ts,
            )
        sender.flush()
        state["depth_snapshots_ingested"] += 1
    except Exception:
        logger.exception("QuestDB depth ingest failed; will reconnect on next snapshot")
        state["questdb_errors"] += 1
        _questdb_sender = None


# =============================================================================
# WEBSOCKET PUB/SUB (same relay role as V1 - downstream dashboards subscribe)
# =============================================================================
async def broadcast(payload: dict) -> None:
    if not subscribers:
        return
    msg = json.dumps(payload)
    dead = []
    for ws in subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        subscribers.discard(ws)


# =============================================================================
# BINANCE LIVE FEED (real depth + real trade volume/side, resilient reconnect)
# =============================================================================
async def handle_binance_message(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Malformed JSON from Binance feed, skipping frame")
        return

    stream = msg.get("stream", "")
    data = msg.get("data", {})

    if stream.endswith("@aggTrade"):
        price = float(data["p"])
        volume = float(data["q"])
        is_buyer_maker = data["m"]  # True => aggressive SELL hit the bid
        is_buy = not is_buyer_maker
        ts_ms = int(data["T"])

        state["last_price"] = price
        state["last_tick_time"] = datetime.now(timezone.utc)

        await asyncio.to_thread(ingest_tick, price, volume, is_buy, ts_ms)
        await broadcast({
            "type": "tick",
            "symbol": SYMBOL,
            "price": price,
            "volume": volume,
            "side": "BUY" if is_buy else "SELL",
            "timestamp": ts_ms,
        })

    elif "@depth" in stream:
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        ts_ms = int(time.time() * 1000)

        await asyncio.to_thread(ingest_depth, bids, asks, ts_ms)
        await broadcast({
            "type": "depth",
            "symbol": SYMBOL,
            "bids": bids[:10],
            "asks": asks[:10],
            "timestamp": ts_ms,
        })

        # MARK PRICE (Binance fallback): PAXGUSDT trades infrequently, so real
        # aggTrade ticks can go quiet for minutes while the order book keeps
        # moving. Without this, the chart would freeze during those gaps.
        # This only fires when iTick's real spot feed hasn't sent anything
        # recently (market closed, iTick down, or key not configured) - when
        # iTick is live, ITS ticks drive the price instead (see
        # itick_feed_loop), since that's the real spot price a broker shows.
        now_utc = datetime.now(timezone.utc)
        last_tick = state["last_tick_time"]
        seconds_since_tick = (now_utc - last_tick).total_seconds() if last_tick else 999
        seconds_since_mark = (now_utc - state["last_mark_time"]).total_seconds() if state["last_mark_time"] else 999
        last_itick = state["last_itick_time"]
        seconds_since_itick = (now_utc - last_itick).total_seconds() if last_itick else 999
        if (bids and asks and seconds_since_tick > 2.0 and seconds_since_mark >= 1.0
                and seconds_since_itick > ITICK_STALE_FALLBACK_SECONDS):
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
            state["last_price"] = mid
            state["last_mark_time"] = now_utc
            await broadcast({
                "type": "mark_price",
                "symbol": SYMBOL,
                "price": round(mid, 2),
                "bid": float(bids[0][0]),
                "ask": float(asks[0][0]),
                "timestamp": ts_ms,
            })

    elif stream.endswith("@kline_1m"):
        k = data.get("k", {})
        candle = {
            "time": k["t"],
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "closed": bool(k["x"]),
        }
        recent_candles.append(candle)
        await broadcast({"type": "kline", "symbol": SYMBOL, "candle": candle})


async def binance_feed_loop() -> None:
    backoff = 1
    while True:
        try:
            logger.info(f"Connecting to Binance combined stream ({BINANCE_SYMBOL})...")
            async with websockets.connect(
                BINANCE_WS_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                state["connected"] = True
                backoff = 1
                logger.info("Connected to Binance live feed.")
                async for raw in ws:
                    await handle_binance_message(raw)
        except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            state["connected"] = False
            state["reconnect_count"] += 1
            logger.warning(
                f"Binance feed disconnected ({e!r}); reconnecting in {backoff}s "
                f"(attempt #{state['reconnect_count']})"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except Exception:
            state["connected"] = False
            logger.exception("Unexpected error in Binance feed loop; retrying in 5s")
            await asyncio.sleep(5)


# =============================================================================
# ITICK REAL SPOT XAUUSD FEED (primary price driver for downstream charts)
# =============================================================================
async def handle_itick_message(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    data = msg.get("data")
    if not isinstance(data, dict):
        return

    # REAL BID/ASK DEPTH (added so the price moves like a real broker feed
    # instead of a "last trade" tape). iTick's "tick"/"quote" messages only
    # carry the LAST DONE (ld) price - for OTC spot gold, real dealer prints
    # can be sparse, so ld can sit flat for a long stretch then jump, which
    # is exactly the "choti choti, not live-like" pattern observed and
    # confirmed against a live capture before this change. "depth" messages
    # carry the real top-of-book bid/ask, which updates far more
    # continuously - confirmed against iTick's own docs
    # (https://docs.itick.org/en/websocket/forex) before writing this, not
    # guessed. This branch is purely ADDITIVE: if depth data is ever
    # missing/malformed, it returns without touching state, and the
    # unchanged ld-based path below keeps working exactly as it did before.
    if data.get("type") == "depth":
        try:
            bids = data.get("b") or []
            asks = data.get("a") or []
            if not bids or not asks:
                return
            best_bid = float(bids[0]["p"])
            best_ask = float(asks[0]["p"])
            if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
                return
            mid = (best_bid + best_ask) / 2.0
        except (KeyError, TypeError, ValueError, IndexError):
            return

        ts_ms = int(data.get("t", time.time() * 1000))
        now_utc = datetime.now(timezone.utc)
        state["last_itick_time"] = now_utc
        state["last_price"] = mid

        await broadcast({
            "type": "mark_price",
            "symbol": SYMBOL,
            "price": round(mid, 2),
            "bid": round(best_bid, 2),
            "ask": round(best_ask, 2),
            "timestamp": ts_ms,
            "source": "iTick (real bid/ask mid)",
        })
        return

    if "ld" not in data:
        return  # connection/auth/subscribe ack messages, not a price update

    try:
        price = float(data["ld"])
    except (TypeError, ValueError):
        return
    if price <= 0:
        return

    ts_ms = int(data.get("t", time.time() * 1000))
    now_utc = datetime.now(timezone.utc)
    state["last_itick_time"] = now_utc
    state["last_price"] = price

    await broadcast({
        "type": "mark_price",
        "symbol": SYMBOL,
        "price": round(price, 2),
        "bid": round(price, 2),
        "ask": round(price, 2),
        "timestamp": ts_ms,
        "source": "iTick (real spot XAUUSD)",
    })


async def itick_feed_loop() -> None:
    if not ITICK_API_KEY:
        logger.info("GOLDFLOW_ITICK_API_KEY not set - real spot XAUUSD tick feed disabled "
                     "(Binance PAXG mid-price remains the chart's price driver).")
        return

    # iTick's free tier strictly rate-limits new connection attempts (HTTP 429
    # at the WebSocket handshake). Binance/Port 9000's generic 1s-start
    # backoff was too aggressive for it - every burst of reconnects tripped
    # the rate limit and kept the feed down for many minutes at a time. iTick
    # gets its own gentler schedule, with a long fixed cooldown specifically
    # on 429 instead of following the normal exponential ramp.
    ITICK_MAX_BACKOFF = 60
    ITICK_RATE_LIMIT_COOLDOWN = 45
    backoff = 5
    sub_tick = json.dumps({"ac": "subscribe", "params": ITICK_SYMBOL, "types": "tick"})
    sub_quote = json.dumps({"ac": "subscribe", "params": ITICK_SYMBOL, "types": "quote"})
    # Real bid/ask depth - see handle_itick_message for why this was added
    # (smoother, more continuously-updating price than last-trade-only).
    sub_depth = json.dumps({"ac": "subscribe", "params": ITICK_SYMBOL, "types": "depth"})
    while True:
        try:
            logger.info(f"Connecting to iTick real spot feed ({ITICK_SYMBOL})...")
            async with websockets.connect(
                ITICK_WS_URL,
                additional_headers={"token": ITICK_API_KEY},
                ping_interval=20, ping_timeout=10,
            ) as ws:
                await ws.send(sub_tick)
                await ws.send(sub_quote)
                await ws.send(sub_depth)
                state["itick_connected"] = True
                backoff = 5
                logger.info("Connected to iTick real spot XAUUSD feed.")
                async for raw in ws:
                    await handle_itick_message(raw)
        except websockets.exceptions.InvalidStatus as e:
            state["itick_connected"] = False
            state["itick_reconnect_count"] += 1
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 429:
                logger.warning(
                    f"iTick rate-limited our reconnect (HTTP 429); cooling down "
                    f"{ITICK_RATE_LIMIT_COOLDOWN}s before trying again "
                    f"(attempt #{state['itick_reconnect_count']})."
                )
                await asyncio.sleep(ITICK_RATE_LIMIT_COOLDOWN)
            else:
                logger.warning(f"iTick handshake rejected ({e!r}); retrying in {backoff}s.")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ITICK_MAX_BACKOFF)
        except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            state["itick_connected"] = False
            state["itick_reconnect_count"] += 1
            logger.warning(
                f"iTick feed disconnected ({e!r}); reconnecting in {backoff}s "
                f"(attempt #{state['itick_reconnect_count']}). "
                f"Normal if forex/gold market is currently closed (weekend)."
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, ITICK_MAX_BACKOFF)
        except Exception:
            state["itick_connected"] = False
            logger.exception("Unexpected error in iTick feed loop; retrying in 10s")
            await asyncio.sleep(10)


# =============================================================================
# HTTP / WEBSOCKET ROUTES
# =============================================================================
@app.websocket("/ws")
async def stream_order_flow(websocket: WebSocket):
    await websocket.accept()
    subscribers.add(websocket)
    logger.info(f"Client subscribed. Total subscribers: {len(subscribers)}")
    try:
        await websocket.send_text(json.dumps({
            "type": "init",
            "candles": list(recent_candles),
            "last_price": state["last_price"],
            "real_spot": {k: v for k, v in real_spot_gold.items() if k != "last_update"} if real_spot_gold["price"] else None,
        }))
        while True:
            await websocket.receive_text()  # keep-alive; client messages ignored
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.discard(websocket)
        logger.info(f"Client unsubscribed. Total subscribers: {len(subscribers)}")


@app.get("/health")
async def health():
    now = datetime.now(timezone.utc)
    last_tick = state["last_tick_time"]
    last_mark = state["last_mark_time"]
    last_itick = state["last_itick_time"]
    # Price is "live" if a real Binance trade tick, a real bid/ask mark, OR a
    # real iTick spot tick arrived recently - PAXGUSDT can go quiet on trades
    # for minutes while the order book (and therefore the mark price) keeps
    # moving, and iTick becomes the primary driver whenever it's connected.
    most_recent = max([t for t in (last_tick, last_mark, last_itick) if t is not None], default=None)
    staleness_sec = (now - most_recent).total_seconds() if most_recent else None
    tick_staleness_sec = (now - last_tick).total_seconds() if last_tick else None
    healthy = (
        state["connected"]
        and staleness_sec is not None
        and staleness_sec < STALE_FEED_THRESHOLD_SECONDS
    )
    payload = {
        "status": "healthy" if healthy else "degraded",
        "connected_to_binance": state["connected"],
        "last_price": state["last_price"],
        "feed_staleness_seconds": staleness_sec,
        "real_trade_tick_staleness_seconds": tick_staleness_sec,
        "reconnect_count": state["reconnect_count"],
        "ticks_ingested": state["ticks_ingested"],
        "depth_snapshots_ingested": state["depth_snapshots_ingested"],
        "questdb_errors": state["questdb_errors"],
        "active_subscribers": len(subscribers),
        "uptime_seconds": (now - state["started_at"]).total_seconds(),
        "real_spot_gold": {
            "enabled": bool(GOLDAPI_KEY),
            "price": real_spot_gold["price"],
            "bid": real_spot_gold["bid"],
            "ask": real_spot_gold["ask"],
            "source": real_spot_gold["source"],
            "last_update": real_spot_gold["last_update"].isoformat() if real_spot_gold["last_update"] else None,
            "consecutive_errors": real_spot_gold["errors"],
        },
        "itick_spot": {
            "enabled": bool(ITICK_API_KEY),
            "connected": state["itick_connected"],
            "reconnect_count": state["itick_reconnect_count"],
            "last_tick_seconds_ago": (
                (now - state["last_itick_time"]).total_seconds() if state["last_itick_time"] else None
            ),
            "note": "No ticks expected while forex/gold spot market is closed (weekends).",
        },
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)


@app.get("/", response_class=HTMLResponse)
async def root():
    return (
        "<h2>Goldflow Core WebSocket Engine V2</h2>"
        "<p>Port 9000 pilot upgrade of Port 8000. See <a href='/health'>/health</a> "
        "and connect to <code>ws://127.0.0.1:9000/ws</code>.</p>"
    )


@app.on_event("startup")
async def startup_event():
    global _main_event_loop
    logger.info(f"Core Engine V2 starting on {ENGINE_HOST}:{ENGINE_PORT}")
    _main_event_loop = asyncio.get_running_loop()
    asyncio.create_task(binance_feed_loop())
    asyncio.create_task(itick_feed_loop())
    threading.Thread(target=goldapi_poller, daemon=True).start()


@app.on_event("shutdown")
async def shutdown_event():
    global _questdb_sender
    if _questdb_sender is not None:
        _questdb_sender.close()
        _questdb_sender = None


if __name__ == "__main__":
    uvicorn.run(app, host=ENGINE_HOST, port=ENGINE_PORT)
