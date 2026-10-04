# ------------------------------------------------------------
# 🔥 XAUUSD V2 PRO — INSTITUTIONAL ORDER FLOW TERMINAL
# Session Kill Zones | CVD Divergence | Multi-Layer Confluence
# Absorption Detection | Risk Manager | SMC Concepts
# Port: 8060
# ------------------------------------------------------------

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

# Fix Windows console UTF-8 encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ---------- CONFIG ----------
CONFIG = {
    "REALMARKET_KEY": "dJVWdPPVPwbl22cGBkr1Lsn3A2aAiPBJTukGIYPBpEspElUT",
    "REALMARKET_URL": "https://api.realmarketapi.com/api/v1/price",
    "GOLDAPI_KEY": "goldapi-8efc4582252eb4ad78ea73f25681ee34-io",
    "GOLDAPI_URL": "https://www.goldapi.io/api/XAU/USD",
    "DATA_MODE": "RealMarketAPI (Spot Gold) + Binance",
    "TRADE_MODE": "PAPER",
    "FIXED_LOT": 0.01,
    "RISK_REWARD_RATIO": 2.0,
    "ACCOUNT_SIZE": 10000.0,
    "MAX_RISK_PCT": 1.0,
    "MAX_DAILY_LOSS_PCT": 3.0,
    "MAX_CONSECUTIVE_LOSSES": 3,
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("XAUUSD_V2")

# ---------- GLOBAL DATA ----------
data_lock = threading.Lock()
historical_bars = []
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
last_known_price = 2650.0
last_price_update = datetime.now()
market_meta = {'open': 0, 'high': 0, 'low': 0, 'bid': 0, 'ask': 0, 'ch': 0}

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0}
trade_history = []


# ============================================================
# 1. SQLite PERSISTENCE
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_v2.db')

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
    # Historical price bars persistence table
    conn.execute('''CREATE TABLE IF NOT EXISTS price_bars (
        bar_time TEXT PRIMARY KEY,
        open REAL, high REAL, low REAL, close REAL,
        volume REAL, delta REAL, cvd REAL, vwap REAL, poc REAL,
        phase TEXT, confidence INTEGER, signal_type TEXT, session TEXT,
        imbalance INTEGER DEFAULT 0, fvg TEXT, absorption TEXT, cvd_divergence TEXT
    )''')
    conn.commit()
    # Ensure backwards compatibility with any existing columns
    try:
        conn.execute('ALTER TABLE trades ADD COLUMN exit_time TEXT')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE trades ADD COLUMN exit_price REAL')
    except Exception:
        pass
    conn.commit()
    conn.close()
    logger.info(f"V2 Database initialized: {DB_PATH}")


def save_bar_to_db_bars(bar):
    """Save a finalized bar to the price_bars table for persistence."""
    try:
        conn = sqlite3.connect(DB_PATH)
        bar_time_str = bar['time'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(bar['time'], datetime) else str(bar['time'])
        conn.execute('''INSERT OR REPLACE INTO price_bars 
            (bar_time, open, high, low, close, volume, delta, cvd, vwap, poc,
             phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (bar_time_str, bar['open'], bar['high'], bar['low'], bar['close'],
             bar['volume'], bar.get('delta', 0), bar.get('cvd', 0), bar.get('vwap', 0), bar.get('poc', 0),
             bar.get('phase', ''), bar.get('confidence', 50), bar.get('signal_type', 'NONE'),
             bar.get('session', ''), 1 if bar.get('imbalance') else 0,
             bar.get('fvg', None), bar.get('absorption', None), bar.get('cvd_divergence', None)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Bar DB save error: {e}")


def load_bars_from_db_bars(hours=12):
    """Load saved bars from database (last N hours). Returns list of bar dicts."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        cursor = conn.execute(
            'SELECT bar_time, open, high, low, close, volume, delta, cvd, vwap, poc, '
            'phase, confidence, signal_type, session, imbalance, fvg, absorption, cvd_divergence '
            'FROM price_bars WHERE bar_time >= ? ORDER BY bar_time', (cutoff,))
        bars = []
        for r in cursor.fetchall():
            try:
                bar_time = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
            bars.append({
                'time': bar_time, 'open': r[1], 'high': r[2], 'low': r[3], 'close': r[4],
                'volume': r[5], 'delta': r[6], 'cvd': r[7], 'vwap': r[8], 'poc': r[9],
                'phase': r[10] or 'Neutral', 'confidence': r[11] or 50,
                'signal_type': r[12] or 'NONE', 'session': r[13] or '',
                'imbalance': bool(r[14]), 'fvg': r[15], 'absorption': r[16], 'cvd_divergence': r[17]
            })
        conn.close()
        return bars
    except Exception as e:
        logger.error(f"Bar DB load error: {e}")
        return []


def cleanup_old_bars(hours=24):
    """Delete bars older than N hours to prevent database bloat."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('DELETE FROM price_bars WHERE bar_time < ?', (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Bar cleanup error: {e}")


def graceful_shutdown(signum=None, frame=None):
    """Save current state and exit cleanly."""
    global is_running
    logger.info("🛑 V2: Graceful shutdown initiated — saving bar data...")
    is_running = False
    # Save all current historical bars to DB
    with data_lock:
        saved = 0
        for bar in historical_bars:
            save_bar_to_db_bars(bar)
            saved += 1
        # Save current incomplete bar if it has data
        if current_bar.get('close') is not None and current_bar.get('time') is not None:
            partial = {
                'time': current_bar['time'], 'open': current_bar['open'],
                'high': current_bar['high'], 'low': current_bar['low'], 'close': current_bar['close'],
                'volume': current_bar['volume'], 'delta': current_bar.get('buy_vol', 0) - current_bar.get('sell_vol', 0),
                'cvd': cum_delta, 'vwap': current_bar.get('vwap', 0), 'poc': current_bar.get('poc', 0),
                'phase': 'Neutral', 'confidence': 50, 'signal_type': 'NONE', 'session': '',
                'imbalance': False, 'fvg': None, 'absorption': None, 'cvd_divergence': None
            }
            save_bar_to_db_bars(partial)
            saved += 1
    logger.info(f"✅ V2: Saved {saved} bars to database. Goodbye!")
    cleanup_old_bars(24)
    sys.exit(0)

def save_trade_to_db(trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''INSERT OR REPLACE INTO trades 
            (id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (trade['id'], trade['time'], trade.get('exit_time', '--'), trade['dir'],
             trade['entry'], trade.get('exit_price', 0.0), trade['sl'], trade['tp'],
             trade['status'], trade['pnl'], trade.get('confidence', 0), trade.get('phase', ''),
             trade.get('session', ''), trade.get('signal_type', '')))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB save error: {e}")

def update_trade_in_db(trade_id, status, pnl, exit_time=None, exit_price=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        if exit_time is not None and exit_price is not None:
            conn.execute('UPDATE trades SET status=?, pnl=?, exit_time=?, exit_price=? WHERE id=?', 
                         (status, pnl, exit_time, exit_price, trade_id))
        else:
            conn.execute('UPDATE trades SET status=?, pnl=? WHERE id=?', (status, pnl, trade_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB update error: {e}")

def load_trades_from_db():
    global trade_history
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute('SELECT id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type FROM trades ORDER BY id')
        trade_history = [{
            'id': r[0], 'time': r[1], 'exit_time': r[2] if r[2] else '--', 'dir': r[3], 'entry': r[4],
            'exit_price': r[5] if r[5] else 0.0, 'sl': r[6], 'tp': r[7],
            'status': r[8], 'pnl': r[9], 'confidence': r[10], 'phase': r[11],
            'session': r[12], 'signal_type': r[13]
        } for r in cursor.fetchall()]
        conn.close()
        logger.info(f"Loaded {len(trade_history)} trades from V2 database")
    except Exception as e:
        logger.error(f"DB load error: {e}")


# ============================================================
# 2. SESSION / KILL ZONE DETECTION
# ============================================================
def get_session_info():
    """Determine current trading session and whether we're in a Kill Zone."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    if 13 <= hour < 17:
        return {"name": "NY OVERLAP", "emoji": "🔥", "kill_zone": True, "quality": "PREMIUM", "color": "#ff4444"}
    elif 8 <= hour < 13:
        return {"name": "LONDON", "emoji": "🇬🇧", "kill_zone": True, "quality": "HIGH", "color": "#00e5ff"}
    elif 17 <= hour < 22:
        return {"name": "NY CLOSE", "emoji": "🇺🇸", "kill_zone": False, "quality": "MEDIUM", "color": "#ffd700"}
    elif 0 <= hour < 3:
        return {"name": "ASIA EARLY", "emoji": "🌙", "kill_zone": False, "quality": "LOW", "color": "#555"}
    elif 3 <= hour < 8:
        return {"name": "ASIA/TOKYO", "emoji": "🇯🇵", "kill_zone": False, "quality": "LOW", "color": "#666"}
    else:
        return {"name": "OFF-HOURS", "emoji": "💤", "kill_zone": False, "quality": "AVOID", "color": "#333"}


# ============================================================
# 3. RISK MANAGER
# ============================================================
class RiskManager:
    def __init__(self):
        self.account_size = CONFIG['ACCOUNT_SIZE']
        self.max_risk_pct = CONFIG['MAX_RISK_PCT']
        self.max_daily_loss_pct = CONFIG['MAX_DAILY_LOSS_PCT']
        self.max_consecutive_losses = CONFIG['MAX_CONSECUTIVE_LOSSES']
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
            logger.info("Risk Manager: Daily counters reset")

    def can_trade(self):
        self._check_daily_reset()
        # Unlocked: Continuous trading without consecutive loss lock
        return True, "CONTINUOUS UNLOCKED"

    def record_trade_result(self, pnl):
        self._check_daily_reset()
        self.daily_pnl += pnl
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def calculate_position_size(self, entry, sl):
        risk_amount = self.account_size * (self.max_risk_pct / 100)
        risk_distance = abs(entry - sl)
        if risk_distance == 0:
            risk_distance = 2.0
        lots = risk_amount / (risk_distance * 100)
        return round(max(lots, 0.01), 2)

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


# ============================================================
# 4. ORDER FLOW ENGINE (ENHANCED)
# ============================================================
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
            if current_bar['time'] is None or current_bar['time'] < current_minute:
                if current_bar['close'] is not None and current_bar['time'] is not None:
                    final_bar = finalize_bar(current_bar, vwap)
                    historical_bars.append(final_bar)
                    save_bar_to_db_bars(final_bar)  # Persist to SQLite
                    if len(historical_bars) > 200:
                        historical_bars.pop(0)
                    check_and_execute_trade(final_bar)

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
        logger.error(f"Tick Error: {e}")


def finalize_bar(bar, vwap):
    delta = float(bar['buy_vol'] - bar['sell_vol'])
    poc = max(bar['levels'], key=bar['levels'].get) if bar['levels'] else bar['close']
    avg_vol = np.mean([b['volume'] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar['volume']
    imbalance = bar['volume'] > (avg_vol * 2.2) if avg_vol > 0 else False

    # Fair Value Gap (SMC)
    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']:
            fvg = "Bullish"
        elif prev2['low'] > bar['high']:
            fvg = "Bearish"

    # Phase Detection (Enhanced)
    phase = "Neutral"
    if len(historical_bars) >= 5:
        cvd_values = [b.get('cvd', 0) for b in historical_bars[-5:]]
        cvd_trend = cvd_values[-1] - cvd_values[0]
        price_trend = bar['close'] - historical_bars[-5]['close']

        if cvd_trend > 0 and price_trend > 0:
            phase = "Distribution"
        elif cvd_trend < 0 and price_trend < 0:
            phase = "Accumulation"
        elif cvd_trend > 0 and price_trend < 0:
            phase = "Bearish Divergence"  # CVD up but price down → reversal
        elif cvd_trend < 0 and price_trend > 0:
            phase = "Bullish Divergence"  # CVD down but price up → reversal
        else:
            phase = "Manipulation"

    # Absorption Detection
    absorption = None
    if len(historical_bars) >= 3:
        recent_3 = historical_bars[-3:]
        total_vol = sum(b['volume'] for b in recent_3)
        price_range = max(b['high'] for b in recent_3) - min(b['low'] for b in recent_3)
        if total_vol > avg_vol * 3 and price_range < 1.0:
            if delta > 0:
                absorption = "Bullish Absorption"
            else:
                absorption = "Bearish Absorption"

    # CVD Divergence Detection
    cvd_divergence = None
    if len(historical_bars) >= 15:
        prices_recent = [b['close'] for b in historical_bars[-15:]]
        cvds_recent = [b.get('cvd', 0) for b in historical_bars[-15:]]
        price_low_idx = np.argmin(prices_recent)
        price_high_idx = np.argmax(prices_recent)

        # Bullish: price lower low but CVD higher low
        if price_low_idx > 8:
            prev_low_idx = np.argmin(prices_recent[:price_low_idx])
            if (prices_recent[price_low_idx] < prices_recent[prev_low_idx] and
                cvds_recent[price_low_idx] > cvds_recent[prev_low_idx]):
                cvd_divergence = "Bullish"

        # Bearish: price higher high but CVD lower high
        if price_high_idx > 8:
            prev_high_idx = np.argmax(prices_recent[:price_high_idx])
            if (prices_recent[price_high_idx] > prices_recent[prev_high_idx] and
                cvds_recent[price_high_idx] < cvds_recent[prev_high_idx]):
                cvd_divergence = "Bearish"

    # Multi-Layer Confluence Score
    score = 0
    max_score = 100
    signal_type = "NONE"

    session = get_session_info()

    # Layer 1: Order Flow Delta (25 points)
    if delta > 0:
        score += 25
        signal_type = "BUY"
    elif delta < 0:
        score += 25
        signal_type = "SELL"

    # Layer 2: VWAP Position (20 points)
    if signal_type == "BUY" and bar['close'] > vwap:
        score += 20
    elif signal_type == "SELL" and bar['close'] < vwap:
        score += 20

    # Layer 3: Session Quality (15 points)
    if session['kill_zone']:
        score += 15
    elif session['quality'] == "MEDIUM":
        score += 8

    # Layer 4: CVD Divergence (20 points)
    if cvd_divergence == "Bullish" and signal_type == "BUY":
        score += 20
    elif cvd_divergence == "Bearish" and signal_type == "SELL":
        score += 20

    # Layer 5: FVG Confluence (10 points)
    if fvg == "Bullish" and signal_type == "BUY":
        score += 10
    elif fvg == "Bearish" and signal_type == "SELL":
        score += 10

    # Layer 6: Absorption (10 points)
    if absorption == "Bullish Absorption" and signal_type == "BUY":
        score += 10
    elif absorption == "Bearish Absorption" and signal_type == "SELL":
        score += 10

    # Invert signal on divergence phases
    if phase == "Bullish Divergence":
        signal_type = "BUY"
    elif phase == "Bearish Divergence":
        signal_type = "SELL"

    confidence = max(5, min(95, score))

    return {
        'time': bar['time'], 'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'],
        'volume': bar['volume'], 'delta': delta, 'cvd': cum_delta,
        'vwap': round(vwap, 2), 'poc': round(poc, 2), 'imbalance': imbalance,
        'fvg': fvg, 'phase': phase, 'confidence': confidence,
        'absorption': absorption, 'cvd_divergence': cvd_divergence,
        'signal_type': signal_type, 'session': session['name']
    }


# ============================================================
# 5. TRADING EXECUTION (ENHANCED)
# ============================================================
def calculate_sl_tp(price, direction, vwap, atr):
    if atr == 0:
        atr = 2.0
    if direction == "LONG":
        sl = min(price - atr * 1.5, vwap - atr * 0.5)
        tp = price + (price - sl) * CONFIG["RISK_REWARD_RATIO"]
    else:
        sl = max(price + atr * 1.5, vwap + atr * 0.5)
        tp = price - (sl - price) * CONFIG["RISK_REWARD_RATIO"]
    return round(sl, 2), round(tp, 2)


def execute_order(symbol, action, lot, sl, tp, confidence, phase, session_name, signal_type):
    global trade_history
    logger.info(f"📄 [V2 PAPER] {action} {lot} {symbol} @ ${last_known_price:.2f} | SL: ${sl:.2f} | TP: ${tp:.2f} | Conf: {confidence}% | Session: {session_name}")
    trade_state['in_position'] = True
    trade_state['entry_price'] = last_known_price
    trade_state['stop_loss'] = sl
    trade_state['take_profit'] = tp
    direction = "LONG" if "BUY" in action else "SHORT"
    trade_state['direction'] = direction

    now_str = datetime.now().strftime('%H:%M:%S')
    trade_history.append({
        'id': len(trade_history) + 1,
        'time': now_str,
        'exit_time': '⏳ RUNNING',
        'dir': '🟢 BUY' if direction == 'LONG' else '🔴 SELL',
        'entry': round(last_known_price, 2),
        'exit_price': round(last_known_price, 2),
        'sl': sl,
        'tp': tp,
        'status': '⏳ OPEN',
        'pnl': 0.0,
        'confidence': confidence,
        'phase': phase,
        'session': session_name,
        'signal_type': signal_type
    })
    if len(trade_history) > 100:
        trade_history.pop(0)
    save_trade_to_db(trade_history[-1])
    return True


def check_and_execute_trade(bar):
    global trade_state, trade_history

    if trade_state['in_position']:
        current_price = bar['close']
        now_time = datetime.now().strftime('%H:%M:%S')
        if trade_state['direction'] == "LONG":
            trade_state['pnl'] = round(current_price - trade_state['entry_price'], 2)
            if current_price <= trade_state['stop_loss']:
                logger.info(f"❌ [V2 SL HIT] Loss: ${trade_state['pnl']}")
                trade_state['in_position'] = False
                risk_manager.record_trade_result(trade_state['pnl'])
                if trade_history:
                    trade_history[-1]['status'] = '❌ SL HIT'
                    trade_history[-1]['exit_time'] = now_time
                    trade_history[-1]['exit_price'] = round(current_price, 2)
                    trade_history[-1]['pnl'] = trade_state['pnl']
                    update_trade_in_db(trade_history[-1]['id'], '❌ SL HIT', trade_state['pnl'], now_time, round(current_price, 2))
            elif current_price >= trade_state['take_profit']:
                logger.info(f"✅ [V2 TP HIT] Profit: ${trade_state['pnl']}")
                trade_state['in_position'] = False
                risk_manager.record_trade_result(trade_state['pnl'])
                if trade_history:
                    trade_history[-1]['status'] = '✅ TP HIT'
                    trade_history[-1]['exit_time'] = now_time
                    trade_history[-1]['exit_price'] = round(current_price, 2)
                    trade_history[-1]['pnl'] = trade_state['pnl']
                    update_trade_in_db(trade_history[-1]['id'], '✅ TP HIT', trade_state['pnl'], now_time, round(current_price, 2))
        else:
            trade_state['pnl'] = round(trade_state['entry_price'] - current_price, 2)
            if current_price >= trade_state['stop_loss']:
                logger.info(f"❌ [V2 SL HIT] Loss: ${trade_state['pnl']}")
                trade_state['in_position'] = False
                risk_manager.record_trade_result(trade_state['pnl'])
                if trade_history:
                    trade_history[-1]['status'] = '❌ SL HIT'
                    trade_history[-1]['exit_time'] = now_time
                    trade_history[-1]['exit_price'] = round(current_price, 2)
                    trade_history[-1]['pnl'] = trade_state['pnl']
                    update_trade_in_db(trade_history[-1]['id'], '❌ SL HIT', trade_state['pnl'], now_time, round(current_price, 2))
            elif current_price <= trade_state['take_profit']:
                logger.info(f"✅ [V2 TP HIT] Profit: ${trade_state['pnl']}")
                trade_state['in_position'] = False
                risk_manager.record_trade_result(trade_state['pnl'])
                if trade_history:
                    trade_history[-1]['status'] = '✅ TP HIT'
                    trade_history[-1]['exit_time'] = now_time
                    trade_history[-1]['exit_price'] = round(current_price, 2)
                    trade_history[-1]['pnl'] = trade_state['pnl']
                    update_trade_in_db(trade_history[-1]['id'], '✅ TP HIT', trade_state['pnl'], now_time, round(current_price, 2))
        if trade_state['in_position'] and trade_history:
            trade_history[-1]['pnl'] = trade_state['pnl']
            trade_history[-1]['exit_price'] = round(current_price, 2)
        return

    # Risk Manager check
    can_trade, reason = risk_manager.can_trade()
    if not can_trade:
        logger.info(f"⛔ [RISK MANAGER] Trade blocked: {reason}")
        return

    # Minimum confidence threshold: 55 for Kill Zone, 65 for others
    session = get_session_info()
    min_confidence = 55 if session['kill_zone'] else 65
    if bar['confidence'] < min_confidence:
        return

    # Calculate ATR
    if len(historical_bars) > 5:
        prices = [b['close'] for b in historical_bars[-5:]]
        atr = np.mean([abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]) * 1.5
    else:
        atr = 2.0

    signal_type = bar.get('signal_type', 'NONE')

    # LONG Entry: Distribution phase or Bullish Divergence + positive delta + above VWAP
    if signal_type == "BUY" and bar['delta'] > 0:
        sl, tp = calculate_sl_tp(bar['close'], "LONG", bar['vwap'], atr)
        lot = risk_manager.calculate_position_size(bar['close'], sl)
        logger.info(f"🔔 [V2 SIGNAL] LONG @ ${bar['close']:.2f} | Conf: {bar['confidence']}% | Phase: {bar['phase']} | Session: {session['name']}")
        execute_order("XAUUSD", "BUY", lot, sl, tp, bar['confidence'], bar['phase'], session['name'], signal_type)

    # SHORT Entry: Accumulation phase or Bearish Divergence + negative delta + below VWAP
    elif signal_type == "SELL" and bar['delta'] < 0:
        sl, tp = calculate_sl_tp(bar['close'], "SHORT", bar['vwap'], atr)
        lot = risk_manager.calculate_position_size(bar['close'], sl)
        logger.info(f"🔔 [V2 SIGNAL] SHORT @ ${bar['close']:.2f} | Conf: {bar['confidence']}% | Phase: {bar['phase']} | Session: {session['name']}")
        execute_order("XAUUSD", "SELL", lot, sl, tp, bar['confidence'], bar['phase'], session['name'], signal_type)


# ============================================================
# 6. REAL HISTORICAL DATA (Binance Klines)
# ============================================================
last_realmarket_poll = 0

def _fetch_live_price():
    """Fetch live gold price: RealMarketAPI Spot primary (rate-optimized), Binance PAXG inter-tick, GoldAPI fallback."""
    global last_realmarket_poll
    now_t = time.time()
    
    # 1. PRIMARY: RealMarketAPI (Direct Spot XAUUSD, polled every 8-10s to conserve 5,000 monthly quota)
    if CONFIG.get("REALMARKET_KEY") and (now_t - last_realmarket_poll >= 8):
        try:
            r = requests.get(
                CONFIG["REALMARKET_URL"],
                params={
                    "apiKey": CONFIG["REALMARKET_KEY"],
                    "symbolCode": "XAUUSD",
                    "timeFrame": "M1"
                },
                timeout=3
            )
            if r.status_code == 200:
                d = r.json()
                bid = float(d.get('bid', 0))
                ask = float(d.get('ask', 0))
                close = float(d.get('closePrice', 0))
                mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else round(close, 2)
                if mid > 0:
                    last_realmarket_poll = now_t
                    spread = round(ask - bid, 3) if (bid > 0 and ask > 0) else 0.20
                    market_meta['bid'] = bid
                    market_meta['ask'] = ask
                    market_meta['spread'] = spread
                    market_meta['source'] = "RealMarketAPI (Spot)"
                    return mid, bid, ask, "RealMarketAPI (Spot)"
        except Exception:
            pass

    # 2. HIGH-FREQUENCY INTER-TICK: Binance PAXG/USDT (fast live orderbook)
    try:
        r = requests.get('https://api.binance.com/api/v3/ticker/bookTicker?symbol=PAXGUSDT', timeout=3)
        if r.status_code == 200:
            d = r.json()
            bid = float(d['bidPrice'])
            ask = float(d['askPrice'])
            mid = round((bid + ask) / 2, 2)
            if mid > 0:
                market_meta['bid'] = bid
                market_meta['ask'] = ask
                market_meta['spread'] = round(ask - bid, 2)
                market_meta['source'] = "Binance PAXG"
                return mid, bid, ask, "Binance PAXG"
    except Exception:
        pass

    # 3. FALLBACK: GoldAPI.io
    try:
        headers = {'x-access-token': CONFIG['GOLDAPI_KEY'], 'Content-Type': 'application/json'}
        r = requests.get(CONFIG['GOLDAPI_URL'], headers=headers, timeout=4)
        if r.status_code == 200:
            d = r.json()
            p = float(d.get('price', 0))
            if p > 0:
                bid = float(d.get('bid', p - 0.3))
                ask = float(d.get('ask', p + 0.3))
                market_meta['bid'] = bid
                market_meta['ask'] = ask
                market_meta['spread'] = round(ask - bid, 2)
                market_meta['source'] = "GoldAPI"
                return p, bid, ask, "GoldAPI"
    except Exception:
        pass

    return None, None, None, None


def initialize_historical_bars():
    """Load historical bars: SQLite first (for chart continuity), then Binance for fresh data."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    
    # STEP 1: Try loading saved bars from SQLite (chart persistence)
    saved_bars = load_bars_from_db_bars(hours=4)
    if saved_bars and len(saved_bars) >= 10:
        logger.info(f"📦 V2: Found {len(saved_bars)} saved bars in database — restoring chart...")
        with data_lock:
            historical_bars = saved_bars[-200:]  # Keep last 200
            # Restore cumulative values from saved data
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
        logger.info(f"✅ V2: Restored {len(historical_bars)} bars from database | Last: ${last_known_price:.2f}")
        
        # Also try to fetch latest bars from Binance to fill any gap
        try:
            last_bar_time = historical_bars[-1]['time']
            minutes_gap = int((datetime.now() - last_bar_time).total_seconds() / 60)
            if minutes_gap > 1:
                logger.info(f"📡 V2: Filling {minutes_gap} minute gap from Binance with precise timestamps...")
                start_ts = int(last_bar_time.timestamp() * 1000)
                end_ts = int(datetime.now().timestamp() * 1000)
                r = requests.get('https://api.binance.com/api/v3/klines',
                                 params={'symbol': 'PAXGUSDT', 'interval': '1m', 'startTime': start_ts, 'endTime': end_ts, 'limit': 1000},
                                 timeout=10)
                if r.status_code == 200:
                    klines = r.json()
                    new_count = 0
                    with data_lock:
                        for k in klines:
                            bar_time = datetime.fromtimestamp(k[0] / 1000).replace(second=0, microsecond=0)
                            if bar_time > last_bar_time:
                                open_p, high_p, low_p, close_p = float(k[1]), float(k[2]), float(k[3]), float(k[4])
                                vol = float(k[5])
                                taker_buy_vol = float(k[9])
                                delta = taker_buy_vol - (vol - taker_buy_vol)
                                cum_delta += delta
                                cum_vol += vol
                                cum_pv += ((open_p + high_p + low_p + close_p) / 4) * vol
                                vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else close_p
                                bar = {
                                    'time': bar_time, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                                    'volume': vol, 'delta': delta, 'cvd': cum_delta,
                                    'vwap': vwap, 'poc': round((high_p + low_p) / 2, 2),
                                    'imbalance': False, 'fvg': None,
                                    'phase': "Distribution" if delta > 0 else "Accumulation",
                                    'confidence': 50, 'absorption': None, 'cvd_divergence': None,
                                    'signal_type': 'NONE', 'session': ''
                                }
                                historical_bars.append(bar)
                                save_bar_to_db_bars(bar)
                                new_count += 1
                        if historical_bars:
                            last_known_price = historical_bars[-1]['close']
                    if new_count > 0:
                        logger.info(f"📡 V2: Filled {new_count} new bars from Binance")
        except Exception as e:
            logger.warning(f"Gap fill failed: {e}")
        
        cleanup_old_bars(24)
        return

    # STEP 2: No saved bars — fetch full history from Binance
    logger.info("V2: No saved bars found. Fetching REAL historical bars from Binance PAXG Klines...")

    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': 'PAXGUSDT', 'interval': '1m', 'limit': 120},
                         timeout=10)
        if r.status_code == 200:
            klines = r.json()
            with data_lock:
                historical_bars.clear()
                cum_vol = 0.0
                cum_pv = 0.0
                cum_delta = 0.0

                for k in klines:
                    bar_time = datetime.fromtimestamp(k[0] / 1000).replace(second=0, microsecond=0)
                    open_p = float(k[1])
                    high_p = float(k[2])
                    low_p = float(k[3])
                    close_p = float(k[4])
                    vol = float(k[5])
                    taker_buy_vol = float(k[9])  # Taker buy base asset volume
                    taker_sell_vol = vol - taker_buy_vol

                    # Real delta from actual exchange data!
                    delta = taker_buy_vol - taker_sell_vol
                    cum_delta += delta
                    cum_vol += vol
                    cum_pv += ((open_p + high_p + low_p + close_p) / 4) * vol
                    vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else close_p

                    bar = {
                        'time': bar_time, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                        'volume': vol, 'delta': delta, 'cvd': cum_delta,
                        'vwap': vwap, 'poc': round((high_p + low_p) / 2, 2),
                        'imbalance': False, 'fvg': None,
                        'phase': "Distribution" if delta > 0 else "Accumulation",
                        'confidence': 50, 'absorption': None, 'cvd_divergence': None,
                        'signal_type': 'NONE', 'session': ''
                    }
                    historical_bars.append(bar)

                last_known_price = historical_bars[-1]['close']
                market_meta['open'] = historical_bars[0]['open']
                market_meta['high'] = max(b['high'] for b in historical_bars)
                market_meta['low'] = min(b['low'] for b in historical_bars)
                market_meta['bid'] = last_known_price - 0.2
                market_meta['ask'] = last_known_price + 0.2

            logger.info(f"✅ V2: Loaded {len(historical_bars)} REAL bars from Binance | Last: ${last_known_price:.2f}")
            return
    except Exception as e:
        logger.warning(f"Binance Klines failed: {e}")

    # Fallback: synthetic bars anchored to live price
    logger.info("V2: Falling back to synthetic bars...")
    anchor_price = 2650.0
    price, bid, ask, source = _fetch_live_price()
    if price and price > 0:
        anchor_price = price
    _generate_synthetic_bars(anchor_price)


def _generate_synthetic_bars(anchor_price):
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price
    now = datetime.now()
    start_time = now - timedelta(minutes=120)
    current_p = anchor_price - random.uniform(1.0, 3.0)

    with data_lock:
        historical_bars.clear()
        cum_vol = 0.0
        cum_pv = 0.0
        cum_delta = 0.0
        for i in range(120):
            bar_time = (start_time + timedelta(minutes=i)).replace(second=0, microsecond=0)
            drift = random.uniform(-0.5, 0.5)
            open_p = round(current_p, 2)
            close_p = round(open_p + drift, 2)
            high_p = round(max(open_p, close_p) + random.uniform(0.1, 0.4), 2)
            low_p = round(min(open_p, close_p) - random.uniform(0.1, 0.4), 2)
            vol = float(random.randint(400, 1600))
            delta = float(int(vol * random.uniform(0.08, 0.32) * (1 if close_p > open_p else -1)))

            cum_vol += vol
            cum_pv += ((open_p + high_p + low_p + close_p) / 4) * vol
            cum_delta += delta
            vwap = round(cum_pv / cum_vol, 2)

            bar = {
                'time': bar_time, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                'volume': vol, 'delta': delta, 'cvd': cum_delta,
                'vwap': vwap, 'poc': round((high_p + low_p) / 2, 2),
                'imbalance': vol > 1200, 'fvg': None,
                'phase': "Distribution" if cum_delta > 0 else "Accumulation",
                'confidence': random.randint(40, 75), 'absorption': None, 'cvd_divergence': None,
                'signal_type': 'NONE', 'session': ''
            }
            historical_bars.append(bar)
            current_p = close_p

        historical_bars[-1]['close'] = anchor_price
        last_known_price = anchor_price

    logger.info(f"✅ V2: Synthetic bars loaded at ${anchor_price:.2f}")


# ============================================================
# 7. LIVE DATA POLLING (Bid/Ask Based Delta)
# ============================================================
def live_data_thread():
    global last_known_price, market_meta, is_running
    logger.info("V2: Live Data Worker Active (Binance PAXG, 3s interval)...")

    while is_running:
        price, bid, ask, source = _fetch_live_price()
        if price and price > 0:
            market_meta['bid'] = bid
            market_meta['ask'] = ask
            market_meta['high'] = max(market_meta.get('high', price), price)
            market_meta['low'] = min(market_meta.get('low', price), price)

            vol = float(random.randint(60, 180))
            # Bid/Ask based aggressor detection (institutional standard)
            mid = (bid + ask) / 2
            is_buy = price > mid
            process_tick(price, vol, is_buy)
            logger.info(f"💹 V2 [{source}] {datetime.now().strftime('%H:%M:%S')} | ${price:.2f} | Bid: ${bid:.2f} Ask: ${ask:.2f} | CVD: {cum_delta:+.0f}")

        # Micro ticks (balanced 50/50)
        for _ in range(3):
            if not is_running:
                break
            time.sleep(1.0)
            micro_vol = float(random.randint(12, 35))
            micro_drift = random.choice([-0.06, -0.04, -0.02, 0.02, 0.04, 0.06])
            micro_p = round(last_known_price + micro_drift, 2)
            process_tick(micro_p, micro_vol, micro_drift > 0)


# ============================================================
# 8. DASH UI LAYOUT (PREMIUM V2)
# ============================================================
v2_app = dash.Dash(__name__, external_stylesheets=['https://codepen.io/chriddyp/pen/bWLwgP.css'])
v2_app.title = "⚡ XAUUSD V2 Institutional Terminal"

v2_app.layout = html.Div([
    # Header
    html.Div([
        html.Div([
            html.Span("⚡", style={'fontSize': '28px', 'marginRight': '10px'}),
            html.Span("XAUUSD V2", style={'fontSize': '22px', 'fontWeight': '800', 'color': '#FFD700'}),
            html.Span(" — Institutional Order Flow Terminal", style={'fontSize': '14px', 'color': '#888', 'marginLeft': '8px'}),
        ], style={'display': 'flex', 'alignItems': 'center'}),
        html.Div(id='session-badge', style={'display': 'flex', 'alignItems': 'center', 'gap': '10px'}),
    ], style={
        'display': 'flex', 'justifyContent': 'space-between', 'alignItems': 'center',
        'padding': '12px 24px', 'background': 'linear-gradient(135deg, #0d0d1a 0%, #1a1a2e 100%)',
        'borderBottom': '2px solid #FFD700'
    }),

    # Risk Manager + Stats Cards Row
    html.Div([
        # Live Price Card
        html.Div([
            html.Div("LIVE PRICE", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-live-price', children="$0.00", style={'fontSize': '28px', 'fontWeight': '800', 'color': '#FFD700'}),
            html.Div(id='v2-bid-ask', children="Bid/Ask: --", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '160px'}),

        # Session Card
        html.Div([
            html.Div("SESSION", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-session-name', children="--", style={'fontSize': '18px', 'fontWeight': '700', 'color': '#00e5ff'}),
            html.Div(id='v2-session-quality', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '140px'}),

        # CVD Card
        html.Div([
            html.Div("CVD FLOW", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-cvd-val', children="0", style={'fontSize': '18px', 'fontWeight': '700'}),
            html.Div(id='v2-phase', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '130px'}),

        # Confidence Card
        html.Div([
            html.Div("CONFIDENCE", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-confidence', children="0%", style={'fontSize': '18px', 'fontWeight': '700'}),
            html.Div(id='v2-signal-type', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '120px'}),

        # Risk Manager Card
        html.Div([
            html.Div("RISK MANAGER", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-risk-status', children="OK", style={'fontSize': '16px', 'fontWeight': '700', 'color': '#089981'}),
            html.Div(id='v2-daily-pnl', children="P/L: $0.00", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '140px'}),

        # Trades Today Card
        html.Div([
            html.Div("TRADES TODAY", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase', 'letterSpacing': '1px'}),
            html.Div(id='v2-trades-count', children="0", style={'fontSize': '18px', 'fontWeight': '700', 'color': '#d1d4dc'}),
            html.Div(id='v2-consec-losses', children="Losses: 0", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '120px'}),
    ], style={'display': 'flex', 'gap': '12px', 'padding': '15px 24px', 'overflowX': 'auto', 'backgroundColor': '#111'}),

    # TradingView Widget
    html.Iframe(
        src="https://s.tradingview.com/widgetembed/?symbol=OANDA%3AXAUUSD&interval=1&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en&hide_side_toolbar=0&allow_symbol_change=1",
        width="100%", height="420", style={'border': 'none', 'borderRadius': '8px', 'margin': '0 24px', 'maxWidth': 'calc(100% - 48px)'}
    ),

    html.Hr(style={'borderColor': '#2a2e39', 'margin': '10px 24px'}),

    # Time Frame Selector
    html.Div([
        html.Label("🔬 V2 Order Flow Analysis", style={'color': '#00e5ff', 'fontWeight': '700', 'fontSize': '16px', 'marginRight': '20px'}),
        html.Label("Time Frame:", style={'color': '#aaa', 'marginRight': '8px'}),
        dcc.Dropdown(
            id='v2-tf-select',
            options=[{'label': 'M1', 'value': 'M1'}, {'label': 'M5', 'value': 'M5'}, {'label': 'M15', 'value': 'M15'}],
            value='M1', clearable=False, searchable=False,
            style={'width': '100px', 'display': 'inline-block', 'color': '#111', 'verticalAlign': 'middle'}),
    ], style={'padding': '10px 24px', 'display': 'flex', 'alignItems': 'center'}),

    dcc.Interval(id='v2-live-update', interval=1000, n_intervals=0),
    html.Div(id='v2-signal-banner', style={'margin': '0 24px 10px 24px'}),
    dcc.Graph(id='v2-chart', style={'height': '55vh', 'margin': '0 24px'}),

    # Trade History Table
    html.Hr(style={'borderColor': '#2a2e39', 'margin': '20px 24px'}),
    html.H4("📋 V2 Trade History (BUY / SELL + SL/TP + Session + Signal)",
            style={'textAlign': 'center', 'color': '#FFD700', 'marginBottom': '15px', 'fontWeight': 'bold'}),
    html.Div(id='v2-trade-table', style={'overflowX': 'auto', 'margin': '0 24px 30px'}),

], style={'backgroundColor': '#0b0e14', 'padding': '0', 'color': '#d1d4dc', 'minHeight': '100vh', 'fontFamily': '-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif'})


# ============================================================
# 9. CALLBACKS
# ============================================================

# Stats Cards Callback
@v2_app.callback(
    [Output('v2-live-price', 'children'), Output('v2-live-price', 'style'),
     Output('v2-bid-ask', 'children'),
     Output('v2-session-name', 'children'), Output('v2-session-name', 'style'),
     Output('v2-session-quality', 'children'),
     Output('v2-cvd-val', 'children'), Output('v2-cvd-val', 'style'),
     Output('v2-phase', 'children'),
     Output('v2-confidence', 'children'), Output('v2-confidence', 'style'),
     Output('v2-signal-type', 'children'),
     Output('v2-risk-status', 'children'), Output('v2-risk-status', 'style'),
     Output('v2-daily-pnl', 'children'),
     Output('v2-trades-count', 'children'),
     Output('v2-consec-losses', 'children'),
     Output('session-badge', 'children')],
    [Input('v2-live-update', 'n_intervals')]
)
def update_stats(n):
    session = get_session_info()
    risk = risk_manager.get_status()

    price_str = f"${last_known_price:.2f}"
    price_style = {'fontSize': '28px', 'fontWeight': '800', 'color': '#FFD700'}
    src = market_meta.get('source', 'Live')
    sp = market_meta.get('spread', 0.20)
    bid_ask = f"B: ${market_meta.get('bid', 0):.2f} | A: ${market_meta.get('ask', 0):.2f} | Sp: ${sp:.2f} ({src})"

    session_style = {'fontSize': '18px', 'fontWeight': '700', 'color': session['color']}
    session_quality = f"Quality: {session['quality']}" + (" 🔥 KILL ZONE" if session['kill_zone'] else "")

    cvd_str = f"{cum_delta:+.0f}"
    cvd_color = '#089981' if cum_delta >= 0 else '#f23645'
    cvd_style = {'fontSize': '18px', 'fontWeight': '700', 'color': cvd_color}

    with data_lock:
        last_bar = historical_bars[-1] if historical_bars else {}
    phase = last_bar.get('phase', 'Neutral')
    conf = last_bar.get('confidence', 50)
    sig = last_bar.get('signal_type', 'NONE')

    conf_str = f"{conf}%"
    conf_color = '#089981' if conf >= 65 else ('#ffd700' if conf >= 50 else '#f23645')
    conf_style = {'fontSize': '18px', 'fontWeight': '700', 'color': conf_color}
    sig_str = f"Signal: {sig}"

    risk_str = "⚡ UNLOCKED" if risk['can_trade'] else f"⛔ {risk['reason']}"
    risk_color = '#089981' if risk['can_trade'] else '#f23645'
    risk_style = {'fontSize': '16px', 'fontWeight': '700', 'color': risk_color}
    daily_pnl_str = f"Daily P/L: ${risk['daily_pnl']:+.2f}"

    trades_str = str(risk['trades_today'])
    losses_str = f"Losses: {risk['consecutive_losses']} (Continuous ⚡)"

    # Session badge for header
    badge = html.Div([
        html.Span(session['emoji'], style={'fontSize': '20px'}),
        html.Span(f" {session['name']}", style={'color': session['color'], 'fontWeight': '700', 'fontSize': '14px'}),
        html.Span(f" | {datetime.now().strftime('%H:%M:%S')}", style={'color': '#555', 'fontSize': '12px', 'marginLeft': '8px'}),
    ])

    return (price_str, price_style, bid_ask,
            f"{session['emoji']} {session['name']}", session_style, session_quality,
            cvd_str, cvd_style, f"Phase: {phase}",
            conf_str, conf_style, sig_str,
            risk_str, risk_style, daily_pnl_str,
            trades_str, losses_str, badge)


# Standalone Clean Signal Banner Callback (No overlap, backwards compatible)
@v2_app.callback(
    Output('v2-signal-banner', 'children'),
    [Input('v2-live-update', 'n_intervals')]
)
def update_v2_signal_banner(n):
    session = get_session_info()
    with data_lock:
        last_bar = historical_bars[-1] if historical_bars else {}
    phase = last_bar.get('phase', 'Neutral')
    conf = last_bar.get('confidence', 50)
    if trade_state.get('in_position'):
        d = trade_state['direction']
        running_pnl = (last_known_price - trade_state['entry_price']) if d == "LONG" else (trade_state['entry_price'] - last_known_price)
        return html.Div(
            f"📈 LIVE POSITION: {d} @ ${trade_state['entry_price']:.2f} | SL: ${trade_state['stop_loss']:.2f} | TP: ${trade_state['take_profit']:.2f} | RUNNING P/L: ${running_pnl:+.2f}",
            style={'backgroundColor': 'rgba(8, 153, 129, 0.15)', 'border': '1px solid #089981', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px', 'boxShadow': '0 2px 10px rgba(8, 153, 129, 0.2)'}
        )
    elif conf >= 55:
        dirn = "LONG 🟢" if phase in ["Distribution", "Bullish Divergence"] else "SHORT 🔴"
        return html.Div(
            f"🔔 ACTIVE AI SIGNAL: {dirn} @ ${last_known_price:.2f} | Confidence: {conf}% | {session['emoji']} {session['name']}",
            style={'backgroundColor': 'rgba(255, 215, 0, 0.12)', 'border': '1px solid #FFD700', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px'}
        )
    return html.Div()


# Trade History Table
@v2_app.callback(
    Output('v2-trade-table', 'children'),
    [Input('v2-live-update', 'n_intervals')]
)
def update_trade_table(n):
    header_style = {
        'backgroundColor': '#131722', 'color': '#FFD700', 'padding': '11px 14px',
        'textAlign': 'center', 'fontWeight': '700', 'fontSize': '11px',
        'textTransform': 'uppercase', 'letterSpacing': '0.6px',
        'borderBottom': '2px solid #FFD700'
    }
    cell_style = {
        'backgroundColor': '#0b0e14', 'color': '#d1d4dc', 'padding': '9px 12px',
        'textAlign': 'center', 'fontSize': '12px', 'borderBottom': '1px solid #1a1e29'
    }
    headers = ['#', 'Open Time', 'Exit Time', 'Type', 'Open Price', 'Exit Price', 'SL', 'TP', 'P/L ($)', 'Status', 'Session']
    thead = html.Thead(html.Tr([html.Th(h, style=header_style) for h in headers]))

    rows = []
    with data_lock:
        display_trades = list(reversed(trade_history[-40:]))
    for t in display_trades:
        pnl_val = t['pnl']
        pnl_color = '#089981' if pnl_val > 0 else ('#f23645' if pnl_val < 0 else '#aaa')
        pnl_text = f"+${pnl_val:.2f}" if pnl_val > 0 else (f"-${abs(pnl_val):.2f}" if pnl_val < 0 else "$0.00")
        
        status = t.get('status', '⏳ OPEN')
        status_color = '#089981' if 'TP' in status else ('#f23645' if 'SL' in status else '#FFD700')
        dir_color = '#089981' if 'BUY' in t['dir'] else '#f23645'

        exit_tm = t.get('exit_time', '--')
        exit_time_color = '#ffd700' if 'RUNNING' in str(exit_tm) or 'OPEN' in status else '#888'
        
        # Exit price: if open, show current running price; otherwise show recorded exit price
        if 'OPEN' in status:
            exit_pr_str = f"${last_known_price:.2f}"
            exit_pr_color = '#ffd700'
        else:
            ep = t.get('exit_price', 0.0)
            exit_pr_str = f"${ep:.2f}" if ep > 0 else f"${t['entry']:.2f}"
            exit_pr_color = '#fff'

        row = html.Tr([
            html.Td(t['id'], style={**cell_style, 'color': '#787b86', 'fontWeight': '600'}),
            html.Td(t.get('time', '--'), style={**cell_style, 'color': '#e0e3eb', 'fontWeight': '500'}),
            html.Td(exit_tm, style={**cell_style, 'color': exit_time_color, 'fontWeight': '500'}),
            html.Td(t['dir'], style={**cell_style, 'color': dir_color, 'fontWeight': '700'}),
            html.Td(f"${t['entry']:.2f}", style={**cell_style, 'color': '#fff', 'fontWeight': '600'}),
            html.Td(exit_pr_str, style={**cell_style, 'color': exit_pr_color, 'fontWeight': '600'}),
            html.Td(f"${t['sl']:.2f}", style={**cell_style, 'color': '#f23645', 'fontWeight': '500'}),
            html.Td(f"${t['tp']:.2f}", style={**cell_style, 'color': '#089981', 'fontWeight': '500'}),
            html.Td(pnl_text, style={**cell_style, 'color': pnl_color, 'fontWeight': '700'}),
            html.Td(status, style={**cell_style, 'color': status_color, 'fontWeight': '700'}),
            html.Td(t.get('session', '-'), style={**cell_style, 'color': '#00e5ff', 'fontSize': '11px'}),
        ])
        rows.append(row)

    if not rows:
        rows = [html.Tr([html.Td('No trades recorded yet — waiting for market signals...', colSpan=11,
                style={**cell_style, 'color': '#666', 'fontStyle': 'italic', 'padding': '24px'})])]

    tbody = html.Tbody(rows)
    table = html.Table([thead, tbody], style={
        'width': '100%', 'borderCollapse': 'collapse', 'borderRadius': '8px',
        'overflow': 'hidden', 'border': '1px solid #1e222d',
        'boxShadow': '0 4px 20px rgba(0, 0, 0, 0.3)'
    })
    return table


# Chart Callback
@v2_app.callback(
    Output('v2-chart', 'figure'),
    [Input('v2-live-update', 'n_intervals'), Input('v2-tf-select', 'value')]
)
def update_chart(n, tf='M1'):
    if tf is None:
        tf = 'M1'
    with data_lock:
        if not historical_bars:
            initialize_historical_bars()
        bars = [dict(b) for b in historical_bars]

        if current_bar['close'] is not None:
            curr = {
                'time': current_bar['time'] if current_bar['time'] else datetime.now().replace(second=0, microsecond=0),
                'open': current_bar['open'], 'high': current_bar['high'],
                'low': current_bar['low'], 'close': current_bar['close'],
                'volume': current_bar['volume'],
                'delta': current_bar['buy_vol'] - current_bar['sell_vol'],
                'cvd': cum_delta,
                'vwap': current_bar.get('vwap', last_known_price),
                'poc': current_bar.get('poc', last_known_price),
                'imbalance': current_bar['volume'] > 1200, 'fvg': None,
                'phase': bars[-1]['phase'] if bars else "Neutral",
                'confidence': bars[-1]['confidence'] if bars else 50,
                'absorption': None, 'cvd_divergence': None, 'signal_type': 'NONE', 'session': ''
            }
        else:
            curr = None

    all_bars = bars + ([curr] if curr else [])
    if not all_bars:
        initialize_historical_bars()
        all_bars = [dict(b) for b in historical_bars]

    # Time frame aggregation
    if tf and tf != 'M1' and all_bars:
        mins = int(tf[1:])
        resampled = []
        for b in all_bars:
            t = b['time'].replace(minute=(b['time'].minute // mins) * mins, second=0)
            if resampled and resampled[-1]['time'] == t:
                l = resampled[-1]
                l['high'] = max(l['high'], b['high'])
                l['low'] = min(l['low'], b['low'])
                l['close'] = b['close']
                l['volume'] = l['volume'] + b['volume']
            else:
                nb = dict(b)
                nb['time'] = t
                resampled.append(nb)
        all_bars = resampled

    df = pd.DataFrame(all_bars)
    df['delta'] = pd.to_numeric(df.get('delta', 0), errors='coerce').fillna(0.0)
    df['cvd'] = pd.to_numeric(df.get('cvd', 0), errors='coerce').fillna(cum_delta)
    df['confidence'] = pd.to_numeric(df.get('confidence', 50), errors='coerce').fillna(50)
    
    # True Institutional Dynamic VWAP (Smooth cumulative benchmark like TradingView)
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cum_pv_series = (typical_price * df['volume']).cumsum()
    cum_vol_series = df['volume'].cumsum()
    df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])

    # Format time as continuous category string to eliminate flat gaps on restarts
    df['time_str'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')

    last = df.iloc[-1]
    last_price = float(last['close'])
    cur_delta = float(last['delta'])
    cur_cvd = float(cum_delta)
    cur_vwap = float(last['vwap'])
    cur_phase = str(last.get('phase', 'Neutral'))
    cur_conf = int(last['confidence'])
    session = get_session_info()
    live_ts = datetime.now().strftime('%H:%M:%S')

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.42, 0.18, 0.20, 0.20],
        subplot_titles=(
            f"● LIVE [{live_ts}] ${last_price:.2f} | VWAP: ${cur_vwap:.2f} | {cur_phase} | {session['emoji']} {session['name']}",
            f"Volume Delta ({cur_delta:+.0f} Δ) & POC",
            f"CVD ({cur_cvd:+.0f})",
            f"Confluence Score ({cur_conf}%)"
        )
    )

    # Row 1: Candlestick + VWAP + Signals
    fig.add_trace(go.Candlestick(
        x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        name="XAUUSD", increasing_line_color='#089981', decreasing_line_color='#f23645', showlegend=False
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df['time_str'], y=df['vwap'], mode='lines', name='VWAP',
        line=dict(color='#ffd700', width=2)
    ), row=1, col=1)

    # Imbalance markers
    imb_mask = df.get('imbalance', pd.Series([False]*len(df))) == True
    if imb_mask.any():
        fig.add_trace(go.Scatter(
            x=df[imb_mask]['time_str'], y=df[imb_mask]['high'] * 1.0002, mode='markers',
            marker=dict(symbol='star', size=12, color='#00e5ff'), name='⚡ Imbalance'
        ), row=1, col=1)

    # Trade markers
    if trade_state.get('in_position'):
        fig.add_hline(y=trade_state['entry_price'], line_color='#2962ff', line_width=2, line_dash='dash', row=1, col=1,
                      annotation_text=f"Entry ${trade_state['entry_price']:.2f}")
        fig.add_hline(y=trade_state['stop_loss'], line_color='#f23645', line_width=2, line_dash='dot', row=1, col=1,
                      annotation_text=f"SL ${trade_state['stop_loss']:.2f}")
        fig.add_hline(y=trade_state['take_profit'], line_color='#089981', line_width=2, line_dash='dot', row=1, col=1,
                      annotation_text=f"TP ${trade_state['take_profit']:.2f}")

    # Row 2: Delta
    colors_delta = ['#089981' if d >= 0 else '#f23645' for d in df['delta']]
    fig.add_trace(go.Bar(x=df['time_str'], y=df['delta'], name='Delta', marker_color=colors_delta), row=2, col=1)

    # Row 3: CVD
    fig.add_trace(go.Scatter(
        x=df['time_str'], y=df['cvd'], mode='lines', name='CVD',
        line=dict(color='#00e5ff', width=2), fill='tozeroy', fillcolor='rgba(0,229,255,0.15)'
    ), row=3, col=1)

    # Row 4: Confidence
    fig.add_trace(go.Bar(
        x=df['time_str'], y=df['confidence'], name='Confidence %',
        marker_color=df['confidence'], marker_colorscale='RdYlGn',
        text=[f"{int(c)}%" for c in df['confidence']], textposition='outside'
    ), row=4, col=1)
    fig.add_hline(y=65, line_dash="dash", line_color="#ffd700", row=4, col=1, annotation_text="Entry Threshold")
    fig.add_hline(y=55, line_dash="dot", line_color="#ff9800", row=4, col=1, annotation_text="Kill Zone Threshold")

    fig.update_layout(
        template='plotly_dark', height=850, showlegend=True, hovermode='x unified',
        plot_bgcolor='#0b0e14', paper_bgcolor='#0b0e14',
        font=dict(color='#d1d4dc', family='-apple-system, BlinkMacSystemFont, Segoe UI, Roboto'),
        margin=dict(t=70, b=30, l=50, r=50),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1)
    )
    fig.update_xaxes(type='category', rangeslider_visible=False, gridcolor='#1a1a2e', nticks=15)
    fig.update_yaxes(gridcolor='#1a1a2e')

    return fig


# ============================================================
# 10. STARTUP
# ============================================================
if __name__ == '__main__':
    logger.info("🚀 Starting XAUUSD V2 — Institutional Order Flow Terminal...")
    
    # Register graceful shutdown handlers
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(lambda: logger.info("V2: atexit cleanup complete"))
    
    init_db()
    load_trades_from_db()
    initialize_historical_bars()
    data_thread = threading.Thread(target=live_data_thread, daemon=True)
    data_thread.start()
    logger.info("🌐 V2 Dashboard: http://0.0.0.0:8060")
    logger.info("📊 Features: Kill Zones | CVD Divergence | Confluence Scoring | Risk Manager | SQLite | Bar Persistence")
    v2_app.run(debug=False, use_reloader=False, host="0.0.0.0", port=8060)
