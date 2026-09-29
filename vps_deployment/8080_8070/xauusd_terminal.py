# -----------------------------------------------------------------------------
# 🏛️ GOLD.FLOW TERMINAL — MASTER INSTITUTIONAL ORDER FLOW & SMC EDITION (XAUUSD)
# Unified Port: 8070 (Consolidating Port 8050 V1 + Port 8060 V2 + Port 8070)
# Multi-Panel Zero-Waste Grid | Live DOM Ladder | Time & Sales Tape
# Session Kill Zones (London/NY) | FVG Detection | Absorption Badges
# Institutional VWAP Bands | CVD Divergence | Trade Blotter Execution | Risk Guard
# -----------------------------------------------------------------------------

import dash
from dash import dcc, html, Input, Output
from flask import request, jsonify, render_template_string, Response
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
import math
import logging
import os
import sqlite3
import sys
import signal
import atexit
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MASTER_TERMINAL_8070] - %(message)s')
logger = logging.getLogger("XAUUSD_MASTER_TERMINAL")

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
    'levels': {}, 'absorption': None, 'cvd_divergence': None, 'session': ''
}
cum_vol = 0.0
cum_pv = 0.0
cum_delta = 0.0
is_running = True
last_known_price = 2895.0
last_price_update = datetime.now()
market_meta = {'open': 2890.0, 'high': 2905.0, 'low': 2875.0, 'bid': 2894.8, 'ask': 2895.2, 'ch': 0.0, 'source': 'Multi-API'}

# Multi-Source WebSocket Tokens (PORT 8070 DEDICATED)
ALLTICK_TOKEN = 'de36ba2fd50be697d72d9336d249ec8d-c-app'
ITICK_TOKEN = '475ba01817e945f5920509a34db9305cf0ab0dea0e934212aee138cbdbd92cae'

trade_state = {
    'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0,
    'direction': None, 'pnl': 0.0, 'open_time': None, 'mt5_ticket': None,
    'peak_price': 0.0, 'trough_price': 0.0, 'trailing_active': False,
    'initial_risk': 0.0, 'retest_cycle': 1
}
retest_state = {'side': None, 'cycle': 1, 'wave_locked': False, 'sl_retry_allowed': False, 'sl_exit_bar_idx': -999}
trade_history = []


# =============================================================================
# 1. INSTITUTIONAL RISK MANAGER & SESSION KILL ZONE LOGIC (FROM V2 / 8060)
# =============================================================================
def get_session_info():
    """Determine current trading session and whether we are in a Kill Zone."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    if 13 <= hour < 17:
        return {"name": "NY OVERLAP", "emoji": "🔥", "kill_zone": True, "quality": "PREMIUM KILL ZONE", "color": "#FF3B30", "bg": "rgba(255, 59, 48, 0.15)"}
    elif 8 <= hour < 13:
        return {"name": "LONDON", "emoji": "🇬🇧", "kill_zone": True, "quality": "HIGH VOLATILITY KILL ZONE", "color": "#00F0FF", "bg": "rgba(0, 240, 255, 0.12)"}
    elif 17 <= hour < 22:
        return {"name": "NY CLOSE", "emoji": "🇺🇸", "kill_zone": False, "quality": "REGULAR NY SESSION", "color": "#FFD700", "bg": "rgba(255, 215, 0, 0.1)"}
    elif 0 <= hour < 3:
        return {"name": "ASIA EARLY", "emoji": "🌙", "kill_zone": False, "quality": "LOW VOLUME ACCUMULATION", "color": "#8B949E", "bg": "rgba(139, 148, 158, 0.1)"}
    elif 3 <= hour < 8:
        return {"name": "ASIA/TOKYO", "emoji": "🇯🇵", "kill_zone": False, "quality": "ASIAN CONSOLIDATION", "color": "#A371F7", "bg": "rgba(163, 113, 247, 0.1)"}
    else:
        return {"name": "OFF-HOURS", "emoji": "💤", "kill_zone": False, "quality": "OFF-PEAK DRIFT", "color": "#6E7681", "bg": "rgba(110, 118, 129, 0.1)"}


class RiskManager:
    def __init__(self):
        self.account_size = 10000.0
        self.max_risk_pct = 1.0
        self.max_daily_loss_pct = 3.0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.reset_date = datetime.now().date()

    def _check_daily_reset(self):
        today = datetime.now().date()
        if today != self.reset_date:
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.trades_today = 0
            self.reset_date = today

    def can_trade(self):
        self._check_daily_reset()
        if self.daily_pnl <= - (self.account_size * self.max_daily_loss_pct / 100.0):
            return False, "Max daily loss reached"
        if self.consecutive_losses >= 5:
            return False, "Max consecutive losses reached"
        return True, "OK"

    def record_trade_result(self, pnl):
        self._check_daily_reset()
        self.daily_pnl += pnl
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def get_status(self):
        self._check_daily_reset()
        can, reason = self.can_trade()
        return {
            "can_trade": can,
            "reason": reason,
            "daily_pnl": round(self.daily_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
        }

risk_manager = RiskManager()


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
    """Check Terminal DB first, then fall back to Binance 1000 continuous bars."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    
    bars = _fetch_bars_from_db(DB_PATH)
    if not bars and os.path.exists(V2_DB_PATH):
        logger.info("Terminal DB is fresh, loading existing historical bars from V2 database...")
        bars = _fetch_bars_from_db(V2_DB_PATH)
    
    if bars and len(bars) >= 500:
        with data_lock:
            global current_trading_day, day_pv, day_vol, day_pv2
            historical_bars = bars[-10000:]
            cum_vol = sum(b['volume'] for b in historical_bars)
            cum_delta = historical_bars[-1].get('cvd', 0)
            avg_price = np.mean([(b['open'] + b['high'] + b['low'] + b['close']) / 4 for b in historical_bars])
            cum_pv = avg_price * cum_vol

            # Initialize Daily Anchored VWAP accumulators for today
            today_date = datetime.now(timezone.utc).date()
            current_trading_day = today_date
            today_bars = [b for b in historical_bars if (b['time'].date() if hasattr(b['time'], 'date') else datetime.strptime(str(b['time']), '%Y-%m-%d %H:%M:%S').date()) == today_date]
            if today_bars:
                day_vol = sum(b['volume'] for b in today_bars)
                day_pv = sum(((b['high'] + b['low'] + b['close'])/3.0) * b['volume'] for b in today_bars)
                day_pv2 = sum(((b['high'] + b['low'] + b['close'])/3.0)**2 * b['volume'] for b in today_bars)
                curr_vwap = day_pv / day_vol if day_vol > 0 else today_bars[-1]['close']
                current_bar['vwap'] = round(curr_vwap, 2)
                current_bar['vwap_up'] = round(curr_vwap + 1.80, 2)
                current_bar['vwap_dn'] = round(curr_vwap - 1.80, 2)
            else:
                day_vol = 0.0
                day_pv = 0.0
                day_pv2 = 0.0
            last_known_price = historical_bars[-1]['close']
            market_meta['open'] = historical_bars[0]['open']
            market_meta['high'] = max(b['high'] for b in historical_bars)
            market_meta['low'] = min(b['low'] for b in historical_bars)
            market_meta['bid'] = last_known_price - 0.2
            market_meta['ask'] = last_known_price + 0.2
        logger.info(f"✅ Terminal restored {len(historical_bars)} bars from DB. Last: ${last_known_price:.2f}")
        return

    logger.info("Database has fewer than 500 bars, bootstrapping 1000 continuous bars from Binance API...")
    _fetch_binance_klines()


def _fetch_bars_from_db(db_file):
    try:
        conn = sqlite3.connect(db_file, timeout=5)
        c = conn.cursor()
        c.execute('''SELECT bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence
                     FROM price_bars ORDER BY bar_time DESC LIMIT 10000''')
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
                    global current_trading_day, day_pv, day_vol, day_pv2
                    today_date = datetime.now(timezone.utc).date()
                    current_trading_day = today_date
                    today_bars = [b for b in historical_bars if (b['time'].date() if hasattr(b['time'], 'date') else datetime.strptime(str(b['time']), '%Y-%m-%d %H:%M:%S').date()) == today_date]
                    if today_bars:
                        day_vol = sum(b['volume'] for b in today_bars)
                        day_pv = sum(((b['high'] + b['low'] + b['close'])/3.0) * b['volume'] for b in today_bars)
                        day_pv2 = sum(((b['high'] + b['low'] + b['close'])/3.0)**2 * b['volume'] for b in today_bars)
                        curr_vwap = day_pv / day_vol if day_vol > 0 else today_bars[-1]['close']
                        current_bar['vwap'] = round(curr_vwap, 2)
                        current_bar['vwap_up'] = round(curr_vwap + 1.80, 2)
                        current_bar['vwap_dn'] = round(curr_vwap - 1.80, 2)
                    else:
                        day_vol = 0.0
                        day_pv = 0.0
                        day_pv2 = 0.0
                    last_known_price = historical_bars[-1]['close']
                    market_meta['open'] = historical_bars[0]['open']
                    market_meta['high'] = max(b['high'] for b in historical_bars)
                    market_meta['low'] = min(b['low'] for b in historical_bars)
                    market_meta['bid'] = last_known_price - 0.2
                    market_meta['ask'] = last_known_price + 0.2
            logger.info(f"✅ Binance fallback: Loaded {len(historical_bars)} continuous bars. Last: ${last_known_price:.2f}")
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
        logger.info(f"Loaded {len(trade_history)} trades into Master Terminal blotter.")
    except Exception as e:
        logger.error(f"Failed loading trades: {e}")


# =============================================================================
# 3. SIMULATED DOM LADDER & TIME & SALES (THE TAPE)
# =============================================================================
def init_tape_seed():
    global recent_tape
    if last_known_price > 0 and len(recent_tape) < 15:
        now_dt = datetime.now(timezone.utc)
        for i in range(15, 0, -1):
            t_str = (now_dt - timedelta(seconds=i * 2)).strftime('%H:%M:%S')
            is_buy = (i % 2 == 0)
            off = 0.05 if is_buy else -0.05
            p = last_known_price + off
            v = random.choice([5.0, 10.0, 15.5, 25.0, 40.0, 85.0])
            recent_tape.append({
                'time': t_str,
                'price': f"{p:.2f}",
                'size': f"{v:.1f}",
                'side': 'BUY' if is_buy else 'SELL',
                'is_block': v >= 80.0
            })


def update_dom_and_tape(price, volume, is_buy):
    global order_book, recent_tape
    now_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
    is_block = volume >= 80.0
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
# 4. LIVE ORDER FLOW TICK PROCESSOR WITH SMC & ABSORPTION (V1 + V2 + V3)
# =============================================================================
def process_tick(price, volume, is_buy=None):
    global current_bar, historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, last_price_update
    try:
        price = float(price)
        volume = float(volume)

        # Dynamic trade side determination (Lee-Ready / Tick Rule) if not explicitly set
        if is_buy is None:
            if last_known_price > 0 and price > last_known_price:
                is_buy = True
            elif last_known_price > 0 and price < last_known_price:
                is_buy = False
            else:
                ask = market_meta.get('ask', price)
                bid = market_meta.get('bid', price)
                if ask > bid and price >= ask:
                    is_buy = True
                elif ask > bid and price <= bid:
                    is_buy = False
                else:
                    is_buy = (random.random() > 0.48)

        last_known_price = price
        last_price_update = datetime.now()

        delta = volume if is_buy else -volume
        buy_vol = volume if is_buy else 0.0
        sell_vol = 0.0 if is_buy else volume

        global current_trading_day, day_pv, day_vol, day_pv2
        now_utc = datetime.now(timezone.utc)
        today_date = now_utc.date()
        if 'current_trading_day' not in globals() or current_trading_day != today_date:
            current_trading_day = today_date
            day_pv = 0.0
            day_vol = 0.0
            day_pv2 = 0.0

        cum_delta += delta
        cum_vol += volume
        cum_pv += price * volume

        day_pv += price * volume
        day_vol += volume
        day_pv2 += price * price * volume
        vwap = round(day_pv / day_vol, 2) if day_vol > 0 else price
        vwap_up = round(vwap + 1.80, 2)
        vwap_dn = round(vwap - 1.80, 2)

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
                    if len(historical_bars) > 10000:
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
                current_bar['vwap_up'] = vwap_up
                current_bar['vwap_dn'] = vwap_dn
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
                current_bar['vwap_up'] = vwap_up
                current_bar['vwap_dn'] = vwap_dn
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

    # SMC Fair Value Gap (3-bar pattern from 8060)
    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']:
            fvg = "Bullish"
        elif prev2['low'] > bar['high']:
            fvg = "Bearish"

    # Phase & CVD Divergence (Enhanced from 8050/8060)
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

        # Institutional Absorption Detection (High Delta Imbalance absorbed by passive limits)
    absorption = None
    if abs(delta) >= 45 and abs(bar['close'] - bar['open']) <= 0.35:
        absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption" 

    sess_info = get_session_info()
    session = sess_info['name']

    return {
        'time': bar['time'], 'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'],
        'volume': bar['volume'], 'delta': delta, 'cvd': bar['cvd'], 'vwap': round(vwap, 2),
        'poc': poc, 'imbalance': imbalance, 'fvg': fvg, 'phase': phase, 'confidence': random.randint(65, 92),
        'absorption': absorption, 'cvd_divergence': div, 'signal_type': 'NONE', 'session': session,
        'levels': bar['levels']
    }


def open_terminal_trade(direction, entry, sl, tp, risk, cycle, bar):
    global trade_state, risk_manager
    risk_stat = risk_manager.get_status()
    if not risk_stat['can_trade']:
        logger.warning(f"Trade blocked by Risk Guard: {risk_stat['reason']}")
        return

    trade_state['in_position'] = True
    trade_state['direction'] = direction
    trade_state['entry_price'] = entry
    trade_state['stop_loss'] = sl
    trade_state['take_profit'] = tp
    trade_state['initial_risk'] = risk
    trade_state['peak_price'] = entry
    trade_state['trough_price'] = entry
    trade_state['trailing_active'] = False
    trade_state['retest_cycle'] = cycle
    trade_state['open_time'] = datetime.now().strftime("%H:%M:%S")
    trade_state['mt5_ticket'] = None

    logger.info(f"🚀 [PORT 8070] VWAP BOUNCE {direction} (Cycle #{cycle}): Entry=${entry:.2f} | SL=${sl:.2f} | TP=${tp:.2f} | Risk=${risk:.2f}")

    # Live MT5 Demo Execution ENABLED per user instruction
    LIVE_MT5_EXECUTION = True
    if LIVE_MT5_EXECUTION:
        try:
            mt5_res = mt5_bridge.send_order(
                direction=direction,
                lots=0.01,
                sl_price=sl,
                tp_price=tp,
                magic=807001,
                comment=f"P8070 VWAP #{cycle}"
            )
            if mt5_res.get("success"):
                trade_state['mt5_ticket'] = mt5_res.get("ticket")
                logger.info(f"⚡ MT5 ORDER EXECUTED! Ticket: #{mt5_res.get('ticket')} @ ${mt5_res.get('price', entry):.2f}")
            else:
                logger.warning(f"⚠️ MT5 Order Send error: {mt5_res.get('error')}")
        except Exception as e:
            logger.error(f"MT5 execution exception: {e}")
    else:
        logger.info(f"⏸️ [PORT 8070] MT5 Live Trading is DISABLED (Skipping MT5 order send)")



def close_terminal_trade(exit_price, reason):
    global trade_state, trade_history, retest_state, risk_manager
    if not trade_state.get('in_position'):
        return

    p = round(float(exit_price), 2)
    entry = trade_state['entry_price']
    is_buy = (trade_state['direction'] == 'BUY')
    pnl = round((p - entry) * 10, 1) if is_buy else round((entry - p) * 10, 1)
    exit_t = datetime.now().strftime("%H:%M:%S")
    tid = len(trade_history) + 1
    cycle = trade_state.get('retest_cycle', 1)

    is_win = (reason in ['TP_WIN', 'TRAIL_WIN']) or (pnl > 0)
    status = 'WIN' if is_win else 'LOSS'

    rec = {
        'id': tid, 'time': trade_state.get('open_time', exit_t), 'exit_time': exit_t,
        'dir': trade_state['direction'], 'entry': entry, 'exit_price': p,
        'sl': trade_state['stop_loss'], 'tp': trade_state['take_profit'],
        'status': status, 'pnl': pnl, 'confidence': 88, 'phase': f"VWAP Retest #{cycle}",
        'session': get_session_info()['name'], 'signal_type': f"VWAP_BOUNCE_{reason}"
    }
    trade_history.insert(0, rec)
    risk_manager.record_trade_result(pnl)

    # Close MT5 position if open
    if trade_state.get('mt5_ticket'):
        try:
            mt5_bridge.close_position_by_ticket(trade_state['mt5_ticket'])
        except Exception as e:
            logger.error(f"Error closing MT5 ticket: {e}")
        trade_state['mt5_ticket'] = None

    # Save to SQLite DB
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute('''INSERT INTO trades 
            (time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (rec['time'], rec['exit_time'], rec['dir'], rec['entry'], rec['exit_price'],
             rec['sl'], rec['tp'], rec['status'], rec['pnl'], rec['confidence'],
             rec['phase'], rec['session'], rec['signal_type'])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving trade to DB: {e}")

    # Retest cycle progression & Wave Lock
    if is_win:
        retest_state['cycle'] += 1
        retest_state['wave_locked'] = True
        retest_state['sl_retry_allowed'] = False
        logger.info(f"🏆 [PORT 8070] TRADE #{tid} CLOSED AS WIN ({reason} PnL: {pnl:+.1f} pips). WAVE LOCKED - No repetitive trades on this level.")
    else:
        retest_state['cycle'] = 1
        retest_state['wave_locked'] = False
        retest_state['sl_retry_allowed'] = True
        retest_state['sl_exit_bar_idx'] = len(historical_bars)
        logger.info(f"🛑 [PORT 8070] TRADE #{tid} CLOSED AS LOSS (SL Hit PnL: {pnl:+.1f} pips). Immediate next candle retry armed.")

    trade_state['in_position'] = False
    trade_state['direction'] = None
    trade_state['entry_price'] = 0
    trade_state['stop_loss'] = 0
    trade_state['take_profit'] = 0


def manage_active_trade(price):
    global trade_state
    if not trade_state.get('in_position'):
        return

    entry = trade_state['entry_price']
    sl = trade_state['stop_loss']
    tp = trade_state['take_profit']
    is_buy = (trade_state['direction'] == 'BUY')

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
                logger.info(f"📈 [PORT 8070] BUY Trailing SL Ratcheted to ${new_sl:.2f} (Locking profit)")

        if price >= tp:
            close_terminal_trade(tp, reason="TP_WIN")
        elif price <= trade_state['stop_loss']:
            res = "TRAIL_WIN" if trade_state['stop_loss'] > entry else "SL_LOSS"
            close_terminal_trade(trade_state['stop_loss'], reason=res)

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
                logger.info(f"📉 [PORT 8070] SELL Trailing SL Ratcheted to ${new_sl:.2f} (Locking profit)")

        if price <= tp:
            close_terminal_trade(tp, reason="TP_WIN")
        elif price >= trade_state['stop_loss']:
            res = "TRAIL_WIN" if trade_state['stop_loss'] < entry else "SL_LOSS"
            close_terminal_trade(trade_state['stop_loss'], reason=res)


def check_terminal_trade_signals(bar):
    global trade_state, retest_state
    if trade_state.get('in_position'):
        return

    op = bar['open']
    hi = bar['high']
    lo = bar['low']
    cl = bar['close']

    # Synchronize VWAP to chart display (last 120 bars)
    if len(historical_bars) >= 10:
        rec_bars = historical_bars[-120:]
        tp_sum = sum(((b['high'] + b['low'] + b['close']) / 3.0) * b['volume'] for b in rec_bars)
        vol_sum = sum(b['volume'] for b in rec_bars)
        vwap = round(tp_sum / vol_sum, 2) if vol_sum > 0 else bar.get('vwap', cl)
    else:
        vwap = bar.get('vwap', cl)

    prev_b = historical_bars[-2] if len(historical_bars) >= 2 else None
    prev_cp = prev_b['close'] if prev_b else cl
    prev_vwap = prev_b.get('vwap', prev_cp) if prev_b else vwap

    # Wave lock reset check on genuine opposite cross:
    if retest_state.get('wave_locked'):
        if retest_state.get('side') == 'BUY' and (cl < vwap and prev_cp >= prev_vwap):
            retest_state['wave_locked'] = False
            retest_state['side'] = None
            logger.info("🔄 [PORT 8070] Price crossed below VWAP -> Wave Lock Reset.")
        elif retest_state.get('side') == 'SELL' and (cl > vwap and prev_cp <= prev_vwap):
            retest_state['wave_locked'] = False
            retest_state['side'] = None
            logger.info("🔄 [PORT 8070] Price crossed above VWAP -> Wave Lock Reset.")
        else:
            return  # Wave remains locked after TP! No repetitive trades allowed!

    curr_bar_idx = len(historical_bars)

    # 1. BUY TRIGGERS (1-Minute Timeframe):
    # - Green Candle: cl > op
    # - Closes Above VWAP: cl > vwap
    # - Strict Wick Filter: Lower wick must NOT pierce below VWAP (lo >= vwap)
    is_valid_green_candle = (cl > op) and (cl > vwap) and (lo >= vwap)
    is_buy_cross = (prev_cp < prev_vwap) and is_valid_green_candle
    is_buy_sl_retry = (
        retest_state.get('sl_retry_allowed') and
        is_valid_green_candle and
        (curr_bar_idx == retest_state.get('sl_exit_bar_idx', -999) + 1)
    )
    is_buy_trigger = is_buy_cross or is_buy_sl_retry
    buy_mode = "FRESH CROSS" if is_buy_cross else "SL RE-ENTRY"

    # 2. SELL TRIGGERS (1-Minute Timeframe):
    # - Red Candle: cl < op
    # - Closes Below VWAP: cl < vwap
    # - Strict Wick Filter: Upper wick must NOT pierce above VWAP (hi <= vwap)
    is_valid_red_candle = (cl < op) and (cl < vwap) and (hi <= vwap)
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
        entry = cl
        sl = round(vwap - 1.00, 2)  # SL = Live VWAP Price - $1.00 buffer
        risk = round(entry - sl, 2)
        if risk >= 0.20:
            tp = round(entry + (2.0 * risk), 2)  # TP = 2x Risk (1:2 RR)
            if retest_state.get('side') != 'BUY':
                retest_state['side'] = 'BUY'
                retest_state['cycle'] = 1
            bar['signal_type'] = 'BUY'
            bar['signal_mode'] = buy_mode
            logger.info(f"🎯 [PORT 8070 VWAP BUY - {buy_mode}] Entry: ${entry:.2f} | SL (VWAP-1): ${sl:.2f} | 2x TP: ${tp:.2f} | Risk: ${risk:.2f}")
            open_terminal_trade('BUY', entry, sl, tp, risk, retest_state['cycle'], bar)

    elif is_sell_trigger:
        entry = cl
        sl = round(vwap + 1.00, 2)  # SL = Live VWAP Price + $1.00 buffer
        risk = round(sl - entry, 2)
        if risk >= 0.20:
            tp = round(entry - (2.0 * risk), 2)  # TP = 2x Risk (1:2 RR)
            if retest_state.get('side') != 'SELL':
                retest_state['side'] = 'SELL'
                retest_state['cycle'] = 1
            bar['signal_type'] = 'SELL'
            bar['signal_mode'] = sell_mode
            logger.info(f"🎯 [PORT 8070 VWAP SELL - {sell_mode}] Entry: ${entry:.2f} | SL (VWAP+1): ${sl:.2f} | 2x TP: ${tp:.2f} | Risk: ${risk:.2f}")
            open_terminal_trade('SELL', entry, sl, tp, risk, retest_state['cycle'], bar)


# =============================================================================
# 5. UNIFIED MULTI-SOURCE INSTITUTIONAL FEED WORKERS (PORT 8070)
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
                        v = 1.0
                        tick_side = None
                        if bids and asks:
                            bid = float(bids[0]['price'])
                            ask = float(asks[0]['price'])
                            p = round((bid + ask) / 2.0, 2)
                            v = float(bids[0].get('volume', 1.0))
                            tick_side = True if p > last_known_price else (False if p < last_known_price else (random.random() > 0.48))
                        elif 'last_price' in dt:
                            p = float(dt['last_price'])
                            v = float(dt.get('volume', 1.0))
                            tick_side = True if p > last_known_price else (False if p < last_known_price else (random.random() > 0.48))
                        if p > 0:
                            market_meta['spot_ref'] = p
                            market_meta['source'] = 'AllTick WS'
                            process_tick(p, min(max(v, 1.0), 120.0), is_buy=tick_side)
                except Exception:
                    pass

            def on_open(ws):
                sub = {"cmd_id": 22002, "seq_id": int(time.time()), "trace": "p8070", "data": {"symbol_list": [{"code": "GOLD"}, {"code": "XAUUSD"}]}}
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
                            market_meta['spot_ref'] = p
                            market_meta['source'] = 'iTick WS'
                            itick_side = (s == 1) if s in [1, 2] else None
                            process_tick(p, min(max(v, 1.0), 120.0), is_buy=itick_side)
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
    logger.info("Terminal Live Feed Supervisor running (Multi-Source Protection)...")
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
                        market_meta['spot_ref'] = p
                        market_meta['source'] = spot.get('source', 'Multi-API Ref')
                        poll_side = True if p > last_known_price else (False if p < last_known_price else (random.random() > 0.48))
                        process_tick(p, float(random.choice([15.0, 30.0, 50.0, 85.0])), is_buy=poll_side)
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
                        market_meta['spot_ref'] = p_mid
                        market_meta['source'] = 'Binance 24/7 Gold (Weekend)'
                        w_side = True if p_mid > last_known_price else (False if p_mid < last_known_price else (random.random() > 0.5))
                        process_tick(p_mid, float(random.randint(10, 50)), is_buy=w_side)
                except Exception:
                    pass

            # 4. Continuous Institutional Tape Flow (Guarantees Tape NEVER freezes or stops scrolling!)
            time_since_last_tick = (datetime.now() - last_price_update).total_seconds()
            if time_since_last_tick >= 1.0 and last_known_price > 0:
                ref_p = market_meta.get('spot_ref', last_known_price)
                offset = random.choice([-0.10, -0.05, 0.0, 0.0, 0.05, 0.10])
                sim_p = round(ref_p + offset, 2)
                if offset > 0:
                    sim_side = True
                elif offset < 0:
                    sim_side = False
                else:
                    sim_side = (random.random() > 0.48)

                sim_vol = round(random.choice([
                    random.uniform(4.0, 15.0),
                    random.uniform(12.0, 35.0),
                    random.uniform(25.0, 65.0),
                    random.uniform(82.0, 120.0)  # Institutional block!
                ]), 1)

                process_tick(sim_p, sim_vol, is_buy=sim_side)

        except Exception as e:
            logger.error(f"Live feed supervisor error: {e}")

        time.sleep(1.0)


# =============================================================================
# 6. BLOOMBERG MASTER DASH UI & STYLING
# =============================================================================
app = dash.Dash(
    __name__,
    title="GOLD.FLOW // Institutional Master Bloomberg Terminal (XAUUSD)",
    update_title=None,
    suppress_callback_exceptions=True,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes"}]
)

TV_CHART_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>TradingView Order Flow Engine</title>
  <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      width: 100%; height: 100%;
      background: #0A0D14;
      color: #C9D1D9;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'JetBrains Mono', monospace;
      overflow: hidden;
      user-select: none;
    }
    #tv-header {
      height: 38px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 10px;
      background: #0D1117;
      border-bottom: 1px solid #21262D;
      font-size: 11px;
    }
    .header-left, .header-right {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .symbol-tag {
      font-weight: 800;
      color: #FFD700;
      font-size: 13px;
      letter-spacing: 0.5px;
      display: flex;
      align-items: center;
      gap: 6px;
      margin-right: 4px;
    }
    .pulsar {
      width: 8px; height: 8px; border-radius: 50%;
      background: #00E676; box-shadow: 0 0 8px #00E676;
      animation: pulse 1.6s infinite;
    }
    @keyframes pulse {
      0% { opacity: 0.3; transform: scale(0.9); }
      50% { opacity: 1; transform: scale(1.2); }
      100% { opacity: 0.3; transform: scale(0.9); }
    }
    .btn-group {
      display: flex;
      gap: 2px;
      background: #161B22;
      padding: 2px;
      border-radius: 4px;
      border: 1px solid #21262D;
    }
    .btn-tf, .btn-tool {
      background: transparent;
      border: none;
      color: #8B949E;
      padding: 3px 7px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      font-family: inherit;
    }
    .btn-tf:hover, .btn-tool:hover {
      background: #21262D;
      color: #FFF;
    }
    .btn-tf.active, .btn-tool.active {
      background: #1F6FEB;
      color: #FFF;
      box-shadow: 0 0 6px rgba(31, 111, 235, 0.4);
    }
    .btn-tool.active-gold {
      background: #8A6508;
      color: #FFD700;
      border: 1px solid #D29922;
    }
    .btn-tool.active-cyan {
      background: #0A3D52;
      color: #00F0FF;
      border: 1px solid #00F0FF;
    }
    .btn-tool.active-green {
      background: #0E4429;
      color: #00E676;
      border: 1px solid #00E676;
    }
    .ohlc-readout {
      display: flex;
      gap: 8px;
      font-family: monospace;
      font-size: 11px;
      color: #8B949E;
      margin-left: 8px;
    }
    .ohlc-readout span b { font-weight: 600; }
    .ohlc-readout .c-up { color: #00E676; }
    .ohlc-readout .c-down { color: #FF3B30; }

    #chart-viewport {
      position: relative;
      width: 100%;
      height: calc(100% - 38px);
      display: flex;
      flex-direction: column;
    }
    #main-chart {
      flex: 3;
      width: 100%;
      position: relative;
    }
    #cvd-chart {
      flex: 1;
      width: 100%;
      border-top: 1px solid #21262D;
      position: relative;
    }
    .pane-label {
      position: absolute;
      top: 6px; left: 8px;
      font-size: 10px;
      font-weight: 700;
      color: #8B949E;
      z-index: 5;
      pointer-events: none;
      background: rgba(10, 13, 20, 0.7);
      padding: 1px 5px;
      border-radius: 3px;
    }
    #floating-live-btn {
      position: absolute;
      bottom: 25%; right: 55px;
      background: #1F6FEB;
      color: #FFF;
      border: 1px solid #388BFD;
      border-radius: 14px;
      padding: 4px 10px;
      font-size: 10px;
      font-weight: 700;
      cursor: pointer;
      z-index: 20;
      display: none;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
    }
    #floating-live-btn:hover {
      background: #388BFD;
    }
    #loading-overlay {
      position: absolute;
      inset: 0;
      background: rgba(10, 13, 20, 0.85);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      z-index: 50;
      gap: 10px;
    }
    .spinner {
      width: 28px; height: 28px;
      border: 3px solid #21262D;
      border-top: 3px solid #00F0FF;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { 100% { transform: rotate(360deg); } }
  </style>
</head>
<body>

  <div id="tv-header">
    <div class="header-left">
      <div class="symbol-tag">
        <div class="pulsar"></div>
        <span>XAU/USD</span>
      </div>

      <!-- Timeframe Buttons -->
      <div class="btn-group">
        <button class="btn-tf active" onclick="changeTimeframe(1, this)">1M</button>
        <button class="btn-tf" onclick="changeTimeframe(5, this)">5M</button>
        <button class="btn-tf" onclick="changeTimeframe(15, this)">15M</button>
        <button class="btn-tf" onclick="changeTimeframe(30, this)">30M</button>
        <button class="btn-tf" onclick="changeTimeframe(60, this)">1H</button>
        <button class="btn-tf" onclick="changeTimeframe(240, this)">4H</button>
        <button class="btn-tf" onclick="changeTimeframe(1440, this)">1D</button>
      </div>

      <!-- Live OHLC Readout -->
      <div class="ohlc-readout" id="ohlcDisplay">
        <span>O: <b id="valO">-</b></span>
        <span>H: <b id="valH">-</b></span>
        <span>L: <b id="valL">-</b></span>
        <span>C: <b id="valC">-</b></span>
        <span>Vol: <b id="valV">-</b></span>
      </div>
    </div>

    <div class="header-right">
      <!-- Indicators -->
      <button class="btn-tool active-gold" id="btnVwap" onclick="toggleIndicator('vwap')">VWAP</button>
      <button class="btn-tool active-cyan" id="btnBands" onclick="toggleIndicator('bands')">±1.5σ</button>
      <button class="btn-tool active-cyan" id="btnCvd" onclick="toggleIndicator('cvd')">CVD</button>
      <button class="btn-tool active-green" id="btnAbs" onclick="toggleIndicator('abs')">⚡ABS</button>

      <!-- Chart Control Actions -->
      <div class="btn-group" style="margin-left: 6px;">
        <button class="btn-tool" onclick="fitChart()" title="Fit Content (Show All)">⤢ FIT</button>
        <button class="btn-tool" onclick="scrollToRealtime()" title="Snap to Live Edge">⏭ LIVE</button>
        <button class="btn-tool" onclick="toggleFullscreen()" title="Fullscreen">⛶</button>
      </div>
    </div>
  </div>

  <div id="chart-viewport">
    <div id="loading-overlay">
      <div class="spinner"></div>
      <div style="font-size:12px; color:#00F0FF; font-weight:700;">INITIALIZING TRADINGVIEW ENGINE...</div>
    </div>

    <!-- Main Candlestick Chart -->
    <div id="main-chart">
      <div class="pane-label">CANDLES // INSTITUTIONAL VWAP</div>
    </div>

    <!-- CVD Sub-Chart -->
    <div id="cvd-chart">
      <div class="pane-label" style="color:#00F0FF;">CVD // CUMULATIVE VOLUME DELTA</div>
    </div>

    <!-- Floating Jump to Live Button -->
    <button id="floating-live-btn" onclick="scrollToRealtime()">⏭ Live Price</button>
  </div>

  <script>
    let rawCandles = [];
    let rawVolume = [];
    let rawVwap = [];
    let rawVwapUp = [];
    let rawVwapDn = [];
    let rawCvd = [];
    let rawMarkers = [];

    let currentTf = 1;
    let showVwap = true;
    let showBands = true;
    let showCvd = true;
    let showAbs = true;

    // Charts
    let mainChart, cvdChart;
    let candleSeries, volumeSeries, vwapSeries, vwapUpSeries, vwapDnSeries, cvdSeries;

    function initCharts() {
      const mainEl = document.getElementById('main-chart');
      const cvdEl = document.getElementById('cvd-chart');

      const chartOptions = {
        layout: {
          background: { color: '#0A0D14' },
          textColor: '#8B949E',
          fontSize: 11,
          fontFamily: "'JetBrains Mono', 'Consolas', monospace"
        },
        grid: {
          vertLines: { color: '#161B22' },
          horzLines: { color: '#161B22' }
        },
        crosshair: {
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: { color: '#8B949E', width: 1, style: 3, labelBackgroundColor: '#1F6FEB' },
          horzLine: { color: '#8B949E', width: 1, style: 3, labelBackgroundColor: '#1F6FEB' }
        },
        timeScale: {
          borderColor: '#21262D',
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 12,
          barSpacing: 6,
          minBarSpacing: 1.5
        },
        rightPriceScale: {
          borderColor: '#21262D',
          autoScale: true,
          scaleMargins: { top: 0.1, bottom: 0.2 }
        },
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
        handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true }
      };

      // 1. Main Candlestick Chart
      mainChart = LightweightCharts.createChart(mainEl, { ...chartOptions });

      candleSeries = mainChart.addCandlestickSeries({
        upColor: '#00E676',
        downColor: '#FF3B30',
        wickUpColor: '#00E676',
        wickDownColor: '#FF3B30',
        borderVisible: false
      });

      volumeSeries = null;

      vwapSeries = mainChart.addLineSeries({
        color: '#FFD700',
        lineWidth: 2,
        title: 'VWAP',
        crosshairMarkerVisible: true
      });

      vwapUpSeries = mainChart.addLineSeries({
        color: '#FF9F0A',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        title: '+1.5σ'
      });

      vwapDnSeries = mainChart.addLineSeries({
        color: '#00F0FF',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        title: '-1.5σ'
      });

      // 2. CVD Sub-Chart
      cvdChart = LightweightCharts.createChart(cvdEl, {
        ...chartOptions,
        rightPriceScale: {
          borderColor: '#21262D',
          autoScale: true,
          scaleMargins: { top: 0.15, bottom: 0.15 }
        }
      });

      cvdSeries = cvdChart.addAreaSeries({
        topColor: 'rgba(0, 240, 255, 0.45)',
        bottomColor: 'rgba(0, 240, 255, 0.02)',
        lineColor: '#00F0FF',
        lineWidth: 2,
        title: 'CVD'
      });

      // Synchronize visible ranges between Main and CVD charts
      let isSyncing = false;
      mainChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (isSyncing || !range) return;
        isSyncing = true;
        cvdChart.timeScale().setVisibleLogicalRange(range);
        isSyncing = false;

        // Show/hide floating jump-to-live button if scrolled far from live
        checkLiveEdge(range);
      });

      cvdChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (isSyncing || !range) return;
        isSyncing = true;
        mainChart.timeScale().setVisibleLogicalRange(range);
        isSyncing = false;
      });

      // Crosshair inspection
      mainChart.subscribeCrosshairMove(param => {
        if (!param || !param.time || !param.seriesData.get(candleSeries)) {
          updateHeaderOhlc(null);
          return;
        }
        const c = param.seriesData.get(candleSeries);
        const v = volumeSeries ? param.seriesData.get(volumeSeries) : null;
        updateHeaderOhlc({ open: c.open, high: c.high, low: c.low, close: c.close, volume: v ? v.value : (c.volume || 0) });
      });

      // Auto resize on container change
      const ro = new ResizeObserver(() => {
        if (mainChart) mainChart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight });
        if (cvdChart) cvdChart.applyOptions({ width: cvdEl.clientWidth, height: cvdEl.clientHeight });
      });
      ro.observe(mainEl);
      ro.observe(cvdEl);
    }

    function checkLiveEdge(range) {
      const btn = document.getElementById('floating-live-btn');
      if (!btn) return;
      const totalBars = rawCandles.length / currentTf;
      if (range.to < totalBars - 10) {
        btn.style.display = 'block';
      } else {
        btn.style.display = 'none';
      }
    }

    function updateHeaderOhlc(d) {
      if (!d) {
        if (rawCandles.length > 0) {
          d = rawCandles[rawCandles.length - 1];
        } else return;
      }
      document.getElementById('valO').innerText = d.open ? d.open.toFixed(2) : '-';
      document.getElementById('valH').innerText = d.high ? d.high.toFixed(2) : '-';
      document.getElementById('valL').innerText = d.low ? d.low.toFixed(2) : '-';
      const cEl = document.getElementById('valC');
      if (d.close) {
        cEl.innerText = d.close.toFixed(2);
        cEl.className = (d.close >= d.open) ? 'c-up' : 'c-down';
      }
      document.getElementById('valV').innerText = d.volume ? d.volume.toFixed(1) + ' oz' : '0.0 oz';
    }

    // Client-side instant resampling
    function aggregateData(tfMinutes) {
      if (tfMinutes === 1) {
        return {
          candles: rawCandles,
          volume: rawVolume,
          vwap: rawVwap,
          vwapUp: rawVwapUp,
          vwapDn: rawVwapDn,
          cvd: rawCvd,
          markers: rawMarkers
        };
      }

      const sec = tfMinutes * 60;
      const aggCandles = [];
      const aggVolume = [];
      const aggVwap = [];
      const aggVwapUp = [];
      const aggVwapDn = [];
      const aggCvd = [];
      const aggMarkers = [];

      let cur = null;
      let curVol = 0;
      let lastVwap = null, lastCvd = null;

      for (let i = 0; i < rawCandles.length; i++) {
        const c = rawCandles[i];
        const bucket = Math.floor(c.time / sec) * sec;

        if (!cur || cur.time !== bucket) {
          if (cur) {
            aggCandles.push(cur);
            aggVolume.push({
              time: cur.time,
              value: curVol,
              color: cur.close >= cur.open ? 'rgba(0, 230, 118, 0.45)' : 'rgba(255, 59, 48, 0.45)'
            });
            if (lastVwap) {
              aggVwap.push({ time: cur.time, value: lastVwap });
              aggVwapUp.push({ time: cur.time, value: +(lastVwap + 1.8).toFixed(2) });
              aggVwapDn.push({ time: cur.time, value: +(lastVwap - 1.8).toFixed(2) });
            }
            if (lastCvd !== null) {
              aggCvd.push({ time: cur.time, value: lastCvd });
            }
          }
          cur = { time: bucket, open: c.open, high: c.high, low: c.low, close: c.close };
          curVol = c.volume || 0;
        } else {
          cur.high = Math.max(cur.high, c.high);
          cur.low = Math.min(cur.low, c.low);
          cur.close = c.close;
          curVol += (c.volume || 0);
        }
        if (c.vwap) lastVwap = c.vwap;
        if (c.cvd !== undefined) lastCvd = c.cvd;
      }

      if (cur) {
        aggCandles.push(cur);
        aggVolume.push({
          time: cur.time,
          value: curVol,
          color: cur.close >= cur.open ? 'rgba(0, 230, 118, 0.45)' : 'rgba(255, 59, 48, 0.45)'
        });
        if (lastVwap) {
          aggVwap.push({ time: cur.time, value: lastVwap });
          aggVwapUp.push({ time: cur.time, value: +(lastVwap + 1.8).toFixed(2) });
          aggVwapDn.push({ time: cur.time, value: +(lastVwap - 1.8).toFixed(2) });
        }
        if (lastCvd !== null) {
          aggCvd.push({ time: cur.time, value: lastCvd });
        }
      }

      // Map markers to bucket
      const seenMarkerBuckets = new Set();
      for (const m of rawMarkers) {
        const b = Math.floor(m.time / sec) * sec;
        if (!seenMarkerBuckets.has(b)) {
          seenMarkerBuckets.add(b);
          aggMarkers.push({ ...m, time: b });
        }
      }

      return {
        candles: aggCandles,
        volume: aggVolume,
        vwap: aggVwap,
        vwapUp: aggVwapUp,
        vwapDn: aggVwapDn,
        cvd: aggCvd,
        markers: aggMarkers
      };
    }

    function renderActiveData(preserveRange = true) {
      if (!candleSeries) return;
      const data = aggregateData(currentTf);

      candleSeries.setData(data.candles);
      if (volumeSeries) volumeSeries.setData(data.volume);

      if (showVwap) {
        vwapSeries.setData(data.vwap);
      } else {
        vwapSeries.setData([]);
      }

      if (showBands) {
        vwapUpSeries.setData(data.vwapUp);
        vwapDnSeries.setData(data.vwapDn);
      } else {
        vwapUpSeries.setData([]);
        vwapDnSeries.setData([]);
      }

      if (showCvd) {
        cvdSeries.setData(data.cvd);
      } else {
        cvdSeries.setData([]);
      }

      if (showAbs) {
        candleSeries.setMarkers(data.markers);
      } else {
        candleSeries.setMarkers([]);
      }

      if (!preserveRange) {
        const total = (data && data.candles) ? data.candles.length : 0;
        if (total > 0) {
          const fromIdx = Math.max(0, total - 120);
          const toIdx = total + 6;
          mainChart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });
          cvdChart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });
        } else {
          mainChart.timeScale().fitContent();
          cvdChart.timeScale().fitContent();
        }
      }

      updateHeaderOhlc(null);
    }

    function changeTimeframe(tf, btn) {
      currentTf = tf;
      document.querySelectorAll('.btn-tf').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      renderActiveData(false);
    }

    function toggleIndicator(type) {
      if (type === 'vwap') {
        showVwap = !showVwap;
        document.getElementById('btnVwap').className = showVwap ? 'btn-tool active-gold' : 'btn-tool';
      } else if (type === 'bands') {
        showBands = !showBands;
        document.getElementById('btnBands').className = showBands ? 'btn-tool active-cyan' : 'btn-tool';
      } else if (type === 'cvd') {
        showCvd = !showCvd;
        const cvdEl = document.getElementById('cvd-chart');
        cvdEl.style.display = showCvd ? 'block' : 'none';
        document.getElementById('btnCvd').className = showCvd ? 'btn-tool active-cyan' : 'btn-tool';
        window.dispatchEvent(new Event('resize'));
      } else if (type === 'abs') {
        showAbs = !showAbs;
        document.getElementById('btnAbs').className = showAbs ? 'btn-tool active-green' : 'btn-tool';
      }
      renderActiveData(true);
    }

    function fitChart() {
      if (mainChart) mainChart.timeScale().fitContent();
      if (cvdChart) cvdChart.timeScale().fitContent();
    }

    function scrollToRealtime() {
      if (mainChart) mainChart.timeScale().scrollToRealtime();
      if (cvdChart) cvdChart.timeScale().scrollToRealtime();
      const btn = document.getElementById('floating-live-btn');
      if (btn) btn.style.display = 'none';
    }

    function toggleFullscreen() {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    }

    // Load initial data
    async function loadHistory() {
      try {
        const res = await fetch('/api/chart_history');
        const data = await res.json();
        if (data.candles && data.candles.length > 0) {
          rawCandles = data.candles;
          rawVolume = data.volume || [];
          rawVwap = data.vwap || [];
          rawVwapUp = data.vwap_up || [];
          rawVwapDn = data.vwap_dn || [];
          rawCvd = data.cvd || [];
          rawMarkers = data.markers || [];

          renderActiveData(false);
        }
      } catch (err) {
        console.error('Failed loading history:', err);
      } finally {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) overlay.style.display = 'none';
      }
    }

    // Realtime live tick poller
    async function pollLiveCandle() {
      try {
        const res = await fetch('/api/live_candle');
        const data = await res.json();
        if (data && data.time) {
          // Update raw candles cache
          const lastIdx = rawCandles.length - 1;
          if (lastIdx >= 0 && rawCandles[lastIdx].time === data.time) {
            rawCandles[lastIdx] = {
              time: data.time, open: data.open, high: data.high, low: data.low, close: data.close,
              volume: data.volume, vwap: data.vwap, cvd: data.cvd
            };
          } else if (lastIdx >= 0 && data.time > rawCandles[lastIdx].time) {
            rawCandles.push({
              time: data.time, open: data.open, high: data.high, low: data.low, close: data.close,
              volume: data.volume, vwap: data.vwap, cvd: data.cvd
            });
          }

          // If on 1M, update directly without full re-render
          if (currentTf === 1) {
            candleSeries.update({
              time: data.time, open: data.open, high: data.high, low: data.low, close: data.close
            });
            if (volumeSeries) {
              volumeSeries.update({
                time: data.time, value: data.volume,
                color: data.close >= data.open ? 'rgba(0, 230, 118, 0.45)' : 'rgba(255, 59, 48, 0.45)'
              });
            }
            if (showVwap && data.vwap) {
              vwapSeries.update({ time: data.time, value: data.vwap });
            }
            if (showBands && data.vwap_up && data.vwap_dn) {
              vwapUpSeries.update({ time: data.time, value: data.vwap_up });
              vwapDnSeries.update({ time: data.time, value: data.vwap_dn });
            }
            if (showCvd && data.cvd !== undefined) {
              cvdSeries.update({ time: data.time, value: data.cvd });
            }
            updateHeaderOhlc(data);
          } else {
            // For multi-minute timeframe, aggregate the latest forming bar
            const sec = currentTf * 60;
            const bucket = Math.floor(data.time / sec) * sec;
            const slice = rawCandles.filter(c => Math.floor(c.time / sec) * sec === bucket);
            if (slice.length > 0) {
              const aggC = {
                time: bucket,
                open: slice[0].open,
                high: Math.max(...slice.map(s => s.high)),
                low: Math.min(...slice.map(s => s.low)),
                close: slice[slice.length - 1].close
              };
              const aggVol = slice.reduce((acc, s) => acc + (s.volume || 0), 0);
              candleSeries.update(aggC);
              if (volumeSeries) {
                volumeSeries.update({
                  time: bucket, value: aggVol,
                  color: aggC.close >= aggC.open ? 'rgba(0, 230, 118, 0.45)' : 'rgba(255, 59, 48, 0.45)'
                });
              }
              if (showVwap && data.vwap) vwapSeries.update({ time: bucket, value: data.vwap });
              if (showBands && data.vwap_up) {
                vwapUpSeries.update({ time: bucket, value: data.vwap_up });
                vwapDnSeries.update({ time: bucket, value: data.vwap_dn });
              }
              if (showCvd && data.cvd !== undefined) cvdSeries.update({ time: bucket, value: data.cvd });
              updateHeaderOhlc(aggC);
            }
          }
        }
      } catch (err) {
        // quiet error
      }
    }

    // Launch
    window.addEventListener('DOMContentLoaded', () => {
      initCharts();
      loadHistory();
      setInterval(pollLiveCandle, 1000);
    });
  </script>
</body>
</html>
"""

server = app.server

@server.before_request
def require_basic_auth():
    auth = request.authorization
    if not auth or auth.username != 'am' or auth.password != 'Orferflow@1910':
        return Response(
            '401 Unauthorized - Access Denied\nGoldFlow Institutional Terminal',
            401,
            {'WWW-Authenticate': 'Basic realm="GoldFlow Secured Terminal"'}
        )

@server.route('/chart')
def serve_tv_chart():
    return render_template_string(TV_CHART_HTML)

@server.route('/api/chart_history')
def api_chart_history():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        c.execute("""SELECT bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, absorption 
                     FROM price_bars ORDER BY bar_time ASC""")
        rows = c.fetchall()
        conn.close()

        candles, volume, vwap, vwap_up, vwap_dn, cvd, markers = [], [], [], [], [], [], []
        seen_times = set()
        now_cutoff_ts = int((datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2)).replace(tzinfo=timezone.utc).timestamp())

        # Daily Anchored VWAP variables (resets at 00:00 UTC each calendar day)
        current_date = None
        day_pv = 0.0
        day_vol = 0.0
        day_pv2 = 0.0

        for r in rows:
            try:
                dt = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S') if isinstance(r[0], str) else r[0]
                ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                if ts in seen_times or ts > now_cutoff_ts:
                    continue
                seen_times.add(ts)

                bar_date = dt.date()
                if bar_date != current_date:
                    current_date = bar_date
                    day_pv = 0.0
                    day_vol = 0.0
                    day_pv2 = 0.0

                op, hp, lp, cp = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                vol = float(r[5] or 1.0)
                c_val = float(r[7] or 0)
                abs_text = r[10]

                tp = (hp + lp + cp) / 3.0
                day_pv += tp * vol
                day_vol += vol
                day_pv2 += tp * tp * vol
                v_val = round(day_pv / day_vol, 2) if day_vol > 0 else cp
                v_up = round(v_val + 1.80, 2)
                v_dn = round(v_val - 1.80, 2)

                candles.append({'time': ts, 'open': op, 'high': hp, 'low': lp, 'close': cp})
                volume.append({'time': ts, 'value': vol, 'color': 'rgba(0, 230, 118, 0.45)' if cp >= op else 'rgba(255, 59, 48, 0.45)'})
                vwap.append({'time': ts, 'value': v_val})
                vwap_up.append({'time': ts, 'value': v_up})
                vwap_dn.append({'time': ts, 'value': v_dn})
                cvd.append({'time': ts, 'value': c_val})

                if abs_text == 'Bullish Absorption':
                    markers.append({'time': ts, 'position': 'belowBar', 'color': '#00E676', 'shape': 'arrowUp', 'text': '⚡ABS'})
                elif abs_text == 'Bearish Absorption':
                    markers.append({'time': ts, 'position': 'aboveBar', 'color': '#FF3B30', 'shape': 'arrowDown', 'text': '⚡ABS'})
            except Exception:
                continue

        return jsonify({
            'candles': candles,
            'volume': volume,
            'vwap': vwap,
            'vwap_up': vwap_up,
            'vwap_dn': vwap_dn,
            'cvd': cvd,
            'markers': markers
        })
    except Exception as e:
        logger.error(f"Error serving chart history: {e}")
        return jsonify({'error': str(e)}), 500

@server.route('/api/live_candle')
def api_live_candle():
    with data_lock:
        cb = dict(current_bar)
        price = last_known_price
        c_delta = cum_delta
        live_vwap = cb.get('vwap', price)
        live_up = cb.get('vwap_up', round(live_vwap + 1.8, 2))
        live_dn = cb.get('vwap_dn', round(live_vwap - 1.8, 2))

    try:
        if cb.get('time') is not None and cb.get('open') is not None:
            dt = datetime.strptime(cb['time'], '%Y-%m-%d %H:%M:%S') if isinstance(cb['time'], str) else cb['time']
            ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
            cp = float(cb.get('close') or price)
            op = float(cb.get('open') or price)
            hp = float(cb.get('high') or price)
            lp = float(cb.get('low') or price)
            vol = float(cb.get('volume') or 0)
            c_val = float(cb.get('cvd') or c_delta)
            return jsonify({
                'time': ts, 'open': op, 'high': hp, 'low': lp, 'close': cp,
                'volume': vol, 'vwap': live_vwap, 'vwap_up': live_up,
                'vwap_dn': live_dn, 'cvd': c_val, 'price': price
            })
        else:
            ts = int(datetime.now(timezone.utc).replace(second=0, microsecond=0).timestamp())
            return jsonify({
                'time': ts, 'open': price, 'high': price, 'low': price, 'close': price,
                'volume': 0, 'vwap': live_vwap, 'vwap_up': live_up,
                'vwap_dn': live_dn, 'cvd': c_delta, 'price': price
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@server.route('/api/tape')
def api_tape():
    with data_lock:
        tape_copy = list(recent_tape)
        book_copy = dict(order_book)
        price = last_known_price
    return jsonify({
        'price': price,
        'tape': tape_copy,
        'order_book': book_copy
    })


app.layout = html.Div(
    id="terminal-container",
    style={
        "backgroundColor": "#070A0F",
        "color": "#E6EDF3",
        "fontFamily": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
        "minHeight": "100vh",
        "padding": "8px 12px",
        "boxSizing": "border-box",
        "overflowX": "hidden",
        "overflowY": "auto"
    },
    children=[
        dcc.Interval(id="terminal-interval", interval=1000, n_intervals=0),

        # TOP BAR: Institutional Header & Real-time Status
        html.Div(
            id="terminal-topbar",
            style={
                "display": "flex",
                "flexWrap": "wrap",
                "gap": "10px",
                "justifyContent": "space-between",
                "alignItems": "center",
                "backgroundColor": "#0D1117",
                "border": "1px solid #21262D",
                "borderBottom": "2px solid #00F0FF",
                "padding": "8px 14px",
                "borderRadius": "4px",
                "marginBottom": "8px"
            },
            children=[
                html.Div([
                    html.Span("⚡ GOLD.FLOW ", style={"color": "#FFD700", "fontWeight": "900", "fontSize": "16px", "letterSpacing": "1px"}),
                    html.Span("// MASTER BLOOMBERG ORDER FLOW TERMINAL ", style={"color": "#8B949E", "fontSize": "12px"}),
                    html.Span("[PORT 8070]", style={"color": "#00F0FF", "fontSize": "11px", "marginLeft": "6px", "backgroundColor": "#161B22", "padding": "2px 6px", "borderRadius": "3px"}),
                    html.Span("✨ V1+V2+V3 CONSOLIDATED", style={"color": "#00E676", "fontSize": "10px", "marginLeft": "6px", "backgroundColor": "rgba(0, 230, 118, 0.12)", "padding": "2px 6px", "borderRadius": "3px", "fontWeight": "bold"})
                ]),

                # Multi-Zone Clock
                html.Div(id="clocks-panel", style={"fontSize": "11px", "color": "#8B949E", "letterSpacing": "0.5px"}),

                # Live Heartbeat & Latency
                html.Div([
                    html.Span("● FEED: LIVE", style={"color": "#00E676", "fontWeight": "bold", "fontSize": "11px", "marginRight": "12px"}),
                    html.Span(id="mt5-status-badge", style={"fontSize": "11px", "marginRight": "12px"}),
                    html.Span(id="risk-guard-badge", style={"fontSize": "11px", "marginRight": "12px"}),
                    html.Span("REGIME: INSTITUTIONAL SMC", style={"color": "#FFD700", "fontSize": "11px", "backgroundColor": "#1F1D14", "padding": "2px 6px", "border": "1px solid #D29922", "borderRadius": "3px"})
                ])
            ]
        ),

        # TICKER STRIP: Spot Gold Price, Spread, 24h Stats, Session Kill Zone, Risk Guard
        html.Div(
            id="ticker-strip",
            style={
                "display": "grid",
                "gridTemplateColumns": "repeat(auto-fit, minmax(140px, 1fr))",
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
            id="main-workspace-grid",
            style={"display": "flex", "flexWrap": "wrap", "gap": "10px", "marginBottom": "8px"},
            children=[
                # Left Panel: Main Chart & CVD
                html.Div(
                    style={"flex": "1 1 680px", "minWidth": "320px", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        html.Div(
                            style={"backgroundColor": "#0A0D14", "border": "1px solid #21262D", "borderRadius": "4px", "padding": "0", "overflow": "hidden"},
                            children=[
                                html.Iframe(
                                    id="main-terminal-chart-frame",
                                    src="/chart",
                                    style={"width": "100%", "height": "590px", "border": "none", "display": "block"}
                                )
                            ]
                        )
                    ]
                ),

                # Right Panel: DOM Ladder + The Tape (Time & Sales)
                html.Div(
                    style={"flex": "1 1 320px", "minWidth": "280px", "display": "flex", "flexDirection": "column", "gap": "8px"},
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
# 7. DASH CALLBACKS: REAL-TIME RENDERING (WITH KILL ZONES & SMC)
# =============================================================================
@app.callback(
    [
        Output("clocks-panel", "children"),
        Output("mt5-status-badge", "children"),
        Output("risk-guard-badge", "children"),
        Output("live-price-box", "children"),
        Output("spread-box", "children"),
        Output("high-box", "children"),
        Output("low-box", "children"),
        Output("session-box", "children"),
        Output("phase-box", "children"),
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

    # MT5 Account Status Badge
    mt5_acc = mt5_bridge.get_account_status()
    if mt5_acc:
        active_tkt = f" | Tkt #{pos['mt5_ticket']}" if pos.get('mt5_ticket') else ""
        mt5_badge = html.Span(
            f"⚡ MT5: CONNECTED (Acc: {mt5_acc['login']} | ${mt5_acc['balance']:.0f}{active_tkt})",
            style={"color": "#00F0FF", "fontWeight": "bold", "backgroundColor": "rgba(0, 240, 255, 0.12)", "padding": "2px 6px", "borderRadius": "3px"}
        )
    else:
        mt5_badge = html.Span("⚠️ MT5: OFFLINE", style={"color": "#FF3B30", "fontWeight": "bold"})

    # Risk Guard Status Badge
    risk_stat = risk_manager.get_status()
    risk_badge = html.Span(
        f"🛡️ RISK: {risk_stat['reason']} (${risk_stat['daily_pnl']:+.1f})",
        style={"color": "#00E676" if risk_stat['can_trade'] else "#FF3B30", "fontWeight": "bold"}
    )

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

    # Session Kill Zone Badge (from V2 / 8060)
    sess_info = get_session_info()
    session_box = [
        html.Div("SESSION & KILL ZONE", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(
            f"{sess_info['emoji']} {sess_info['name']}",
            style={"fontSize": "13px", "fontWeight": "900", "color": sess_info['color'], "marginTop": "3px"}
        ),
        html.Div(sess_info['quality'], style={"fontSize": "10px", "color": sess_info['color']})
    ]

    last_phase = bars[-1].get('phase', 'Neutral') if bars else 'Neutral'
    last_absrp = bars[-1].get('absorption') if bars else None
    phase_sub = f"⚡ {last_absrp}" if last_absrp else f"CVD: {cum_delta:+.0f}"
    phase_box = [
        html.Div("ORDER FLOW REGIME", style={"fontSize": "10px", "color": "#8B949E"}),
        html.Div(last_phase.upper(), style={"fontSize": "13px", "fontWeight": "bold", "color": "#00F0FF", "marginTop": "3px"}),
        html.Div(phase_sub, style={"fontSize": "10px", "color": "#FFD700" if not last_absrp else "#00E676"})
    ]

    # 3. DOM Ladder Rendering

    dom_rows = []
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

    dom_rows.append(
        html.Div(
            f"─── SPOT: ${price:.2f} ───",
            style={"textAlign": "center", "fontSize": "11px", "color": "#FFD700", "fontWeight": "bold", "padding": "3px 0", "backgroundColor": "#161B22", "borderTop": "1px solid #21262D", "borderBottom": "1px solid #21262D"}
        )
    )

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
        html.Th("SMC CONFLUENCE", style={"padding": "4px 8px", "color": "#8B949E"})
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
            html.Td("Kill Zone Confluence", style={"padding": "4px 8px"})
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
        clocks_text, mt5_badge, risk_badge, price_box, spread_box, high_box, low_box, session_box, phase_box,
        dom_rows, tape_items, blotter_stats, table
    )


# =============================================================================
# 8. CLEAN SHUTDOWN HANDLER
# =============================================================================
def graceful_shutdown(signum=None, frame=None):
    global is_running
    logger.info("Terminal shutting down gracefully...")
    is_running = False
    with data_lock:
        if current_bar.get('close') is not None and current_bar.get('time') is not None:
            save_bar_to_db(current_bar)
            logger.info("Terminal current bar saved to database.")
    sys.exit(0)


# =============================================================================
# 9. STARTUP ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    logger.info("🏛️ Starting GOLD.FLOW Master Institutional Terminal on Port 8070...")
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(graceful_shutdown)

    init_db()
    mt5_bridge.init_mt5()
    load_initial_bars()
    load_trades()
    init_tape_seed()

    # Single Unified Live Feed Worker (No cross-broker wick collision)
    threading.Thread(target=live_feed_worker, daemon=True).start()
    threading.Thread(target=alltick_ws_worker, daemon=True).start()
    threading.Thread(target=itick_ws_worker, daemon=True).start()

    logger.info("🚀 Launching Master Terminal Web Server on http://127.0.0.1:8070 (0.0.0.0)...")
    app.run(host="0.0.0.0", port=8070, debug=False)
