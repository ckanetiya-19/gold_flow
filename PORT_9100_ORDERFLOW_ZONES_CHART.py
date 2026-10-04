"""
PORT 9100 - ORDER-FLOW ZONES CHART (NEW, standalone, read-only)
============================================================================
NEW file, NEW port (9100). Does NOT touch ANY existing file - not the legacy
8000-series, not PORT_9000_CORE_WEBSOCKET_ENGINE_V2.py, not PORT_9060/9080
(V2) or their 9061/9081 chart mirrors. This process only ever CONNECTS OUT
to Port 9000's already-public, already-multi-subscriber feed
(ws://127.0.0.1:9000/ws) exactly like every other pilot dashboard does - it
is just one more read-only subscriber. Nothing it does can affect 9000,
9060, 9080, or any strategy/MT5 code running there.

SAFETY SCOPE (updated - MT5 execution added at explicit user request, see
below): this dashboard can now place REAL orders on a DEMO MT5 account for
the validated Order-Block setup, gated behind TWO switches that must BOTH
be on (GOLDFLOW_MT5_TRADING_ENABLED, the same project-wide master switch
every other port uses, AND GOLDFLOW_9100_MT5_ENABLED, this port's own),
both default OFF. Every order function independently re-checks
mt5.account_info().trade_mode == ACCOUNT_TRADE_MODE_DEMO before sending
anything and refuses outright on a live/real account, no matter what the
switches say - the same non-negotiable check used everywhere else in this
project. No password is ever read from an env var or stored anywhere - this
only ever attaches to an MT5 terminal already open and logged in. The
trailing exit means orders are placed with NO broker-side take-profit
(tp=0) - only the SL is set and actively modified as it trails, matching
the pattern already used for every other trailing-based strategy here.

ENTRY/SL/TP LOGIC (rewritten after a full backtest validation pass - see
backtest_orderflow_zones_tiers.py and backtest_orderflow_zones_tiers2.py in
the project root for the full methodology and results). The original
swing-fractal zone logic barely beat a random-entry control (net/trade
$0.348 vs $0.287) - real, but a weak edge. After testing 7 Tier-1/Tier-2
ideas plus an SL/TP redesign (each checked against its own random-entry
control, on real tick data, with in-sample/out-of-sample validation), the
combination below beat random by 5x+ and IMPROVED out-of-sample (a strong
sign it isn't overfit):
  - Zones = ORDER BLOCKS (the last opposite-colored candle before an
    impulsive move of >= 1.2xATR within 3 bars), not plain swing highs/lows.
  - SL = the origin candle's OWN high/low (+/- a small tick buffer) -
    "structural" - not an arbitrary ATR-scaled buffer off the zone edge.
  - Entry only taken if estimated reward:risk (to the nearest opposite
    zone) >= 1.5.
  - Entry only taken if the signal candle shows a STACKED FOOTPRINT
    IMBALANCE (3+ consecutive price levels, in the same bar, with buy:sell
    or sell:buy ratio >= 3) confirming the direction.
  - Entry only taken if Cumulative Volume Delta has broken its own most
    recent short-term swing structure in the trade's direction ("delta
    leads price").
Backtest result (12.7 real days, tick-level, $2-arm/$1-trail exit): net/trade
$0.844 vs a matched random control's $0.158, win rate 62.5%, MaxDD 7.3%,
and it held up (even improved) out-of-sample. Ideas that were tested and
DROPPED because they didn't beat their own random control once combined
with the above (Daily-Bias filter, Anchored VWAP, Delta-POC, order-book
imbalance, relative volume, session-time filter, liquidity-pool reward
target) are documented in the two backtest files, not silently omitted.

Purpose (per user's request):
  1. One single, polished, TradingView/ATAS-style chart (own price bars,
     own VWAP, computed independently the same way 9060/9080 already do -
     see _rolling_chart_vwap) that also shows, automatically, on the SAME
     chart:
       - Order-Block Supply / Demand zones (screen_02 / card02 concept,
         refined per the backtest above) on two timeframes at once (1m =
         LTF zones, 15m = HTF zones) so both a fine and a broader
         "Multi-Time-Frame Context" (screen_04) are visible together.
       - Daily Bias panel (screen_03): Weekly direction + Daily structure
         + nearest untouched liquidity target, in one line.
       - Automatic BUY/SELL Entry + SL + TP annotation whenever price
         reacts off an active zone (mirrors the SL/TP/Entry idea from the
         reference screenshots) - visual only, see safety scope above.
       - Footprint (screen_05): click any candle to see real buy-vs-sell
         volume broken down by price level inside that bar (this data was
         already being computed bar-by-bar for POC on 9080; this file
         tracks it split by side too).
       - Liquidity heatmap (screen_06): built from the SAME Binance PAXG
         order-book depth (depth20) already broadcast by Port 9000 - this
         is the existing "volume/order-flow proxy" data source, per this
         project's established architecture principle. IMPORTANT CAVEAT:
         this is a PAXG order book, not a literal centralized XAUUSD spot
         order book (none exists for OTC forex/gold) - same proxy-only
         caveat that already applies to CVD/volume everywhere else in this
         project. Price-wise PAXG tracks spot gold closely so the heatmap
         still lines up usefully against the price axis, but it is
         approximate, not a literal broker order book.
  2. Everything lives in ONE dashboard/ONE page (per explicit instruction
     "ek j dashboard ma" - heatmap is a toggle-able overlay layer on the
     main chart, footprint is a click-to-inspect side panel - not separate
     screens/apps like ATAS/Bookmap split things across multiple windows).

Run: python PORT_9100_ORDERFLOW_ZONES_CHART.py
(Port 9000 must already be running.)
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# =============================================================================
# CONFIG
# =============================================================================
SOURCE_WS_URL = "ws://127.0.0.1:9000/ws"
HOST = "127.0.0.1"
PORT = 9100
SYMBOL = "XAUUSD"
PRICE_LEVEL_ROUND = 0.1          # footprint price-bucket size (matches 9080's POC rounding)
HEATMAP_PRICE_BIN = 0.5          # heatmap price-bucket size
HEATMAP_WINDOW_SECONDS = 30 * 60  # how much depth history the heatmap covers
DEPTH_SAMPLE_MIN_INTERVAL = 1.0   # store at most 1 depth snapshot/sec (feed sends every 100ms)

# Persistence - restarting this process must NOT lose chart history (candles,
# zones, VWAP, bias all depend on historical_bars). Every finalized bar,
# every closed setup, and the currently-open setup are saved to a local
# SQLite file and reloaded on startup, same pattern as vwap_fc_paper.db /
# portfolio_paper.db elsewhere in this project.
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orderflow_zones_9100.db")
BARS_RETENTION_DAYS = 14         # prune bars older than this on startup
MAX_BARS_IN_MEMORY = BARS_RETENTION_DAYS * 1440  # effectively "unlimited" (matches DB retention,
# not a tight display cap like the old 1000) - sane outer bound so an uninterrupted multi-week
# run doesn't grow the in-memory list forever; the chart itself shows everything up to this.
ZONE_CALC_WINDOW = 300            # zones/entry-logic still scan only recent bars (performance +
# trading relevance - a zone from days ago at a stale price level isn't a live setup candidate;
# this does NOT limit what the chart displays or what a restart restores, only zone detection)

# Entry/SL/TP logic constants - all backtest-validated, see header note above.
ZONE_ATR_BUFFER = 0.3            # supply/ask-side edge of an Order Block, in xATR
IMPULSE_MULT = 1.2               # Order Block: forward move must be >= this x ATR
OB_LOOKAHEAD = 3                 # bars after the origin candle to confirm the impulse
SL_TICK_BUFFER = 0.05            # structural SL: origin candle extreme +/- this
MIN_RR = 1.5                     # minimum estimated reward:risk to accept a setup
STACK_MIN_RUN = 3                # stacked-imbalance: consecutive price levels required
STACK_RATIO = 3.0                # stacked-imbalance: buy:sell (or sell:buy) ratio required
TRAIL_ARM = 2.0                  # once floating profit reaches this, SL starts trailing
TRAIL_DIST = 1.0                 # trailing distance behind the best price reached, once armed

logging.basicConfig(level=logging.INFO, format="%(asctime)s INFO [orderflow-zones-9100] %(message)s")
logger = logging.getLogger("orderflow_zones_9100")

app = FastAPI(title="Goldflow Order-Flow Zones Chart (Port 9100)")

# =============================================================================
# MT5 (optional, real demo-account execution) - added at explicit user
# request. Package import is wrapped in try/except so this whole file keeps
# working paper-only if MetaTrader5 isn't installed, same pattern as every
# other port here. No credentials ever pass through this code or any env
# var - if the terminal isn't running/logged in, this silently stays
# disabled. Orders carry NO broker-side take-profit (tp=0) since the exit
# is a trailing stop, not a fixed target - only SL is set/modified.
# =============================================================================
try:
    import MetaTrader5 as mt5
    MT5_PACKAGE_AVAILABLE = True
except ImportError:
    MT5_PACKAGE_AVAILABLE = False

MT5_SYMBOL = os.getenv("GOLDFLOW_MT5_SYMBOL", "XAUUSD.sd")
MT5_LOT = float(os.getenv("GOLDFLOW_MT5_LOT", "0.01"))
MT5_MAGIC = 91000
_mt5_ready = False

# Master switch: the SAME project-wide switch every other port's MT5 code
# checks. Port-specific switch: this port's own, so enabling MT5 elsewhere
# never silently arms this one and vice versa. BOTH must be "1".
MT5_TRADING_ENABLED = os.getenv("GOLDFLOW_MT5_TRADING_ENABLED", "0") == "1"
MT5_9100_ENABLED = os.getenv("GOLDFLOW_9100_MT5_ENABLED", "0") == "1"


def mt5_init():
    global _mt5_ready
    if not MT5_PACKAGE_AVAILABLE:
        logger.info("MetaTrader5 package not installed - MT5 execution unavailable (paper tracking only).")
        return
    if not (MT5_TRADING_ENABLED and MT5_9100_ENABLED):
        logger.info("MT5 trading DISABLED for Port 9100 (master switch and/or 9100-specific switch off) - paper tracking only.")
        return
    try:
        if not mt5.initialize():
            logger.warning(f"MT5 initialize() failed: {mt5.last_error()} - paper tracking continues.")
            return
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            logger.error("MT5 account is NOT a demo account - refusing to enable trading for Port 9100. "
                         "Paper tracking continues; MT5 will not be touched.")
            return
        _mt5_ready = True
        logger.info(f"MT5 attached (DEMO account #{acc.login}) - Port 9100 trading ARMED. Symbol={MT5_SYMBOL} Lot={MT5_LOT}")
    except Exception:
        logger.exception("MT5 init error - paper tracking continues.")


def mt5_place_order(direction, sl, tp, magic=None, comment=None):
    if not _mt5_ready:
        return None
    try:
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            logger.error("MT5 account no longer demo at order time - refusing order.")
            return None
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if tick is None:
            logger.warning(f"MT5 no tick data for {MT5_SYMBOL} - order skipped.")
            return None
        price = tick.ask if direction == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": MT5_SYMBOL, "volume": MT5_LOT,
            "type": order_type, "price": price, "sl": round(sl, 2),
            "tp": round(tp, 2) if tp else 0.0,
            "deviation": 20, "magic": magic or MT5_MAGIC, "comment": (comment or "GF-9100")[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.warning(f"MT5 order failed: retcode={getattr(result, 'retcode', None)} "
                            f"comment={getattr(result, 'comment', None)}")
            return None
        logger.info(f"MT5 demo order placed for Port 9100, ticket=#{result.order}")
        return result.order
    except Exception:
        logger.exception("MT5 place_order error")
        return None


def mt5_modify_position(ticket, sl, tp):
    if not _mt5_ready or not ticket:
        return
    try:
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return
        request = {
            "action": mt5.TRADE_ACTION_SLTP, "symbol": MT5_SYMBOL, "position": ticket,
            "sl": round(sl, 2), "tp": round(tp, 2) if tp else 0.0,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            # This is a BACKSTOP-only modify now (the real exit is the instant
            # market-close in mt5_close_position). retcode 10016 ("Invalid
            # stops") just means the broker's minimum stop-distance rule
            # rejected a too-tight SL - harmless here, so log at debug level
            # instead of spamming warnings, since the close-order guarantees
            # the exit regardless.
            logger.debug(f"MT5 trailing-SL modify skipped for #{ticket} (broker rejected, backstop only) - "
                          f"retcode={getattr(result, 'retcode', None)} comment={getattr(result, 'comment', None)}")
    except Exception:
        logger.exception("MT5 modify_position error")


def mt5_close_position(ticket, side):
    """Instantly close an open MT5 position at market - the PRIMARY exit,
    mirroring the paper engine's decision the moment it fires. Sends an
    opposite-direction market DEAL against the position ticket. Does NOT
    depend on the trailing-SL modify having succeeded, so the demo account
    exits exactly when paper does (fill price is the live broker bid/ask at
    that instant, so it differs from the paper exit by roughly the spread -
    same close, near-same profit)."""
    if not _mt5_ready or not ticket:
        return
    try:
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return  # already gone (e.g. broker-side initial SL already hit it)
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if tick is None:
            logger.warning(f"MT5 no tick for {MT5_SYMBOL} - cannot close #{ticket}.")
            return
        # close a BUY by SELLing at bid; close a SELL by BUYing at ask
        close_type = mt5.ORDER_TYPE_SELL if side == "BUY" else mt5.ORDER_TYPE_BUY
        price = tick.bid if side == "BUY" else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": MT5_SYMBOL,
            "volume": positions[0].volume, "type": close_type, "position": ticket,
            "price": price, "deviation": 20, "magic": MT5_MAGIC,
            "comment": "GF-9100-close"[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.warning(f"MT5 close FAILED for #{ticket} - retcode={getattr(result, 'retcode', None)} "
                            f"comment={getattr(result, 'comment', None)} (position may still be open on broker!)")
        else:
            logger.info(f"MT5 demo position #{ticket} closed at market (mirrors paper exit).")
    except Exception:
        logger.exception("MT5 close_position error")

# =============================================================================
# SHARED STATE
# =============================================================================
data_lock = threading.Lock()
depth_lock = threading.Lock()

EMPTY_BAR = {
    "time": None, "open": None, "high": None, "low": None, "close": None,
    "volume": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0, "cvd": 0.0, "vwap": 0.0,
    "levels_buy": {}, "levels_sell": {},
}
current_bar = dict(EMPTY_BAR)
historical_bars = []  # finalized bars, newest last (each also gets poc/imbalance/fvg/absorption)

cum_vol = 0.0
cum_pv = 0.0
cum_delta = 0.0
last_known_price = None
last_depth_store_ts = 0.0
depth_history = deque(maxlen=int(HEATMAP_WINDOW_SECONDS / DEPTH_SAMPLE_MIN_INTERVAL) + 60)

current_setup = None       # active visual Entry/SL/TP suggestion, or None
last_setup_outcome = None  # short text, e.g. "Last: BUY hit TP +$1.80"
setup_history = deque(maxlen=100)  # closed setups (visual only, no MT5) - most recent last

conn_state = {"connected": False}


# =============================================================================
# PERSISTENCE (SQLite) - so a restart never loses chart history. Same
# single-connection pattern is safe here because everything in this file
# runs on one asyncio event loop / one OS thread (no threading.Thread
# workers, unlike some other ports in this project).
# =============================================================================
db_lock = threading.Lock()
_db = sqlite3.connect(DB_FILE, check_same_thread=False)
_db.execute("""CREATE TABLE IF NOT EXISTS bars (
    time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
    volume REAL, buy_vol REAL, sell_vol REAL, delta REAL, cvd REAL, vwap REAL,
    poc REAL, imbalance INTEGER, fvg TEXT, absorption TEXT,
    levels_buy TEXT, levels_sell TEXT
)""")
_db.execute("""CREATE TABLE IF NOT EXISTS setup_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, side TEXT, entry REAL, sl REAL, tp REAL,
    outcome TEXT, exit_price REAL, pnl REAL, opened_at INTEGER, closed_at INTEGER
)""")
_db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
_db.commit()


def _db_save_bar(bar):
    with db_lock:
        _db.execute(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(bar["time"].timestamp()), bar["open"], bar["high"], bar["low"], bar["close"],
             bar.get("volume", 0.0), bar.get("buy_vol", 0.0), bar.get("sell_vol", 0.0),
             bar.get("delta", 0.0), bar.get("cvd", 0.0), bar.get("vwap", 0.0), bar.get("poc"),
             1 if bar.get("imbalance") else 0, bar.get("fvg"), bar.get("absorption"),
             json.dumps({str(k): v for k, v in bar.get("levels_buy", {}).items()}),
             json.dumps({str(k): v for k, v in bar.get("levels_sell", {}).items()})),
        )
        _db.commit()


def _db_load_bars(limit=None):
    """Loads bars - unlimited by default (everything within the
    BARS_RETENTION_DAYS window), so a restart brings back EVERYTHING that
    was in memory before, not just a fixed recent slice. `limit` stays
    available for callers that want a bounded read (e.g. quick diagnostics)."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=BARS_RETENTION_DAYS)).timestamp())
    with db_lock:
        _db.execute("DELETE FROM bars WHERE time < ?", (cutoff,))
        _db.commit()
        if limit is None:
            rows = _db.execute("SELECT * FROM bars ORDER BY time ASC").fetchall()
        else:
            rows = list(reversed(_db.execute("SELECT * FROM bars ORDER BY time DESC LIMIT ?", (limit,)).fetchall()))
    bars = []
    for r in rows:
        (t, o, h, l, c, vol, bv, sv, delta, cvd, vwap, poc, imbalance, fvg, absorption, lb, ls) = r
        bars.append({
            "time": datetime.fromtimestamp(t, tz=timezone.utc), "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "buy_vol": bv, "sell_vol": sv, "delta": delta, "cvd": cvd, "vwap": vwap,
            "poc": poc, "imbalance": bool(imbalance), "fvg": fvg, "absorption": absorption,
            "levels_buy": {float(k): v for k, v in json.loads(lb or "{}").items()},
            "levels_sell": {float(k): v for k, v in json.loads(ls or "{}").items()},
        })
    return bars


def _db_save_closed_setup(rec):
    with db_lock:
        _db.execute(
            "INSERT INTO setup_history (side,entry,sl,tp,outcome,exit_price,pnl,opened_at,closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rec["side"], rec["entry"], rec["sl"], rec["tp"], rec["outcome"], rec["exit_price"],
             rec["pnl"], rec["opened_at"], rec["closed_at"]),
        )
        _db.commit()


def _db_load_setup_history(limit=100):
    with db_lock:
        rows = _db.execute(
            "SELECT side,entry,sl,tp,outcome,exit_price,pnl,opened_at,closed_at FROM setup_history "
            "ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
    keys = ["side", "entry", "sl", "tp", "outcome", "exit_price", "pnl", "opened_at", "closed_at"]
    return [dict(zip(keys, r)) for r in rows]


def _db_save_state():
    with db_lock:
        _db.execute("INSERT OR REPLACE INTO state VALUES ('current_setup', ?)",
                     (json.dumps(current_setup) if current_setup else "",))
        _db.execute("INSERT OR REPLACE INTO state VALUES ('last_setup_outcome', ?)",
                     (last_setup_outcome or "",))
        _db.commit()


def _db_load_state():
    with db_lock:
        rows = dict(_db.execute("SELECT key, value FROM state").fetchall())
    setup = json.loads(rows["current_setup"]) if rows.get("current_setup") else None
    outcome = rows.get("last_setup_outcome") or None
    return setup, outcome


# =============================================================================
# BAR BUILDING (mirrors 9080's process_tick/update_mark_price logic, but
# additionally splits the per-price footprint by BUY vs SELL side, which
# 9080/9060 do not currently track separately)
# =============================================================================
def finalize_bar(bar):
    levels = {}
    for p, v in bar["levels_buy"].items():
        levels[p] = levels.get(p, 0.0) + v
    for p, v in bar["levels_sell"].items():
        levels[p] = levels.get(p, 0.0) + v
    poc = max(levels, key=levels.get) if levels else bar["close"]

    avg_vol = (sum(b["volume"] for b in historical_bars[-20:]) / len(historical_bars[-20:])
               if len(historical_bars) >= 10 else bar["volume"])
    imbalance = bar["volume"] > (avg_vol * 1.8) if avg_vol > 0 else False

    fvg = None
    if len(historical_bars) >= 1:
        prev = historical_bars[-1]
        if prev["high"] < bar["low"]:
            fvg = "Bullish"
        elif prev["low"] > bar["high"]:
            fvg = "Bearish"

    delta = bar["buy_vol"] - bar["sell_vol"]
    absorption = None
    if abs(delta) > 50 and abs(bar["close"] - bar["open"]) < 0.25:
        absorption = "Bullish" if delta > 0 else "Bearish"

    return {
        **bar, "poc": round(poc, 2), "imbalance": imbalance, "fvg": fvg, "absorption": absorption,
    }


def process_tick(price, volume, is_buy):
    global cum_vol, cum_pv, cum_delta
    ref_price = last_known_price if last_known_price else price
    delta = volume if is_buy else -volume
    cum_delta += delta
    cum_vol += volume
    cum_pv += ref_price * volume
    with data_lock:
        if current_bar["time"] is None:
            return
        current_bar["volume"] += volume
        if is_buy:
            current_bar["buy_vol"] += volume
        else:
            current_bar["sell_vol"] += volume
        current_bar["delta"] = current_bar["buy_vol"] - current_bar["sell_vol"]
        current_bar["cvd"] = cum_delta
        vwap = (cum_pv / cum_vol) if cum_vol > 0 else ref_price
        current_bar["vwap"] = round(vwap, 2)
        lvl = round(ref_price / PRICE_LEVEL_ROUND) * PRICE_LEVEL_ROUND
        bucket = current_bar["levels_buy"] if is_buy else current_bar["levels_sell"]
        bucket[lvl] = bucket.get(lvl, 0.0) + volume


def update_mark_price(price):
    global last_known_price
    last_known_price = price
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.replace(second=0, microsecond=0)
    with data_lock:
        if current_bar["time"] is None or current_bar["time"] < current_minute:
            if current_bar["close"] is not None and current_bar["time"] is not None:
                final_bar = finalize_bar(current_bar)
                historical_bars.append(final_bar)
                if len(historical_bars) > MAX_BARS_IN_MEMORY:
                    del historical_bars[0]
                _db_save_bar(final_bar)
                _update_zones_and_setup_locked()
            current_bar["time"] = current_minute
            current_bar["open"] = price
            current_bar["high"] = price
            current_bar["low"] = price
            current_bar["close"] = price
            current_bar["volume"] = 0.0
            current_bar["buy_vol"] = 0.0
            current_bar["sell_vol"] = 0.0
            current_bar["delta"] = 0.0
            current_bar["cvd"] = cum_delta
            current_bar["vwap"] = round((cum_pv / cum_vol) if cum_vol > 0 else price, 2)
            current_bar["levels_buy"] = {}
            current_bar["levels_sell"] = {}
        else:
            current_bar["high"] = max(current_bar["high"], price)
            current_bar["low"] = min(current_bar["low"], price)
            current_bar["close"] = price


def process_depth(bids, asks):
    global last_depth_store_ts
    import time as _time
    now = _time.time()
    if now - last_depth_store_ts < DEPTH_SAMPLE_MIN_INTERVAL:
        return
    last_depth_store_ts = now
    with depth_lock:
        depth_history.append({"t": now, "bids": bids, "asks": asks})


def seed_from_klines(candles):
    """Seed historical_bars from Port 9000's init payload (Binance 1m klines)
    so the chart isn't empty on startup. Buy/sell split is a simple
    close-vs-open heuristic here (no real per-tick data exists yet for these
    old candles) - live bars going forward use the real split.

    Binance's kline_1m stream pushes an update roughly every second for the
    SAME still-forming candle (only the last one per minute has closed=True),
    and Port 9000's recent_candles stores every one of those updates - so
    this payload has many duplicate-timestamp entries per minute. Dedupe by
    minute here, keeping only the last (most complete/closed) update for
    each, or every "bar" ends up being the same few real minutes repeated.

    Restart continuity: historical_bars may already hold REAL bars reloaded
    from the local DB (see _db_load_bars, called at startup before this) -
    never overwrite those with a synthetic kline reconstruction. Only fills
    genuine gaps (e.g. the process was off for a while)."""
    dedup = {}
    for k in candles:
        t = datetime.fromtimestamp(k["time"] / 1000.0, tz=timezone.utc).replace(second=0, microsecond=0)
        dedup[t] = k  # last occurrence per minute wins (most complete state)
    ordered_times = sorted(dedup.keys())
    if ordered_times:
        ordered_times = ordered_times[:-1]  # drop the still-open current minute

    with data_lock:
        existing_times = {b["time"] for b in historical_bars}
        added = False
        for t in ordered_times:
            if t in existing_times:
                continue
            k = dedup[t]
            vol = float(k.get("volume", 0.0))
            bullish = float(k["close"]) >= float(k["open"])
            buy_vol = vol * (0.55 if bullish else 0.45)
            sell_vol = vol - buy_vol
            bar = {
                "time": t, "open": float(k["open"]), "high": float(k["high"]),
                "low": float(k["low"]), "close": float(k["close"]), "volume": vol,
                "buy_vol": buy_vol, "sell_vol": sell_vol, "delta": buy_vol - sell_vol,
                "cvd": 0.0, "vwap": float(k["close"]),
                "levels_buy": {round(float(k["close"]) / PRICE_LEVEL_ROUND) * PRICE_LEVEL_ROUND: buy_vol},
                "levels_sell": {round(float(k["close"]) / PRICE_LEVEL_ROUND) * PRICE_LEVEL_ROUND: sell_vol},
            }
            historical_bars.append(finalize_bar(bar))
            added = True
        if added:
            historical_bars.sort(key=lambda b: b["time"])


# =============================================================================
# ZONES (Supply/Demand), MULTI-TIMEFRAME TREND, DAILY BIAS, ENTRY/SL/TP SETUP
# All pure functions over historical_bars - deterministic, explainable rules.
# First-pass thresholds; expect to tune after you watch it live for a while.
# =============================================================================
def calc_atr(bars, period=14):
    if len(bars) < 2:
        return 2.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return (sum(recent) / len(recent)) if recent else 2.0


def resample(bars, tf_minutes):
    if not bars:
        return []
    out, order, agg = {}, [], {}
    for b in bars:
        day_start = b["time"].replace(hour=0, minute=0, second=0, microsecond=0)
        delta_min = int((b["time"] - day_start).total_seconds() // 60)
        bucket = day_start + timedelta(minutes=(delta_min // tf_minutes) * tf_minutes)
        if bucket not in agg:
            agg[bucket] = dict(b)
            agg[bucket]["time"] = bucket
            order.append(bucket)
        else:
            a = agg[bucket]
            a["high"] = max(a["high"], b["high"])
            a["low"] = min(a["low"], b["low"])
            a["close"] = b["close"]
            a["volume"] = a.get("volume", 0) + b.get("volume", 0)
    return [agg[k] for k in order]


def build_zones(bars, tf_label, atr, max_zones=4, impulse_mult=IMPULSE_MULT, lookahead=OB_LOOKAHEAD):
    """Order-Block zones (backtest-validated - see header note): a zone's
    origin is the last opposite-colored candle before an impulsive move of
    >= impulse_mult x ATR within `lookahead` bars - not a plain swing
    high/low. Each zone carries origin_low/origin_high (the origin candle's
    own extremes) for the structural SL used in check_zone_reaction."""
    if len(bars) < lookahead + 2:
        return []
    zones = []
    for m in range(1, len(bars) - lookahead):
        b = bars[m]
        a = atr if atr > 0 else 1.0
        if b["close"] < b["open"]:
            fwd_high = max(bars[j]["high"] for j in range(m + 1, m + 1 + lookahead))
            if fwd_high - b["close"] >= impulse_mult * a:
                top = max(b["open"], b["close"])
                bottom = b["low"] - ZONE_ATR_BUFFER * a
                mitigated = any(bars[j]["close"] < bottom for j in range(m + 1, len(bars)))
                zones.append({"kind": "DEMAND", "top": round(top, 2), "bottom": round(bottom, 2),
                              "start_time": int(b["time"].timestamp()), "tf": tf_label, "mitigated": mitigated,
                              "origin_low": round(b["low"], 2), "origin_high": round(b["high"], 2)})
        if b["close"] > b["open"]:
            fwd_low = min(bars[j]["low"] for j in range(m + 1, m + 1 + lookahead))
            if b["close"] - fwd_low >= impulse_mult * a:
                top = b["high"] + ZONE_ATR_BUFFER * a
                bottom = min(b["open"], b["close"])
                mitigated = any(bars[j]["close"] > top for j in range(m + 1, len(bars)))
                zones.append({"kind": "SUPPLY", "top": round(top, 2), "bottom": round(bottom, 2),
                              "start_time": int(b["time"].timestamp()), "tf": tf_label, "mitigated": mitigated,
                              "origin_low": round(b["low"], 2), "origin_high": round(b["high"], 2)})
    active = [z for z in zones if not z["mitigated"]]
    active.sort(key=lambda z: z["start_time"])
    supply = [z for z in active if z["kind"] == "SUPPLY"][-max_zones:]
    demand = [z for z in active if z["kind"] == "DEMAND"][-max_zones:]
    return supply + demand


def has_stacked_imbalance(bar, direction, min_run=STACK_MIN_RUN, ratio=STACK_RATIO):
    """3+ consecutive footprint price-levels, inside this one bar, with a
    buy:sell (or sell:buy) ratio >= `ratio` in the given direction."""
    prices = sorted(set(bar.get("levels_buy", {})) | set(bar.get("levels_sell", {})))
    if len(prices) < min_run:
        return False
    run = best = 0
    for p in prices:
        bv = bar["levels_buy"].get(p, 0.0)
        sv = bar["levels_sell"].get(p, 0.0)
        if direction == "Bullish" and bv >= ratio * max(sv, 0.01):
            run += 1
        elif direction == "Bearish" and sv >= ratio * max(bv, 0.01):
            run += 1
        else:
            run = 0
        best = max(best, run)
    return best >= min_run


def find_cvd_break(bars, direction, left=2, right=2, lookback=300):
    """True if Cumulative Volume Delta has broken its own most recent
    confirmed short-term swing high (Bullish) / swing low (Bearish) -
    'delta leads price'. Mini-fractal on bars[i]['cvd'], same method as
    price-zone swings, just applied to the CVD series instead."""
    window_bars = bars[-lookback:] if len(bars) > lookback else bars
    n = len(window_bars)
    if n < left + right + 1:
        return False
    last_high_val = last_low_val = None
    for i in range(left, n - right):
        cvd_window = [window_bars[j]["cvd"] for j in range(i - left, i + right + 1)]
        if window_bars[i]["cvd"] == max(cvd_window) and cvd_window.count(window_bars[i]["cvd"]) == 1:
            last_high_val = window_bars[i]["cvd"]
        if window_bars[i]["cvd"] == min(cvd_window) and cvd_window.count(window_bars[i]["cvd"]) == 1:
            last_low_val = window_bars[i]["cvd"]
    last_cvd = window_bars[-1]["cvd"]
    if direction == "Bullish":
        return last_high_val is not None and last_cvd > last_high_val
    return last_low_val is not None and last_cvd < last_low_val


def compute_daily_bias(bars):
    if not bars:
        return {"bias": "N/A"}
    price = bars[-1]["close"]
    today = bars[-1]["time"].date()
    todays = [b for b in bars if b["time"].date() == today]
    day_open = todays[0]["open"] if todays else price
    day_high = max(b["high"] for b in todays) if todays else price
    day_low = min(b["low"] for b in todays) if todays else price
    week_start = today - timedelta(days=today.weekday())
    week_bars = [b for b in bars if b["time"].date() >= week_start]
    week_open = week_bars[0]["open"] if week_bars else day_open

    weekly_dir = "BULLISH" if price >= week_open else "BEARISH"
    daily_dir = "BULLISH" if price >= day_open else "BEARISH"
    if weekly_dir == daily_dir:
        bias = weekly_dir
        target = day_high if bias == "BULLISH" else day_low
    else:
        bias = "MIXED / RANGE"
        target = day_high if price >= (day_high + day_low) / 2 else day_low
    return {
        "bias": bias, "weekly_dir": weekly_dir, "daily_dir": daily_dir,
        "liquidity_target": round(target, 2), "day_high": round(day_high, 2), "day_low": round(day_low, 2),
    }


def compute_mtf_trend(bars, tf_minutes=15, fast=9, slow=21):
    rb = resample(bars, tf_minutes)
    closes = [b["close"] for b in rb]
    if len(closes) < slow + 1:
        return {"tf": f"{tf_minutes}m", "direction": "N/A"}

    def ema(vals, period):
        k = 2 / (period + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    ema_fast = ema(closes, fast)[-1]
    ema_slow = ema(closes, slow)[-1]
    direction = "UP" if ema_fast > ema_slow else "DOWN" if ema_fast < ema_slow else "FLAT"
    return {"tf": f"{tf_minutes}m", "direction": direction, "ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)}


def check_zone_reaction(bars, zones, atr):
    """Backtest-validated entry rule (see header note): Order-Block zone
    touch+reject+confirm, THEN structural SL, THEN min-1.5R, THEN stacked
    footprint imbalance, THEN CVD structure break - a zone only produces a
    setup if it clears all four. Checks each touched zone in turn so one
    zone failing a filter doesn't block a different zone that passes."""
    if len(bars) < 2:
        return None
    last = bars[-1]
    for z in zones:
        if z["kind"] == "DEMAND" and last["low"] <= z["top"] and last["close"] > z["top"] and last["close"] > last["open"]:
            entry = z["top"]
            sl = round(z["origin_low"] - SL_TICK_BUFFER, 2)
            risk = entry - sl
            if risk <= 0:
                continue
            opp = [zz for zz in zones if zz["kind"] == "SUPPLY" and zz["bottom"] > entry]
            tp = min(zz["bottom"] for zz in opp) if opp else round(entry + 2 * risk, 2)
            if (tp - entry) / risk < MIN_RR:
                continue
            if not has_stacked_imbalance(last, "Bullish"):
                continue
            if not find_cvd_break(bars, "Bullish"):
                continue
            return {"side": "BUY", "entry": round(entry, 2), "sl": sl, "tp": round(tp, 2),
                    "opened_at": int(last["time"].timestamp())}
        if z["kind"] == "SUPPLY" and last["high"] >= z["bottom"] and last["close"] < z["bottom"] and last["close"] < last["open"]:
            entry = z["bottom"]
            sl = round(z["origin_high"] + SL_TICK_BUFFER, 2)
            risk = sl - entry
            if risk <= 0:
                continue
            opp = [zz for zz in zones if zz["kind"] == "DEMAND" and zz["top"] < entry]
            tp = max(zz["top"] for zz in opp) if opp else round(entry - 2 * risk, 2)
            if (entry - tp) / risk < MIN_RR:
                continue
            if not has_stacked_imbalance(last, "Bearish"):
                continue
            if not find_cvd_break(bars, "Bearish"):
                continue
            return {"side": "SELL", "entry": round(entry, 2), "sl": sl, "tp": round(tp, 2),
                    "opened_at": int(last["time"].timestamp())}
    return None


def _record_closed_setup(outcome, exit_price, closed_at):
    """outcome: 'TP' or 'SL'. Appends a row to setup_history and sets the
    short last_setup_outcome line - called with data_lock already held."""
    global last_setup_outcome
    s = current_setup
    pnl = (exit_price - s["entry"]) if s["side"] == "BUY" else (s["entry"] - exit_price)
    rec = {
        "side": s["side"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
        "outcome": outcome, "exit_price": round(exit_price, 2), "pnl": round(pnl, 2),
        "opened_at": s["opened_at"], "closed_at": closed_at,
    }
    setup_history.append(rec)
    _db_save_closed_setup(rec)
    sign = "+" if pnl >= 0 else ""
    last_setup_outcome = f"Last: {s['side']} hit {outcome} {sign}{pnl:.2f}"


def _update_zones_and_setup_locked():
    """Called with data_lock already held, right after a bar finalizes."""
    global current_setup, last_setup_outcome
    bars = historical_bars
    if len(bars) < 6:
        return
    last = bars[-1]
    closed_at = int(last["time"].timestamp())

    # SL TRAILING (backtest-validated $2-arm/$1-trail): everything else about
    # a setup (zone, structural SL at entry, min-RR filter, stacked-imbalance
    # + CVD-break confluence, the displayed "tp" estimate) is UNCHANGED - only
    # the exit itself now trails instead of waiting for a fixed TP. Once
    # floating profit reaches TRAIL_ARM, the stop only ever tightens (never
    # loosens) to TRAIL_DIST behind the best price reached since entry.
    # Outcome stays "SL" (a real loss, stopped before ever arming) or "TP"
    # (armed and locked in profit) so the existing stats/UI - which count a
    # "TP" outcome as a win - need no changes at all.
    if current_setup is not None:
        s = current_setup
        if s["side"] == "BUY":
            s["best"] = max(s.get("best", s["entry"]), last["high"])
            if not s.get("armed") and (s["best"] - s["entry"]) >= TRAIL_ARM:
                s["armed"] = True
            if s.get("armed"):
                new_sl = round(max(s["sl"], s["best"] - TRAIL_DIST), 2)
                if new_sl != s["sl"]:
                    s["sl"] = new_sl
                    if s.get("ticket"):
                        mt5_modify_position(s["ticket"], s["sl"], 0)
            if last["low"] <= s["sl"]:
                if s.get("ticket"):
                    mt5_close_position(s["ticket"], s["side"])  # instant MT5 exit, mirrors paper
                _record_closed_setup("TP" if s.get("armed") else "SL", s["sl"], closed_at)
                current_setup = None
        else:
            s["best"] = min(s.get("best", s["entry"]), last["low"])
            if not s.get("armed") and (s["entry"] - s["best"]) >= TRAIL_ARM:
                s["armed"] = True
            if s.get("armed"):
                new_sl = round(min(s["sl"], s["best"] + TRAIL_DIST), 2)
                if new_sl != s["sl"]:
                    s["sl"] = new_sl
                    if s.get("ticket"):
                        mt5_modify_position(s["ticket"], s["sl"], 0)
            if last["high"] >= s["sl"]:
                if s.get("ticket"):
                    mt5_close_position(s["ticket"], s["side"])  # instant MT5 exit, mirrors paper
                _record_closed_setup("TP" if s.get("armed") else "SL", s["sl"], closed_at)
                current_setup = None

    if current_setup is None:
        # build_zones does an O(n) mitigation-scan per candidate zone - bound
        # it to a recent window (doesn't limit what the CHART shows/restores,
        # only how far back zone-detection looks; a zone from days ago at a
        # stale price isn't a live setup candidate anyway). bars itself
        # (passed to check_zone_reaction/find_cvd_break) stays unbounded.
        calc_bars = bars[-ZONE_CALC_WINDOW:]
        atr = calc_atr(calc_bars)
        ltf_zones = build_zones(calc_bars, "LTF", atr)
        new_setup = check_zone_reaction(bars, ltf_zones, atr)
        if new_setup:
            # No broker-side TP (tp=0) - the exit is a trailing stop, not a
            # fixed target; only SL is placed/modified. Paper tracking
            # proceeds regardless of whether the MT5 order succeeds.
            new_setup["ticket"] = mt5_place_order(new_setup["side"], new_setup["sl"], 0,
                                                    MT5_MAGIC, f"GF-9100")
            current_setup = new_setup

    _db_save_state()


# =============================================================================
# WEBSOCKET CLIENT (subscribes to Port 9000, read-only)
# =============================================================================
async def handle_message(raw):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    mtype = msg.get("type")
    if mtype == "init":
        candles = msg.get("candles") or []
        if candles:
            seed_from_klines(candles)
        lp = msg.get("last_price")
        if lp:
            update_mark_price(float(lp))
    elif mtype == "mark_price":
        update_mark_price(float(msg["price"]))
    elif mtype == "real_spot":
        update_mark_price(float(msg["price"]))
    elif mtype == "tick":
        process_tick(float(msg["price"]), float(msg["volume"]), msg.get("side") == "BUY")
    elif mtype == "depth":
        process_depth(msg.get("bids", []), msg.get("asks", []))


async def ws_client_loop():
    backoff = 1
    while True:
        try:
            logger.info("Connecting to Port 9000 feed (read-only subscriber)...")
            async with websockets.connect(SOURCE_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                conn_state["connected"] = True
                backoff = 1
                logger.info("Connected to Port 9000.")
                async for raw in ws:
                    await handle_message(raw)
        except Exception as e:
            conn_state["connected"] = False
            logger.warning(f"Disconnected from Port 9000 ({e!r}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# =============================================================================
# VWAP (matches the exact rolling formula already used on 9060/9080's own
# charts - see Env/architecture notes - so this chart's VWAP line reads the
# same way a user already trusts from those dashboards)
# =============================================================================
def rolling_chart_vwap(bars, window=100):
    """TRUE sliding window (last `window` bars ending at each point) - not
    cumulative-from-the-start-of-`bars`. This matters now that `bars` can be
    the full unlimited history (up to 14 days): a naive cumulative sum
    would make the VWAP line a slow multi-day average instead of the
    responsive ~100-bar line every other port's own chart shows, silently
    changing its meaning just because more candles became visible. Uses
    running sums (add the incoming bar, subtract the one leaving the
    window) so this stays O(n) regardless of how much history is passed in."""
    out = []
    sum_pv = sum_vol = sum_tp2v = 0.0
    win = deque()  # (tp, vol) pairs currently inside the window - deque for O(1) popleft
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b.get("volume", 0) or 0
        win.append((tp, vol))
        sum_pv += tp * vol
        sum_vol += vol
        sum_tp2v += tp * tp * vol
        if len(win) > window:
            otp, ovol = win.popleft()
            sum_pv -= otp * ovol
            sum_vol -= ovol
            sum_tp2v -= otp * otp * ovol
        vwap = (sum_pv / sum_vol) if sum_vol > 0 else b["close"]
        variance = max(0.0, (sum_tp2v / sum_vol) - vwap * vwap) if sum_vol > 0 else 0.0
        std = variance ** 0.5
        out.append((round(vwap, 2), round(vwap + 1.28 * std, 2), round(vwap - 1.28 * std, 2)))
    return out


# =============================================================================
# HTTP API
# =============================================================================
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1H": 60}


@app.get("/api/chart_data")
async def chart_data(tf: str = "1m"):
    with data_lock:
        bars = list(historical_bars)  # unlimited - everything in memory (restart restores all of it)
        cur = dict(current_bar)
        setup = dict(current_setup) if current_setup else None
        outcome = last_setup_outcome
        history = list(setup_history)[-20:][::-1]  # most recent first

    tf_min = TF_MINUTES.get(tf, 1)
    display_bars = resample(bars, tf_min) if tf_min > 1 else bars
    window = display_bars  # unlimited - chart shows the full history, not just a recent slice
    vwaps = rolling_chart_vwap(window)
    candles = []
    for b, (vwap, vu, vl) in zip(window, vwaps):
        candles.append({
            "time": int(b["time"].timestamp()), "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"], "volume": b.get("volume", 0),
            "vwap": vwap, "vwap_upper": vu, "vwap_lower": vl,
            # POC/imbalance/FVG/absorption are only meaningful on the true 1m bars
            # they were computed on - resample() doesn't recompute them for
            # higher TFs, so don't show misleading markers on those views.
            "poc": b.get("poc") if tf_min == 1 else None,
            "imbalance": bool(b.get("imbalance")) if tf_min == 1 else False,
            "fvg": b.get("fvg") if tf_min == 1 else None,
            "absorption": b.get("absorption") if tf_min == 1 else None,
            "delta": round(b.get("delta", 0), 1),
        })

    # Zones stay scoped to a recent window (same ZONE_CALC_WINDOW the live
    # entry-logic itself uses in _update_zones_and_setup_locked) - both for
    # performance (build_zones' mitigation-scan is O(n) per candidate) and
    # so what's DISPLAYED always matches what's actually driving live
    # signals. This does not limit candle display or restart-restoration,
    # only how far back zone-detection looks - a zone from days ago at a
    # stale price isn't a live setup candidate anyway.
    calc_bars = bars[-ZONE_CALC_WINDOW:]
    atr = calc_atr(calc_bars)
    zones_ltf = build_zones(calc_bars, "LTF", atr)
    zones_htf = build_zones(resample(calc_bars, 15), "HTF", atr)
    bias = compute_daily_bias(bars)     # needs full history for correct day/week open
    mtf = compute_mtf_trend(bars)       # needs enough history for EMA warmup

    last_price = cur["close"] if cur.get("close") is not None else (bars[-1]["close"] if bars else None)

    all_history = list(setup_history)
    n_closed = len(all_history)
    n_wins = sum(1 for h in all_history if h["outcome"] == "TP")
    total_pnl = round(sum(h["pnl"] for h in all_history), 2)
    stats = {
        "n_closed": n_closed, "n_wins": n_wins,
        "win_rate": round(n_wins / n_closed * 100, 1) if n_closed else None,
        "total_pnl": total_pnl,
    }

    return JSONResponse({
        "candles": candles,
        "zones_ltf": zones_ltf, "zones_htf": zones_htf,
        "daily_bias": bias, "mtf_trend": mtf,
        "setup": setup, "last_setup_outcome": outcome,
        "setup_history": history, "setup_stats": stats,
        "last_price": last_price,
        "source_connected": conn_state["connected"],
    })


@app.get("/api/footprint")
async def footprint(time: int):
    with data_lock:
        target = None
        for b in historical_bars[-300:]:
            if int(b["time"].timestamp()) == time:
                target = b
                break
        if target is None and current_bar.get("time") and int(current_bar["time"].timestamp()) == time:
            target = current_bar
        if target is None:
            return JSONResponse({"time": time, "levels": []})
        prices = set(target["levels_buy"].keys()) | set(target["levels_sell"].keys())
        levels = [{"price": p, "buy": round(target["levels_buy"].get(p, 0.0), 2),
                    "sell": round(target["levels_sell"].get(p, 0.0), 2)} for p in prices]
        levels.sort(key=lambda x: x["price"], reverse=True)
    return JSONResponse({"time": time, "levels": levels})


@app.get("/api/debug_zones")
async def debug_zones():
    with data_lock:
        bars = list(historical_bars[-300:])
    atr = calc_atr(bars)
    rb = resample(bars, 15)
    zones_ltf = build_zones(bars, "LTF", atr, max_zones=10)
    zones_htf = build_zones(rb, "HTF", atr, max_zones=10)
    return JSONResponse({
        "n_bars": len(bars), "n_bars_total": len(historical_bars), "atr": atr,
        "first_time": bars[0]["time"].isoformat() if bars else None,
        "last_time": bars[-1]["time"].isoformat() if bars else None,
        "n_15m_bars": len(rb), "rb_times": [b["time"].isoformat() for b in rb],
        "n_ob_zones_ltf": len(zones_ltf), "n_ob_zones_htf": len(zones_htf),
        "zones_ltf_raw": zones_ltf,
        "zones_htf_raw": zones_htf,
    })


@app.get("/api/heatmap")
async def heatmap():
    import time as _time
    cutoff = _time.time() - HEATMAP_WINDOW_SECONDS
    with depth_lock:
        snaps = [s for s in depth_history if s["t"] >= cutoff]
    bins = {}
    for snap in snaps:
        for side in ("bids", "asks"):
            for p, q in snap.get(side, []):
                pb = round(float(p) / HEATMAP_PRICE_BIN) * HEATMAP_PRICE_BIN
                bins[pb] = bins.get(pb, 0.0) + float(q)
    if not bins:
        return JSONResponse({"cells": [], "note": "PAXG order-book proxy, not a literal spot broker order book."})
    mx = max(bins.values())
    cells = [{"price": round(p, 2), "intensity": round(v / mx, 3)} for p, v in sorted(bins.items())]
    return JSONResponse({"cells": cells, "note": "PAXG order-book proxy, not a literal spot broker order book."})


# =============================================================================
# FRONTEND (single page - TradingView Lightweight Charts + one canvas overlay
# for zones/heatmap - "ek j dashboard", no separate screens)
# =============================================================================
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>XAUUSD Order-Flow Zones</title>
<meta charset="utf-8">
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  html, body { margin:0; padding:0; width:100%; height:100%; background:#0b0e14; overflow:hidden; font-family:'Segoe UI',sans-serif; }
  #topbar { display:flex; align-items:center; justify-content:space-between; padding:8px 14px; background:#0d1117; border-bottom:1px solid #21262d; }
  #title { color:#FFD700; font-weight:700; font-size:14px; }
  #status { font-size:11px; color:#00e676; }
  #status.down { color:#f23645; }
  #toggles button, #tfrow button { background:#1c2030; color:#ccc; border:1px solid #2a2e39; padding:6px 12px; margin-left:4px; border-radius:4px; cursor:pointer; font-size:11px; }
  #toggles button.on { background:#38BDF8; color:#111; font-weight:700; }
  #tfrow button.active { background:#FFD700; color:#111; font-weight:700; }
  #main { display:flex; height:calc(100vh - 44px); }
  #chartWrap { position:relative; flex:1; }
  #chartContainer { width:100%; height:100%; }
  #overlayCanvas { position:absolute; top:0; left:0; pointer-events:none; z-index:4; }
  #sidepanel { position:relative; z-index:5; width:230px; background:#0d1117; border-left:1px solid #21262d; padding:10px; box-sizing:border-box; overflow-y:auto; font-size:11px; color:#cbd5e1; }
  .panel-card { background:#161b22; border:1px solid #21262d; border-radius:6px; padding:8px; margin-bottom:10px; }
  .panel-title { color:#FFD700; font-weight:700; font-size:11px; margin-bottom:6px; letter-spacing:0.5px; }
  .row { display:flex; justify-content:space-between; margin-bottom:3px; }
  .bull { color:#00E676; } .bear { color:#FF3366; } .neutral { color:#94a3b8; }
  .hist-row { display:flex; justify-content:space-between; padding:4px 2px; border-bottom:1px solid #1c2230; font-size:10.5px; }
  .hist-row:last-child { border-bottom:none; }
  .hist-side-buy { color:#00E676; font-weight:700; }
  .hist-side-sell { color:#FF3366; font-weight:700; }
  .hist-tp { color:#00E676; } .hist-sl { color:#FF3366; }
  #footprintPanel { position:absolute; bottom:14px; left:14px; background:rgba(13,17,23,0.95); border:1px solid #2a2e39; border-radius:6px; padding:8px 10px; font-size:11px; color:#cbd5e1; max-height:260px; overflow-y:auto; display:none; z-index:5; }
  #footprintPanel .fp-row { display:flex; gap:8px; }
  #footprintPanel .fp-buy { color:#00E676; width:60px; text-align:right; }
  #footprintPanel .fp-sell { color:#FF3366; width:60px; }
  #ohlc { position:absolute; top:10px; left:14px; color:#eee; font-size:12px; z-index:5; background:rgba(13,17,23,0.7); padding:4px 8px; border-radius:4px; }
</style>
</head>
<body>
  <div id="topbar">
    <div id="title">XAUUSD // ORDER-FLOW ZONES [PORT 9100]</div>
    <div id="tfrow">
      <button data-tf="1m" class="active">1m</button>
      <button data-tf="5m">5m</button>
      <button data-tf="15m">15m</button>
      <button data-tf="1H">1H</button>
    </div>
    <div id="toggles">
      <button id="btnZones" class="on">Zones</button>
      <button id="btnHeatmap">Heatmap</button>
    </div>
    <div id="status">LIVE</div>
  </div>
  <div id="main">
    <div id="chartWrap">
      <div id="ohlc"></div>
      <div id="chartContainer"></div>
      <canvas id="overlayCanvas"></canvas>
      <div id="footprintPanel"></div>
    </div>
    <div id="sidepanel">
      <div class="panel-card">
        <div class="panel-title">DAILY BIAS</div>
        <div id="biasBody">-</div>
      </div>
      <div class="panel-card">
        <div class="panel-title">MULTI-TIMEFRAME TREND</div>
        <div id="mtfBody">-</div>
      </div>
      <div class="panel-card">
        <div class="panel-title">ACTIVE SETUP (visual only)</div>
        <div id="setupBody">No active setup</div>
      </div>
      <div class="panel-card">
        <div class="panel-title">TRADE HISTORY (visual only)</div>
        <div id="historyStats" style="color:#64748b;margin-bottom:6px;">No closed setups yet</div>
        <div id="historyList" style="max-height:220px;overflow-y:auto;"></div>
      </div>
      <div class="panel-card">
        <div class="panel-title">ZONES LEGEND (Order Blocks)</div>
        <div class="row"><span class="bear">Supply</span><span>price rejects down</span></div>
        <div class="row"><span class="bull">Demand</span><span>price rejects up</span></div>
        <div style="color:#64748b;margin-top:4px;">Entry requires: zone reject + min 1.5R + stacked footprint imbalance + CVD structure break. Click any candle to see its footprint (buy/sell by price level).</div>
      </div>
    </div>
  </div>

<script>
let chart, candleSeries, vwapSeries, upperSeries, lowerSeries;
let entryLine=null, slLine=null, tpLine=null;
let currentZonesLTF=[], currentZonesHTF=[];
let heatmapData=[];
let zonesOn=true, heatmapOn=false;
let currentTf='1m';

function initChart(){
  const container = document.getElementById('chartContainer');
  chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: { background:{color:'#0b0e14'}, textColor:'#94a3b8' },
    grid: { vertLines:{color:'rgba(255,255,255,0.05)'}, horzLines:{color:'rgba(255,255,255,0.05)'} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor:'rgba(255,255,255,0.1)' },
    timeScale: { borderColor:'rgba(255,255,255,0.1)', timeVisible:true, secondsVisible:false },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#00E676', downColor:'#FF3366', borderVisible:false,
    wickUpColor:'#00E676', wickDownColor:'#FF3366',
  });
  vwapSeries = chart.addLineSeries({ color:'#FFD700', lineWidth:2 });
  upperSeries = chart.addLineSeries({ color:'rgba(0,229,255,0.5)', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed });
  lowerSeries = chart.addLineSeries({ color:'rgba(0,229,255,0.5)', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed });

  chart.timeScale().subscribeVisibleTimeRangeChange(redrawOverlay);
  chart.subscribeClick(param => { if (param.time) showFootprint(param.time); });
  window.addEventListener('resize', redrawOverlay);
}

function resizeCanvas(){
  const wrap = document.getElementById('chartWrap');
  const canvas = document.getElementById('overlayCanvas');
  canvas.width = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
}

function redrawOverlay(){
  resizeCanvas();
  const canvas = document.getElementById('overlayCanvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if (heatmapOn) drawHeatmap(ctx);
  if (zonesOn) drawZones(ctx);
}

function drawZones(ctx){
  const all = [...currentZonesHTF.map(z=>({...z, htf:true})), ...currentZonesLTF.map(z=>({...z, htf:false}))];
  all.forEach(z => {
    const x1 = chart.timeScale().timeToCoordinate(z.start_time);
    const yTop = candleSeries.priceToCoordinate(z.top);
    const yBot = candleSeries.priceToCoordinate(z.bottom);
    if (x1==null || yTop==null || yBot==null) return;
    const x2 = ctx.canvas.width;
    const isSupply = z.kind === 'SUPPLY';
    const alpha = z.htf ? 0.10 : 0.16;
    ctx.fillStyle = isSupply ? `rgba(255,51,102,${alpha})` : `rgba(0,230,118,${alpha})`;
    ctx.strokeStyle = isSupply ? 'rgba(255,51,102,0.55)' : 'rgba(0,230,118,0.55)';
    ctx.lineWidth = z.htf ? 1.5 : 1;
    ctx.fillRect(x1, yTop, x2-x1, Math.max(2, yBot-yTop));
    ctx.strokeRect(x1, yTop, x2-x1, Math.max(2, yBot-yTop));
    ctx.fillStyle = isSupply ? '#FF3366' : '#00E676';
    ctx.font = z.htf ? 'bold 10px sans-serif' : '9px sans-serif';
    ctx.fillText((z.htf?'HTF ':'') + z.kind, x1+4, yTop + (isSupply ? 12 : -4));
  });
}

function heatColor(t){
  // Bookmap/ATAS-style gradient: deep blue(low) -> cyan -> green -> yellow -> red(high)
  const stops = [
    [0.0, [13, 27, 89]], [0.25, [0, 145, 219]], [0.5, [0, 200, 120]],
    [0.75, [255, 214, 0]], [1.0, [255, 51, 51]],
  ];
  for (let i = 0; i < stops.length - 1; i++){
    const [t0, c0] = stops[i], [t1, c1] = stops[i+1];
    if (t >= t0 && t <= t1){
      const f = (t - t0) / (t1 - t0 || 1);
      return [Math.round(c0[0]+(c1[0]-c0[0])*f), Math.round(c0[1]+(c1[1]-c0[1])*f), Math.round(c0[2]+(c1[2]-c0[2])*f)];
    }
  }
  return stops[stops.length-1][1];
}

function drawHeatmap(ctx){
  if (!heatmapData.length) return;
  // Fill the actual price-bin height in pixels (not a thin fixed line) so bands
  // look like a continuous texture instead of faint hairlines.
  let binPx = 8;
  if (heatmapData.length >= 2){
    const y0 = candleSeries.priceToCoordinate(heatmapData[0].price);
    const y1 = candleSeries.priceToCoordinate(heatmapData[1].price);
    if (y0 != null && y1 != null) binPx = Math.max(4, Math.abs(y0 - y1) + 1);
  }
  heatmapData.forEach(cell => {
    const y = candleSeries.priceToCoordinate(cell.price);
    if (y == null) return;
    const [r, g, b] = heatColor(cell.intensity);
    const alpha = 0.18 + cell.intensity * 0.55;  // always at least faintly visible, strong at hot zones
    ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
    ctx.fillRect(0, y - binPx / 2, ctx.canvas.width, binPx);
  });
}

async function showFootprint(time){
  try {
    const res = await fetch(`/api/footprint?time=${time}`);
    const data = await res.json();
    const panel = document.getElementById('footprintPanel');
    if (!data.levels || !data.levels.length){ panel.style.display='none'; return; }
    let html = `<div style="color:#FFD700;font-weight:700;margin-bottom:4px;">Footprint @ ${new Date(time*1000).toLocaleTimeString()}</div>`;
    data.levels.forEach(l => {
      html += `<div class="fp-row"><span class="fp-buy">${l.buy.toFixed(1)}</span><span>${l.price.toFixed(1)}</span><span class="fp-sell">${l.sell.toFixed(1)}</span></div>`;
    });
    panel.innerHTML = html;
    panel.style.display = 'block';
  } catch(e) {}
}

async function loadHeatmap(){
  if (!heatmapOn) return;
  try {
    const res = await fetch('/api/heatmap');
    const data = await res.json();
    heatmapData = data.cells || [];
    redrawOverlay();
  } catch(e) {}
}

async function loadCandles(){
  try {
    const res = await fetch(`/api/chart_data?tf=${currentTf}`);
    const data = await res.json();
    document.getElementById('status').className = data.source_connected ? '' : 'down';
    document.getElementById('status').innerText = data.source_connected ? 'LIVE (Port 9000)' : 'RECONNECTING...';
    if (!data.candles.length) return;

    candleSeries.setData(data.candles.map(c => ({ time:c.time, open:c.open, high:c.high, low:c.low, close:c.close })));
    vwapSeries.setData(data.candles.map(c => ({ time:c.time, value:c.vwap })));
    upperSeries.setData(data.candles.map(c => ({ time:c.time, value:c.vwap_upper })));
    lowerSeries.setData(data.candles.map(c => ({ time:c.time, value:c.vwap_lower })));

    const markers = [];
    data.candles.forEach(c => {
      if (c.absorption) markers.push({ time:c.time, position: c.absorption==='Bullish'?'belowBar':'aboveBar', color: c.absorption==='Bullish'?'#00e676':'#ff3366', shape:'circle', text:'ABS' });
      else if (c.fvg) markers.push({ time:c.time, position: c.fvg==='Bullish'?'belowBar':'aboveBar', color:'#38bdf8', shape:'square', text:'FVG' });
      else if (c.imbalance) markers.push({ time:c.time, position:'aboveBar', color:'#00e5ff', shape:'circle', text:'*' });
    });
    candleSeries.setMarkers(markers);

    currentZonesLTF = data.zones_ltf || [];
    currentZonesHTF = data.zones_htf || [];

    const last = data.candles[data.candles.length-1];
    document.getElementById('ohlc').innerText =
      `O:${last.open.toFixed(2)} H:${last.high.toFixed(2)} L:${last.low.toFixed(2)} C:${last.close.toFixed(2)} VWAP:${last.vwap.toFixed(2)}`;

    const b = data.daily_bias || {};
    document.getElementById('biasBody').innerHTML =
      `<div class="row"><span>Bias</span><span class="${b.bias==='BULLISH'?'bull':b.bias==='BEARISH'?'bear':'neutral'}">${b.bias||'-'}</span></div>
       <div class="row"><span>Weekly</span><span>${b.weekly_dir||'-'}</span></div>
       <div class="row"><span>Daily</span><span>${b.daily_dir||'-'}</span></div>
       <div class="row"><span>Target</span><span>${b.liquidity_target!=null?b.liquidity_target.toFixed(2):'-'}</span></div>`;

    const m = data.mtf_trend || {};
    document.getElementById('mtfBody').innerHTML =
      `<div class="row"><span>${m.tf||'-'}</span><span class="${m.direction==='UP'?'bull':m.direction==='DOWN'?'bear':'neutral'}">${m.direction||'-'}</span></div>`;

    if (entryLine){ candleSeries.removePriceLine(entryLine); entryLine=null; }
    if (slLine){ candleSeries.removePriceLine(slLine); slLine=null; }
    if (tpLine){ candleSeries.removePriceLine(tpLine); tpLine=null; }
    const s = data.setup;
    if (s){
      entryLine = candleSeries.createPriceLine({ price:s.entry, color:'#2962ff', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dashed, title:'ENTRY '+s.side });
      slLine = candleSeries.createPriceLine({ price:s.sl, color:'#FF3366', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dotted, title:'SL' });
      tpLine = candleSeries.createPriceLine({ price:s.tp, color:'#00E676', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dotted, title:'TP' });
      document.getElementById('setupBody').innerHTML =
        `<div class="row"><span class="${s.side==='BUY'?'bull':'bear'}">${s.side}</span><span>Entry ${s.entry.toFixed(2)}</span></div>
         <div class="row"><span>SL</span><span class="bear">${s.sl.toFixed(2)}</span></div>
         <div class="row"><span>TP</span><span class="bull">${s.tp.toFixed(2)}</span></div>`;
    } else {
      document.getElementById('setupBody').innerText = data.last_setup_outcome || 'No active setup';
    }

    const stats = data.setup_stats || {};
    const histStatsEl = document.getElementById('historyStats');
    if (stats.n_closed) {
      const pnlClass = stats.total_pnl >= 0 ? 'bull' : 'bear';
      const pnlSign = stats.total_pnl >= 0 ? '+' : '';
      histStatsEl.innerHTML = `${stats.n_wins}/${stats.n_closed} TP (${stats.win_rate}%) &nbsp;|&nbsp; Net <span class="${pnlClass}">${pnlSign}${stats.total_pnl.toFixed(2)}</span>`;
    } else {
      histStatsEl.innerText = 'No closed setups yet';
    }
    const histListEl = document.getElementById('historyList');
    const hist = data.setup_history || [];
    if (hist.length) {
      histListEl.innerHTML = hist.map(h => {
        const sideClass = h.side === 'BUY' ? 'hist-side-buy' : 'hist-side-sell';
        const outClass = h.outcome === 'TP' ? 'hist-tp' : 'hist-sl';
        const pnlSign = h.pnl >= 0 ? '+' : '';
        const fmtT = (ts) => new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const openT = fmtT(h.opened_at);
        const closeT = fmtT(h.closed_at);
        return `<div class="hist-row"><span class="${sideClass}">${h.side}</span><span>${h.entry.toFixed(2)}→${h.exit_price.toFixed(2)}</span><span class="${outClass}">${h.outcome} ${pnlSign}${h.pnl.toFixed(2)}</span><span style="color:#64748b;" title="opened ${openT}, closed ${closeT}">${openT}→${closeT}</span></div>`;
      }).join('');
    } else {
      histListEl.innerHTML = '';
    }

    redrawOverlay();
  } catch(e) {
    document.getElementById('status').className='down';
    document.getElementById('status').innerText='ERROR';
  }
}

document.querySelectorAll('#tfrow button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tfrow button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentTf = btn.dataset.tf;
    loadCandles();
  });
});

document.getElementById('btnZones').addEventListener('click', function(){
  zonesOn = !zonesOn;
  this.className = zonesOn ? 'on' : '';
  redrawOverlay();
});
document.getElementById('btnHeatmap').addEventListener('click', function(){
  heatmapOn = !heatmapOn;
  this.className = heatmapOn ? 'on' : '';
  if (heatmapOn) loadHeatmap(); else redrawOverlay();
});

initChart();
loadCandles();
setInterval(loadCandles, 1000);
setInterval(loadHeatmap, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_PAGE


@app.on_event("startup")
async def startup_event():
    global current_setup, last_setup_outcome, cum_delta
    mt5_init()
    with data_lock:
        loaded = _db_load_bars()
        historical_bars.extend(loaded)
        if loaded:
            cum_delta = loaded[-1]["cvd"]  # so CVD continues, not a reset-to-0 cliff post-restart
        current_setup, last_setup_outcome = _db_load_state()
        setup_history.extend(_db_load_setup_history())
    logger.info(f"Restored {len(loaded)} bars, {len(setup_history)} closed setups from {DB_FILE} "
                f"(chart history survives restarts now).")
    logger.info(f"Order-Flow Zones Chart starting on {HOST}:{PORT} (read-only subscriber of {SOURCE_WS_URL})")
    asyncio.create_task(ws_client_loop())


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
