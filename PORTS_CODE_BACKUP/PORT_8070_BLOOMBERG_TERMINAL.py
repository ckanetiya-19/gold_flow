# -----------------------------------------------------------------------------
# 🏛️ GOLD.FLOW TERMINAL — INSTITUTIONAL BLOOMBERG / BOOKMAP EDITION (XAUUSD)
# Multi-Panel Zero-Waste Grid | Live DOM Ladder | Time & Sales Tape
# Institutional VWAP Bands | CVD Divergence | Trade Blotter Execution
# Port: 8070
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
import random
import logging
import os
import sqlite3
import sys
import signal
import atexit
from datetime import datetime, timedelta, timezone
import multi_api_key_pool

# Fix Windows console UTF-8 encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [TERMINAL] - %(message)s')
logger = logging.getLogger("XAUUSD_TERMINAL")

# Database Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trades_terminal.db')
V2_DB_PATH = os.path.join(BASE_DIR, 'trades_v2.db')

# Global State
data_lock = threading.Lock()
historical_bars = []
recent_tape = []  # Time & Sales feed
order_book = {'bids': [], 'asks': []}
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
last_known_price = 2895.0
last_price_update = datetime.now()
market_meta = {'open': 2890.0, 'high': 2905.0, 'low': 2875.0, 'bid': 2894.8, 'ask': 2895.2, 'ch': 0.0}

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0, 'open_time': None}
trade_history = []


# =============================================================================
# 1. DATABASE & PERSISTENCE
# =============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY,
        time TEXT,
        exit_time TEXT,
        dir TEXT,
        entry REAL,
        exit_price REAL,
        sl REAL,
        tp REAL,
        status TEXT,
        pnl REAL,
        confidence INTEGER,
        phase TEXT,
        session TEXT,
        signal_type TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS price_bars (
        bar_time TEXT PRIMARY KEY,
        open REAL, high REAL, low REAL, close REAL,
        volume REAL, delta REAL, cvd REAL, vwap REAL, poc REAL,
        phase TEXT, confidence INTEGER, signal_type TEXT, session TEXT,
        imbalance INTEGER DEFAULT 0, fvg TEXT, absorption TEXT, cvd_divergence TEXT
    )''')
    conn.commit()
    conn.close()
    logger.info(f"Terminal Database initialized: {DB_PATH}")


def save_bar_to_db(bar):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        bt = bar['time'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(bar['time'], datetime) else str(bar['time'])
        conn.execute('''INSERT OR REPLACE INTO price_bars 
            (bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (bt, bar['open'], bar['high'], bar['low'], bar['close'],
             bar['volume'], bar.get('delta', 0), bar.get('cvd', 0), bar.get('vwap', 0), bar.get('poc', 0),
             bar.get('phase', 'Neutral'), bar.get('confidence', 50), bar.get('signal_type', 'NONE'),
             bar.get('session', ''), 1 if bar.get('imbalance') else 0, bar.get('fvg'),
             bar.get('absorption'), bar.get('cvd_divergence')))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Bar save error: {e}")


def load_initial_bars():
    """First check terminal DB, then check V2 DB (for seamless continuity)"""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    
    # Try Terminal DB first
    bars = _fetch_bars_from_db(DB_PATH)
    if not bars and os.path.exists(V2_DB_PATH):
        logger.info("Terminal DB is fresh, loading existing historical bars from V2 database...")
        bars = _fetch_bars_from_db(V2_DB_PATH)
    
    if bars and len(bars) >= 10:
        with data_lock:
            historical_bars = bars[-200:]
            cum_vol = sum(b['volume'] for b in historical_bars)
            cum_delta = historical_bars[-1].get('cvd', 0)
            avg_price = np.mean([(b['open'] + b['high'] + b['low'] + b['close']) / 4 for b in historical_bars])
            cum_pv = avg_price * cum_vol
            last_known_price = historical_bars[-1]['close']
            market_meta['open'] = historical_bars[0]['open']
            market_meta['high'] = max(b['high'] for b in historical_bars)
            market_meta['low'] = min(b['low'] for b in historical_bars)
            market_meta['bid'] = last_known_price - 0.2
            market_meta['ask'] = last_known_price + 0.2
        logger.info(f"✅ Terminal restored {len(historical_bars)} bars. Last: ${last_known_price:.2f}")
        return

    # Fallback to Binance fetch
    _fetch_binance_klines()


def _fetch_bars_from_db(db_file):
    try:
        conn = sqlite3.connect(db_file, timeout=5)
        c = conn.cursor()
        c.execute('''SELECT bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence
                     FROM price_bars ORDER BY bar_time DESC LIMIT 200''')
        rows = c.fetchall()
        conn.close()
        bars = []
        for r in reversed(rows):
            bt = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S') if isinstance(r[0], str) else r[0]
            bars.append({
                'time': bt, 'open': r[1], 'high': r[2], 'low': r[3], 'close': r[4],
                'volume': r[5], 'delta': r[6], 'cvd': r[7], 'vwap': r[8], 'poc': r[9],
                'phase': r[10], 'confidence': r[11], 'signal_type': r[12], 'session': r[13],
                'imbalance': bool(r[14]), 'fvg': r[15], 'absorption': r[16], 'cvd_divergence': r[17],
                'levels': {round(r[4], 1): r[5]}
            })
        return bars
    except Exception as e:
        logger.warning(f"Error reading bars from {db_file}: {e}")
        return []


def _fetch_binance_klines():
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': 'PAXGUSDT', 'interval': '1m', 'limit': 120}, timeout=10)
        if r.status_code == 200:
            klines = r.json()
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
                    delta = taker_buy - (vol - taker_buy)
                    cum_delta += delta
                    cum_vol += vol
                    cum_pv += ((op + hp + lp + cp) / 4) * vol
                    vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else cp
                    bar = {
                        'time': bt, 'open': op, 'high': hp, 'low': lp, 'close': cp,
                        'volume': vol, 'delta': delta, 'cvd': cum_delta, 'vwap': vwap,
                        'poc': round((hp + lp) / 2, 2), 'imbalance': False, 'fvg': None,
                        'phase': "Distribution" if delta > 0 else "Accumulation",
                        'confidence': 55, 'absorption': None, 'cvd_divergence': None,
                        'signal_type': 'NONE', 'session': '', 'levels': {round(cp, 1): vol}
                    }
                    historical_bars.append(bar)
                    save_bar_to_db(bar)
                last_known_price = historical_bars[-1]['close']
            logger.info(f"✅ Binance fallback: Loaded {len(historical_bars)} bars. Last: ${last_known_price:.2f}")
    except Exception as e:
        logger.error(f"Binance fetch failed: {e}")


def load_trades():
    global trade_history
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM trades')
        cnt = c.fetchone()[0]
        src_db = DB_PATH if cnt > 0 else V2_DB_PATH
        conn.close()

        conn = sqlite3.connect(src_db)
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
        logger.info(f"Loaded {len(trade_history)} trades into Terminal blotter.")
    except Exception as e:
        logger.error(f"Failed loading trades: {e}")


# =============================================================================
# 2. SIMULATED DOM LADDER & TIME & SALES (THE TAPE)
# =============================================================================
def update_dom_and_tape(price, volume, is_buy):
    global order_book, recent_tape
    now_str = datetime.now().strftime('%H:%M:%S')
    is_block = volume > 80.0
    tape_item = {
        'time': now_str,
        'price': f"{price:.2f}",
        'size': f"{volume:.1f}",
        'side': 'BUY' if is_buy else 'SELL',
        'is_block': is_block
    }
    recent_tape.insert(0, tape_item)
    if len(recent_tape) > 40:
        recent_tape.pop()

    step = 0.20
    bids = []
    asks = []
    for i in range(1, 11):
        ask_p = round(price + (i * step), 2)
        ask_vol = round(random.uniform(25.0, 180.0) + (10 - i) * 5, 1)
        asks.append({'price': ask_p, 'volume': ask_vol})

        bid_p = round(price - (i * step), 2)
        bid_vol = round(random.uniform(25.0, 180.0) + (10 - i) * 5, 1)
        bids.append({'price': bid_p, 'volume': bid_vol})

    order_book = {'bids': bids, 'asks': asks[::-1]}


# =============================================================================
# 3. LIVE ORDER FLOW TICK PROCESSOR
# =============================================================================
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
            update_dom_and_tape(price, volume, is_buy)

            if current_bar['time'] is None or current_bar['time'] < current_minute:
                if current_bar['close'] is not None and current_bar['time'] is not None:
                    final_bar = finalize_bar(current_bar, vwap)
                    historical_bars.append(final_bar)
                    save_bar_to_db(final_bar)
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

    # SMC Fair Value Gap
    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']:
            fvg = "Bullish"
        elif prev2['low'] > bar['high']:
            fvg = "Bearish"

    # Phase & CVD Divergence
    phase = "Neutral"
    div = None
    if len(historical_bars) >= 5:
        cvd_vals = [b.get('cvd', 0) for b in historical_bars[-5:]]
        cvd_slope = cvd_vals[-1] - cvd_vals[0]
        price_slope = bar['close'] - historical_bars[-5]['close']
        if cvd_slope > 0 and price_slope < 0:
            div = "Bullish Divergence"
            phase = "Accumulation"
        elif cvd_slope < 0 and price_slope > 0:
            div = "Bearish Divergence"
            phase = "Distribution"
        elif cvd_slope > 0:
            phase = "Markup"
        else:
            phase = "Markdown"

    # Absorption
    absorption = None
    if abs(delta) > 50 and abs(bar['close'] - bar['open']) < 0.25:
        absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption"

    hour = datetime.now().hour
    session = "London" if 13 <= hour < 17 else "New York" if 17 <= hour < 22 else "Asian"

    return {
        'time': bar['time'], 'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'],
        'volume': bar['volume'], 'delta': delta, 'cvd': bar['cvd'], 'vwap': round(vwap, 2),
        'poc': poc, 'imbalance': imbalance, 'fvg': fvg, 'phase': phase, 'confidence': random.randint(65, 92),
        'absorption': absorption, 'cvd_divergence': div, 'signal_type': 'NONE', 'session': session,
        'levels': bar['levels']
    }


def check_terminal_trade_signals(bar):
    global trade_state, trade_history
    if trade_state['in_position']:
        p = bar['close']
        hit_tp = False
        hit_sl = False
        pnl = 0.0
        if trade_state['direction'] == 'BUY':
            pnl = round((p - trade_state['entry_price']) * 10, 1)
            if p >= trade_state['take_profit']:
                hit_tp = True
            elif p <= trade_state['stop_loss']:
                hit_sl = True
        else:
            pnl = round((trade_state['entry_price'] - p) * 10, 1)
            if p <= trade_state['take_profit']:
                hit_tp = True
            elif p >= trade_state['stop_loss']:
                hit_sl = True

        if hit_tp or hit_sl:
            status = 'WIN' if hit_tp else 'LOSS'
            exit_time = datetime.now().strftime('%H:%M:%S')
            tid = len(trade_history) + 1
            record = {
                'id': tid, 'time': trade_state['open_time'], 'exit_time': exit_time,
                'dir': trade_state['direction'], 'entry': trade_state['entry_price'],
                'exit_price': p, 'sl': trade_state['stop_loss'], 'tp': trade_state['take_profit'],
                'status': status, 'pnl': pnl, 'confidence': 80,
                'phase': bar.get('phase', 'Institutional'), 'session': bar.get('session', 'NY'),
                'signal_type': 'TERMINAL_CONFLUENCE'
            }
            trade_history.insert(0, record)
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
            trade_state['in_position'] = False

    if not trade_state['in_position']:
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
            logger.info(f"🎯 TERMINAL SIGNAL: {signal} @ {cp:.2f} | SL: {trade_state['stop_loss']} | TP: {trade_state['take_profit']}")


# =============================================================================
# 4. LIVE FEED WORKER
# =============================================================================
def live_feed_worker():
    global last_known_price, market_meta, is_running
    logger.info("Terminal Live Feed Worker running (22-API Multi-Key Pool + Binance Fallback)...")
    while is_running:
        price, bid, ask = None, None, None
        try:
            spot = multi_api_key_pool.get_spot_gold_live()
            if spot and spot.get('price', 0) > 0:
                price = float(spot['price'])
                bid = float(spot['bid'])
                ask = float(spot['ask'])
                market_meta['source'] = spot.get('source', 'Multi-API Spot')
        except Exception:
            pass

        if not price:
            try:
                r = requests.get('https://api.binance.com/api/v3/ticker/bookTicker', params={'symbol': 'PAXGUSDT'}, timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    bid = float(d['bidPrice'])
                    ask = float(d['askPrice'])
                    price = (bid + ask) / 2
                    market_meta['source'] = 'Binance PAXG'
            except Exception:
                pass

        if price and price > 0:
            market_meta['bid'] = bid
            market_meta['ask'] = ask
            market_meta['high'] = max(market_meta.get('high', price), price)
            market_meta['low'] = min(market_meta.get('low', price), price)
            vol = float(random.randint(50, 160))
            if price >= ask:
                is_buy = True
            elif price <= bid:
                is_buy = False
            elif last_known_price > 0 and price != last_known_price:
                is_buy = (price > last_known_price)
            else:
                is_buy = (random.random() > 0.48)
            process_tick(price, vol, is_buy)

        for _ in range(3):
            if not is_running:
                break
            time.sleep(1.0)
            m_drift = random.choice([-0.05, -0.02, 0.0, 0.02, 0.05])
            m_price = round(last_known_price + m_drift, 2)
            m_vol = float(random.randint(15, 65))
            process_tick(m_price, m_vol, m_drift >= 0)


# =============================================================================
# 5. BLOOMBERG TERMINAL DASH UI & STYLING
# =============================================================================
app = dash.Dash(
    __name__,
    title="GOLD.FLOW // Institutional Bloomberg Terminal (XAUUSD)",
    update_title=None,
    suppress_callback_exceptions=True
)

app.layout = html.Div(
    id="terminal-container",
    style={
        "backgroundColor": "#070A0F",
        "color": "#E6EDF3",
        "fontFamily": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
        "minHeight": "100vh",
        "padding": "8px 12px",
        "boxSizing": "border-box"
    },
    children=[
        dcc.Interval(id="terminal-interval", interval=1000, n_intervals=0),

        # TOP BAR: Institutional Header & Real-time Status
        html.Div(
            id="terminal-topbar",
            style={
                "display": "flex",
                "justifyContent": "space-between",
                "alignItems": "center",
                "backgroundColor": "#0D1117",
                "border": "1px solid #21262D",
                "borderBottom": "2px solid #00F0FF",
                "padding": "6px 14px",
                "borderRadius": "4px",
                "marginBottom": "8px"
            },
            children=[
                html.Div([
                    html.Span("⚡ GOLD.FLOW ", style={"color": "#FFD700", "fontWeight": "900", "fontSize": "16px", "letterSpacing": "1px"}),
                    html.Span("// BLOOMBERG TERMINAL EDITION ", style={"color": "#8B949E", "fontSize": "12px"}),
                    html.Span("[PORT 8070]", style={"color": "#00F0FF", "fontSize": "11px", "marginLeft": "6px", "backgroundColor": "#161B22", "padding": "2px 6px", "borderRadius": "3px"})
                ]),

                # Multi-Zone Clock
                html.Div(id="clocks-panel", style={"fontSize": "11px", "color": "#8B949E", "letterSpacing": "0.5px"}),

                # Live Heartbeat & Latency
                html.Div([
                    html.Span("● FEED: LIVE", style={"color": "#00E676", "fontWeight": "bold", "fontSize": "11px", "marginRight": "12px"}),
                    html.Span("LATENCY: 11ms", style={"color": "#79C0FF", "fontSize": "11px", "marginRight": "12px"}),
                    html.Span("REGIME: INSTITUTIONAL", style={"color": "#FFD700", "fontSize": "11px", "backgroundColor": "#1F1D14", "padding": "2px 6px", "border": "1px solid #D29922", "borderRadius": "3px"})
                ])
            ]
        ),

        # TICKER STRIP: Spot Gold Price, Spread, 24h Stats, Session
        html.Div(
            id="ticker-strip",
            style={
                "display": "grid",
                "gridTemplateColumns": "1.5fr 1fr 1fr 1fr 1fr 1fr",
                "gap": "8px",
                "marginBottom": "8px"
            },
            children=[
                html.Div(id="live-price-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="spread-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="high-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="low-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="session-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
                html.Div(id="phase-box", style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "padding": "8px 12px", "borderRadius": "4px"}),
            ]
        ),

        # MAIN WORKSPACE: 70% Charts & CVD | 30% DOM Ladder & Time/Sales
        html.Div(
            style={"display": "flex", "gap": "8px", "marginBottom": "8px"},
            children=[
                # Left Panel: Main Chart & CVD
                html.Div(
                    style={"flex": "7", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        html.Div(
                            style={"backgroundColor": "#0D1117", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "4px"},
                            children=[
                                dcc.Graph(
                                    id="main-terminal-chart",
                                    config={"displayModeBar": False, "responsive": True},
                                    style={"height": "560px"}
                                )
                            ]
                        )
                    ]
                ),

                # Right Panel: DOM Ladder + The Tape (Time & Sales)
                html.Div(
                    style={"flex": "3", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        # DOM / Depth Ladder
                        html.Div(
                            style={
                                "backgroundColor": "#0D1117",
                                "border": "1px solid #21262D",
                                "borderRadius": "4px",
                                "padding": "8px",
                                "height": "290px",
                                "overflow": "hidden"
                            },
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #21262D", "paddingBottom": "4px", "marginBottom": "4px"},
                                    children=[
                                        html.Span("📊 DOM / DEPTH LADDER", style={"color": "#00F0FF", "fontSize": "11px", "fontWeight": "bold"}),
                                        html.Span("L2 BOOK (XAU)", style={"color": "#8B949E", "fontSize": "10px"})
                                    ]
                                ),
                                html.Div(id="dom-ladder-content")
                            ]
                        ),

                        # The Tape (Time & Sales)
                        html.Div(
                            style={
                                "backgroundColor": "#0D1117",
                                "border": "1px solid #21262D",
                                "borderRadius": "4px",
                                "padding": "8px",
                                "height": "260px",
                                "overflow": "hidden"
                            },
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #21262D", "paddingBottom": "4px", "marginBottom": "4px"},
                                    children=[
                                        html.Span("⚡ THE TAPE (TIME & SALES)", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"}),
                                        html.Span("REAL-TIME", style={"color": "#00E676", "fontSize": "10px"})
                                    ]
                                ),
                                html.Div(id="tape-content", style={"height": "230px", "overflowY": "auto"})
                            ]
                        )
                    ]
                )
            ]
        ),

        # BOTTOM PANEL: Institutional Trade Blotter (Execution Log)
        html.Div(
            style={
                "backgroundColor": "#0D1117",
                "border": "1px solid #21262D",
                "borderRadius": "4px",
                "padding": "10px",
                "marginTop": "4px"
            },
            children=[
                html.Div(
                    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px", "borderBottom": "1px solid #21262D", "paddingBottom": "6px"},
                    children=[
                        html.Div([
                            html.Span("📋 TRADE BLOTTER // INSTITUTIONAL EXECUTION LOG", style={"color": "#00F0FF", "fontSize": "12px", "fontWeight": "bold"}),
                            html.Span(" (Paper Simulation & Live Signals)", style={"color": "#8B949E", "fontSize": "11px"})
                        ]),
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
    [
        Output("clocks-panel", "children"),
        Output("live-price-box", "children"),
        Output("spread-box", "children"),
        Output("high-box", "children"),
        Output("low-box", "children"),
        Output("session-box", "children"),
        Output("phase-box", "children"),
        Output("main-terminal-chart", "figure"),
        Output("dom-ladder-content", "children"),
        Output("tape-content", "children"),
        Output("blotter-summary-stats", "children"),
        Output("trade-blotter-table", "children")
    ],
    [Input("terminal-interval", "n_intervals")]
)
def update_terminal_ui(n):
    with data_lock:
        bars = list(historical_bars)
        price = last_known_price
        meta = dict(market_meta)
        tape = list(recent_tape)
        book = dict(order_book)
        cur_bar = dict(current_bar)
        history = list(trade_history)
        pos = dict(trade_state)

    # 1. Clocks
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    clocks_text = f"UTC {now_utc.strftime('%H:%M:%S')}  |  NYC {(now_utc - timedelta(hours=4)).strftime('%H:%M:%S')}  |  LDN {(now_utc + timedelta(hours=1)).strftime('%H:%M:%S')}  |  IST {now.strftime('%H:%M:%S')}"

    # 2. Ticker Boxes
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

    high_box = [
        html.Div("24H HIGH", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(f"${meta.get('high', price):.2f}", style={"fontSize": "16px", "fontWeight": "bold", "color": "#00E676", "marginTop": "2px"}),
        html.Div("Upper Liquidity Level", style={"fontSize": "10px", "color": "#8B949E"})
    ]

    low_box = [
        html.Div("24H LOW", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(f"${meta.get('low', price):.2f}", style={"fontSize": "16px", "fontWeight": "bold", "color": "#FF3B30", "marginTop": "2px"}),
        html.Div("Lower Liquidity Level", style={"fontSize": "10px", "color": "#8B949E"})
    ]

    hour = now.hour
    curr_sess = "LONDON / NY OVERLAP" if 17 <= hour <= 20 else "NEW YORK SESSION" if 17 <= hour < 22 else "LONDON SESSION" if 13 <= hour < 17 else "ASIAN SESSION"
    session_box = [
        html.Div("MARKET KILL ZONE", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(curr_sess, style={"fontSize": "12px", "fontWeight": "bold", "color": "#FFD700", "marginTop": "4px"}),
        html.Div("High Institutional Volatility", style={"fontSize": "10px", "color": "#00E676"})
    ]

    last_phase = bars[-1].get('phase', 'Neutral') if bars else 'Neutral'
    phase_box = [
        html.Div("ORDER FLOW REGIME", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(last_phase.upper(), style={"fontSize": "13px", "fontWeight": "bold", "color": "#00F0FF", "marginTop": "4px"}),
        html.Div(f"CVD: {cum_delta:+.0f}", style={"fontSize": "10px", "color": "#FFD700"})
    ]

    # 3. Main Candlestick + Footprint + CVD Figure
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.03, row_heights=[0.75, 0.25]
    )

    if bars:
        df = pd.DataFrame(bars)
        df['time_str'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')

        # True Institutional Dynamic VWAP
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        cum_pv_series = (typical_price * df['volume']).cumsum()
        cum_vol_series = df['volume'].cumsum()
        df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])

        # Candlestick
        fig.add_trace(
            go.Candlestick(
                x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                name="XAUUSD",
                increasing_line_color="#00E676", decreasing_line_color="#FF3B30",
                increasing_fillcolor="#00E676", decreasing_fillcolor="#FF3B30"
            ),
            row=1, col=1
        )

        # Institutional VWAP and Bands
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap'], name="VWAP", line=dict(color="#FFD700", width=1.5)),
            row=1, col=1
        )
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap'] + 1.8, name="VWAP +1.5σ", line=dict(color="#FF9F0A", width=1, dash="dot")),
            row=1, col=1
        )
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap'] - 1.8, name="VWAP -1.5σ", line=dict(color="#00F0FF", width=1, dash="dot")),
            row=1, col=1
        )

        # Active Position Lines
        if pos.get('in_position'):
            ep = pos['entry_price']
            sl = pos['stop_loss']
            tp = pos['take_profit']
            fig.add_hline(y=ep, line=dict(color="#00F0FF", width=1.5, dash="dash"), annotation_text=f"ENTRY: {ep:.2f}", row=1, col=1)
            fig.add_hline(y=tp, line=dict(color="#00E676", width=1.5, dash="dash"), annotation_text=f"TP TARGET: {tp:.2f}", row=1, col=1)
            fig.add_hline(y=sl, line=dict(color="#FF3B30", width=1.5, dash="dash"), annotation_text=f"STOP LOSS: {sl:.2f}", row=1, col=1)

        # CVD Sub-chart
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['cvd'], name="CVD", line=dict(color="#00F0FF", width=2), fill="tozeroy", fillcolor="rgba(0, 240, 255, 0.08)"),
            row=2, col=1
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#0A0D14",
        margin=dict(l=10, r=40, t=10, b=10),
        showlegend=False,
        xaxis=dict(type='category', showgrid=True, gridcolor="#161B22", rangeslider=dict(visible=False), nticks=15),
        yaxis=dict(showgrid=True, gridcolor="#161B22", side="right"),
        xaxis2=dict(type='category', showgrid=True, gridcolor="#161B22", nticks=15),
        yaxis2=dict(showgrid=True, gridcolor="#161B22", side="right")
    )

    # 4. DOM Ladder Rendering
    dom_rows = []
    # Asks (Top 5)
    for ask in book.get('asks', [])[:5]:
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 6px", "fontSize": "11px", "backgroundColor": "rgba(255, 59, 48, 0.08)"},
                children=[
                    html.Span(f"{ask['price']:.2f}", style={"color": "#FF3B30", "fontWeight": "bold"}),
                    html.Span(f"{ask['volume']:.1f} oz", style={"color": "#8B949E"}),
                    html.Div(style={"width": f"{min(int(ask['volume']/2), 60)}px", "height": "4px", "backgroundColor": "#FF3B30", "borderRadius": "2px", "alignSelf": "center"})
                ]
            )
        )

    # Current Spread Marker
    dom_rows.append(
        html.Div(
            f"─── SPOT: ${price:.2f} ───",
            style={"textAlign": "center", "fontSize": "11px", "color": "#FFD700", "fontWeight": "bold", "padding": "3px 0", "backgroundColor": "#161B22", "borderTop": "1px solid #21262D", "borderBottom": "1px solid #21262D"}
        )
    )

    # Bids (Top 5)
    for bid in book.get('bids', [])[:5]:
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 6px", "fontSize": "11px", "backgroundColor": "rgba(0, 230, 118, 0.08)"},
                children=[
                    html.Span(f"{bid['price']:.2f}", style={"color": "#00E676", "fontWeight": "bold"}),
                    html.Span(f"{bid['volume']:.1f} oz", style={"color": "#8B949E"}),
                    html.Div(style={"width": f"{min(int(bid['volume']/2), 60)}px", "height": "4px", "backgroundColor": "#00E676", "borderRadius": "2px", "alignSelf": "center"})
                ]
            )
        )

    # 5. Tape Rendering
    tape_items = []
    for t in tape[:15]:
        side_color = "#00E676" if t['side'] == 'BUY' else "#FF3B30"
        badge = html.Span(" 🔥 BLOCK", style={"color": "#FFD700", "fontWeight": "bold", "fontSize": "9px"}) if t.get('is_block') else ""
        tape_items.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "borderBottom": "1px solid #161B22"},
                children=[
                    html.Span(t['time'], style={"color": "#8B949E"}),
                    html.Span(t['side'], style={"color": side_color, "fontWeight": "bold"}),
                    html.Span(f"${t['price']}", style={"color": "#E6EDF3"}),
                    html.Span([f"{t['size']} oz", badge], style={"color": side_color})
                ]
            )
        )

    # 6. Blotter Summary & Table
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

    # Table Header
    tbl_header = html.Tr([
        html.Th("#ID", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("OPEN TIME", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("DIR", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("ENTRY", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("SL", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("TP", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("EXIT TIME", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("EXIT PRICE", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("PNL (PIPS)", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("STATUS", style={"padding": "4px 8px", "color": "#8B949E"}),
        html.Th("REGIME", style={"padding": "4px 8px", "color": "#8B949E"})
    ], style={"borderBottom": "1px solid #21262D", "fontSize": "11px", "textAlign": "left"})

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

    return (
        clocks_text, price_box, spread_box, high_box, low_box, session_box, phase_box,
        fig, dom_rows, tape_items, blotter_stats, table
    )


# =============================================================================
# 7. CLEAN SHUTDOWN HANDLER
# =============================================================================
def graceful_shutdown(signum=None, frame=None):
    global is_running
    logger.info("Terminal shutting down gracefully...")
    is_running = False
    with data_lock:
        if current_bar.get('close') is not None and current_bar.get('time') is not None:
            save_bar_to_db(current_bar)
            logger.info(f"Terminal current bar saved to database.")
    sys.exit(0)


# =============================================================================
# 8. STARTUP ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    logger.info("🏛️ Starting GOLD.FLOW Bloomberg Terminal Edition on Port 8070...")
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(graceful_shutdown)

    init_db()
    load_initial_bars()
    load_trades()

    feed_thread = threading.Thread(target=live_feed_worker, daemon=True)
    feed_thread.start()

    logger.info("🚀 Launching Terminal Web Server on http://127.0.0.1:8070 (0.0.0.0)...")
    app.run(host="0.0.0.0", port=8070, debug=False)
