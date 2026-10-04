"""
itick_feed.py - reusable, self-contained iTick real-spot feed engine.
============================================================================
Purpose: let each dashboard run its OWN iTick connection instead of all of
them depending on the shared Port 9000 engine (which, if it zombies, kills
every downstream port's volume at once). One iTick connection per port,
within iTick's 5-connection budget.

It connects DIRECTLY to iTick (real spot XAUUSD: real per-trade price + REAL
per-trade size + timestamp) and calls a single `on_message` callback with
dicts shaped EXACTLY like the messages Port 9000 used to relay, so a port's
existing `handle_engine_message` keeps working with a one-line feed swap:

    tick  : {"type":"tick","price":float,"volume":float,"side":"BUY"/"SELL","timestamp":ms}
    depth : {"type":"depth","bids":[[price,qty]],"asks":[[price,qty]]}     (iTick L1 only)
    mark_price: {"type":"mark_price","price":float,"bid":float,"ask":float,"timestamp":ms}

Side is ESTIMATED via Lee-Ready (quote rule -> tick rule -> zero-plus tick),
identical to the Port 9900 terminal, because spot gold has no real aggressor
tag. Volume is the REAL iTick trade size (not a proxy, not random).

NOTE on depth: iTick's free depth is L1 (best bid/ask, size not exposed), so
the emitted depth carries one level each side with qty 0 - a port that drew a
deep Binance PAXG book should switch that panel to the synthetic
traded-volume ladder (as Port 9900 does). Volume/CVD themselves are REAL.

Usage (threaded, for a sync/Dash port):
    from itick_feed import ITickFeed
    feed = ITickFeed(key=os.getenv("GOLDFLOW_9060_ITICK_KEY", ""),
                     on_message=handle_engine_message, logger=logger, name="9060")
    feed.start_in_thread()          # daemon thread, own asyncio loop
"""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone

import websockets

DEFAULT_WS_URL = "wss://api-free.itick.org/forex"
DEFAULT_SYMBOL = "XAUUSD$GB"


class ITickFeed:
    def __init__(self, key, on_message, logger, name="itick",
                 ws_url=DEFAULT_WS_URL, symbol=DEFAULT_SYMBOL,
                 subscribe_depth=True):
        self.key = key
        self.on_message = on_message          # callable(dict) -> None
        self.logger = logger
        self.name = name
        self.ws_url = ws_url
        self.symbol = symbol
        self.subscribe_depth = subscribe_depth
        # side-classification state (Lee-Ready)
        self._best_bid = None
        self._best_ask = None
        self._last_price = None
        self._last_side = None                # for zero-plus tick rule
        self._last_dedup = None               # skip exact re-broadcast heartbeats
        # health
        self.connected = False
        self.last_tick_ts = None
        self.reconnects = 0
        self._stop = False
        self._thread = None
        self._ws = None                       # live socket (for watchdog force-reconnect)

    # ------------------------------------------------------------------ side
    def _classify_side(self, price):
        """True=BUY (aggressor lifted ask), False=SELL. ESTIMATED."""
        if self._best_bid is not None and self._best_ask is not None and self._best_ask > self._best_bid:
            mid = (self._best_bid + self._best_ask) / 2.0
            self._last_side = price >= mid
            return self._last_side
        if self._last_price is not None:
            if price > self._last_price:
                self._last_side = True
                return True
            if price < self._last_price:
                self._last_side = False
                return False
        return self._last_side if self._last_side is not None else True

    # --------------------------------------------------------------- emit
    def _safe_emit(self, msg):
        try:
            self.on_message(json.dumps(msg))
        except TypeError:
            # on_message may accept a dict rather than raw JSON string
            try:
                self.on_message(msg)
            except Exception:
                self.logger.exception(f"[itick-{self.name}] on_message failed")
        except Exception:
            self.logger.exception(f"[itick-{self.name}] on_message failed")

    # --------------------------------------------------------------- parse
    async def _handle_raw(self, raw):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        d = msg.get("data")
        if not isinstance(d, dict):
            return
        typ = d.get("type")
        if typ == "tick":
            try:
                price = float(d["ld"]); size = float(d.get("v", 0) or 0); t = int(d.get("t", 0))
            except (KeyError, TypeError, ValueError):
                return
            if price <= 0 or size <= 0:
                return
            dedup = (t, price, size)
            if dedup == self._last_dedup:
                return
            self._last_dedup = dedup
            is_buy = self._classify_side(price)
            self._last_price = price
            self.last_tick_ts = time.time()
            self._safe_emit({"type": "tick", "price": price, "volume": size,
                             "side": "BUY" if is_buy else "SELL", "timestamp": t})
        elif typ == "depth":
            a = d.get("a") or []
            b = d.get("b") or []
            try:
                if a:
                    self._best_ask = float(a[0]["p"])
                if b:
                    self._best_bid = float(b[0]["p"])
            except (KeyError, TypeError, ValueError, IndexError):
                return
            if self._best_bid and self._best_ask:
                self._safe_emit({"type": "depth",
                                 "bids": [[self._best_bid, 0.0]],
                                 "asks": [[self._best_ask, 0.0]],
                                 "timestamp": int(time.time() * 1000)})
                # also a mark_price so the chart keeps moving between trades
                mid = (self._best_bid + self._best_ask) / 2.0
                self._safe_emit({"type": "mark_price", "price": round(mid, 3),
                                 "bid": self._best_bid, "ask": self._best_ask,
                                 "timestamp": int(time.time() * 1000)})

    # --------------------------------------------------------------- loop
    async def _run(self):
        if not self.key:
            self.logger.error(f"[itick-{self.name}] No iTick key set - feed will not connect.")
            return
        backoff = 5
        sub_tick = json.dumps({"ac": "subscribe", "params": self.symbol, "types": "tick"})
        sub_depth = json.dumps({"ac": "subscribe", "params": self.symbol, "types": "depth"})
        while not self._stop:
            try:
                self.logger.info(f"[itick-{self.name}] Connecting to iTick ({self.symbol})...")
                async with websockets.connect(self.ws_url, additional_headers={"token": self.key},
                                               ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    await ws.send(sub_tick)
                    if self.subscribe_depth:
                        await ws.send(sub_depth)
                    self.connected = True
                    self.logger.info(f"[itick-{self.name}] Connected to iTick real trade tape.")
                    got_data = False
                    async for raw in ws:
                        if self._stop:
                            break
                        if not got_data:
                            got_data = True
                            backoff = 5          # reset ONLY once real data flows (stable connection)
                        await self._handle_raw(raw)
                # Server closed the socket cleanly (code 1000). Must STILL back off:
                # a no-sleep reconnect here caused a tight loop that tripped iTick's
                # 429 rate limit. If the close came before any data, backoff keeps
                # growing (5->10->20->40->60) so we stop hammering a refused key.
                self.connected = False
                if not self._stop:
                    self.reconnects += 1
                    self.logger.info(f"[itick-{self.name}] closed by server; reconnect in {backoff}s.")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
            except websockets.exceptions.InvalidStatus as e:
                self.connected = False
                code = getattr(getattr(e, "response", None), "status_code", None)
                wait = 45 if code == 429 else backoff
                self.reconnects += 1
                self.logger.warning(f"[itick-{self.name}] handshake rejected ({code}); retry in {wait}s.")
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self.connected = False
                self.reconnects += 1
                self.logger.warning(f"[itick-{self.name}] feed error ({e!r}); retry in {backoff}s "
                                    f"(no ticks expected weekends).")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # --------------------------------------------------------------- control
    async def run(self):
        """Run the feed inside an EXISTING asyncio loop (for async/FastAPI
        ports): `asyncio.create_task(feed.run())`. on_message stays sync."""
        await self._run()

    def start_in_thread(self):
        """Run the async feed in its own daemon thread (for sync/Dash ports)."""
        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run())
        self._thread = threading.Thread(target=_worker, daemon=True, name=f"itick-{self.name}")
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop = True

    async def force_reconnect(self):
        """Watchdog hook: drop the current socket so the loop reconnects.
        Used when the feed is 'connected' but has gone silent (zombie guard)."""
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def health(self):
        return {"connected": self.connected,
                "last_tick_sec_ago": round(time.time() - self.last_tick_ts, 1) if self.last_tick_ts else None,
                "reconnects": self.reconnects}
