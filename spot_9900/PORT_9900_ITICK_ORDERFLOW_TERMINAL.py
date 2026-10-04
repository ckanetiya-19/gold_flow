"""
PORT 9900 - iTick REAL Spot Order-Flow Terminal (NEW, standalone)
============================================================================
NEW file, NEW port (9900). Does NOT touch ANY existing file or port
(8000-series legacy, 9000 engine, 9050-9100 dashboards). Completely separate.

WHAT THIS IS (per the user's final decision):
  A pure SPOT-gold order-flow terminal built ONLY from iTick's real trade
  tape (price + REAL per-trade size + timestamp), verified in Gate 1. No
  PAXG, no AllTick, no Binance - iTick only. This is the "right instrument,
  real size" foundation the whole discussion converged on.

CANDLES = REAL MARKET DATA ONLY (no fake wicks):
  Each candle's O/H/L/C is built STRICTLY from actual iTick TRADE prices
  (the `ld` of each trade tick). Nothing from bid/ask, nothing from a second
  price source - so there are NO artificial wicks like a broker/TradingView
  chart that mixes a quote feed with the trade feed (the exact "wick bug"
  this project fought before). Volume = sum of real trade sizes.

WHAT IT COMPUTES (tape-based - all REAL, from iTick trades):
  - Real CVD, Delta (size-weighted, side via quote-rule/Lee-Ready + BVC)
  - Footprint (buy/sell volume per price level), POC, Volume Profile
  - Absorption (aggressive volume vs price response)
  - Synthetic DOM ladder + traded-volume heatmap (reconstructed from the
    tape - a TRADED-volume view, NOT real resting-order liquidity, since
    spot gold has no real L2 book; clearly labelled as such)
  - Real-time Tape (Time & Sales)

WHAT IT DELIBERATELY DOES NOT FAKE (needs real L2/MBO, not available on
spot): OBI/WOBI, Microprice, OFI, Liquidity-Pull/spoofing, real
resting-liquidity heatmap. These require an order book with sizes +
add/cancel events (only CME futures MBO / crypto have that). Shown as
"N/A - needs L2/MBO" rather than invented.

NO TRADING / NO MT5 in this version - VIEW + validation first (staged plan).
Every inferred value is flagged ESTIMATED so it is never mistaken for
measured exchange data.

Run: python PORT_9900_ITICK_ORDERFLOW_TERMINAL.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import strategies_9900
import mt5_exec

# =============================================================================
# CONFIG
# =============================================================================
HOST = "127.0.0.1"
PORT = 9900
ITICK_WS_URL = os.getenv("GOLDFLOW_9900_ITICK_WS_URL", "wss://api-free.itick.org/forex")
ITICK_SYMBOL = os.getenv("GOLDFLOW_9900_ITICK_SYMBOL", "XAUUSD$GB")
# Its OWN iTick key (separate from Port 9000's), so both can run at once.
ITICK_KEY = os.getenv("GOLDFLOW_9900_ITICK_KEY", "") or os.getenv("GOLDFLOW_ITICK_API_KEY", "")

PRICE_BIN = 0.1                  # footprint / DOM price bucket
DOM_LEVELS = 6                   # levels each side of spot in the synthetic ladder
DOM_WINDOW_SEC = 5 * 60          # traded-volume window the DOM/heatmap reflect
HEATMAP_BIN = 0.5
HEATMAP_WINDOW_SEC = 30 * 60
TAPE_MAX = 200
BARS_RETENTION_DAYS = 14
MAX_BARS = BARS_RETENTION_DAYS * 1440
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orderflow_9900.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s INFO [orderflow-9900] %(message)s")
logger = logging.getLogger("orderflow_9900")
app = FastAPI(title="iTick Real Order-Flow Terminal (Port 9900)")

# =============================================================================
# STATE
# =============================================================================
data_lock = threading.Lock()
EMPTY_BAR = {
    "time": None, "open": None, "high": None, "low": None, "close": None,
    "volume": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0, "cvd": 0.0,
    "levels_buy": {}, "levels_sell": {},
}
current_bar = dict(EMPTY_BAR)
historical_bars = []
cum_delta = 0.0
best_bid = None
best_ask = None
last_trade_price = None
last_tick_side = None            # for zero-plus tick rule
tape = deque(maxlen=TAPE_MAX)
trade_hist = deque(maxlen=200000)  # (ts, price, size, is_buy) for DOM/heatmap windows
conn_state = {"connected": False, "last_trade_sec": None}
_last_dedup = None               # (t, ld, v) to skip exact re-broadcasts
STRATS = None                    # paper strategy layer (set at startup) - NO MT5


# =============================================================================
# PERSISTENCE
# =============================================================================
db_lock = threading.Lock()
_db = sqlite3.connect(DB_FILE, check_same_thread=False)
_db.execute("""CREATE TABLE IF NOT EXISTS bars (
    time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
    volume REAL, buy_vol REAL, sell_vol REAL, delta REAL, cvd REAL,
    levels_buy TEXT, levels_sell TEXT)""")
_db.commit()


def _db_save_bar(b):
    with db_lock:
        _db.execute("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(b["time"].timestamp()), b["open"], b["high"], b["low"], b["close"],
                      b["volume"], b["buy_vol"], b["sell_vol"], b["delta"], b["cvd"],
                      json.dumps({str(k): v for k, v in b["levels_buy"].items()}),
                      json.dumps({str(k): v for k, v in b["levels_sell"].items()})))
        _db.commit()


def _db_load_bars():
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=BARS_RETENTION_DAYS)).timestamp())
    with db_lock:
        _db.execute("DELETE FROM bars WHERE time < ?", (cutoff,))
        _db.commit()
        rows = _db.execute("SELECT * FROM bars ORDER BY time ASC").fetchall()
    out = []
    for r in rows:
        t, o, h, l, c, v, bv, sv, dl, cv, lb, ls = r
        out.append({"time": datetime.fromtimestamp(t, tz=timezone.utc), "open": o, "high": h,
                    "low": l, "close": c, "volume": v, "buy_vol": bv, "sell_vol": sv,
                    "delta": dl, "cvd": cv,
                    "levels_buy": {float(k): vv for k, vv in json.loads(lb or "{}").items()},
                    "levels_sell": {float(k): vv for k, vv in json.loads(ls or "{}").items()}})
    return out


# =============================================================================
# SIDE CLASSIFICATION (Lee-Ready: quote rule -> tick rule -> zero-plus tick)
# =============================================================================
def classify_side(price):
    """Returns True=BUY (aggressor lifted ask), False=SELL. ESTIMATED - spot
    has no real aggressor tag, so this is Lee-Ready inference:
      1) Quote rule: trade at/above ask-mid => buy, at/below bid-mid => sell
      2) Tick rule: up-tick => buy, down-tick => sell
      3) Zero-plus tick: unchanged => keep last side."""
    global last_tick_side
    if best_bid is not None and best_ask is not None and best_ask > best_bid:
        mid = (best_bid + best_ask) / 2.0
        if price >= mid:
            last_tick_side = True
            return True
        else:
            last_tick_side = False
            return False
    # tick rule
    if last_trade_price is not None:
        if price > last_trade_price:
            last_tick_side = True
            return True
        if price < last_trade_price:
            last_tick_side = False
            return False
    # zero-plus tick
    return last_tick_side if last_tick_side is not None else True


# =============================================================================
# TRADE + CANDLE (candles built from TRADE prices ONLY - no fake wicks)
# =============================================================================
def on_trade(price, size, ts_ms):
    global cum_delta, last_trade_price, current_bar
    now_utc = datetime.now(timezone.utc)
    minute = now_utc.replace(second=0, microsecond=0)
    is_buy = classify_side(price)
    delta = size if is_buy else -size
    lvl = round(price / PRICE_BIN) * PRICE_BIN

    with data_lock:
        cum_delta += delta
        # roll candle
        if current_bar["time"] is None or current_bar["time"] < minute:
            if current_bar["close"] is not None and current_bar["time"] is not None:
                fb = dict(current_bar)
                historical_bars.append(fb)
                if len(historical_bars) > MAX_BARS:
                    del historical_bars[0]
                _db_save_bar(fb)
                # PAPER strategies: run once per finalized real-spot bar.
                if STRATS is not None:
                    try:
                        vw = rolling_vwap(historical_bars)[-1]
                        STRATS.on_bar_finalized(historical_bars, vw, now_utc)
                    except Exception:
                        logger.exception("strategy on_bar hook failed")
            current_bar = {"time": minute, "open": price, "high": price, "low": price,
                           "close": price, "volume": 0.0, "buy_vol": 0.0, "sell_vol": 0.0,
                           "delta": 0.0, "cvd": cum_delta, "levels_buy": {}, "levels_sell": {}}
        # OHLC strictly from trade prices (real market, no fake wicks)
        current_bar["high"] = max(current_bar["high"], price)
        current_bar["low"] = min(current_bar["low"], price)
        current_bar["close"] = price
        current_bar["volume"] += size
        if is_buy:
            current_bar["buy_vol"] += size
            current_bar["levels_buy"][lvl] = current_bar["levels_buy"].get(lvl, 0.0) + size
        else:
            current_bar["sell_vol"] += size
            current_bar["levels_sell"][lvl] = current_bar["levels_sell"].get(lvl, 0.0) + size
        current_bar["delta"] = current_bar["buy_vol"] - current_bar["sell_vol"]
        current_bar["cvd"] = cum_delta
        tape.appendleft({"ts": ts_ms, "price": round(price, 2), "size": round(size, 2),
                         "side": "BUY" if is_buy else "SELL"})
        trade_hist.append((time.time(), price, size, is_buy))
    last_trade_price = price
    conn_state["last_trade_sec"] = time.time()
    # PAPER strategies: per-tick trailing/exit for the VWAP-Crossover position.
    if STRATS is not None:
        STRATS.on_price(price, now_utc)


# =============================================================================
# iTick WS CLIENT (direct, iTick-only)
# =============================================================================
async def handle_msg(raw):
    global best_bid, best_ask, _last_dedup
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
        key = (t, price, size)
        if key == _last_dedup:          # skip exact re-broadcast (heartbeat)
            return
        _last_dedup = key
        on_trade(price, size, t)
    elif typ == "depth":
        a = d.get("a") or []; b = d.get("b") or []
        try:
            if a:
                best_ask = float(a[0]["p"])
            if b:
                best_bid = float(b[0]["p"])
        except (KeyError, TypeError, ValueError, IndexError):
            pass


async def itick_loop():
    if not ITICK_KEY:
        logger.error("No iTick key set (GOLDFLOW_9900_ITICK_KEY). Terminal will have no data.")
        return
    backoff = 5
    sub_tick = json.dumps({"ac": "subscribe", "params": ITICK_SYMBOL, "types": "tick"})
    sub_depth = json.dumps({"ac": "subscribe", "params": ITICK_SYMBOL, "types": "depth"})
    while True:
        try:
            logger.info(f"Connecting to iTick ({ITICK_SYMBOL}) - direct, iTick-only...")
            async with websockets.connect(ITICK_WS_URL, additional_headers={"token": ITICK_KEY},
                                           ping_interval=20, ping_timeout=10) as ws:
                await ws.send(sub_tick)
                await ws.send(sub_depth)
                conn_state["connected"] = True
                logger.info("Connected to iTick real trade tape.")
                got_data = False
                async for raw in ws:
                    if not got_data:
                        got_data = True
                        backoff = 5      # reset ONLY once real data flows (stable connection)
                    await handle_msg(raw)
            # Server closed cleanly (code 1000): STILL back off - a no-sleep
            # reconnect loop here storms iTick and trips its 429 rate limit
            # (which knocked out every connection from this IP).
            conn_state["connected"] = False
            logger.info(f"iTick closed by server; reconnect in {backoff}s.")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except websockets.exceptions.InvalidStatus as e:
            conn_state["connected"] = False
            code = getattr(getattr(e, "response", None), "status_code", None)
            wait = 45 if code == 429 else backoff
            logger.warning(f"iTick handshake rejected ({code}); retry in {wait}s.")
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            conn_state["connected"] = False
            logger.warning(f"iTick feed error ({e!r}); retry in {backoff}s. (No ticks expected weekends.)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# =============================================================================
# ANALYTICS (all tape-based / real)
# =============================================================================
def rolling_vwap(bars, window=100):
    out = []
    win = deque()
    spv = sv = 0.0
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b.get("volume", 0) or 0
        win.append((tp, v)); spv += tp * v; sv += v
        if len(win) > window:
            otp, ov = win.popleft(); spv -= otp * ov; sv -= ov
        out.append(round(spv / sv, 2) if sv > 0 else round(b["close"], 2))
    return out


def build_dom():
    """Synthetic DOM: traded volume by price over the recent window, split
    buy/sell, shown as a ladder around spot. NOT real resting liquidity."""
    cutoff = time.time() - DOM_WINDOW_SEC
    buys, sells = {}, {}
    for ts, price, size, is_buy in reversed(trade_hist):
        if ts < cutoff:
            break
        p = round(price / PRICE_BIN) * PRICE_BIN
        (buys if is_buy else sells)[p] = (buys if is_buy else sells).get(p, 0.0) + size
    spot = last_trade_price or (historical_bars[-1]["close"] if historical_bars else 0)
    asks, bids = [], []
    step = PRICE_BIN
    for i in range(1, DOM_LEVELS + 1):
        pa = round((spot + i * step) / PRICE_BIN) * PRICE_BIN
        pb = round((spot - i * step) / PRICE_BIN) * PRICE_BIN
        asks.append({"price": round(pa, 2), "size": round(sells.get(pa, 0.0) + buys.get(pa, 0.0), 1)})
        bids.append({"price": round(pb, 2), "size": round(buys.get(pb, 0.0) + sells.get(pb, 0.0), 1)})
    return {"asks": list(reversed(asks)), "spot": round(spot, 2), "bids": bids,
            "note": "Synthetic (traded-volume) ladder from tape - NOT real resting-order L2."}


def build_heatmap():
    cutoff = time.time() - HEATMAP_WINDOW_SEC
    bins = {}
    for ts, price, size, is_buy in reversed(trade_hist):
        if ts < cutoff:
            break
        p = round(price / HEATMAP_BIN) * HEATMAP_BIN
        bins[p] = bins.get(p, 0.0) + size
    if not bins:
        return {"cells": []}
    mx = max(bins.values())
    return {"cells": [{"price": round(p, 2), "intensity": round(v / mx, 3)} for p, v in sorted(bins.items())],
            "note": "Traded-volume heatmap from tape (not resting liquidity)."}


def absorption_score():
    if len(historical_bars) < 2:
        return None
    b = historical_bars[-1]
    price_change = abs(b["close"] - b["open"])
    aggression = abs(b["delta"])
    if aggression < 1:
        return None
    ae = aggression / (price_change + 0.01)
    if ae > 50 and price_change < 0.3:
        return "Bullish" if b["delta"] < 0 else "Bearish"
    return None


# =============================================================================
# API
# =============================================================================
@app.get("/api/chart_data")
async def chart_data():
    with data_lock:
        bars = list(historical_bars)
        cur = dict(current_bar)
    window = bars
    vwaps = rolling_vwap(window)
    candles = []
    for b, vw in zip(window, vwaps):
        candles.append({"time": int(b["time"].timestamp()), "open": b["open"], "high": b["high"],
                        "low": b["low"], "close": b["close"], "volume": round(b.get("volume", 0), 2),
                        "vwap": vw, "delta": round(b.get("delta", 0), 1), "cvd": round(b.get("cvd", 0), 1)})
    last_price = cur["close"] if cur.get("close") is not None else (bars[-1]["close"] if bars else None)
    return JSONResponse({
        "candles": candles,
        "cvd": round(cum_delta, 1),
        "absorption": absorption_score(),
        "last_price": last_price,
        "best_bid": best_bid, "best_ask": best_ask,
        "connected": conn_state["connected"],
        "last_trade_sec_ago": round(time.time() - conn_state["last_trade_sec"], 1) if conn_state["last_trade_sec"] else None,
        "unavailable": ["OBI/WOBI", "Microprice", "OFI", "Liquidity-Pull/Spoofing", "Real resting-liquidity heatmap"],
    })


@app.get("/api/dom")
async def dom():
    with data_lock:
        return JSONResponse(build_dom())


@app.get("/api/heatmap")
async def heatmap():
    with data_lock:
        return JSONResponse(build_heatmap())


@app.get("/api/tape")
async def get_tape():
    with data_lock:
        return JSONResponse({"tape": list(tape)[:60]})


@app.get("/api/strategies")
async def strategies():
    if STRATS is None:
        return JSONResponse({"vwapx": None, "zones": None, "mode": "loading"})
    with data_lock:
        return JSONResponse(STRATS.snapshot())


HTML_PAGE = """
<!DOCTYPE html><html><head><title>iTick Order-Flow Terminal</title><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root{--bg:#06080f;--panel:#0c1220;--panel2:#111a2e;--accent:#00d4ff;--up:#26de81;--down:#ff4d6d;
       --text:#d8e0ec;--dim:#5b6b85;--grid:rgba(120,160,220,.08);--border:#1a2740;
       --font:'JetBrains Mono',monospace}
 html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);height:100%;overflow:hidden}
 #top{display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--panel);border-bottom:2px solid var(--accent)}
 #title{color:var(--accent);font-weight:700;font-size:14px;letter-spacing:1px}
 #status{font-size:11px;color:var(--up)}
 #status.down{color:var(--down)}
 #main{display:flex;height:calc(100vh - 44px)}
 #left{flex:1;min-width:0;position:relative;display:flex;flex-direction:column}
 #chart{width:100%;flex:1 1 60%;min-height:0}
 #cvdrow{flex:0 0 34%;border-top:1px solid var(--border);padding:6px 12px;box-sizing:border-box;font-size:11px;overflow-y:auto}
 #right{width:300px;flex-shrink:0;background:var(--panel);border-left:1px solid var(--border);overflow-y:auto;font-size:11px}
 .card{border-bottom:1px solid var(--border);padding:8px 10px}
 .ctitle{color:var(--accent);font-weight:700;font-size:10px;margin-bottom:6px;letter-spacing:.5px}
 .dom-ask{color:var(--down)} .dom-bid{color:var(--up)}
 .domrow{display:flex;justify-content:space-between;padding:1.5px 0;align-items:center}
 .bar{height:9px;border-radius:2px;display:inline-block;vertical-align:middle;margin-right:5px}
 .spot{text-align:center;background:var(--panel2);color:var(--accent);font-weight:700;padding:3px;margin:2px 0;border-radius:3px}
 .tp-buy{color:var(--up)} .tp-sell{color:var(--down)}
 .tprow{display:flex;justify-content:space-between;font-size:10.5px;padding:1.5px 0}
 .na{color:var(--dim);font-size:10px}
 #ohlc{position:absolute;top:6px;left:12px;font-size:12px;background:rgba(0,0,0,.4);padding:3px 8px;border-radius:4px;z-index:5}
 .stwrap{margin-bottom:8px}
 .sthead{display:flex;justify-content:space-between;font-size:10px;color:var(--text);font-weight:700;margin:4px 0 2px}
 .sthead .net-pos{color:var(--up)} .sthead .net-neg{color:var(--down)}
 .stopen{font-size:10px;padding:2px 5px;border-radius:3px;background:var(--panel2);margin:2px 0}
 .strow{display:flex;justify-content:space-between;font-size:10px;padding:1px 0;color:var(--dim)}
 .b-buy{color:var(--up)} .b-sell{color:var(--down)}
 .paper-tag{font-size:9px;color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:0 4px;margin-left:6px}
</style></head><body>
<div id="top"><div id="title">XAUUSD // iTICK REAL ORDER-FLOW [PORT 9900]</div>
 <div style="font-size:10px;color:var(--dim)">iTick tape only · real trades · no PAXG · candles=real (no fake wicks)</div>
 <div id="status">● LIVE</div></div>
<div id="main">
 <div id="left"><div id="ohlc"></div><div id="chart"></div>
   <div id="cvdrow"><div class="ctitle">CVD / DELTA (real, size-weighted · side=ESTIMATED via Lee-Ready)</div><div id="cvdbody">-</div></div>
 </div>
 <div id="right">
   <div class="card"><div class="ctitle">STRATEGIES <span class="paper-tag">MT5 DEMO</span></div><div id="stbody">-</div></div>
   <div class="card"><div class="ctitle">DOM / DEPTH LADDER (synthetic)</div><div id="dombody">-</div></div>
   <div class="card"><div class="ctitle">TAPE (TIME & SALES)</div><div id="tapebody">-</div></div>
   <div class="card"><div class="ctitle">NOT AVAILABLE ON SPOT (needs L2/MBO)</div><div id="nabody" class="na">-</div></div>
 </div>
</div>
<script>
let chart,cs,vw;
function init(){
 chart=LightweightCharts.createChart(document.getElementById('chart'),{autoSize:true,
  layout:{background:{color:'#06080f'},textColor:'#5b6b85',fontFamily:"'JetBrains Mono',monospace"},
  grid:{vertLines:{color:'rgba(120,160,220,.08)'},horzLines:{color:'rgba(120,160,220,.08)'}},
  timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#1a2740'},
  rightPriceScale:{borderColor:'#1a2740'}});
 cs=chart.addCandlestickSeries({upColor:'#26de81',downColor:'#ff4d6d',borderVisible:false,wickUpColor:'#26de81',wickDownColor:'#ff4d6d'});
 vw=chart.addLineSeries({color:'#00d4ff',lineWidth:2});
}
async function loadChart(){
 try{
  const d=await (await fetch('/api/chart_data')).json();
  document.getElementById('status').className=d.connected?'':'down';
  document.getElementById('status').innerText=d.connected?('LIVE · last trade '+(d.last_trade_sec_ago??'?')+'s'):'RECONNECTING';
  if(d.candles.length){
   cs.setData(d.candles.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
   vw.setData(d.candles.map(c=>({time:c.time,value:c.vwap})));
   const l=d.candles[d.candles.length-1];
   document.getElementById('ohlc').innerText=`O:${l.open.toFixed(2)} H:${l.high.toFixed(2)} L:${l.low.toFixed(2)} C:${l.close.toFixed(2)} VWAP:${l.vwap.toFixed(2)}`;
   const dc=l.delta>=0?'#26de81':'#ff4d6d';
   document.getElementById('cvdbody').innerHTML=
    `<div>CVD: <b style="color:${d.cvd>=0?'#26de81':'#ff4d6d'}">${d.cvd}</b></div>`+
    `<div>Bar Delta: <b style="color:${dc}">${l.delta}</b></div>`+
    `<div>Bid/Ask: ${d.best_bid?d.best_bid.toFixed(2):'-'} / ${d.best_ask?d.best_ask.toFixed(2):'-'}</div>`+
    (d.absorption?`<div style="color:#00d4ff">Absorption: ${d.absorption}</div>`:'');
   document.getElementById('nabody').innerText=(d.unavailable||[]).join(' · ');
  }
 }catch(e){document.getElementById('status').className='down';}
}
async function loadDom(){
 try{const d=await (await fetch('/api/dom')).json();
  let h='';
  const all=[...(d.asks||[]),...(d.bids||[])].map(x=>x.size);
  const mx=all.length?Math.max(...all,1):1;
  (d.asks||[]).forEach(a=>h+=`<div class="domrow dom-ask"><span>${a.price.toFixed(2)}</span><span><span class="bar" style="width:${a.size/mx*70}px;background:var(--down);opacity:.5"></span>${a.size}</span></div>`);
  h+=`<div class="spot">SPOT ${d.spot.toFixed(2)}</div>`;
  (d.bids||[]).forEach(b=>h+=`<div class="domrow dom-bid"><span>${b.price.toFixed(2)}</span><span><span class="bar" style="width:${b.size/mx*70}px;background:var(--up);opacity:.5"></span>${b.size}</span></div>`);
  h+=`<div class="na" style="margin-top:4px">${d.note||''}</div>`;
  document.getElementById('dombody').innerHTML=h;
 }catch(e){}
}
async function loadTape(){
 try{const d=await (await fetch('/api/tape')).json();
  let h='';(d.tape||[]).slice(0,30).forEach(t=>{
   const cl=t.side==='BUY'?'tp-buy':'tp-sell';
   const tm=new Date(t.ts).toLocaleTimeString();
   h+=`<div class="tprow"><span style="color:var(--dim)">${tm}</span><span class="${cl}">${t.side}</span><span>${t.price.toFixed(2)}</span><span>${t.size}</span></div>`;});
  document.getElementById('tapebody').innerHTML=h;
 }catch(e){}
}
function renderStrat(s){
 if(!s) return '<div class="na">-</div>';
 const st=s.stats||{n:0,wins:0,losses:0,net:0};
 const nc=st.net>=0?'net-pos':'net-neg';
 let h=`<div class="stwrap"><div class="sthead"><span>${s.name}</span>`+
   `<span class="${nc}">${st.n} tr · ${st.wins}W/${st.losses}L · $${st.net>=0?'+':''}${st.net.toFixed(2)}</span></div>`;
 (s.open||[]).forEach(p=>{
   const bc=p.side==='BUY'?'b-buy':'b-sell';
   h+=`<div class="stopen"><span class="${bc}">● ${p.side} OPEN</span> @${p.entry.toFixed(2)} · SL ${p.sl.toFixed(2)}`+
      (p.tp?` · TP ${p.tp.toFixed(2)}`:'')+(p.armed?' · <span style="color:var(--up)">TRAIL</span>':'')+`</div>`;
 });
 if(!(s.open||[]).length) h+=`<div class="strow"><span>no open position</span></div>`;
 (s.today||[]).slice(-6).reverse().forEach(t=>{
   const bc=t.side==='BUY'?'b-buy':'b-sell';
   const pc=t.pnl>=0?'color:var(--up)':'color:var(--down)';
   h+=`<div class="strow"><span class="${bc}">${t.side}</span><span>${t.entry?t.entry.toFixed(2):'-'}→${t.exit?t.exit.toFixed(2):'-'}</span>`+
      `<span>${t.outcome||''}</span><span style="${pc}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</span></div>`;
 });
 return h+'</div>';
}
async function loadStrategies(){
 try{const d=await (await fetch('/api/strategies')).json();
  document.getElementById('stbody').innerHTML=renderStrat(d.vwapx)+renderStrat(d.zones);
 }catch(e){}
}
init();loadChart();loadDom();loadTape();loadStrategies();
setInterval(loadChart,1000);setInterval(loadDom,1000);setInterval(loadTape,800);setInterval(loadStrategies,1500);
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_PAGE


@app.on_event("startup")
async def startup():
    global cum_delta, STRATS
    with data_lock:
        loaded = _db_load_bars()
        historical_bars.extend(loaded)
        if loaded:
            cum_delta = loaded[-1]["cvd"]
    # MT5 demo execution (demo-guarded inside mt5_exec: real accounts are blocked).
    _mt5 = mt5_exec.MT5Exec(logger, name="9900")
    _mt5.init()
    STRATS = strategies_9900.Strategies9900(logger, os.path.dirname(os.path.abspath(__file__)), mt5exec=_mt5)
    logger.info(f"Restored {len(loaded)} bars from {DB_FILE}. iTick key: {'set' if ITICK_KEY else 'MISSING'}.")
    logger.info(f"iTick Order-Flow Terminal on {HOST}:{PORT} - iTick-only, no PAXG. "
                f"Strategies ON (VWAP-Crossover magic 90901 + Zones magic 90902); MT5 {'DEMO' if _mt5.ready else 'off/paper'}.")
    asyncio.create_task(itick_loop())


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
