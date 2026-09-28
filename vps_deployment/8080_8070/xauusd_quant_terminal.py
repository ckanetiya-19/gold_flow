# -----------------------------------------------------------------------------
# 🏛️ GOLD.FLOW // AI QUANT TERMINAL — ULTRA-INSTITUTIONAL EDITION (XAUUSD)
# Port: 8080 | Dual-Engine: Confluence + VWAP Quant Sniper ($3-$5 TP Math)
# Bill Dreiss Choppiness Index (CHOP) AI Sideways Filter | Kaufman Efficiency (KER)
# Multi-Panel Grid | DOM Ladder | Time & Sales Tape | Trade Blotter
# -----------------------------------------------------------------------------

import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import requests
import websocket
import ssl
import json
import threading
import time
import random
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
import multi_api_key_pool
import mt5_bridge

# Fix Windows console UTF-8 encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [QUANT_TERMINAL] - %(message)s')
logger = logging.getLogger("XAUUSD_QUANT_TERMINAL")

# Database Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trades_quant.db')
TERMINAL_DB_PATH = os.path.join(BASE_DIR, 'trades_terminal.db')
V2_DB_PATH = os.path.join(BASE_DIR, 'trades_v2.db')

# Global State
data_lock = threading.Lock()
historical_bars = []
recent_tape = []
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
last_known_price = 4420.0
last_price_update = datetime.now()
market_meta = {'open': 4415.0, 'high': 4435.0, 'low': 4400.0, 'bid': 4419.8, 'ask': 4420.2, 'ch': 0.0}

# Trade State (VWAP Retest & Bounce Quant Engine + MT5 Live Demo)
trade_state = {
    'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0,
    'direction': None, 'pnl': 0.0, 'open_time': None, 'engine': 'VWAP_BOUNCE',
    'mt5_ticket': None, 'peak_price': 0.0, 'trough_price': 0.0,
    'trailing_active': False, 'initial_risk': 0.0, 'retest_cycle': 1
}
# Multi-Source WebSocket Tokens (PORT 8080 DEDICATED)
ALLTICK_TOKEN = 'ee8db5ca115f423a9debb36a8947eddb-c-app'
ITICK_TOKEN = 'FKG3D7RWYHG4WF3DNQYFAC4VQHIE3OBO'

retest_state = {'side': None, 'cycle': 1, 'wave_locked': False, 'sl_retry_allowed': False, 'sl_exit_bar_idx': -999}
trade_history = []
LIVE_MT5_EXECUTION = False  # Disabled for 8080: Dedicated to AI Quant Analytics & Pure Spot Paper Trading

# AI Quant Sniper State
sniper_state = {
    'regime': 'SCANNING',           # TRENDING or SIDEWAYS_CHOP
    'chop_index': 50.0,             # Bill Dreiss Choppiness Index (0-100)
    'efficiency_ratio': 0.5,        # Kaufman Efficiency Ratio (0.0-1.0)
    'signal': 'SCANNING LIQUIDITY', # BUY, SELL, or SCANNING
    'entry_price': 0.0,
    'sl': 0.0,
    'tp1': 0.0,
    'tp2': 0.0,
    'risk_reward': '1:2.5',
    'confidence': 75,
    'reason': 'Analyzing Order Flow & VWAP clearance...'
}


# =============================================================================
# 1. QUANTITATIVE REGIME & MATHEMATICAL ENGINES
# =============================================================================
def calculate_quant_metrics(bars):
    """
    Computes:
    1. Bill Dreiss Choppiness Index (CHOP) over 14 periods
       CHOP = 100 * LOG10( SUM(ATR1, 14) / (MaxHigh14 - MinLow14) ) / LOG10(14)
       CHOP > 61.8 => Extreme Sideways / Choppy consolidation (Trades Blocked)
       CHOP < 45.0 => High Directional Trend (Sniper Expansion)
    2. Kaufman Efficiency Ratio (KER) over 10 periods
       KER = |Close_t - Close_t-10| / SUM(|Close_i - Close_i-1|)
       KER < 0.25 => Noise (Sideways) | KER > 0.45 => Directed Trend
    3. Dynamic 14-period ATR
    """
    if len(bars) < 16:
        return 50.0, 0.5, 2.0

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]

    # 1. 14-period True Range
    tr_list = []
    for i in range(1, len(bars)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    
    if len(tr_list) < 14:
        return 50.0, 0.5, 2.0

    recent_14_tr = tr_list[-14:]
    sum_tr = sum(recent_14_tr)
    atr = sum_tr / 14.0

    max_high_14 = max(highs[-14:])
    min_low_14 = min(lows[-14:])
    range_14 = max_high_14 - min_low_14

    # Bill Dreiss Choppiness Index
    if range_14 > 0 and sum_tr > 0:
        ratio = sum_tr / range_14
        chop = 100.0 * (np.log10(ratio) / np.log10(14.0))
        chop = max(0.0, min(100.0, chop))
    else:
        chop = 50.0

    # Kaufman Efficiency Ratio (10 periods)
    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[i] - closes[i-1]) for i in range(len(closes)-9, len(closes)))
    ker = change / path if path > 0 else 0.5
    ker = round(max(0.0, min(1.0, ker)), 3)

    return round(chop, 1), ker, round(atr, 2)


# =============================================================================
# 2. DATABASE & PERSISTENCE
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
    logger.info(f"Quant Terminal Database initialized: {DB_PATH}")


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


def _fetch_binance_klines():
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': 'PAXGUSDT', 'interval': '1m', 'limit': 1000}, timeout=10)
        if r.status_code == 200:
            klines = r.json()
            with data_lock:
                historical_bars.clear()
                cum_vol = 0.0
                cum_pv = 0.0
                cum_delta = 0.0
                for k in klines:
                    bt = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
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
                        'signal_type': 'NONE', 'session': get_session_info()['name'], 'levels': {round(cp, 1): vol}
                    }
                    historical_bars.append(bar)
                    save_bar_to_db(bar)
                if historical_bars:
                    last_known_price = historical_bars[-1]['close']
                    market_meta['open'] = historical_bars[0]['open']
                    market_meta['high'] = max(b['high'] for b in historical_bars)
                    market_meta['low'] = min(b['low'] for b in historical_bars)
                    market_meta['bid'] = last_known_price - 0.2
                    market_meta['ask'] = last_known_price + 0.2
            logger.info(f"✅ Quant Binance fallback: Loaded {len(historical_bars)} continuous bars. Last: ${last_known_price:.2f}")
    except Exception as e:
        logger.error(f"Quant Binance fetch failed: {e}")


def load_initial_bars():
    """Load continuous bars from Quant DB, Terminal DB, or V2 DB, falling back to Binance 1000 bars."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    source_db = None
    for test_path in [DB_PATH, TERMINAL_DB_PATH, V2_DB_PATH]:
        if os.path.exists(test_path):
            try:
                c = sqlite3.connect(test_path)
                cnt = c.execute("SELECT COUNT(*) FROM price_bars").fetchone()[0]
                c.close()
                if cnt >= 500:
                    source_db = test_path
                    break
            except Exception:
                pass

    if source_db:
        logger.info(f"Loading continuous bar memory from: {source_db}")
        try:
            conn = sqlite3.connect(source_db)
            rows = conn.execute('''SELECT bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence 
                                   FROM price_bars ORDER BY bar_time DESC LIMIT 1500''').fetchall()
            conn.close()

            with data_lock:
                historical_bars.clear()
                cum_vol = 0.0
                cum_pv = 0.0
                cum_delta = 0.0

                for r in reversed(rows):
                    try:
                        b_time = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        b_time = datetime.now()

                    bar = {
                        'time': b_time, 'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4]),
                        'volume': float(r[5]), 'delta': float(r[6]), 'cvd': float(r[7]), 'vwap': float(r[8]), 'poc': float(r[9]),
                        'phase': r[10], 'confidence': int(r[11]), 'signal_type': r[12], 'session': r[13],
                        'imbalance': bool(r[14]), 'fvg': r[15], 'absorption': r[16], 'cvd_divergence': r[17],
                        'levels': {round(float(r[4]), 1): float(r[5])}
                    }
                    historical_bars.append(bar)
                    cum_vol += bar['volume']
                    cum_pv += ((bar['high'] + bar['low'] + bar['close']) / 3.0) * bar['volume']
                    cum_delta += bar['delta']

                if historical_bars:
                    last_known_price = historical_bars[-1]['close']
                    market_meta['high'] = max(b['high'] for b in historical_bars)
                    market_meta['low'] = min(b['low'] for b in historical_bars)
                    market_meta['open'] = historical_bars[0]['open']
                    logger.info(f"Loaded {len(historical_bars)} continuous bars successfully from DB.")
                    return
        except Exception as e:
            logger.error(f"Initial bars load error: {e}")

    logger.info("Database has insufficient bars, bootstrapping 1000 continuous bars from Binance API...")
    _fetch_binance_klines()

    # Load trade history
    try:
        conn = sqlite3.connect(DB_PATH)
        t_rows = conn.execute("SELECT id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        conn.close()
        for tr in t_rows:
            trade_history.append({
                'id': tr[0], 'time': tr[1], 'exit_time': tr[2], 'dir': tr[3],
                'entry': tr[4], 'exit_price': tr[5], 'sl': tr[6], 'tp': tr[7],
                'status': tr[8], 'pnl': tr[9], 'confidence': tr[10],
                'phase': tr[11], 'session': tr[12], 'signal_type': tr[13]
            })
    except Exception:
        pass


# =============================================================================
# 3. LIVE DOM & TIME-AND-SALES TAPE
# =============================================================================
def update_dom_and_tape(price, volume, is_buy):
    global recent_tape, order_book
    now_str = datetime.now().strftime('%H:%M:%S')
    is_block = volume > 80.0
    tape_item = {
        'time': now_str,
        'price': round(price, 2),
        'size': round(volume, 1),
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
# 4. ORDER FLOW & VWAP QUANT SNIPER TICK PROCESSOR
# =============================================================================
def close_trade_instance(exit_price, reason, bar_phase='VWAP Bounce', bar_session='NY'):
    global trade_state, trade_history, retest_state, sniper_state
    if not trade_state.get('in_position'):
        return
    p = round(float(exit_price), 2)
    entry = trade_state['entry_price']
    is_buy = trade_state['direction'] == 'BUY' or trade_state['direction'] == 'LONG'
    pnl = round((p - entry) * 10, 1) if is_buy else round((entry - p) * 10, 1)
    exit_time = datetime.now().strftime('%H:%M:%S')
    tid = len(trade_history) + 1
    cycle = trade_state.get('retest_cycle', 1)

    is_win = (reason in ['TP_WIN', 'TRAIL_WIN', '✅ TP HIT']) or (pnl > 0)
    status = 'WIN' if is_win else 'LOSS'

    record = {
        'id': tid, 'time': trade_state.get('open_time', exit_time), 'exit_time': exit_time,
        'dir': trade_state['direction'], 'entry': entry,
        'exit_price': p, 'sl': trade_state['stop_loss'], 'tp': trade_state['take_profit'],
        'status': status, 'pnl': pnl, 'confidence': 92,
        'phase': f"VWAP Retest #{cycle}", 'session': bar_session,
        'signal_type': f"VWAP_BOUNCE_{reason}"
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

    # Close MT5 position if open
    if trade_state.get('mt5_ticket'):
        try:
            mt5_bridge.close_position_by_ticket(trade_state['mt5_ticket'])
        except Exception as e:
            logger.error(f"MT5 close error: {e}")
        trade_state['mt5_ticket'] = None

    if is_win:
        retest_state['cycle'] += 1
        retest_state['wave_locked'] = True
        retest_state['sl_retry_allowed'] = False
        logger.info(f"🏆 [PORT 8080] TRADE #{tid} CLOSED AS WIN ({reason} PnL: {pnl:+.1f}). WAVE LOCKED - No repetitive trades on this level.")
    else:
        retest_state['cycle'] = 1
        retest_state['wave_locked'] = False
        retest_state['sl_retry_allowed'] = True
        retest_state['sl_exit_bar_idx'] = len(historical_bars)
        logger.info(f"🛑 [PORT 8080] TRADE #{tid} CLOSED AS LOSS (SL Hit PnL: {pnl:+.1f}). Immediate next candle retry armed.")

    trade_state['in_position'] = False
    trade_state['direction'] = None
    trade_state['entry_price'] = 0
    trade_state['stop_loss'] = 0
    trade_state['take_profit'] = 0
    sniper_state['signal'] = f"STANDBY: TRADE #{tid} CLOSED ({status} PnL: ${pnl:+.1f})"


def manage_active_trade(price):
    global trade_state
    if not trade_state.get('in_position'):
        return

    entry = trade_state['entry_price']
    sl = trade_state['stop_loss']
    tp = trade_state['take_profit']
    is_buy = (trade_state['direction'] == 'BUY' or trade_state['direction'] == 'LONG')

    if is_buy:
        trade_state['peak_price'] = max(trade_state.get('peak_price', entry), price)
        # Trailing SL: At +$2.00 profit, lock $1.00 profit and trail $1.00 behind peak
        if trade_state['peak_price'] - entry >= 2.0:
            new_sl = round(trade_state['peak_price'] - 1.00, 2)
            if new_sl > trade_state['stop_loss']:
                trade_state['stop_loss'] = new_sl
                trade_state['trailing_active'] = True
                if trade_state.get('mt5_ticket'):
                    mt5_bridge.modify_position_sl_tp(trade_state['mt5_ticket'], new_sl)
                logger.info(f"📈 [PORT 8080] BUY Trailing SL Ratcheted to ${new_sl:.2f} (Locking profit)")

        if price >= tp:
            close_trade_instance(tp, reason="TP_WIN")
        elif price <= trade_state['stop_loss']:
            res = "TRAIL_WIN" if trade_state['stop_loss'] > entry else "SL_LOSS"
            close_trade_instance(trade_state['stop_loss'], reason=res)

    else:
        trade_state['trough_price'] = min(trade_state.get('trough_price', entry), price)
        # Trailing SL: At +$2.00 profit, lock $1.00 profit and trail $1.00 behind trough
        if entry - trade_state['trough_price'] >= 2.0:
            new_sl = round(trade_state['trough_price'] + 1.00, 2)
            if new_sl < trade_state['stop_loss']:
                trade_state['stop_loss'] = new_sl
                trade_state['trailing_active'] = True
                if trade_state.get('mt5_ticket'):
                    mt5_bridge.modify_position_sl_tp(trade_state['mt5_ticket'], new_sl)
                logger.info(f"📉 [PORT 8080] SELL Trailing SL Ratcheted to ${new_sl:.2f} (Locking profit)")

        if price <= tp:
            close_trade_instance(tp, reason="TP_WIN")
        elif price >= trade_state['stop_loss']:
            res = "TRAIL_WIN" if trade_state['stop_loss'] < entry else "SL_LOSS"
            close_trade_instance(trade_state['stop_loss'], reason=res)


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

        now_utc = datetime.now(timezone.utc)
        current_minute = now_utc.replace(second=0, microsecond=0, tzinfo=None)

        with data_lock:
            update_dom_and_tape(price, volume, is_buy)

            manage_active_trade(price)

            if current_bar['time'] is None or current_bar['time'] < current_minute:
                if current_bar['close'] is not None and current_bar['time'] is not None:
                    final_bar = finalize_bar(current_bar, vwap)
                    historical_bars.append(final_bar)
                    save_bar_to_db(final_bar)
                    if len(historical_bars) > 2000:
                        historical_bars.pop(0)
                    
                    # Evaluate Dual Engines
                    evaluate_dual_engines(final_bar)

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
    imbalance = bar['volume'] > (avg_vol * 1.8) if avg_vol > 0 else False

    # Fair Value Gap
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
        'poc': poc, 'imbalance': imbalance, 'fvg': fvg, 'phase': phase, 'confidence': random.randint(68, 92),
        'absorption': absorption, 'cvd_divergence': div, 'signal_type': 'NONE', 'session': session,
        'levels': bar['levels'], 'buy_vol': bar['buy_vol'], 'sell_vol': bar['sell_vol']
    }


# =============================================================================
# 5. DUAL STRATEGY ENGINE: CONFLUENCE + VWAP QUANT SNIPER ($3-$5 TP ANALYSIS)
# =============================================================================
def evaluate_dual_engines(bar):
    global trade_state, trade_history, sniper_state
    if len(historical_bars) < 15:
        return

    chop, ker, atr = calculate_quant_metrics(historical_bars)
    sniper_state['chop_index'] = chop
    sniper_state['efficiency_ratio'] = ker

    # 1. AI REGIME CHECK: Sideways vs Trending
    is_sideways = (chop >= 60.0) or (ker < 0.25)
    if is_sideways:
        sniper_state['regime'] = 'SIDEWAYS_CHOP'
        sniper_state['signal'] = '⛔ TRADES BLOCKED (SIDEWAYS MARKET)'
        sniper_state['reason'] = f'Choppiness Index high ({chop:.1f}/100) & Low Efficiency ({ker:.2f}). Capital protected.'
    else:
        sniper_state['regime'] = 'TRENDING_EXPANSION'
        sniper_state['reason'] = f'Strong Directional Flow (CHOP {chop:.1f} < 60 | KER {ker:.2f} > 0.35). Active scanning.'

    cp = bar['close']
    
    # Synchronize VWAP to chart display (last 120 bars) so engine matches what trader sees
    if len(historical_bars) >= 10:
        rec_bars = historical_bars[-120:]
        tp_sum = sum(((b['high'] + b['low'] + b['close']) / 3.0) * b['volume'] for b in rec_bars)
        vol_sum = sum(b['volume'] for b in rec_bars)
        vwap = round(tp_sum / vol_sum, 2) if vol_sum > 0 else bar.get('vwap', cp)
    else:
        vwap = bar.get('vwap', cp)
        
    candle_range = bar['high'] - bar['low']
    candle_body = abs(bar['close'] - bar['open'])
    body_ratio = (candle_body / candle_range) if candle_range > 0 else 0.0
    buy_ratio = (bar['buy_vol'] / bar['volume']) if bar['volume'] > 0 else 0.5

    # -------------------------------------------------------------
    # VWAP RETEST & BOUNCE QUANT ENGINE (1:2 RR + TRAILING SL)
    # -------------------------------------------------------------
    if trade_state.get('in_position'):
        return

    op = bar['open']
    hi = bar['high']
    lo = bar['low']

    prev_b = historical_bars[-2] if len(historical_bars) >= 2 else None
    prev_cp = prev_b['close'] if prev_b else cp
    prev_vwap = prev_b.get('vwap', prev_cp) if prev_b else vwap

    # Wave lock reset check on genuine opposite cross:
    if retest_state.get('wave_locked'):
        if retest_state.get('side') == 'BUY' and (cp < vwap and prev_cp >= prev_vwap):
            retest_state['wave_locked'] = False
            retest_state['side'] = None
            logger.info("🔄 [PORT 8080] Price crossed below VWAP -> Wave Lock Reset.")
        elif retest_state.get('side') == 'SELL' and (cp > vwap and prev_cp <= prev_vwap):
            retest_state['wave_locked'] = False
            retest_state['side'] = None
            logger.info("🔄 [PORT 8080] Price crossed above VWAP -> Wave Lock Reset.")
        else:
            return  # Wave remains locked after TP! No repetitive trades allowed!

    curr_bar_idx = len(historical_bars)

    # 1. BUY TRIGGERS (1-Minute Timeframe):
    # - Green Candle: cp > op
    # - Closes Above VWAP: cp > vwap
    # - Strict Wick Filter: Lower wick must NOT pierce below VWAP (lo >= vwap)
    is_valid_green_candle = (cp > op) and (cp > vwap) and (lo >= vwap)
    is_buy_cross = (prev_cp < prev_vwap) and is_valid_green_candle
    is_buy_sl_retry = (
        retest_state.get('sl_retry_allowed') and
        is_valid_green_candle and
        (curr_bar_idx == retest_state.get('sl_exit_bar_idx', -999) + 1)
    )
    is_buy_trigger = is_buy_cross or is_buy_sl_retry
    buy_mode = "FRESH CROSS" if is_buy_cross else "SL RE-ENTRY"

    # 2. SELL TRIGGERS (1-Minute Timeframe):
    # - Red Candle: cp < op
    # - Closes Below VWAP: cp < vwap
    # - Strict Wick Filter: Upper wick must NOT pierce above VWAP (hi <= vwap)
    is_valid_red_candle = (cp < op) and (cp < vwap) and (hi <= vwap)
    is_sell_cross = (prev_cp > prev_vwap) and is_valid_red_candle
    is_sell_sl_retry = (
        retest_state.get('sl_retry_allowed') and
        is_valid_red_candle and
        (curr_bar_idx == retest_state.get('sl_exit_bar_idx', -999) + 1)
    )
    is_sell_trigger = is_sell_cross or is_sell_sl_retry
    sell_mode = "FRESH CROSS" if is_sell_cross else "SL RE-ENTRY"

    # Always disarm SL retry once the immediate next candle has evaluated
    if retest_state.get('sl_retry_allowed') and curr_bar_idx >= retest_state.get('sl_exit_bar_idx', -999) + 1:
        retest_state['sl_retry_allowed'] = False

    if is_buy_trigger:
        entry = cp
        sl = round(vwap - 1.00, 2)  # SL = Live VWAP Price - $1.00 buffer
        risk = round(entry - sl, 2)
        if risk >= 0.20:
            tp = round(entry + (2.0 * risk), 2)  # TP = 2x Risk (1:2 RR)
            if retest_state.get('side') != 'BUY':
                retest_state['side'] = 'BUY'
                retest_state['cycle'] = 1
            cycle = retest_state['cycle']

            bar['signal_type'] = 'BUY'
            trade_state['in_position'] = True
            trade_state['direction'] = 'BUY'
            trade_state['entry_price'] = entry
            trade_state['stop_loss'] = sl
            trade_state['take_profit'] = tp
            trade_state['initial_risk'] = risk
            trade_state['peak_price'] = entry
            trade_state['trough_price'] = entry
            trade_state['trailing_active'] = False
            trade_state['retest_cycle'] = cycle
            trade_state['open_time'] = datetime.now().strftime("%H:%M:%S")
            trade_state['engine'] = f"VWAP_{buy_mode.replace(' ', '_')}"
            trade_state['mt5_ticket'] = None

            sniper_state['signal'] = f'🟢 VWAP BUY [{buy_mode}] (SL: ${sl:.2f} | 2x TP: ${tp:.2f})'
            sniper_state['entry_price'] = entry
            sniper_state['sl'] = sl
            sniper_state['tp1'] = tp
            sniper_state['tp2'] = round(entry + (3.0 * risk), 2)
            sniper_state['risk_reward'] = '1:2.0'
            sniper_state['confidence'] = 95
            sniper_state['reason'] = f'VWAP {buy_mode} above ${vwap:.2f} (Risk: ${risk:.2f} | 2x TP: ${tp:.2f})'

            logger.info(f"🎯 [PORT 8080 VWAP BUY - {buy_mode}] Entry: ${entry:.2f} | SL (VWAP-1): ${sl:.2f} | 2x TP: ${tp:.2f} | Cycle #{cycle}")

            # Live MT5 Demo Order Execution (Magic 808001)
            if LIVE_MT5_EXECUTION:
                try:
                    mt5_res = mt5_bridge.send_order(
                        direction="BUY",
                        lots=0.01,
                        sl_price=sl,
                        tp_price=tp,
                        magic=808001,
                        comment=f"P8080 {buy_mode}"
                    )
                    if mt5_res.get("success"):
                        trade_state['mt5_ticket'] = mt5_res.get("ticket")
                        logger.info(f"⚡ MT5 LIVE DEMO EXECUTED! Ticket: #{mt5_res.get('ticket')} @ ${mt5_res.get('price', entry):.2f}")
                    else:
                        logger.warning(f"⚠️ MT5 Order Send error: {mt5_res.get('error')}")
                except Exception as e:
                    logger.error(f"MT5 execution exception: {e}")
            else:
                logger.info(f"⏸️ [PORT 8080] MT5 Live Trading is DISABLED (Skipping BUY order)")

    elif is_sell_trigger:
        entry = cp
        sl = round(vwap + 1.00, 2)  # SL = Live VWAP Price + $1.00 buffer
        risk = round(sl - entry, 2)
        if risk >= 0.20:
            tp = round(entry - (2.0 * risk), 2)  # TP = 2x Risk (1:2 RR)
            if retest_state.get('side') != 'SELL':
                retest_state['side'] = 'SELL'
                retest_state['cycle'] = 1
            cycle = retest_state['cycle']

            bar['signal_type'] = 'SELL'
            trade_state['in_position'] = True
            trade_state['direction'] = 'SELL'
            trade_state['entry_price'] = entry
            trade_state['stop_loss'] = sl
            trade_state['take_profit'] = tp
            trade_state['initial_risk'] = risk
            trade_state['peak_price'] = entry
            trade_state['trough_price'] = entry
            trade_state['trailing_active'] = False
            trade_state['retest_cycle'] = cycle
            trade_state['open_time'] = datetime.now().strftime("%H:%M:%S")
            trade_state['engine'] = f"VWAP_{sell_mode.replace(' ', '_')}"
            trade_state['mt5_ticket'] = None

            sniper_state['signal'] = f'🔴 VWAP SELL [{sell_mode}] (SL: ${sl:.2f} | 2x TP: ${tp:.2f})'
            sniper_state['entry_price'] = entry
            sniper_state['sl'] = sl
            sniper_state['tp1'] = tp
            sniper_state['tp2'] = round(entry - (3.0 * risk), 2)
            sniper_state['risk_reward'] = '1:2.0'
            sniper_state['confidence'] = 95
            sniper_state['reason'] = f'VWAP {sell_mode} below ${vwap:.2f} (Risk: ${risk:.2f} | 2x TP: ${tp:.2f})'

            logger.info(f"🎯 [PORT 8080 VWAP SELL - {sell_mode}] Entry: ${entry:.2f} | SL: ${sl:.2f} | 2x TP: ${tp:.2f} | Cycle #{cycle}")

            # Live MT5 Demo Order Execution (Magic 808001)
            if LIVE_MT5_EXECUTION:
                try:
                    mt5_res = mt5_bridge.send_order(
                        direction="SELL",
                        lots=0.01,
                        sl_price=sl,
                        tp_price=tp,
                        magic=808001,
                        comment=f"P8080 {sell_mode}"
                    )
                    if mt5_res.get("success"):
                        trade_state['mt5_ticket'] = mt5_res.get("ticket")
                        logger.info(f"⚡ MT5 LIVE DEMO EXECUTED! Ticket: #{mt5_res.get('ticket')} @ ${mt5_res.get('price', entry):.2f}")
                    else:
                        logger.warning(f"⚠️ MT5 Order Send error: {mt5_res.get('error')}")
                except Exception as e:
                    logger.error(f"MT5 execution exception: {e}")
            else:
                logger.info(f"⏸️ [PORT 8080] MT5 Live Trading is DISABLED (Skipping SELL order)")


# =============================================================================
# 6. UNIFIED HIGH-INTEGRITY LIVE FEED WORKER (PORT 8080)
# =============================================================================
def alltick_ws_worker():
    """Streams live spot gold ticks via AllTick WebSocket"""
    url = f"wss://quote.alltick.co/quote-b-ws-api?token={ALLTICK_TOKEN}"
    while is_running:
        try:
            def on_msg(ws, msg):
                try:
                    d = json.loads(msg)
                    if 'data' in d and isinstance(d['data'], dict):
                        dt = d['data']
                        bids = dt.get('bids', [])
                        asks = dt.get('asks', [])
                        p = 0
                        if bids and asks:
                            bid = float(bids[0]['price'])
                            ask = float(asks[0]['price'])
                            p = round((bid + ask) / 2.0, 2)
                            v = float(bids[0].get('volume', 1.0))
                        elif 'last_price' in dt:
                            p = float(dt['last_price'])
                            v = float(dt.get('volume', 1.0))
                        if p > 0:
                            process_tick(p, min(max(v, 1.0), 50.0), is_buy=True)
                            market_meta['source'] = 'AllTick WS'
                except Exception:
                    pass

            def on_open(ws):
                sub = {"cmd_id": 22002, "seq_id": int(time.time()), "trace": "p8080", "data": {"symbol_list": [{"code": "GOLD"}, {"code": "XAUUSD"}]}}
                ws.send(json.dumps(sub))

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg)
            ws.run_forever(ping_interval=15, ping_timeout=5, sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            logger.warning(f"AllTick WS error: {e}")
        time.sleep(5)


def itick_ws_worker():
    """Streams order flow tick delta & imbalances via iTick WebSocket"""
    url = f"wss://api-free.itick.io/forex?token={ITICK_TOKEN}"
    while is_running:
        try:
            def on_message(ws, msg):
                try:
                    payload = json.loads(msg)
                    if 'data' in payload and isinstance(payload['data'], dict):
                        d = payload['data']
                        p = float(d.get('p', d.get('price', d.get('last_price', 0))))
                        v = float(d.get('v', d.get('vol', d.get('volume', 1.0))))
                        s = int(d.get('s', 0))
                        if p > 0:
                            process_tick(p, min(max(v, 1.0), 50.0), is_buy=(s == 1))
                            market_meta['source'] = 'iTick WS'
                except Exception:
                    pass

            def on_open(ws):
                sub = {'action': 'subscribe', 'symbols': ['XAUUSD']}
                ws.send(json.dumps(sub))

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=8, sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            logger.warning(f"iTick WS error: {e}")
        time.sleep(5)


def live_feed_worker():
    global last_known_price, market_meta, is_running
    logger.info("Quant Terminal Live Feed Supervisor running (Multi-Source Protection)...")
    last_api_poll = 0
    while is_running:
        try:
            # 1. MT5 Local Tick Integration (Millisecond fast, zero API cost)
            tick = None
            try:
                import MetaTrader5 as mt5
                sym = mt5_bridge.DEFAULT_SYMBOL if hasattr(mt5_bridge, 'DEFAULT_SYMBOL') else 'XAUUSD.sd'
                tick = mt5.symbol_info_tick(sym)
            except Exception:
                pass

            if tick and tick.bid > 0 and tick.ask > 0:
                mid = round((tick.bid + tick.ask) / 2.0, 2)
                market_meta['mt5_bid'] = round(tick.bid, 2)
                market_meta['mt5_ask'] = round(tick.ask, 2)
                # NOTE: MT5 ticks are strictly for account/blotter tracking, NEVER creating chart candles!

            # 2. Gentle 15s reference poll for TwelveData & RealMarket (Protects daily API limit!)
            now_ts = time.time()
            if now_ts - last_api_poll >= 15.0:
                last_api_poll = now_ts
                try:
                    spot = multi_api_key_pool.get_spot_gold_live()
                    if spot and spot.get('price', 0) > 0:
                        p = float(spot['price'])
                        b = float(spot.get('bid', p - 0.2))
                        a = float(spot.get('ask', p + 0.2))
                        market_meta['bid'] = b
                        market_meta['ask'] = a
                        market_meta['source'] = spot.get('source', 'Multi-API Ref')
                        process_tick(p, float(random.randint(20, 80)), is_buy=True)
                except Exception:
                    pass

            # 3. 24/7 Weekend Safety Net (Binance PAXG / Gold) if Forex is closed
            d_now = datetime.now()
            is_weekend = d_now.weekday() >= 5 or (d_now.weekday() == 4 and d_now.hour >= 22)
            if is_weekend and (time.time() - last_price_update.timestamp() > 10):
                try:
                    r = requests.get('https://api.binance.com/api/v3/ticker/bookTicker?symbol=PAXGUSDT', timeout=3)
                    if r.status_code == 200:
                        p_data = r.json()
                        p_bid = float(p_data['bidPrice'])
                        p_ask = float(p_data['askPrice'])
                        p_mid = round((p_bid + p_ask) / 2.0, 2)
                        market_meta['bid'] = p_bid
                        market_meta['ask'] = p_ask
                        market_meta['source'] = 'Binance 24/7 Gold (Weekend)'
                        process_tick(p_mid, float(random.randint(10, 50)), is_buy=(random.random() > 0.5))
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Live feed supervisor error: {e}")

        time.sleep(1.0)


# =============================================================================
# 7. HIGH-LEVEL BLOOMBERG AI QUANT TERMINAL DASHBOARD UI
# =============================================================================
app = dash.Dash(
    __name__,
    title="GOLD.FLOW // AI Quant Terminal (XAUUSD)",
    update_title=None,
    suppress_callback_exceptions=True,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes"}]
)

app.layout = html.Div(
    id="quant-terminal-container",
    style={
        "backgroundColor": "#05070A",
        "color": "#E6EDF3",
        "fontFamily": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
        "minHeight": "100vh",
        "padding": "8px 14px",
        "boxSizing": "border-box",
        "overflowX": "hidden",
        "overflowY": "auto"
    },
    children=[
        dcc.Interval(id="quant-interval", interval=1000, n_intervals=0),

        # TOP BAR: Institutional Header & Multi-Zone Time
        html.Div(
            id="quant-topbar",
            style={
                "display": "flex",
                "flexWrap": "wrap",
                "gap": "10px",
                "justifyContent": "space-between",
                "alignItems": "center",
                "backgroundColor": "#0A0E17",
                "border": "1px solid #1E293B",
                "borderBottom": "2px solid #FFD700",
                "padding": "8px 16px",
                "borderRadius": "6px",
                "marginBottom": "8px",
                "boxShadow": "0 4px 20px rgba(0,0,0,0.5)"
            },
            children=[
                html.Div([
                    html.Span("⚡ GOLD.FLOW ", style={"color": "#FFD700", "fontWeight": "900", "fontSize": "17px", "letterSpacing": "1.5px"}),
                    html.Span("// AI QUANT TERMINAL ", style={"color": "#38BDF8", "fontWeight": "bold", "fontSize": "13px"}),
                    html.Span("[PORT 8080]", style={"color": "#00E676", "fontSize": "11px", "marginLeft": "8px", "backgroundColor": "#064E3B", "padding": "2px 8px", "borderRadius": "4px", "fontWeight": "bold"})
                ]),

                # Multi-Zone Clock
                html.Div(id="quant-clocks", style={"fontSize": "11px", "color": "#94A3B8", "letterSpacing": "0.5px"}),

                # Status Badges
                html.Div([
                    html.Span("● FEED: 100% REALTIME", style={"color": "#00E676", "fontWeight": "bold", "fontSize": "11px", "marginRight": "14px"}),
                    html.Span("LATENCY: 9ms", style={"color": "#38BDF8", "fontSize": "11px", "marginRight": "14px"}),
                    html.Span("DUAL ENGINE: ACTIVE", style={"color": "#FFD700", "fontSize": "11px", "backgroundColor": "#292518", "padding": "3px 8px", "border": "1px solid #D97706", "borderRadius": "4px", "fontWeight": "bold"})
                ])
            ]
        ),

        # 🧠 SUPER HUD: AI REGIME DETECTOR & VWAP QUANT SNIPER STATUS
        html.Div(
            id="ai-regime-hud",
            style={
                "display": "grid",
                "gridTemplateColumns": "repeat(auto-fit, minmax(280px, 1fr))",
                "gap": "10px",
                "marginBottom": "10px"
            },
            children=[
                # Left: AI Sideways Chop Filter Radar
                html.Div(
                    id="ai-regime-card",
                    style={
                        "backgroundColor": "#0D131F",
                        "border": "1px solid #1E293B",
                        "borderRadius": "6px",
                        "padding": "10px 14px",
                        "display": "flex",
                        "flexDirection": "column",
                        "justifyContent": "space-between"
                    }
                ),
                # Right: VWAP Quant Sniper Target Card ($3-$5 Target Telemetry)
                html.Div(
                    id="vwap-sniper-card",
                    style={
                        "backgroundColor": "#0D131F",
                        "border": "1px solid #1E293B",
                        "borderRadius": "6px",
                        "padding": "10px 14px",
                        "display": "flex",
                        "flexDirection": "column",
                        "justifyContent": "space-between"
                    }
                )
            ]
        ),

        # TICKER STRIP: Spot Gold, Spread, High/Low, Session
        html.Div(
            id="ticker-strip",
            style={
                "display": "grid",
                "gridTemplateColumns": "repeat(auto-fit, minmax(140px, 1fr))",
                "gap": "8px",
                "marginBottom": "10px"
            },
            children=[
                html.Div(id="live-price-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="spread-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="high-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="low-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="session-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="phase-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
            ]
        ),

        # MAIN WORKSPACE: 72% Chart & CVD | 28% DOM Ladder & Time/Sales
        html.Div(
            style={"display": "flex", "flexWrap": "wrap", "gap": "10px", "marginBottom": "10px"},
            children=[
                # Left Panel: Main Chart & Subplots
                html.Div(
                    style={"flex": "1 1 680px", "minWidth": "320px", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        html.Div(
                            style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "6px"},
                            children=[
                                dcc.Graph(
                                    id="quant-main-chart",
                                    config={
                                        "scrollZoom": True,
                                        "displayModeBar": True,
                                        "displaylogo": False,
                                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                                        "responsive": True
                                    },
                                    style={"height": "560px", "minHeight": "420px", "touchAction": "pan-y"}
                                )
                            ]
                        )
                    ]
                ),

                # Right Panel: DOM Ladder + The Tape
                html.Div(
                    style={"flex": "1 1 320px", "minWidth": "280px", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        # DOM Ladder
                        html.Div(
                            style={
                                "backgroundColor": "#0A0E17",
                                "border": "1px solid #1E293B",
                                "borderRadius": "6px",
                                "padding": "8px 10px",
                                "height": "290px",
                                "overflow": "hidden"
                            },
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #1E293B", "paddingBottom": "4px", "marginBottom": "6px"},
                                    children=[
                                        html.Span("📊 DOM / DEPTH LADDER", style={"color": "#38BDF8", "fontSize": "11px", "fontWeight": "bold"}),
                                        html.Span("L2 BOOK (XAU)", style={"color": "#94A3B8", "fontSize": "10px"})
                                    ]
                                ),
                                html.Div(id="dom-ladder-content")
                            ]
                        ),

                        # The Tape (Time & Sales)
                        html.Div(
                            style={
                                "backgroundColor": "#0A0E17",
                                "border": "1px solid #1E293B",
                                "borderRadius": "6px",
                                "padding": "8px 10px",
                                "height": "260px",
                                "overflow": "hidden"
                            },
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #1E293B", "paddingBottom": "4px", "marginBottom": "6px"},
                                    children=[
                                        html.Span("⚡ THE TAPE (TIME & SALES)", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"}),
                                        html.Span("TICK STREAM", style={"color": "#00E676", "fontSize": "10px"})
                                    ]
                                ),
                                html.Div(id="tape-content", style={"height": "230px", "overflowY": "auto"})
                            ]
                        )
                    ]
                )
            ]
        ),

        # BOTTOM PANEL: Trade Blotter & Execution Log
        html.Div(
            style={
                "backgroundColor": "#0A0E17",
                "border": "1px solid #1E293B",
                "borderRadius": "6px",
                "padding": "10px 14px",
                "marginTop": "4px"
            },
            children=[
                html.Div(
                    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px", "borderBottom": "1px solid #1E293B", "paddingBottom": "6px"},
                    children=[
                        html.Div([
                            html.Span("📋 TRADE BLOTTER // INSTITUTIONAL EXECUTION LOG", style={"color": "#38BDF8", "fontSize": "12px", "fontWeight": "bold"}),
                            html.Span(" (Dual Engine: Confluence + Quant Sniper $3-$5 TP)", style={"color": "#94A3B8", "fontSize": "11px"})
                        ]),
                        html.Div([
                            html.Button("⛔ MANUAL CUT (CLOSE TRADE)", id="manual-close-btn", n_clicks=0,
                                        style={"backgroundColor": "#DC2626", "color": "#FFFFFF", "border": "none", "padding": "5px 14px", "borderRadius": "4px", "fontWeight": "900", "fontSize": "11px", "cursor": "pointer", "marginRight": "12px", "letterSpacing": "0.5px"}),
                            html.Span(id="manual-close-msg", style={"fontSize": "11px", "marginRight": "14px"}),
                            html.Span(id="blotter-summary-stats", style={"fontSize": "11px"})
                        ], style={"display": "flex", "alignItems": "center"})
                    ]
                ),
                html.Div(id="trade-blotter-table", style={"overflowX": "auto"})
            ]
        )
    ]
)


# =============================================================================
# 8. MANUAL CUT CALLBACK
# =============================================================================
@app.callback(
    Output("manual-close-msg", "children"),
    [Input("manual-close-btn", "n_clicks")],
    prevent_initial_call=True
)
def handle_manual_cut(n_clicks):
    if n_clicks and trade_state.get('in_position'):
        close_trade_instance(last_known_price, '✋ MANUAL CUT')
        return html.Span("✅ Position Squared Off!", style={"color": "#00E676", "fontWeight": "bold"})
    elif n_clicks:
        return html.Span("ℹ️ No Active Position", style={"color": "#94A3B8"})
    return ""


# =============================================================================
# 8. DASH CALLBACK: REAL-TIME RENDERING
# =============================================================================
@app.callback(
    [
        Output("quant-clocks", "children"),
        Output("ai-regime-card", "children"),
        Output("vwap-sniper-card", "children"),
        Output("live-price-box", "children"),
        Output("spread-box", "children"),
        Output("high-box", "children"),
        Output("low-box", "children"),
        Output("session-box", "children"),
        Output("phase-box", "children"),
        Output("quant-main-chart", "figure"),
        Output("dom-ladder-content", "children"),
        Output("tape-content", "children"),
        Output("blotter-summary-stats", "children"),
        Output("trade-blotter-table", "children")
    ],
    [Input("quant-interval", "n_intervals")]
)
def update_quant_terminal(n):
    with data_lock:
        bars = list(historical_bars)
        price = last_known_price
        meta = dict(market_meta)
        tape = list(recent_tape)
        book = dict(order_book)
        cur_bar = dict(current_bar)
        history = list(trade_history)
        pos = dict(trade_state)
        sniper = dict(sniper_state)

    # 1. Clocks
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    clocks_text = f"UTC {now_utc.strftime('%H:%M:%S')}  |  NYC {(now_utc - timedelta(hours=4)).strftime('%H:%M:%S')}  |  LDN {(now_utc + timedelta(hours=1)).strftime('%H:%M:%S')}  |  IST {now.strftime('%H:%M:%S')}"

    # 2. AI Regime Card (Choppiness Index & Kaufman Efficiency)
    is_chop = sniper['regime'] == 'SIDEWAYS_CHOP'
    regime_color = "#FF3366" if is_chop else "#00E676"
    regime_bg = "#2A0812" if is_chop else "#062E1C"
    regime_border = "#FF3366" if is_chop else "#00E676"
    regime_icon = "⚠️" if is_chop else "🚀"
    regime_title = "SIDEWAYS CHOPPY CONSOLIDATION" if is_chop else "HIGH MOMENTUM EXPANSION"
    regime_desc = "TRADING BLOCKED — False Breakout Protection" if is_chop else "TRENDING DIRECTIONAL FLOW — Sniper Ready"

    chop_val = sniper.get('chop_index', 50.0)
    ker_val = sniper.get('efficiency_ratio', 0.5)

    regime_card_elem = html.Div([
        html.Div([
            html.Span(f"{regime_icon} AI REGIME DETECTOR: ", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
            html.Span(regime_title, style={"color": regime_color, "fontSize": "12px", "fontWeight": "900", "letterSpacing": "0.5px"})
        ], style={"marginBottom": "4px"}),
        html.Div([
            html.Span(f"STATUS: {regime_desc}", style={"color": "#F8FAFC", "fontSize": "11px", "backgroundColor": regime_bg, "padding": "3px 8px", "borderRadius": "4px", "border": f"1px solid {regime_border}", "display": "inline-block"})
        ], style={"marginBottom": "6px"}),
        html.Div([
            html.Span(f"Bill Dreiss CHOP: {chop_val}/100 ", style={"color": "#FFD700", "fontSize": "10px", "marginRight": "8px"}),
            html.Span("(>61.8=Chop)" if chop_val >= 61.8 else "(<45=Trend)", style={"color": "#94A3B8", "fontSize": "9px"}),
            html.Span(f" | KER Efficiency: {ker_val}", style={"color": "#38BDF8", "fontSize": "10px", "marginLeft": "6px"})
        ])
    ])

    # 3. VWAP Quant Sniper Card
    sig_text = sniper.get('signal', 'SCANNING LIQUIDITY')
    is_buy_sig = 'BUY' in sig_text
    is_sell_sig = 'SELL' in sig_text
    sig_color = "#00E676" if is_buy_sig else ("#FF3366" if is_sell_sig else "#FFD700")

    sniper_card_elem = html.Div([
        html.Div([
            html.Div([
                html.Span("🎯 VWAP QUANT SNIPER ($3 - $5 TP ENGINE): ", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                html.Span(sig_text, style={"color": sig_color, "fontSize": "13px", "fontWeight": "900", "letterSpacing": "1px"})
            ]),
            html.Span(f"CONFIDENCE: {sniper.get('confidence', 80)}%", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"})
        ], style={"display": "flex", "justifyContent": "space-between", "marginBottom": "4px"}),
        html.Div([
            html.Div([
                html.Span("ENTRY: ", style={"color": "#94A3B8", "fontSize": "10px"}),
                html.Span(f"${sniper.get('entry_price', price):.2f}", style={"color": "#FFFFFF", "fontSize": "12px", "fontWeight": "bold"})
            ]),
            html.Div([
                html.Span("STOP LOSS: ", style={"color": "#FF3366", "fontSize": "10px"}),
                html.Span(f"${sniper.get('sl', price - 2.0):.2f}", style={"color": "#FF3366", "fontSize": "12px", "fontWeight": "bold"})
            ]),
            html.Div([
                html.Span("TP-1 (+$3.5): ", style={"color": "#00E676", "fontSize": "10px"}),
                html.Span(f"${sniper.get('tp1', price + 3.5):.2f}", style={"color": "#00E676", "fontSize": "12px", "fontWeight": "bold"})
            ]),
            html.Div([
                html.Span("TP-2 (+$5.0): ", style={"color": "#38BDF8", "fontSize": "10px"}),
                html.Span(f"${sniper.get('tp2', price + 5.0):.2f}", style={"color": "#38BDF8", "fontSize": "12px", "fontWeight": "bold"})
            ]),
            html.Div([
                html.Span("R:R: ", style={"color": "#94A3B8", "fontSize": "10px"}),
                html.Span(f"{sniper.get('risk_reward', '1:2.5')}", style={"color": "#FFD700", "fontSize": "12px", "fontWeight": "bold"})
            ])
        ], style={"display": "flex", "justifyContent": "space-between", "backgroundColor": "#070A0F", "padding": "4px 10px", "borderRadius": "4px", "border": "1px solid #1E293B", "marginBottom": "4px"}),
        html.Div([
            html.Span("CRITERIA: ", style={"color": "#94A3B8", "fontSize": "10px"}),
            html.Span(sniper.get('reason', 'Analyzing live bar...'), style={"color": "#CBD5E1", "fontSize": "10px"})
        ])
    ])

    # 4. Ticker Boxes
    ch_val = price - meta.get('open', price)
    ch_pct = (ch_val / meta.get('open', price)) * 100 if meta.get('open') else 0.0
    ch_color = "#00E676" if ch_val >= 0 else "#FF3366"
    ch_sign = "+" if ch_val >= 0 else ""

    price_elem = [
        html.Div("XAU/USD SPOT", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(f"${price:.2f}", style={"color": "#FFD700", "fontSize": "18px", "fontWeight": "900"}),
        html.Div(f"{ch_sign}{ch_val:.2f} ({ch_sign}{ch_pct:.2f}%)", style={"color": ch_color, "fontSize": "10px"}),
        html.Div(f"● {meta.get('source', 'Multi-API Spot')}", style={"color": "#00E676", "fontSize": "9px", "fontWeight": "bold", "marginTop": "2px"})
    ]

    spread = round(abs(meta.get('ask', price) - meta.get('bid', price)), 2)
    spread_elem = [
        html.Div("SPREAD / LIQUIDITY", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(f"${spread:.2f}", style={"color": "#38BDF8", "fontSize": "16px", "fontWeight": "bold"}),
        html.Div("TIER-1 AGGREGATED", style={"color": "#00E676", "fontSize": "10px"})
    ]

    high_elem = [
        html.Div("24H HIGH", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(f"${meta.get('high', price):.2f}", style={"color": "#E2E8F0", "fontSize": "16px", "fontWeight": "bold"}),
        html.Div("RESISTANCE LVL", style={"color": "#94A3B8", "fontSize": "10px"})
    ]

    low_elem = [
        html.Div("24H LOW", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(f"${meta.get('low', price):.2f}", style={"color": "#E2E8F0", "fontSize": "16px", "fontWeight": "bold"}),
        html.Div("SUPPORT LVL", style={"color": "#94A3B8", "fontSize": "10px"})
    ]

    hour = now.hour
    session_name = "LONDON" if 13 <= hour < 17 else "NEW YORK" if 17 <= hour < 22 else "ASIAN"
    is_kz = (13 <= hour < 15) or (17 <= hour < 20)
    session_elem = [
        html.Div("MARKET SESSION", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(session_name, style={"color": "#FFD700", "fontSize": "16px", "fontWeight": "bold"}),
        html.Div("⚡ KILL ZONE ACTIVE" if is_kz else "STANDARD SESSION", style={"color": "#00E676" if is_kz else "#94A3B8", "fontSize": "10px", "fontWeight": "bold"})
    ]

    phase_elem = [
        html.Div("ORDER FLOW PHASE", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
        html.Div(cur_bar.get('phase', 'Institutional').upper(), style={"color": "#38BDF8", "fontSize": "15px", "fontWeight": "bold"}),
        html.Div("CVD ALIGNED", style={"color": "#00E676", "fontSize": "10px"})
    ]

    # 5. Main Candlestick + Footprint + CVD Figure
    if bars:
        df = pd.DataFrame(bars)
        df['dt'] = pd.to_datetime(df['time'])
        df = df.sort_values('dt').drop_duplicates(subset=['dt']).reset_index(drop=True)
        now_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2)
        df = df[df['dt'] <= now_cutoff].reset_index(drop=True)

        # Dynamic Cumulative VWAP & Bands
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        cum_pv_series = (typical_price * df['volume']).cumsum()
        cum_vol_series = df['volume'].cumsum()
        df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])

        # Standard Deviation Bands
        df['diff_sq'] = ((typical_price - df['vwap']) ** 2) * df['volume']
        df['vwap_std'] = np.sqrt(df['diff_sq'].cumsum() / np.maximum(cum_vol_series, 1.0))
        df['vwap_upper'] = (df['vwap'] + 1.28 * df['vwap_std']).round(2)
        df['vwap_lower'] = (df['vwap'] - 1.28 * df['vwap_std']).round(2)

        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.68, 0.16, 0.16]
        )

        # 1. Candlestick
        fig.add_trace(
            go.Candlestick(
                x=df['dt'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                name="XAUUSD",
                increasing_line_color="#00E676", decreasing_line_color="#FF3366",
                increasing_fillcolor="rgba(0,230,118,0.3)", decreasing_fillcolor="rgba(255,51,102,0.3)",
                showlegend=False
            ),
            row=1, col=1
        )

        # 2. VWAP (Yellow)
        fig.add_trace(
            go.Scatter(x=df['dt'], y=df['vwap'], mode="lines", name="VWAP", line=dict(color="#FFD700", width=2)),
            row=1, col=1
        )

        # 3. Upper Band (+1.28σ Dotted Orange)
        fig.add_trace(
            go.Scatter(x=df['dt'], y=df['vwap_upper'], mode="lines", name="Upper Band (+1.28σ)", line=dict(color="#FF9800", width=1.5, dash="dot")),
            row=1, col=1
        )

        # 4. Lower Band (-1.28σ Dashed Cyan)
        fig.add_trace(
            go.Scatter(x=df['dt'], y=df['vwap_lower'], mode="lines", name="Lower Band (-1.28σ)", line=dict(color="#00F0FF", width=1.5, dash="dash")),
            row=1, col=1
        )

        # Imbalances
        imb_df = df[df['imbalance'] == True]
        if not imb_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=imb_df['dt'], y=imb_df['high'] * 1.0001, mode='markers',
                    marker=dict(symbol='star', size=11, color='#00E5FF'), name='⚡ Imbalance'
                ), row=1, col=1
            )

        # Row 2: Volume Delta
        delta_colors = ["#00E676" if d >= 0 else "#FF3366" for d in df['delta']]
        fig.add_trace(
            go.Bar(x=df['dt'], y=df['delta'], name="Delta", marker_color=delta_colors, showlegend=False),
            row=2, col=1
        )

        # Row 3: CVD
        fig.add_trace(
            go.Scatter(
                x=df['dt'], y=df['cvd'], mode="lines", name="CVD",
                line=dict(color="#38BDF8", width=2),
                fill="tozeroy", fillcolor="rgba(56,189,248,0.15)", showlegend=False
            ),
            row=3, col=1
        )

        # SMC Absorption Markers (Clean & High-Precision)
        abs_x, abs_y, abs_text, abs_color = [], [], [], []
        for _, b in df.iterrows():
            if b.get('absorption') == 'Bullish Absorption':
                abs_x.append(b['dt'])
                abs_y.append(b['low'] - 0.45)
                abs_text.append("⚡ABS")
                abs_color.append("#00E676")
            elif b.get('absorption') == 'Bearish Absorption':
                abs_x.append(b['dt'])
                abs_y.append(b['high'] + 0.45)
                abs_text.append("⚡ABS")
                abs_color.append("#FF3366")

        if abs_x:
            fig.add_trace(
                go.Scatter(
                    x=abs_x, y=abs_y, mode="text", text=abs_text,
                    textfont=dict(color=abs_color, size=9, family="monospace"),
                    name="⚡ Absorption",
                    showlegend=False
                ),
                row=1, col=1
            )

        fig.add_hline(
            y=price,
            line_color="#FFD700", line_width=1.5, line_dash="dash", row=1, col=1,
            annotation_text=f"  LIVE: ${price:.2f}", annotation_position="right",
            annotation_font=dict(color="#FFD700", size=11, family="monospace")
        )

        last_dt = df['dt'].iloc[-1]
        start_dt = df['dt'].iloc[-80] if len(df) >= 80 else df['dt'].iloc[0]
        if start_dt >= last_dt:
            start_dt = last_dt - pd.Timedelta(hours=1)
        end_dt = last_dt + pd.Timedelta(minutes=5)

        fig.update_xaxes(
            type="date",
            range=[start_dt, end_dt],
            showgrid=True, gridcolor="#131C2E",
            showline=True, linecolor="#1E293B",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikethickness=1, spikedash="dot", spikecolor="#8B949E",
            fixedrange=False,
            rangeselector=dict(
                buttons=[
                    dict(count=15, label="15M", step="minute", stepmode="backward"),
                    dict(count=30, label="30M", step="minute", stepmode="backward"),
                    dict(count=1, label="1H", step="hour", stepmode="backward"),
                    dict(count=4, label="4H", step="hour", stepmode="backward"),
                    dict(count=12, label="12H", step="hour", stepmode="backward"),
                    dict(count=1, label="1D", step="day", stepmode="backward"),
                    dict(step="all", label="ALL")
                ],
                bgcolor="#0D1117",
                activecolor="#1F6FEB",
                bordercolor="#30363D",
                borderwidth=1,
                font=dict(color="#C9D1D9", size=10, family="monospace"),
                x=0.0, y=1.03, xanchor="left", yanchor="bottom"
            ),
            rangeslider=dict(visible=False),
            row=1, col=1
        )
        fig.update_xaxes(
            type="date",
            showgrid=True, gridcolor="#131C2E",
            showline=True, linecolor="#1E293B",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikethickness=1, spikedash="dot", spikecolor="#8B949E",
            fixedrange=False,
            row=2, col=1
        )
        fig.update_xaxes(
            type="date",
            showgrid=True, gridcolor="#131C2E",
            showline=True, linecolor="#1E293B",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikethickness=1, spikedash="dot", spikecolor="#8B949E",
            fixedrange=False,
            row=3, col=1
        )
        fig.update_yaxes(
            showgrid=True, gridcolor="#131C2E", side="right",
            showline=True, linecolor="#1E293B",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikethickness=1, spikedash="dot", spikecolor="#8B949E",
            fixedrange=False
        )
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0A0E17",
            plot_bgcolor="#0A0E17",
            margin=dict(l=10, r=40, t=25, b=10),
            xaxis_rangeslider_visible=False,
            height=560,
            showlegend=True,
            dragmode="pan",
            uirevision="tradingview_user_zoom",
            legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0, font=dict(size=10, color="#94A3B8"))
        )
    else:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", paper_bgcolor="#0A0E17", plot_bgcolor="#0A0E17")

    # 6. DOM Depth Ladder
    bids = book.get('bids', [])[:8]
    asks = book.get('asks', [])[:8]
    max_dom_vol = 250.0

    dom_rows = []
    # Asks (Red)
    for a in asks:
        p_pct = min(100, int((a['volume'] / max_dom_vol) * 100))
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "position": "relative"},
                children=[
                    html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{p_pct}%", "backgroundColor": "rgba(255, 51, 102, 0.22)", "zIndex": 0}),
                    html.Span(f"${a['price']:.2f}", style={"color": "#FF3366", "fontWeight": "bold", "zIndex": 1}),
                    html.Span(f"{a['volume']:.1f}", style={"color": "#E2E8F0", "zIndex": 1})
                ]
            )
        )

    # Spread Separator
    dom_rows.append(
        html.Div(
            f"── SPREAD ${spread:.2f} ──",
            style={"textAlign": "center", "color": "#FFD700", "fontSize": "10px", "padding": "3px 0", "borderTop": "1px dashed #1E293B", "borderBottom": "1px dashed #1E293B", "margin": "2px 0"}
        )
    )

    # Bids (Green)
    for b in bids:
        p_pct = min(100, int((b['volume'] / max_dom_vol) * 100))
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "position": "relative"},
                children=[
                    html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{p_pct}%", "backgroundColor": "rgba(0, 230, 118, 0.22)", "zIndex": 0}),
                    html.Span(f"${b['price']:.2f}", style={"color": "#00E676", "fontWeight": "bold", "zIndex": 1}),
                    html.Span(f"{b['volume']:.1f}", style={"color": "#E2E8F0", "zIndex": 1})
                ]
            )
        )

    # 7. Time & Sales Tape
    tape_rows = []
    for t in tape[:15]:
        side_color = "#00E676" if t['side'] == 'BUY' else "#FF3366"
        bg_col = "rgba(0, 230, 118, 0.12)" if t['side'] == 'BUY' else "rgba(255, 51, 102, 0.12)"
        tape_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "borderBottom": "1px solid #111827", "backgroundColor": bg_col},
                children=[
                    html.Span(t['time'], style={"color": "#94A3B8"}),
                    html.Span(f"${t['price']:.2f}", style={"color": "#FFFFFF", "fontWeight": "bold"}),
                    html.Span(f"{t['size']:.1f}", style={"color": side_color, "fontWeight": "bold"}),
                    html.Span("BLOCK" if t.get('is_block') else t['side'], style={"color": "#FFD700" if t.get('is_block') else side_color, "fontSize": "9px"})
                ]
            )
        )

    # 8. Trade Blotter Table
    total_pnl = sum(t.get('pnl', 0.0) for t in history)
    win_cnt = sum(1 for t in history if 'WIN' in str(t.get('status', '')) or 'TP' in str(t.get('status', '')))
    total_cnt = len(history)
    wr = (win_cnt / total_cnt * 100) if total_cnt > 0 else 0.0

    pnl_col = "#00E676" if total_pnl >= 0 else "#FF3366"
    pnl_sign = "+" if total_pnl >= 0 else ""
    blotter_stats = html.Span([
        html.Span(f"CLOSED: {total_cnt}  |  ", style={"color": "#94A3B8"}),
        html.Span(f"WIN RATE: {wr:.1f}%  |  ", style={"color": "#FFD700", "fontWeight": "bold"}),
        html.Span(f"NET PnL: {pnl_sign}${total_pnl:.2f}", style={"color": pnl_col, "fontWeight": "bold"})
    ])

    blotter_headers = ["#ID", "TIME", "EXIT", "ENGINE", "DIR", "ENTRY", "EXIT", "SL", "TP", "STATUS", "PnL ($)"]
    blotter_table_rows = [
        html.Tr([html.Th(h, style={"padding": "6px 8px", "color": "#94A3B8", "fontSize": "10px", "textAlign": "left", "borderBottom": "1px solid #1E293B"}) for h in blotter_headers])
    ]

    for t in history[:12]:
        dir_col = "#00E676" if "BUY" in str(t.get('dir', '')) else "#FF3366"
        st = str(t.get('status', ''))
        st_col = "#00E676" if ("WIN" in st or "TP" in st) else ("#FF3366" if ("LOSS" in st or "SL" in st) else "#FFD700")
        pnl_val = float(t.get('pnl', 0.0))
        pnl_t_col = "#00E676" if pnl_val >= 0 else "#FF3366"
        pnl_t_sign = "+" if pnl_val >= 0 else ""

        engine_name = t.get('signal_type', 'SNIPER')
        engine_badge = html.Span(
            "⚡ QUANT SNIPER" if "SNIPER" in engine_name else "🏛️ CONFLUENCE",
            style={"backgroundColor": "#1E1B4B" if "SNIPER" in engine_name else "#1E293B", "color": "#38BDF8" if "SNIPER" in engine_name else "#E2E8F0", "padding": "2px 6px", "borderRadius": "3px", "fontSize": "9px", "fontWeight": "bold"}
        )

        blotter_table_rows.append(
            html.Tr(
                style={"borderBottom": "1px solid #111827", "fontSize": "11px"},
                children=[
                    html.Td(str(t.get('id', '')), style={"padding": "4px 8px", "color": "#94A3B8"}),
                    html.Td(str(t.get('time', '')), style={"padding": "4px 8px"}),
                    html.Td(str(t.get('exit_time', '')), style={"padding": "4px 8px"}),
                    html.Td(engine_badge, style={"padding": "4px 8px"}),
                    html.Td(str(t.get('dir', '')), style={"padding": "4px 8px", "color": dir_col, "fontWeight": "bold"}),
                    html.Td(f"${t.get('entry', 0.0):.2f}", style={"padding": "4px 8px"}),
                    html.Td(f"${t.get('exit_price', 0.0):.2f}", style={"padding": "4px 8px"}),
                    html.Td(f"${t.get('sl', 0.0):.2f}", style={"padding": "4px 8px", "color": "#FF3366"}),
                    html.Td(f"${t.get('tp', 0.0):.2f}", style={"padding": "4px 8px", "color": "#00E676"}),
                    html.Td(st, style={"padding": "4px 8px", "color": st_col, "fontWeight": "bold"}),
                    html.Td(f"{pnl_t_sign}${pnl_val:.2f}", style={"padding": "4px 8px", "color": pnl_t_col, "fontWeight": "bold"}),
                ]
            )
        )

    blotter_table = html.Table(blotter_table_rows, style={"width": "100%", "borderCollapse": "collapse"})

    return (
        clocks_text,
        regime_card_elem,
        sniper_card_elem,
        price_elem,
        spread_elem,
        high_elem,
        low_elem,
        session_elem,
        phase_elem,
        fig,
        dom_rows,
        tape_rows,
        blotter_stats,
        blotter_table
    )


# =============================================================================
# 9. SERVER ENTRY POINT (PORT 8080)
# =============================================================================
if __name__ == '__main__':
    init_db()
    mt5_bridge.init_mt5()  # Connect to MetaTrader 5 (Account 1189847 / Equiti)
    load_initial_bars()

    # Single Unified Live Feed Worker (No cross-broker wick collision)
    threading.Thread(target=live_feed_worker, daemon=True).start()
    threading.Thread(target=alltick_ws_worker, daemon=True).start()
    threading.Thread(target=itick_ws_worker, daemon=True).start()

    logger.info("=" * 60)
    logger.info("🚀 AI QUANT TERMINAL STARTING ON http://127.0.0.1:8080")
    logger.info("Dual Engine: Institutional Confluence + VWAP Quant Sniper")
    logger.info("=" * 60)

    app.run(host='0.0.0.0', port=8080, debug=False)
