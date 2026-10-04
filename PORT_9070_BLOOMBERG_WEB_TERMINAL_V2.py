# -----------------------------------------------------------------------------
# GOLD.FLOW TERMINAL - BLOOMBERG/BOOKMAP EDITION - HEDGE-FUND PILOT UPGRADE
# NEW file, NEW port (9070). PORT_8070_BLOOMBERG_WEB_TERMINAL.py and its live
# port 8070 are completely untouched and keep running independently.
#
# Upgrades over V1 (PORT_8070_BLOOMBERG_WEB_TERMINAL.py) - this file had the
# most fabricated data of any port in the ecosystem:
#   1. DOM/Depth ladder: V1 generated every single bid/ask level with
#      random.uniform(25.0, 180.0) - completely fake liquidity numbers.
#      V2 renders the REAL top-10 Binance order book relayed by Port 9000.
#   2. Tick volume: V1 used random.randint(50,160) / random.randint(15,65)
#      for every trade. V2 uses real aggTrade volume from Port 9000.
#   3. "Confidence" score: V1 had confidence: random.randint(65, 92) - a
#      literally random number with no relation to anything. V2 computes
#      a real rule-based score from delta direction, VWAP position,
#      imbalance, FVG and absorption (same scoring approach used in the
#      Port 9060 pilot).
#   4. Time & Sales tape: side detection logic kept (bid/ask/last-price
#      comparison), but size is now the real trade size, not fabricated.
#   5. Finalized bars persisted to QuestDB (bars_v2_9070) for durable,
#      backtestable storage.
#   6. Binds to 127.0.0.1 only (V1 bound 0.0.0.0).
#   7. Resilient reconnect-with-backoff to Port 9000 with a visible feed
#      status indicator.
#
# Order-flow phase/absorption/CVD-divergence/FVG detection logic is kept
# as-is from V1 - genuine rule-based logic. Still PAPER trading only.
# -----------------------------------------------------------------------------

import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import requests
import json
import threading
import time
import logging
import os
import sqlite3
import sys
import signal
import atexit
from datetime import datetime, timedelta, timezone
from websockets.sync.client import connect as ws_connect
from questdb import Sender, TimestampNanos

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [TERMINAL-V2] - %(message)s')
logger = logging.getLogger("XAUUSD_TERMINAL_V2")

BINANCE_SYMBOL = os.getenv("GOLDFLOW_BINANCE_SYMBOL", "PAXGUSDT")
ENGINE_WS_URL = os.getenv("GOLDFLOW_ENGINE_WS_URL", "ws://127.0.0.1:9000/ws")
QUESTDB_ILP_CONF = os.getenv("GOLDFLOW_QUESTDB_ILP_CONF", "tcp::addr=127.0.0.1:9009;")
DASH_HOST = os.getenv("GOLDFLOW_DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.getenv("GOLDFLOW_DASH_PORT", "9070"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trades_terminal_v2_9070.db')

# Global State
data_lock = threading.Lock()
historical_bars = []
recent_tape = []
order_book = {'bids': [], 'asks': []}  # populated from REAL Port 9000 depth messages
current_bar = {
    'time': None, 'open': None, 'high': -float('inf'), 'low': float('inf'), 'close': None,
    'volume': 0.0, 'buy_vol': 0.0, 'sell_vol': 0.0, 'delta': 0.0, 'cvd': 0.0, 'vwap': 0.0,
    'poc': 0.0, 'phase': 'Neutral', 'confidence': 60, 'imbalance': False, 'fvg': None,
    'levels': {}
}
cum_vol = 0.0
cum_pv = 0.0
cum_delta = 0.0
is_running = True
last_known_price = 0.0
last_price_update = datetime.now()
market_meta = {'open': 0, 'high': 0, 'low': 0, 'bid': 0, 'ask': 0, 'ch': 0.0}
feed_status = {'connected': False, 'reconnects': 0}
real_spot_state = {'price': None, 'bid': None, 'ask': None, 'source': None}

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0, 'open_time': None}
trade_history = []

_questdb_sender = None


def _get_sender():
    global _questdb_sender
    if _questdb_sender is None:
        _questdb_sender = Sender.from_conf(QUESTDB_ILP_CONF)
        _questdb_sender.establish()
    return _questdb_sender


def ingest_bar_to_questdb(bar):
    global _questdb_sender
    try:
        sender = _get_sender()
        bar_time = bar['time']
        ts_ns = int(bar_time.timestamp() * 1_000_000_000) if isinstance(bar_time, datetime) else int(time.time() * 1_000_000_000)
        sender.row(
            "bars_v2_9070",
            symbols={"symbol": "XAUUSD", "phase": str(bar.get('phase', 'Neutral'))},
            columns={
                "open": float(bar['open']), "high": float(bar['high']), "low": float(bar['low']),
                "close": float(bar['close']), "volume": float(bar['volume']),
                "delta": float(bar.get('delta', 0)), "cvd": float(bar.get('cvd', 0)),
                "vwap": float(bar.get('vwap', 0)), "confidence": int(bar.get('confidence', 50)),
            },
            at=TimestampNanos(ts_ns),
        )
        sender.flush()
    except Exception:
        logger.exception("QuestDB bar ingest failed; will retry on next bar")
        _questdb_sender = None


# =============================================================================
# 1. DATABASE & PERSISTENCE (new DB - does not touch trades_terminal.db)
# =============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, time TEXT, exit_time TEXT, dir TEXT, entry REAL,
        exit_price REAL, sl REAL, tp REAL, status TEXT, pnl REAL,
        confidence INTEGER, phase TEXT, session TEXT, signal_type TEXT
    )''')
    conn.commit()
    conn.close()
    logger.info(f"Terminal V2 Database initialized: {DB_PATH}")


def save_trade(record):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''INSERT INTO trades (time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (record['time'], record['exit_time'], record['dir'], record['entry'], record['exit_price'],
                      record['sl'], record['tp'], record['status'], record['pnl'], record['confidence'],
                      record['phase'], record['session'], record['signal_type']))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Trade save error: {e}")


def load_trades():
    global trade_history
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type
                     FROM trades ORDER BY id DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
        trade_history = [{
            'id': r[0], 'time': r[1], 'exit_time': r[2] or '-', 'dir': r[3],
            'entry': r[4], 'exit_price': r[5] or 0.0, 'sl': r[6], 'tp': r[7],
            'status': r[8], 'pnl': r[9] or 0.0, 'confidence': r[10],
            'phase': r[11], 'session': r[12], 'signal_type': r[13]
        } for r in rows]
        logger.info(f"Loaded {len(trade_history)} trades into Terminal V2 blotter.")
    except Exception as e:
        logger.error(f"Failed loading trades: {e}")


def load_initial_bars():
    """REAL Binance klines only - no cross-reading old ports' databases, no synthetic fallback."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    logger.info("Fetching REAL historical bars from Binance...")
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': BINANCE_SYMBOL, 'interval': '1m', 'limit': 120}, timeout=10)
        klines = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"Binance fetch failed: {e}")
        klines = []

    if not klines:
        logger.error("No historical data available. Chart will populate as live ticks arrive.")
        return

    with data_lock:
        historical_bars.clear()
        cum_vol = 0.0
        cum_pv = 0.0
        cum_delta = 0.0
        for k in klines:
            bt = datetime.fromtimestamp(k[0] / 1000).replace(second=0, microsecond=0)
            op, hp, lp, cp = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5])
            taker_buy = float(k[9])
            delta = taker_buy - (vol - taker_buy)  # REAL delta
            cum_delta += delta
            cum_vol += vol
            cum_pv += ((op + hp + lp + cp) / 4) * vol
            vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else cp
            historical_bars.append({
                'time': bt, 'open': op, 'high': hp, 'low': lp, 'close': cp,
                'volume': vol, 'delta': delta, 'cvd': cum_delta, 'vwap': vwap,
                'poc': round((hp + lp) / 2, 2), 'imbalance': False, 'fvg': None,
                'phase': "Distribution" if delta > 0 else "Accumulation",
                'confidence': 50, 'absorption': None, 'cvd_divergence': None,
                'signal_type': 'NONE', 'session': '', 'levels': {round(cp, 1): vol}
            })
        last_known_price = historical_bars[-1]['close']
        market_meta['open'] = historical_bars[0]['open']
        market_meta['high'] = max(b['high'] for b in historical_bars)
        market_meta['low'] = min(b['low'] for b in historical_bars)
        market_meta['bid'] = last_known_price - 0.2
        market_meta['ask'] = last_known_price + 0.2
    logger.info(f"Loaded {len(historical_bars)} REAL bars. Last: ${last_known_price:.2f}")


# =============================================================================
# 2. REAL DOM LADDER & TIME/SALES (from Port 9000 depth+tick relay)
# =============================================================================
def update_tape(price, volume, is_buy):
    global recent_tape
    now_str = datetime.now().strftime('%H:%M:%S')
    is_block = volume > 3.0  # real PAXG order sizes are much smaller units than the old fake oz scale
    recent_tape.insert(0, {
        'time': now_str, 'price': f"{price:.2f}", 'size': f"{volume:.3f}",
        'side': 'BUY' if is_buy else 'SELL', 'is_block': is_block
    })
    if len(recent_tape) > 40:
        recent_tape.pop()


def update_order_book_from_depth(bids, asks):
    """bids/asks are [[price, qty], ...] straight from Binance via Port 9000 - real data."""
    global order_book
    order_book = {
        'bids': [{'price': float(p), 'volume': float(q)} for p, q in bids[:10]],
        'asks': [{'price': float(p), 'volume': float(q)} for p, q in asks[:10]],
    }


# =============================================================================
# 3. LIVE ORDER FLOW TICK PROCESSOR (unchanged math, now fed real ticks)
# =============================================================================
def update_mark_price(price):
    """PAXGUSDT can go minutes without a real trade print while the order
    book keeps moving. Without this, the chart/price freeze during those
    gaps. Extends the current bar's high/low/close from the real bid/ask
    mid-price WITHOUT touching volume/delta/CVD - those stay strictly tied
    to real trade ticks, only the displayed price keeps moving."""
    global last_known_price, last_price_update
    price = float(price)
    last_known_price = price
    last_price_update = datetime.now()
    with data_lock:
        if current_bar['time'] is not None:
            current_bar['high'] = max(current_bar['high'], price)
            current_bar['low'] = min(current_bar['low'], price)
            current_bar['close'] = price


def process_tick(price, volume, is_buy):
    global current_bar, historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, last_price_update
    try:
        price = float(price)
        volume = float(volume)
        last_known_price = price
        last_price_update = datetime.now()

        delta = volume if is_buy else -volume
        buy_vol = volume if is_buy else 0.0
        sell_vol = 0.0 if is_buy else volume

        cum_delta += delta
        cum_vol += volume
        cum_pv += price * volume
        vwap = cum_pv / cum_vol if cum_vol > 0 else price

        now = datetime.now()
        current_minute = now.replace(second=0, microsecond=0)

        with data_lock:
            update_tape(price, volume, is_buy)

            if current_bar['time'] is None or current_bar['time'] < current_minute:
                if current_bar['close'] is not None and current_bar['time'] is not None:
                    final_bar = finalize_bar(current_bar, vwap)
                    historical_bars.append(final_bar)
                    threading.Thread(target=ingest_bar_to_questdb, args=(final_bar,), daemon=True).start()
                    if len(historical_bars) > 200:
                        historical_bars.pop(0)
                    check_terminal_trade_signals(final_bar)

                current_bar['time'] = current_minute
                current_bar['open'] = price
                current_bar['high'] = price
                current_bar['low'] = price
                current_bar['close'] = price
                current_bar['volume'] = volume
                current_bar['buy_vol'] = buy_vol
                current_bar['sell_vol'] = sell_vol
                current_bar['delta'] = delta
                current_bar['cvd'] = cum_delta
                current_bar['vwap'] = round(vwap, 2)
                current_bar['poc'] = price
                current_bar['levels'] = {round(price, 1): volume}
            else:
                current_bar['high'] = max(current_bar['high'], price)
                current_bar['low'] = min(current_bar['low'], price)
                current_bar['close'] = price
                current_bar['volume'] += volume
                current_bar['buy_vol'] += buy_vol
                current_bar['sell_vol'] += sell_vol
                current_bar['delta'] = current_bar['buy_vol'] - current_bar['sell_vol']
                current_bar['cvd'] = cum_delta
                current_bar['vwap'] = round(vwap, 2)
                lvl = round(price, 1)
                current_bar['levels'][lvl] = current_bar['levels'].get(lvl, 0) + volume
                current_bar['poc'] = max(current_bar['levels'], key=current_bar['levels'].get)
    except Exception as e:
        logger.error(f"Tick processing error: {e}")


def finalize_bar(bar, vwap):
    delta = float(bar['buy_vol'] - bar['sell_vol'])
    poc = max(bar['levels'], key=bar['levels'].get) if bar['levels'] else bar['close']
    avg_vol = np.mean([b['volume'] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar['volume']
    imbalance = bar['volume'] > (avg_vol * 2.0) if avg_vol > 0 else False

    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']:
            fvg = "Bullish"
        elif prev2['low'] > bar['high']:
            fvg = "Bearish"

    phase = "Neutral"
    div = None
    if len(historical_bars) >= 5:
        cvd_vals = [b.get('cvd', 0) for b in historical_bars[-5:]]
        cvd_slope = cvd_vals[-1] - cvd_vals[0]
        price_slope = bar['close'] - historical_bars[-5]['close']
        if cvd_slope > 0 and price_slope < 0:
            div = "Bullish Divergence"; phase = "Accumulation"
        elif cvd_slope < 0 and price_slope > 0:
            div = "Bearish Divergence"; phase = "Distribution"
        elif cvd_slope > 0:
            phase = "Markup"
        else:
            phase = "Markdown"

    absorption = None
    if abs(delta) > 50 and abs(bar['close'] - bar['open']) < 0.25:
        absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption"

    hour = datetime.now().hour
    session = "London" if 13 <= hour < 17 else "New York" if 17 <= hour < 22 else "Asian"

    # REAL rule-based confidence (V1 used confidence: random.randint(65, 92) here - pure noise)
    score = 30
    if delta > 0 and bar['close'] > vwap: score += 20
    elif delta < 0 and bar['close'] < vwap: score += 20
    if imbalance: score += 15
    if fvg is not None: score += 10
    if absorption is not None: score += 15
    if div is not None: score += 10
    confidence = max(5, min(95, score))

    return {
        'time': bar['time'], 'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'],
        'volume': bar['volume'], 'delta': delta, 'cvd': bar['cvd'], 'vwap': round(vwap, 2),
        'poc': poc, 'imbalance': imbalance, 'fvg': fvg, 'phase': phase, 'confidence': confidence,
        'absorption': absorption, 'cvd_divergence': div, 'signal_type': 'NONE', 'session': session,
        'levels': bar['levels']
    }


# Strategy master switch: entry strategy is OFF unless GOLDFLOW_STRATEGIES_ENABLED=1.
STRATEGIES_ENABLED = os.getenv("GOLDFLOW_STRATEGIES_ENABLED", "0") == "1"
if not STRATEGIES_ENABLED:
    logger.info("STRATEGIES DISABLED (master switch off) - order-flow analytics only, no signals/trades.")


def check_terminal_trade_signals(bar):
    global trade_state, trade_history
    if trade_state['in_position']:
        p = bar['close']
        hit_tp = hit_sl = False
        pnl = 0.0
        if trade_state['direction'] == 'BUY':
            pnl = round((p - trade_state['entry_price']) * 10, 1)
            if p >= trade_state['take_profit']: hit_tp = True
            elif p <= trade_state['stop_loss']: hit_sl = True
        else:
            pnl = round((trade_state['entry_price'] - p) * 10, 1)
            if p <= trade_state['take_profit']: hit_tp = True
            elif p >= trade_state['stop_loss']: hit_sl = True

        if hit_tp or hit_sl:
            status = 'WIN' if hit_tp else 'LOSS'
            exit_time = datetime.now().strftime('%H:%M:%S')
            record = {
                'id': len(trade_history) + 1, 'time': trade_state['open_time'], 'exit_time': exit_time,
                'dir': trade_state['direction'], 'entry': trade_state['entry_price'],
                'exit_price': p, 'sl': trade_state['stop_loss'], 'tp': trade_state['take_profit'],
                'status': status, 'pnl': pnl, 'confidence': bar.get('confidence', 50),
                'phase': bar.get('phase', 'Institutional'), 'session': bar.get('session', 'NY'),
                'signal_type': 'TERMINAL_CONFLUENCE'
            }
            trade_history.insert(0, record)
            save_trade(record)
            trade_state['in_position'] = False

    if STRATEGIES_ENABLED and not trade_state['in_position']:
        cp = bar['close']
        if len(historical_bars) >= 10:
            rec_bars = historical_bars[-120:]
            tp_sum = sum(((b['high'] + b['low'] + b['close']) / 3.0) * b['volume'] for b in rec_bars)
            vol_sum = sum(b['volume'] for b in rec_bars)
            vwap = round(tp_sum / vol_sum, 2) if vol_sum > 0 else bar.get('vwap', cp)
        else:
            vwap = bar.get('vwap', cp)
        div = bar.get('cvd_divergence')
        absrp = bar.get('absorption')

        signal = None
        if (div == 'Bullish Divergence' or absrp == 'Bullish Absorption') and cp > vwap:
            signal = 'BUY'
        elif (div == 'Bearish Divergence' or absrp == 'Bearish Absorption') and cp < vwap:
            signal = 'SELL'

        if signal:
            sl_dist = 2.0
            tp_dist = 4.0
            trade_state['in_position'] = True
            trade_state['direction'] = signal
            trade_state['entry_price'] = cp
            trade_state['open_time'] = datetime.now().strftime('%H:%M:%S')
            trade_state['stop_loss'] = round(cp - sl_dist if signal == 'BUY' else cp + sl_dist, 2)
            trade_state['take_profit'] = round(cp + tp_dist if signal == 'BUY' else cp - tp_dist, 2)
            bar['signal_type'] = signal
            logger.info(f"TERMINAL SIGNAL: {signal} @ {cp:.2f} | SL: {trade_state['stop_loss']} | TP: {trade_state['take_profit']}")


# =============================================================================
# 4. LIVE FEED - subscribes to Port 9000 Core Engine V2 (real ticks + real depth)
# =============================================================================
def handle_engine_message(raw):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    mtype = msg.get("type")
    if mtype == "tick":
        price = float(msg["price"])
        volume = float(msg["volume"])
        is_buy = msg.get("side") == "BUY"
        market_meta['high'] = max(market_meta.get('high', price), price)
        market_meta['low'] = min(market_meta.get('low', price), price)
        process_tick(price, volume, is_buy)
    elif mtype == "depth":
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        with data_lock:
            update_order_book_from_depth(bids, asks)
        if bids:
            market_meta['bid'] = float(bids[0][0])
        if asks:
            market_meta['ask'] = float(asks[0][0])
    elif mtype == "mark_price":
        # Real trade prints are sparse on PAXGUSDT; this real bid/ask mid
        # keeps the chart moving during those quiet stretches.
        update_mark_price(msg["price"])
        market_meta['bid'] = float(msg.get("bid", msg["price"]))
        market_meta['ask'] = float(msg.get("ask", msg["price"]))
    elif mtype == "init":
        lp = msg.get("last_price")
        if lp:
            global last_known_price
            last_known_price = float(lp)
        rs = msg.get("real_spot")
        if rs:
            real_spot_state.update(rs)
    elif mtype == "real_spot":
        real_spot_state['price'] = float(msg["price"])
        real_spot_state['bid'] = float(msg.get("bid", msg["price"]))
        real_spot_state['ask'] = float(msg.get("ask", msg["price"]))
        real_spot_state['source'] = msg.get("source", "GoldAPI.io")


def live_feed_subscriber():
    global is_running
    backoff = 1
    logger.info(f"Terminal V2 Live Feed Worker: subscribing to Core Engine V2 at {ENGINE_WS_URL}")
    while is_running:
        try:
            with ws_connect(ENGINE_WS_URL, open_timeout=10) as ws:
                feed_status['connected'] = True
                backoff = 1
                logger.info("Connected to Port 9000 Core Engine V2 live feed.")
                for raw in ws:
                    if not is_running:
                        break
                    handle_engine_message(raw)
        except Exception as e:
            feed_status['connected'] = False
            feed_status['reconnects'] += 1
            logger.warning(
                f"Core Engine V2 feed disconnected ({e!r}); reconnecting in {backoff}s "
                f"(attempt #{feed_status['reconnects']}). Is PORT_9000_CORE_WEBSOCKET_ENGINE_V2.py running?"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


# =============================================================================
# 5. BLOOMBERG TERMINAL DASH UI & STYLING (unchanged layout, port/title updated)
# =============================================================================
app = dash.Dash(
    __name__,
    title="GOLD.FLOW // Institutional Bloomberg Terminal V2 (Pilot)",
    update_title=None,
    suppress_callback_exceptions=True
)

app.layout = html.Div(
    id="terminal-container",
    style={"backgroundColor": "#070A0F", "color": "#E6EDF3", "fontFamily": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
           "minHeight": "100vh", "padding": "8px 12px", "boxSizing": "border-box"},
    children=[
        dcc.Interval(id="terminal-interval", interval=1000, n_intervals=0),
        html.Div(id="terminal-topbar", style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                  "backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderBottom": "2px solid #00F0FF",
                  "padding": "6px 14px", "borderRadius": "4px", "marginBottom": "8px"},
            children=[
                html.Div([
                    html.Span("GOLD.FLOW ", style={"color": "#FFD700", "fontWeight": "900", "fontSize": "16px", "letterSpacing": "1px"}),
                    html.Span("// BLOOMBERG TERMINAL V2 PILOT ", style={"color": "#8B949E", "fontSize": "12px"}),
                    html.Span("[PORT 9070]", style={"color": "#00F0FF", "fontSize": "11px", "marginLeft": "6px", "backgroundColor": "#161B22", "padding": "2px 6px", "borderRadius": "3px"})
                ]),
                html.Div(id="clocks-panel", style={"fontSize": "11px", "color": "#8B949E", "letterSpacing": "0.5px"}),
                html.Div(id="feed-status-header", style={"fontSize": "11px"})
            ]
        ),
        html.Div(id="ticker-strip", style={"display": "grid", "gridTemplateColumns": "1.5fr 1fr 1fr 1fr 1fr 1fr", "gap": "8px", "marginBottom": "8px"},
            children=[
                html.Div(id="live-price-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="spread-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="high-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="low-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="session-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="phase-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
            ]
        ),
        html.Div(style={"display": "flex", "gap": "8px", "marginBottom": "8px"},
            children=[
                html.Div(style={"flex": "7", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[html.Div(style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "4px"},
                        children=[dcc.Graph(id="main-terminal-chart", config={"displayModeBar": False, "responsive": True}, style={"height": "560px"})])]
                ),
                html.Div(style={"flex": "3", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        html.Div(style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "8px", "height": "290px", "overflow": "hidden"},
                            children=[
                                html.Div(style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #21262D", "paddingBottom": "4px", "marginBottom": "4px"},
                                    children=[html.Span("REAL DOM / DEPTH LADDER", style={"color": "#00F0FF", "fontSize": "11px", "fontWeight": "bold"}),
                                              html.Span("L2 BOOK (Binance)", style={"color": "#8B949E", "fontSize": "10px"})]),
                                html.Div(id="dom-ladder-content")
                            ]
                        ),
                        html.Div(style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "8px", "height": "260px", "overflow": "hidden"},
                            children=[
                                html.Div(style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #21262D", "paddingBottom": "4px", "marginBottom": "4px"},
                                    children=[html.Span("THE TAPE (REAL TIME & SALES)", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"}),
                                              html.Span("LIVE", style={"color": "#00E676", "fontSize": "10px"})]),
                                html.Div(id="tape-content", style={"height": "230px", "overflowY": "auto"})
                            ]
                        )
                    ]
                )
            ]
        ),
        html.Div(style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "10px", "marginTop": "4px"},
            children=[
                html.Div(style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px", "borderBottom": "1px solid #21262D", "paddingBottom": "6px"},
                    children=[
                        html.Div([html.Span("TRADE BLOTTER // INSTITUTIONAL EXECUTION LOG", style={"color": "#00F0FF", "fontSize": "12px", "fontWeight": "bold"}),
                                  html.Span(" (Paper Simulation, Real Data)", style={"color": "#8B949E", "fontSize": "11px"})]),
                        html.Div(id="blotter-summary-stats", style={"fontSize": "11px"})
                    ]
                ),
                html.Div(id="trade-blotter-table", style={"overflowX": "auto"})
            ]
        )
    ]
)


# =============================================================================
# 6. DASH CALLBACKS: REAL-TIME RENDERING
# =============================================================================
@app.callback(
    [Output("clocks-panel", "children"), Output("feed-status-header", "children"),
     Output("live-price-box", "children"), Output("spread-box", "children"),
     Output("high-box", "children"), Output("low-box", "children"),
     Output("session-box", "children"), Output("phase-box", "children"),
     Output("main-terminal-chart", "figure"), Output("dom-ladder-content", "children"),
     Output("tape-content", "children"), Output("blotter-summary-stats", "children"),
     Output("trade-blotter-table", "children")],
    [Input("terminal-interval", "n_intervals")]
)
def update_terminal_ui(n):
    with data_lock:
        bars = list(historical_bars)
        price = last_known_price
        meta = dict(market_meta)
        tape = list(recent_tape)
        book = dict(order_book)
        history = list(trade_history)
        pos = dict(trade_state)

    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    clocks_text = f"UTC {now_utc.strftime('%H:%M:%S')}  |  NYC {(now_utc - timedelta(hours=4)).strftime('%H:%M:%S')}  |  LDN {(now_utc + timedelta(hours=1)).strftime('%H:%M:%S')}  |  IST {now.strftime('%H:%M:%S')}"

    feed_color = "#00E676" if feed_status['connected'] else "#FF3B30"
    feed_text = "FEED: LIVE (Port 9000)" if feed_status['connected'] else f"FEED: RECONNECTING ({feed_status['reconnects']})"
    feed_header_children = [html.Span(feed_text, style={"color": feed_color, "fontWeight": "bold", "fontSize": "11px"})]
    if real_spot_state['price'] is not None:
        feed_header_children.append(html.Span(
            f"  |  Real Spot ({real_spot_state['source']}): ${real_spot_state['price']:.2f}",
            style={"color": "#FFD700", "fontSize": "11px", "marginLeft": "8px"}
        ))
    feed_header = html.Span(feed_header_children)

    price_color = "#00E676" if price >= meta.get('open', price) else "#FF3B30"
    diff = price - meta.get('open', price)
    pct = (diff / meta.get('open', price) * 100) if meta.get('open', 0) > 0 else 0.0
    price_box = [
        html.Div("XAU/USD SPOT GOLD", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(f"${price:.2f}", style={"fontSize": "18px", "fontWeight": "900", "color": price_color, "marginTop": "2px"}),
        html.Div(f"{diff:+.2f} ({pct:+.2f}%)", style={"fontSize": "10px", "color": price_color})
    ]
    spread_box = [
        html.Div("SPREAD / LIQUIDITY", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(f"${meta.get('ask', price) - meta.get('bid', price):.2f}", style={"fontSize": "16px", "fontWeight": "bold", "color": "#79C0FF", "marginTop": "2px"}),
        html.Div(f"B: {meta.get('bid', price):.2f} | A: {meta.get('ask', price):.2f}", style={"fontSize": "10px", "color": "#8B949E"})
    ]
    high_box = [html.Div("SESSION HIGH", style={"fontSize": "10px", "color": "#8B949E"}),
                html.Div(f"${meta.get('high', price):.2f}", style={"fontSize": "16px", "fontWeight": "bold", "color": "#00E676", "marginTop": "2px"}),
                html.Div("Upper Liquidity Level", style={"fontSize": "10px", "color": "#8B949E"})]
    low_box = [html.Div("SESSION LOW", style={"fontSize": "10px", "color": "#8B949E"}),
               html.Div(f"${meta.get('low', price):.2f}", style={"fontSize": "16px", "fontWeight": "bold", "color": "#FF3B30", "marginTop": "2px"}),
               html.Div("Lower Liquidity Level", style={"fontSize": "10px", "color": "#8B949E"})]

    hour = now.hour
    curr_sess = "LONDON / NY OVERLAP" if 17 <= hour <= 20 else "NEW YORK SESSION" if 17 <= hour < 22 else "LONDON SESSION" if 13 <= hour < 17 else "ASIAN SESSION"
    session_box = [html.Div("MARKET KILL ZONE", style={"fontSize": "10px", "color": "#8B949E"}),
                   html.Div(curr_sess, style={"fontSize": "12px", "fontWeight": "bold", "color": "#FFD700", "marginTop": "4px"}),
                   html.Div("High Institutional Volatility", style={"fontSize": "10px", "color": "#00E676"})]

    last_phase = bars[-1].get('phase', 'Neutral') if bars else 'Neutral'
    phase_box = [html.Div("ORDER FLOW REGIME", style={"fontSize": "10px", "color": "#8B949E"}),
                 html.Div(last_phase.upper(), style={"fontSize": "13px", "fontWeight": "bold", "color": "#00F0FF", "marginTop": "4px"}),
                 html.Div(f"CVD: {cum_delta:+.0f}", style={"fontSize": "10px", "color": "#FFD700"})]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.75, 0.25])
    if bars:
        df = pd.DataFrame(bars)
        df['time_str'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        cum_pv_series = (typical_price * df['volume']).cumsum()
        cum_vol_series = df['volume'].cumsum()
        df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])

        fig.add_trace(go.Candlestick(x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                                      name="XAUUSD", increasing_line_color="#00E676", decreasing_line_color="#FF3B30",
                                      increasing_fillcolor="#00E676", decreasing_fillcolor="#FF3B30"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'], name="VWAP", line=dict(color="#FFD700", width=1.5)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'] + 1.8, name="VWAP +1.5σ", line=dict(color="#FF9F0A", width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'] - 1.8, name="VWAP -1.5σ", line=dict(color="#00F0FF", width=1, dash="dot")), row=1, col=1)

        if pos.get('in_position'):
            fig.add_hline(y=pos['entry_price'], line=dict(color="#00F0FF", width=1.5, dash="dash"), annotation_text=f"ENTRY: {pos['entry_price']:.2f}", row=1, col=1)
            fig.add_hline(y=pos['take_profit'], line=dict(color="#00E676", width=1.5, dash="dash"), annotation_text=f"TP: {pos['take_profit']:.2f}", row=1, col=1)
            fig.add_hline(y=pos['stop_loss'], line=dict(color="#FF3B30", width=1.5, dash="dash"), annotation_text=f"SL: {pos['stop_loss']:.2f}", row=1, col=1)

        fig.add_trace(go.Scatter(x=df['time_str'], y=df['cvd'], name="CVD", line=dict(color="#00F0FF", width=2), fill="tozeroy", fillcolor="rgba(0, 240, 255, 0.08)"), row=2, col=1)

    fig.update_layout(template="plotly_dark", paper_bgcolor="#0D1117", plot_bgcolor="#0A0D14", margin=dict(l=10, r=40, t=10, b=10), showlegend=False,
        xaxis=dict(type='category', showgrid=True, gridcolor="#161B22", rangeslider=dict(visible=False), nticks=15),
        yaxis=dict(showgrid=True, gridcolor="#161B22", side="right"),
        xaxis2=dict(type='category', showgrid=True, gridcolor="#161B22", nticks=15),
        yaxis2=dict(showgrid=True, gridcolor="#161B22", side="right"))

    # REAL DOM Ladder (from Binance via Port 9000 - not random.uniform)
    dom_rows = []
    for ask in reversed(book.get('asks', [])[:5]):
        dom_rows.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 6px", "fontSize": "11px", "backgroundColor": "rgba(255, 59, 48, 0.08)"},
            children=[html.Span(f"{ask['price']:.2f}", style={"color": "#FF3B30", "fontWeight": "bold"}),
                      html.Span(f"{ask['volume']:.3f}", style={"color": "#8B949E"}),
                      html.Div(style={"width": f"{min(int(ask['volume']*20), 60)}px", "height": "4px", "backgroundColor": "#FF3B30", "borderRadius": "2px", "alignSelf": "center"})]))
    dom_rows.append(html.Div(f"--- SPOT: ${price:.2f} ---", style={"textAlign": "center", "fontSize": "11px", "color": "#FFD700", "fontWeight": "bold", "padding": "3px 0", "backgroundColor": "#161B22", "borderTop": "1px solid #21262D", "borderBottom": "1px solid #21262D"}))
    for bid in book.get('bids', [])[:5]:
        dom_rows.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 6px", "fontSize": "11px", "backgroundColor": "rgba(0, 230, 118, 0.08)"},
            children=[html.Span(f"{bid['price']:.2f}", style={"color": "#00E676", "fontWeight": "bold"}),
                      html.Span(f"{bid['volume']:.3f}", style={"color": "#8B949E"}),
                      html.Div(style={"width": f"{min(int(bid['volume']*20), 60)}px", "height": "4px", "backgroundColor": "#00E676", "borderRadius": "2px", "alignSelf": "center"})]))
    if not book.get('bids') and not book.get('asks'):
        dom_rows = [html.Div("Waiting for real depth data from Port 9000...", style={"color": "#555", "fontStyle": "italic", "fontSize": "11px", "padding": "10px"})]

    tape_items = []
    for t in tape[:15]:
        side_color = "#00E676" if t['side'] == 'BUY' else "#FF3B30"
        badge = html.Span(" BLOCK", style={"color": "#FFD700", "fontWeight": "bold", "fontSize": "9px"}) if t.get('is_block') else ""
        tape_items.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "borderBottom": "1px solid #161B22"},
            children=[html.Span(t['time'], style={"color": "#8B949E"}), html.Span(t['side'], style={"color": side_color, "fontWeight": "bold"}),
                      html.Span(f"${t['price']}", style={"color": "#E6EDF3"}), html.Span([f"{t['size']}", badge], style={"color": side_color})]))

    total_trades = len(history)
    wins = len([t for t in history if t.get('status') == 'WIN'])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = sum([t.get('pnl', 0) for t in history])
    pnl_color = "#00E676" if net_pnl >= 0 else "#FF3B30"
    blotter_stats = [
        html.Span(f"CLOSED TRADES: {total_trades}  |  ", style={"color": "#8B949E"}),
        html.Span(f"WIN RATE: {win_rate:.1f}%  |  ", style={"color": "#00E676" if win_rate >= 50 else "#FF3B30", "fontWeight": "bold"}),
        html.Span(f"NET PNL: {net_pnl:+.1f} PIPS", style={"color": pnl_color, "fontWeight": "bold"})
    ]

    tbl_header = html.Tr([html.Th(h, style={"padding": "4px 8px", "color": "#8B949E"}) for h in
        ["#ID", "OPEN TIME", "DIR", "ENTRY", "SL", "TP", "EXIT TIME", "EXIT PRICE", "PNL (PIPS)", "STATUS", "REGIME"]],
        style={"borderBottom": "1px solid #21262D", "fontSize": "11px", "textAlign": "left"})

    tbl_rows = []
    if pos.get('in_position'):
        c_pnl = round((price - pos['entry_price']) * 10, 1) if pos['direction'] == 'BUY' else round((pos['entry_price'] - price) * 10, 1)
        c_col = "#00E676" if c_pnl >= 0 else "#FF3B30"
        tbl_rows.append(html.Tr([
            html.Td("LIVE", style={"padding": "4px 8px", "color": "#00F0FF", "fontWeight": "bold"}),
            html.Td(pos.get('open_time', '-'), style={"padding": "4px 8px"}),
            html.Td(pos['direction'], style={"padding": "4px 8px", "color": "#00E676" if pos['direction'] == 'BUY' else "#FF3B30", "fontWeight": "bold"}),
            html.Td(f"${pos['entry_price']:.2f}", style={"padding": "4px 8px"}),
            html.Td(f"${pos['stop_loss']:.2f}", style={"padding": "4px 8px", "color": "#FF3B30"}),
            html.Td(f"${pos['take_profit']:.2f}", style={"padding": "4px 8px", "color": "#00E676"}),
            html.Td("ACTIVE", style={"padding": "4px 8px", "color": "#00F0FF"}),
            html.Td(f"${price:.2f}", style={"padding": "4px 8px"}),
            html.Td(f"{c_pnl:+.1f}", style={"padding": "4px 8px", "color": c_col, "fontWeight": "bold"}),
            html.Td("OPEN", style={"padding": "4px 8px", "color": "#00F0FF", "fontWeight": "bold"}),
            html.Td("Confluence", style={"padding": "4px 8px"})
        ], style={"backgroundColor": "rgba(0, 240, 255, 0.08)", "fontSize": "11px", "borderBottom": "1px solid #21262D"}))

    for t in history[:10]:
        st_color = "#00E676" if t.get('status') == 'WIN' else "#FF3B30"
        p_color = "#00E676" if t.get('pnl', 0) >= 0 else "#FF3B30"
        tbl_rows.append(html.Tr([
            html.Td(f"#{t['id']}", style={"padding": "4px 8px", "color": "#8B949E"}),
            html.Td(t['time'], style={"padding": "4px 8px"}),
            html.Td(t['dir'], style={"padding": "4px 8px", "color": "#00E676" if t['dir'] == 'BUY' else "#FF3B30", "fontWeight": "bold"}),
            html.Td(f"${t['entry']:.2f}", style={"padding": "4px 8px"}),
            html.Td(f"${t['sl']:.2f}", style={"padding": "4px 8px", "color": "#FF3B30"}),
            html.Td(f"${t['tp']:.2f}", style={"padding": "4px 8px", "color": "#00E676"}),
            html.Td(t.get('exit_time', '-'), style={"padding": "4px 8px"}),
            html.Td(f"${t.get('exit_price', 0):.2f}", style={"padding": "4px 8px"}),
            html.Td(f"{t.get('pnl', 0):+.1f}", style={"padding": "4px 8px", "color": p_color, "fontWeight": "bold"}),
            html.Td(t.get('status', '-'), style={"padding": "4px 8px", "color": st_color, "fontWeight": "bold"}),
            html.Td(t.get('phase', '-'), style={"padding": "4px 8px", "color": "#8B949E"})
        ], style={"fontSize": "11px", "borderBottom": "1px solid #161B22"}))

    table = html.Table([html.Thead(tbl_header), html.Tbody(tbl_rows)], style={"width": "100%", "borderCollapse": "collapse"})

    return (clocks_text, feed_header, price_box, spread_box, high_box, low_box, session_box, phase_box,
            fig, dom_rows, tape_items, blotter_stats, table)


# =============================================================================
# 7. CLEAN SHUTDOWN HANDLER
# =============================================================================
def graceful_shutdown(signum=None, frame=None):
    global is_running
    logger.info("Terminal V2 shutting down gracefully...")
    is_running = False
    if _questdb_sender is not None:
        try:
            _questdb_sender.close()
        except Exception:
            pass
    sys.exit(0)


# =============================================================================
# 8. STARTUP ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    logger.info("Starting GOLD.FLOW Bloomberg Terminal V2 Pilot on Port 9070...")
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(lambda: logger.info("Terminal V2: atexit cleanup complete"))

    init_db()
    load_initial_bars()
    load_trades()

    feed_thread = threading.Thread(target=live_feed_subscriber, daemon=True)
    feed_thread.start()

    logger.info(f"Terminal V2 Web Server (localhost-only): http://{DASH_HOST}:{DASH_PORT}")
    app.run(host=DASH_HOST, port=DASH_PORT, debug=False)
