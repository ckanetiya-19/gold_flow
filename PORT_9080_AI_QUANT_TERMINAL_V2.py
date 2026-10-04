# -----------------------------------------------------------------------------
# GOLD.FLOW // AI QUANT TERMINAL - HEDGE-FUND PILOT UPGRADE
# NEW file, NEW port (9080). PORT_8080_AI_QUANT_TERMINAL.py and its live port
# 8080 are completely untouched and keep running independently.
#
# Upgrades over V1 (PORT_8080_AI_QUANT_TERMINAL.py):
#   1. DOM/Depth ladder: V1 used random.uniform(25.0, 180.0) for every book
#      level. V2 renders the REAL top-10 Binance order book relayed by
#      Port 9000.
#   2. Tick volume/side: V1 used random.randint(50,160) fake volume and a
#      coin-flip fallback for aggressor side. V2 uses real aggTrade volume
#      and real side from Port 9000.
#   3. "Confidence" score: V1 had confidence: random.randint(68, 92) in
#      finalize_bar - pure noise. V2 computes a real rule-based score from
#      delta/VWAP alignment, imbalance, FVG and absorption.
#   4. Finalized bars persisted to QuestDB (bars_v2_9080) for durable,
#      backtestable storage.
#   5. Binds to 127.0.0.1 only (V1 bound 0.0.0.0).
#   6. Resilient reconnect-with-backoff to Port 9000 with a visible feed
#      status indicator.
#
# CHOP (Bill Dreiss Choppiness Index) + KER (Kaufman Efficiency Ratio)
# regime filter, the dual-engine VWAP Quant Sniper + Confluence backup, and
# the Manual Cut button are kept exactly as V1 - that was already genuine
# quantitative math, not random. Still PAPER trading only.
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
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from websockets.sync.client import connect as ws_connect
from questdb import Sender, TimestampNanos

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [QUANT_TERMINAL_V2] - %(message)s')
logger = logging.getLogger("XAUUSD_QUANT_TERMINAL_V2")

# Temporarily hides the Entry/SL/TP price lines on this dashboard's own chart
# (user request, 2026-09-16) - flip back to True to restore them. Does not
# affect trade_state, signals, or MT5 execution - display only.
SHOW_ENTRY_SL_TP_LINES = False

BINANCE_SYMBOL = os.getenv("GOLDFLOW_BINANCE_SYMBOL", "PAXGUSDT")
ENGINE_WS_URL = os.getenv("GOLDFLOW_ENGINE_WS_URL", "ws://127.0.0.1:9000/ws")
QUESTDB_ILP_CONF = os.getenv("GOLDFLOW_QUESTDB_ILP_CONF", "tcp::addr=127.0.0.1:9009;")
DASH_HOST = os.getenv("GOLDFLOW_DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.getenv("GOLDFLOW_DASH_PORT", "9080"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trades_quant_v2_9080.db')

# =============================================================================
# MT5 DEMO AUTO-EXECUTION (optional, off by default) - attaches to an MT5
# terminal the user has ALREADY opened and logged into themselves; no
# credentials ever pass through this code or any env var. If the terminal
# isn't running/logged in, or the MetaTrader5 package isn't installed, this
# silently stays disabled and the dashboard behaves exactly as before
# (paper-only simulation).
# =============================================================================
try:
    import MetaTrader5 as mt5
    MT5_PACKAGE_AVAILABLE = True
except ImportError:
    MT5_PACKAGE_AVAILABLE = False

MT5_SYMBOL = os.getenv("GOLDFLOW_MT5_SYMBOL", "XAUUSD")
MT5_LOT = float(os.getenv("GOLDFLOW_MT5_LOT", "0.01"))
MT5_MAGIC = 90800
_mt5_ready = False
# Master switch: MT5 order placement is OFF unless GOLDFLOW_MT5_TRADING_ENABLED=1.
# Paper trading (signals, trade log, DB) keeps running; only MT5 is never touched.
MT5_TRADING_ENABLED = os.getenv("GOLDFLOW_MT5_TRADING_ENABLED", "0") == "1"
# Strategy master switch: ALL entry strategies (Sniper, Confluence, S1-S5, HFT-Engine) are OFF
# unless GOLDFLOW_STRATEGIES_ENABLED=1. Order-flow analytics/charts/regime keep running.
STRATEGIES_ENABLED = os.getenv("GOLDFLOW_STRATEGIES_ENABLED", "0") == "1"
if not STRATEGIES_ENABLED:
    logger.info("STRATEGIES DISABLED (master switch off) - order-flow analytics only, no signals/trades.")


def mt5_init():
    global _mt5_ready
    if not MT5_TRADING_ENABLED:
        logger.info("MT5: trading DISABLED (master switch off) - paper trading only, MT5 not touched.")
        return
    if not MT5_PACKAGE_AVAILABLE:
        logger.warning("MT5: MetaTrader5 package not installed - auto-execution disabled.")
        return
    if not mt5.initialize():
        logger.warning(f"MT5: terminal not reachable ({mt5.last_error()}). Open MT5, log into your "
                        f"demo account, then restart this dashboard to enable auto-execution.")
        return
    acc = mt5.account_info()
    if acc is None:
        logger.warning("MT5: terminal connected but no account is logged in - auto-execution disabled.")
        return
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        logger.warning(f"MT5: account #{acc.login} is NOT a demo account (trade_mode={acc.trade_mode}) - "
                        f"auto-execution disabled as a safety precaution.")
        return
    if mt5.symbol_info(MT5_SYMBOL) is None:
        logger.warning(f"MT5: symbol '{MT5_SYMBOL}' not found in Market Watch. Set GOLDFLOW_MT5_SYMBOL "
                        f"to your broker's exact gold symbol name. Auto-execution disabled.")
        return
    mt5.symbol_select(MT5_SYMBOL, True)
    _mt5_ready = True
    logger.info(f"MT5 CONNECTED (DEMO): account #{acc.login} on '{acc.server}' (balance ${acc.balance:.2f}) "
                f"- auto-execution ARMED for {MT5_SYMBOL}, {MT5_LOT} lot, magic={MT5_MAGIC}.")


def mt5_place_order(direction, sl, tp, magic=None, comment=None):
    if not MT5_TRADING_ENABLED or not _mt5_ready:
        return None
    try:
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            logger.error("MT5: connected account is NOT a demo account - order BLOCKED (demo-only safety).")
            return None
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if not tick:
            logger.error("MT5: no live tick for symbol, order skipped.")
            return None
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction == "BUY" else tick.bid

        # Callers compute sl/tp from their own reference price (e.g. iTick-
        # driven last_known_price), which can drift from the broker's own
        # live tick. If that drift makes the absolute sl/tp inconsistent
        # with THIS fresh broker price (e.g. a BUY's tp ends up below the
        # broker's current price), re-anchor them here, preserving the
        # caller's intended distance - otherwise MT5 rejects the whole
        # request, sometimes surfacing a confusing generic retcode instead
        # of "invalid stops".
        sl, tp = float(sl), float(tp)
        is_buy = direction == "BUY"
        no_tp = tp == 0.0   # tp=0 means "no take-profit" (e.g. trailing-stop strategies)
        sl_bad = (is_buy and sl >= price) or (not is_buy and sl <= price)
        tp_bad = (not no_tp) and ((is_buy and tp <= price) or (not is_buy and tp >= price))
        if sl_bad or tp_bad:
            sl_dist = abs(price - sl) if abs(price - sl) > 0.05 else 0.5
            sl = round(price - sl_dist, 2) if is_buy else round(price + sl_dist, 2)
            if not no_tp:
                tp_dist = abs(tp - price) if abs(tp - price) > 0.05 else 0.8
                tp = round(price + tp_dist, 2) if is_buy else round(price - tp_dist, 2)
            logger.warning(f"MT5: caller's sl/tp were inconsistent with broker's live price "
                            f"({price}) - re-anchored to sl={sl} tp={tp}")

        # SYMBOL_FILLING_MODE bitmask: bit0(1)=FOK supported, bit1(2)=IOC
        # supported. No bit means RETURN is the broker's only accepted mode.
        # Ask first instead of blind-guessing - avoids repeated rejected
        # attempts (retcode 10030 "Unsupported filling mode" seen otherwise).
        sym_info = mt5.symbol_info(MT5_SYMBOL)
        fmode = sym_info.filling_mode if sym_info else 0
        candidates = []
        if fmode & 2:
            candidates.append(mt5.ORDER_FILLING_IOC)
        if fmode & 1:
            candidates.append(mt5.ORDER_FILLING_FOK)
        candidates.append(mt5.ORDER_FILLING_RETURN)
        result = None
        for filling in candidates:
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": MT5_SYMBOL, "volume": MT5_LOT,
                "type": order_type, "price": price, "sl": float(sl), "tp": float(tp),
                "deviation": 20, "magic": magic or MT5_MAGIC, "comment": comment or "Goldflow-9080-Auto",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[MT5 DEMO ORDER] {direction} {MT5_LOT} {MT5_SYMBOL} @ {price} "
                            f"SL={sl} TP={tp} ticket=#{result.order}")
                return result.order
        logger.error(f"MT5: order failed - retcode={getattr(result, 'retcode', '?')} "
                      f"comment={getattr(result, 'comment', '?')}")
        return None
    except Exception:
        logger.exception("MT5: order_send error")
        return None


def mt5_modify_position(ticket, sl, tp):
    """Move an open position's SL/TP (used for 1:1 break-even and trailing stops)."""
    if not MT5_TRADING_ENABLED or not _mt5_ready or not ticket:
        return False
    try:
        acc = mt5.account_info()
        if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            logger.error("MT5: connected account is NOT a demo account - modify BLOCKED (demo-only safety).")
            return False
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": MT5_SYMBOL,
                    "sl": float(sl), "tp": float(tp) if tp else 0.0}
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        logger.error(f"MT5: modify SL failed for #{ticket} - retcode={getattr(result, 'retcode', '?')} "
                      f"comment={getattr(result, 'comment', '?')}")
        return False
    except Exception:
        logger.exception("MT5: modify_position error")
        return False

# Global State
data_lock = threading.Lock()
historical_bars = []
recent_tape = []
order_book = {'bids': [], 'asks': []}  # REAL depth from Port 9000
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

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0, 'open_time': None, 'engine': 'CONFLUENCE'}
trade_history = []

sniper_state = {
    'regime': 'SCANNING', 'chop_index': 50.0, 'efficiency_ratio': 0.5,
    'signal': 'SCANNING LIQUIDITY', 'entry_price': 0.0, 'sl': 0.0, 'tp1': 0.0, 'tp2': 0.0,
    'risk_reward': '1:2.5', 'confidence': 75, 'reason': 'Analyzing Order Flow & VWAP clearance...'
}

# Quant Sniper circuit breaker - validated via backtest_engines_v6/v7/v8
# (V3_RR_2.0 + breaker(N=4): out-of-sample PnL improved $12.42->$30.40,
# the only one of 3 shortlisted configs that did NOT show overfitting).
SNIPER_BREAKER_MAX_LOSSES = 4
SNIPER_BREAKER_COOLDOWN_MIN = 60
sniper_breaker = {'consec_losses': 0, 'paused_until': None}

_questdb_sender = None


def _get_sender():
    global _questdb_sender
    if _questdb_sender is None:
        _questdb_sender = Sender.from_conf(QUESTDB_ILP_CONF)
        _questdb_sender.establish()
    return _questdb_sender


def ingest_itick_to_questdb(price):
    """Durable iTick (real spot XAUUSD) price log - needed for the Lead-Lag
    verification study (PAXG's own ticks_v2 already exists, but iTick was
    never persisted, only broadcast live, so no historical lead-lag analysis
    was previously possible). Starts accumulating from now."""
    global _questdb_sender
    try:
        sender = _get_sender()
        sender.row(
            "itick_v2",
            symbols={"symbol": "XAUUSD"},
            columns={"price": float(price)},
            at=TimestampNanos(int(datetime.now().timestamp() * 1_000_000_000)),
        )
        sender.flush()
    except Exception:
        logger.exception("QuestDB iTick ingest failed")


def ingest_bar_to_questdb(bar):
    global _questdb_sender
    try:
        sender = _get_sender()
        bar_time = bar['time']
        ts_ns = int(bar_time.timestamp() * 1_000_000_000) if isinstance(bar_time, datetime) else int(time.time() * 1_000_000_000)
        sender.row(
            "bars_v2_9080",
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
# 1. QUANTITATIVE REGIME & MATHEMATICAL ENGINES (unchanged real math)
# =============================================================================
def calculate_quant_metrics(bars):
    if len(bars) < 16:
        return 50.0, 0.5, 2.0

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]

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

    if range_14 > 0 and sum_tr > 0:
        ratio = sum_tr / range_14
        chop = 100.0 * (np.log10(ratio) / np.log10(14.0))
        chop = max(0.0, min(100.0, chop))
    else:
        chop = 50.0

    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[i] - closes[i-1]) for i in range(len(closes)-9, len(closes)))
    ker = change / path if path > 0 else 0.5
    ker = round(max(0.0, min(1.0, ker)), 3)

    return round(chop, 1), ker, round(atr, 2)


# =============================================================================
# 2. DATABASE & PERSISTENCE (new DB - does not touch trades_quant.db)
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
    logger.info(f"Quant Terminal V2 Database initialized: {DB_PATH}")


def load_initial_bars():
    """REAL Binance klines only - no cross-reading old ports' databases, no synthetic fallback."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta
    logger.info("Fetching REAL historical bars from Binance...")
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': BINANCE_SYMBOL, 'interval': '1m', 'limit': 150}, timeout=10)
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
            delta = taker_buy - (vol - taker_buy)
            buy_vol = taker_buy
            sell_vol = vol - taker_buy
            cum_delta += delta
            cum_vol += vol
            cum_pv += ((op + hp + lp + cp) / 4) * vol
            vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else cp
            historical_bars.append({
                'time': bt, 'open': op, 'high': hp, 'low': lp, 'close': cp,
                'volume': vol, 'buy_vol': buy_vol, 'sell_vol': sell_vol,
                'delta': delta, 'cvd': cum_delta, 'vwap': vwap,
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


def load_trades():
    global trade_history
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        conn.close()
        trade_history = [{
            'id': r[0], 'time': r[1], 'exit_time': r[2], 'dir': r[3],
            'entry': r[4], 'exit_price': r[5], 'sl': r[6], 'tp': r[7],
            'status': r[8], 'pnl': r[9], 'confidence': r[10],
            'phase': r[11], 'session': r[12], 'signal_type': r[13]
        } for r in rows]
        logger.info(f"Loaded {len(trade_history)} trades.")
    except Exception as e:
        logger.error(f"Load trades error: {e}")


# =============================================================================
# 3. REAL DOM & TIME-AND-SALES TAPE (from Port 9000 depth+tick relay)
# =============================================================================
def update_tape(price, volume, is_buy):
    global recent_tape
    now_str = datetime.now().strftime('%H:%M:%S')
    is_block = volume > 3.0
    recent_tape.insert(0, {
        'time': now_str, 'price': round(price, 2), 'size': round(volume, 3),
        'side': 'BUY' if is_buy else 'SELL', 'is_block': is_block
    })
    if len(recent_tape) > 40:
        recent_tape.pop()


def update_order_book_from_depth(bids, asks):
    global order_book
    order_book = {
        'bids': [{'price': float(p), 'volume': float(q)} for p, q in bids[:10]],
        'asks': [{'price': float(p), 'volume': float(q)} for p, q in asks[:10]],
    }


# =============================================================================
# 4. ORDER FLOW & VWAP QUANT SNIPER TICK PROCESSOR (unchanged math)
# =============================================================================
def close_trade_instance(exit_price, reason, bar_phase='Institutional', bar_session='NY'):
    global trade_state, trade_history, sniper_breaker
    if not trade_state.get('in_position'):
        return
    p = round(float(exit_price), 2)
    entry = trade_state['entry_price']
    is_buy = trade_state['direction'] in ('BUY', 'LONG')
    pnl = round((p - entry) * 10, 1) if is_buy else round((entry - p) * 10, 1)
    exit_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if trade_state.get('engine') == 'QUANT_SNIPER':
        if pnl > 0:
            sniper_breaker['consec_losses'] = 0
        else:
            sniper_breaker['consec_losses'] += 1
            if sniper_breaker['consec_losses'] >= SNIPER_BREAKER_MAX_LOSSES:
                sniper_breaker['paused_until'] = datetime.now() + timedelta(minutes=SNIPER_BREAKER_COOLDOWN_MIN)
                sniper_breaker['consec_losses'] = 0
                logger.info(f"[QUANT SNIPER BREAKER] {SNIPER_BREAKER_MAX_LOSSES} consecutive losses - "
                            f"pausing new Sniper entries until {sniper_breaker['paused_until'].strftime('%Y-%m-%d %H:%M:%S')}")
    record = {
        'id': len(trade_history) + 1, 'time': trade_state.get('open_time', exit_time), 'exit_time': exit_time,
        'dir': trade_state['direction'], 'entry': entry,
        'exit_price': p, 'sl': trade_state['stop_loss'], 'tp': trade_state['take_profit'],
        'status': reason, 'pnl': pnl, 'confidence': 92 if trade_state.get('engine') == 'QUANT_SNIPER' else 75,
        'phase': bar_phase, 'session': bar_session, 'signal_type': trade_state.get('engine', 'QUANT_SNIPER')
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
    logger.info(f"[TRADE CLOSED: {reason}] Exit: ${p:.2f} | PnL: ${pnl}")


# =============================================================================
# VWAP FIRST-CLOSE PAPER STRATEGY (1-min and 5-min), shared module vwap_first_close.py.
# Paper only (never touches MT5). Validated by backtest_vwap_first_close_be.py.
# =============================================================================
import vwap_first_close as vfc
VWAP_FC_ENABLED = os.getenv("GOLDFLOW_VWAP_FC_ENABLED", "1") == "1"


def _vwap_fc_record(rec):
    """Show a closed VWAP-FC paper trade in this dashboard's trade history + DB (pnl = NET $ per 0.01 lot)."""
    status = {'WIN': 'TP HIT', 'BE': 'BE EXIT', 'LOSS': 'SL HIT'}.get(rec['outcome'], rec['outcome'])
    fmt = '%Y-%m-%d %H:%M:%S'
    record = {
        'id': len(trade_history) + 1, 'time': rec['open_utc'].astimezone().strftime(fmt),
        'exit_time': rec['close_utc'].astimezone().strftime(fmt), 'dir': rec['dir'],
        'entry': round(rec['entry'], 2), 'exit_price': round(rec['exit'], 2), 'sl': round(rec['sl'], 2),
        'tp': round(rec['tp'], 2), 'status': status, 'pnl': round(rec['net'], 2), 'confidence': 0,
        'phase': 'VWAP-FC', 'session': f"{rec['tf']}m", 'signal_type': f"VWAP_FC_{rec['tf']}M"}
    trade_history.insert(0, record)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT INTO trades (time, exit_time, dir, entry, exit_price, sl, tp, status, pnl, confidence, phase, session, signal_type) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (record['time'], record['exit_time'], record['dir'], record['entry'], record['exit_price'],
                      record['sl'], record['tp'], record['status'], record['pnl'], record['confidence'],
                      record['phase'], record['session'], record['signal_type']))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"VWAP-FC trade save error: {e}")


vwap_fc_daily = vfc.DailyVwap()
# V1/V4/V10 MT5 trading turned OFF on demo (user request) - paper-trading/DB logging continues.
# Portfolio (RSI14/VOLIMB/ADXTREND) keeps its own separate MT5 wiring, unaffected.
VWAP_FC_MT5_ENABLED = os.getenv("GOLDFLOW_VWAP_FC_MT5_ENABLED", "0") == "1"
_vwap_fc_mt5_open = mt5_place_order if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else None
_vwap_fc_mt5_modify = mt5_modify_position if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else None
vwap_fc_strats = [vfc.make_variant(v, "9080", 1, vwap_fc_daily, logger, _vwap_fc_record if v == "V1" else None,
                                    mt5_open=_vwap_fc_mt5_open, mt5_modify=_vwap_fc_mt5_modify)
                  for v in vfc.VARIANTS] if VWAP_FC_ENABLED else []
if VWAP_FC_ENABLED:
    logger.info(f"VWAP paper strategies ENABLED (FINAL 6, 1-min only), MT5 live={'YES (demo)' if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else 'no, paper only'}: "
                f"{len(vwap_fc_strats)} variant instances "
                f"({', '.join(vfc.VARIANTS)}); only V1 shows in the dashboard table, all logged to vwap_fc_paper.db "
                f"(magic 90810-90815, comment GF-<variant>).")


def _rolling_chart_vwap(window_bars):
    """Same formula the chart itself plots (update_quant_terminal): cumulative
    Typical Price (H+L+C)/3 weighted by volume, over the same last-100-bar
    window the chart displays - NOT the process-lifetime cumulative bar['vwap']."""
    if not window_bars:
        return None
    tp_sum = sum(((b['high'] + b['low'] + b['close']) / 3.0) * b.get('volume', 0.0) for b in window_bars)
    vol_sum = sum(b.get('volume', 0.0) for b in window_bars)
    return round(tp_sum / vol_sum, 2) if vol_sum > 0 else window_bars[-1]['close']


def _vwap_fc_on_bar(bar, price):
    try:
        now_utc = datetime.now(timezone.utc)
        start_utc = bar['time'].astimezone(timezone.utc)
        chart_vwap = _rolling_chart_vwap(historical_bars[-100:])
        for st in vwap_fc_strats:
            st.on_bar(bar['open'], bar['high'], bar['low'], bar['close'], chart_vwap, start_utc, price, now_utc)
    except Exception:
        logger.exception("VWAP-FC on_bar error")


def _vwap_fc_on_price(price):
    try:
        now_utc = datetime.now(timezone.utc)
        for st in vwap_fc_strats:
            st.on_price(price, now_utc)
    except Exception:
        logger.exception("VWAP-FC on_price error")


# =============================================================================
# PORTFOLIO STRATEGIES: RSI-14 Reversion + Volume Imbalance Spike + ADX-Trend
# (Type 1). Separate signal family from VWAP-FC above; own DB (portfolio_paper.db),
# own MT5 magic range (90820-90823). Validated by backtest_portfolio_windowed_verify.py
# using the SAME 300-bar-windowed indicator computation as this live module.
# =============================================================================
import portfolio_strategies as pf
PORTFOLIO_ENABLED = os.getenv("GOLDFLOW_PORTFOLIO_ENABLED", "1") == "1"
_portfolio_mt5_open = mt5_place_order if MT5_TRADING_ENABLED else None
_portfolio_mt5_modify = mt5_modify_position if MT5_TRADING_ENABLED else None
portfolio_engine = pf.PortfolioEngine("9080", logger, mt5_open=_portfolio_mt5_open,
                                       mt5_modify=_portfolio_mt5_modify) if PORTFOLIO_ENABLED else None
if PORTFOLIO_ENABLED:
    logger.info(f"Portfolio strategies ENABLED (RSI14, VOLIMB, ADXTREND), MT5 live="
                f"{'YES (demo)' if MT5_TRADING_ENABLED else 'no, paper only'} - magic 90820/90821/90823, "
                f"comment GF-P-<strategy>-9080, logged to portfolio_paper.db.")


def _portfolio_on_bar(price):
    if not portfolio_engine:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        portfolio_engine.on_bar(historical_bars, price, now_utc)
    except Exception:
        logger.exception("Portfolio on_bar error")


def _portfolio_on_price(price):
    if not portfolio_engine:
        return
    try:
        portfolio_engine.on_price(price, datetime.now(timezone.utc))
    except Exception:
        logger.exception("Portfolio on_price error")


# =============================================================================
# VWAP FULL-CROSSOVER - new strategy, replaces VWAP-FC/Portfolio per user
# request (those two are disabled via env var, left in place, untouched).
# Rule confirmed from annotated chart screenshots; see vwap_full_crossover.py
# for the full spec. HONESTY NOTE: full-period backtest showed this net
# NEGATIVE overall (-$0.056/trade) before going live - the user asked to
# deploy exactly as specified anyway, to observe live. MT5 execution
# defaults OFF regardless (separate switch below, same safety pattern as
# every other strategy here).
# =============================================================================
import vwap_full_crossover as vxc
VWAPCROSS_ENABLED = os.getenv("GOLDFLOW_VWAPCROSS_ENABLED", "1") == "1"
VWAPCROSS_MT5_ENABLED = os.getenv("GOLDFLOW_VWAPCROSS_MT5_ENABLED", "0") == "1"
_vwapcross_mt5_open = mt5_place_order if (MT5_TRADING_ENABLED and VWAPCROSS_MT5_ENABLED) else None
_vwapcross_mt5_modify = mt5_modify_position if (MT5_TRADING_ENABLED and VWAPCROSS_MT5_ENABLED) else None
vwapcross_engine = vxc.VwapFullCrossover("9080", logger, mt5_open=_vwapcross_mt5_open,
                                          mt5_modify=_vwapcross_mt5_modify) if VWAPCROSS_ENABLED else None
if VWAPCROSS_ENABLED:
    logger.info(f"VWAP Full-Crossover ENABLED (paper), MT5 live="
                f"{'YES (demo)' if (MT5_TRADING_ENABLED and VWAPCROSS_MT5_ENABLED) else 'no, paper only'} - "
                f"magic 90830, comment GF-VWAPX-9080, logged to vwap_full_crossover_paper.db. "
                f"NOTE: NOT backtest-validated (went net negative in full-period test) - deployed live at user's explicit request to observe.")


def _vwapcross_on_bar(bar, price):
    if not vwapcross_engine:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        chart_vwap = _rolling_chart_vwap(historical_bars[-100:])
        vwapcross_engine.on_bar(bar, chart_vwap, price, now_utc)
    except Exception:
        logger.exception("VwapFullCrossover on_bar error")


def _vwapcross_on_price(price):
    if not vwapcross_engine:
        return
    try:
        vwapcross_engine.on_price(price, datetime.now(timezone.utc))
    except Exception:
        logger.exception("VwapFullCrossover on_price error")


def _check_position_exits(price):
    """Shared by update_mark_price - checks the single Confluence/Sniper
    trade_state plus all 5 independent scalp positions against the real spot
    price. Must be called with data_lock already held."""
    if trade_state.get('in_position'):
        p = price
        hit_tp = hit_sl = False
        is_long = trade_state['direction'] in ('BUY', 'LONG')
        if is_long:
            if p >= trade_state['take_profit']: hit_tp = True
            elif p <= trade_state['stop_loss']: hit_sl = True
        else:
            if p <= trade_state['take_profit']: hit_tp = True
            elif p >= trade_state['stop_loss']: hit_sl = True
        if hit_tp or hit_sl:
            close_trade_instance(p, 'TP HIT' if hit_tp else 'SL HIT')

    for _sname in scalp_states:
        _scalp_check_exit(_sname, price)


def update_mark_price(price):
    """iTick (real spot XAUUSD) - or, when iTick is briefly stale, Port 9000's
    own Binance bid/ask-mid fallback - is now the SOLE driver of the candle's
    open/high/low/close, bar creation/finalization, and SL/TP exit checks.
    Previously PAXG trade ticks also set open/close directly in process_tick;
    since PAXG can trade at a sustained few-dollar offset from real spot gold,
    every bar showed an artificial wick toward whichever source last updated.
    Volume/delta/CVD stay strictly tied to real PAXG trade ticks (see
    process_tick) - only the price path moved here."""
    global current_bar, historical_bars, last_known_price, last_price_update
    price = float(price)
    last_known_price = price
    last_price_update = datetime.now()
    now = datetime.now()
    current_minute = now.replace(second=0, microsecond=0)
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else price

    with data_lock:
        _check_position_exits(price)

        if current_bar['time'] is None or current_bar['time'] < current_minute:
            if current_bar['close'] is not None and current_bar['time'] is not None:
                final_bar = finalize_bar(current_bar, vwap)
                # Defensive dedupe: only take ONE candle per minute, no matter
                # what caused this to fire twice for the same time (a feed
                # gap/reconnect burst can make wall-clock timing unreliable -
                # rather than chase that race, just guarantee historical_bars
                # never ends up with two entries for the same minute. Replace
                # the previous one with this more-complete version instead of
                # appending a second, since ticks kept accumulating on it.
                if historical_bars and historical_bars[-1]['time'] == final_bar['time']:
                    historical_bars[-1] = final_bar
                else:
                    historical_bars.append(final_bar)
                threading.Thread(target=ingest_bar_to_questdb, args=(final_bar,), daemon=True).start()
                if len(historical_bars) > 300:
                    historical_bars.pop(0)
                evaluate_dual_engines(final_bar)
                evaluate_scalp_strategies(final_bar)
                _vwap_fc_on_bar(final_bar, price)
                _portfolio_on_bar(price)
                _vwapcross_on_bar(final_bar, price)

            current_bar['time'] = current_minute
            current_bar['open'] = price
            current_bar['high'] = price
            current_bar['low'] = price
            current_bar['close'] = price
            current_bar['volume'] = 0.0
            current_bar['buy_vol'] = 0.0
            current_bar['sell_vol'] = 0.0
            current_bar['delta'] = 0.0
            current_bar['cvd'] = cum_delta
            current_bar['vwap'] = round(vwap, 2)
            current_bar['poc'] = price
            current_bar['levels'] = {}
        else:
            current_bar['high'] = max(current_bar['high'], price)
            current_bar['low'] = min(current_bar['low'], price)
            current_bar['close'] = price
        _vwap_fc_on_price(price)
        _portfolio_on_price(price)
        _vwapcross_on_price(price)


def process_tick(price, volume, is_buy):
    """Real Binance PAXG trade ticks now feed ONLY the tape display plus
    volume/delta/CVD into whichever bar update_mark_price (real-spot-driven)
    currently has open - PAXG's own trade price no longer touches open/high/
    low/close or SL/TP exit checks (see update_mark_price for why)."""
    global cum_vol, cum_pv, cum_delta
    try:
        price = float(price)
        volume = float(volume)
        ref_price = last_known_price if last_known_price else price
        if last_known_price and vwap_fc_strats:
            vwap_fc_daily.update(ref_price, volume, datetime.now(timezone.utc))

        delta = volume if is_buy else -volume
        buy_vol = volume if is_buy else 0.0
        sell_vol = 0.0 if is_buy else volume

        cum_delta += delta
        cum_vol += volume
        cum_pv += ref_price * volume

        with data_lock:
            update_tape(price, volume, is_buy)

            if current_bar['time'] is None:
                return  # no bar open yet (waiting on first real-spot price)
            current_bar['volume'] += volume
            current_bar['buy_vol'] += buy_vol
            current_bar['sell_vol'] += sell_vol
            current_bar['delta'] = current_bar['buy_vol'] - current_bar['sell_vol']
            current_bar['cvd'] = cum_delta
            vwap = (cum_pv / cum_vol) if cum_vol > 0 else ref_price
            current_bar['vwap'] = round(vwap, 2)
            lvl = round(ref_price, 1)
            current_bar['levels'][lvl] = current_bar['levels'].get(lvl, 0) + volume
            current_bar['poc'] = max(current_bar['levels'], key=current_bar['levels'].get)
    except Exception as e:
        logger.error(f"Tick processing error: {e}")


def finalize_bar(bar, vwap):
    delta = float(bar['buy_vol'] - bar['sell_vol'])
    poc = max(bar['levels'], key=bar['levels'].get) if bar['levels'] else bar['close']
    avg_vol = np.mean([b['volume'] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar['volume']
    imbalance = bar['volume'] > (avg_vol * 1.8) if avg_vol > 0 else False

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

    # REAL rule-based confidence (V1 used confidence: random.randint(68, 92) - pure noise)
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
        'levels': bar['levels'], 'buy_vol': bar['buy_vol'], 'sell_vol': bar['sell_vol']
    }


# =============================================================================
# 5. DUAL EXECUTION ENGINE: CONFLUENCE + VWAP QUANT SNIPER (unchanged math)
# =============================================================================
def evaluate_dual_engines(bar):
    global trade_state, trade_history, sniper_state

    chop, ker, atr = calculate_quant_metrics(historical_bars)
    sniper_state['chop_index'] = chop
    sniper_state['efficiency_ratio'] = ker

    is_sideways = (chop >= 60.0) or (ker < 0.25)
    if is_sideways:
        sniper_state['regime'] = 'SIDEWAYS_CHOP'
        sniper_state['signal'] = 'TRADES BLOCKED (SIDEWAYS MARKET)'
        sniper_state['reason'] = f'Choppiness Index high ({chop}/100) & Low Efficiency ({ker}). Capital protected.'
    else:
        sniper_state['regime'] = 'TRENDING_EXPANSION'
        sniper_state['reason'] = f'Strong Directional Flow (CHOP {chop} < 60 | KER {ker} > 0.35). Ready to fire.'

    if trade_state.get('in_position'):
        hit_tp = hit_sl = False
        exit_p = bar['close']
        is_long = trade_state['direction'] in ('BUY', 'LONG')
        if is_long:
            if bar['high'] >= trade_state['take_profit']:
                hit_tp = True; exit_p = trade_state['take_profit']
            elif bar['low'] <= trade_state['stop_loss']:
                hit_sl = True; exit_p = trade_state['stop_loss']
        else:
            if bar['low'] <= trade_state['take_profit']:
                hit_tp = True; exit_p = trade_state['take_profit']
            elif bar['high'] >= trade_state['stop_loss']:
                hit_sl = True; exit_p = trade_state['stop_loss']
        if hit_tp or hit_sl:
            close_trade_instance(exit_p, 'TP HIT' if hit_tp else 'SL HIT', bar.get('phase', 'Institutional'), bar.get('session', 'NY'))

    if STRATEGIES_ENABLED and not trade_state['in_position']:
        cp = bar['close']
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

        sniper_fired = False
        sniper_paused = sniper_breaker['paused_until'] is not None and datetime.now() < sniper_breaker['paused_until']
        if not is_sideways and not sniper_paused and atr > 0:
            # V3_RR_2.0 + breaker(N=4) - validated via backtest_engines_v3/v6/v7/v8:
            # entry threshold ATR-normalized (not fixed $), SL/TP anchored to
            # ENTRY at clean ATR multiples (not to bar high/low - that was the
            # root cause of the old SL-sizing bug), RR = 1:2.
            if (bar['close'] > bar['open']) and (cp >= vwap + 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio >= 0.58):
                entry = cp
                sl = round(entry - 1.0 * atr, 2)
                tp1 = round(entry + 2.0 * atr, 2)
                tp2 = round(entry + 3.0 * atr, 2)

                sniper_state.update({'signal': 'SNIPER BUY ACTIVE', 'entry_price': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
                                      'risk_reward': '1:2.0', 'confidence': 92,
                                      'reason': f'Strong Body ({int(body_ratio*100)}%) + Buy Imbalance ({int(buy_ratio*100)}%) crossing VWAP (ATR-normalized)'})
                mt5_place_order('BUY', sl, tp1)
                trade_state.update({'in_position': True, 'direction': 'BUY', 'entry_price': entry, 'stop_loss': sl,
                                     'take_profit': tp1, 'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'engine': 'QUANT_SNIPER'})
                sniper_fired = True
                logger.info(f"[QUANT SNIPER BUY] Entry: ${entry} | SL: ${sl} | TP: ${tp1}")

            elif (bar['close'] < bar['open']) and (cp <= vwap - 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio <= 0.42):
                entry = cp
                sl = round(entry + 1.0 * atr, 2)
                tp1 = round(entry - 2.0 * atr, 2)
                tp2 = round(entry - 3.0 * atr, 2)

                sniper_state.update({'signal': 'SNIPER SELL ACTIVE', 'entry_price': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
                                      'risk_reward': '1:2.0', 'confidence': 92,
                                      'reason': f'Strong Red Body ({int(body_ratio*100)}%) + Sell Pressure ({int((1-buy_ratio)*100)}%) below VWAP (ATR-normalized)'})
                mt5_place_order('SELL', sl, tp1)
                trade_state.update({'in_position': True, 'direction': 'SELL', 'entry_price': entry, 'stop_loss': sl,
                                     'take_profit': tp1, 'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'engine': 'QUANT_SNIPER'})
                sniper_fired = True
                logger.info(f"[QUANT SNIPER SELL] Entry: ${entry} | SL: ${sl} | TP: ${tp1}")
        elif sniper_paused:
            sniper_state['reason'] = f"Circuit breaker active ({SNIPER_BREAKER_MAX_LOSSES} consecutive losses) - paused until {sniper_breaker['paused_until'].strftime('%H:%M:%S')}"

        if not sniper_fired:
            div = bar.get('cvd_divergence')
            absrp = bar.get('absorption')
            signal = None
            if (div == 'Bullish Divergence' or absrp == 'Bullish Absorption') and cp > vwap:
                signal = 'BUY'
            elif (div == 'Bearish Divergence' or absrp == 'Bearish Absorption') and cp < vwap:
                signal = 'SELL'

            if signal and not is_sideways:
                sl_dist = 2.0
                tp_dist = 4.0
                conf_sl = round(cp - sl_dist if signal == 'BUY' else cp + sl_dist, 2)
                conf_tp = round(cp + tp_dist if signal == 'BUY' else cp - tp_dist, 2)
                mt5_place_order(signal, conf_sl, conf_tp)
                trade_state.update({
                    'in_position': True, 'direction': signal, 'entry_price': cp,
                    'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'stop_loss': conf_sl,
                    'take_profit': conf_tp,
                    'engine': 'CONFLUENCE'
                })


# =============================================================================
# 5B. FIVE INDEPENDENT SCALPING STRATEGIES (additive - each tracks its own
# position, its own MT5 magic number, and never blocks or is blocked by the
# existing Quant Sniper / Confluence engines above). Runs once per finalized
# 1-minute bar, right after evaluate_dual_engines(), which is untouched.
# =============================================================================
SCALP_MAX_CANDLE_RANGE = 5.0  # skip all 5 strategies on an outsized/spike bar

SCALP_MAGIC = {
    'S1_VWAP_MOMENTUM': 90801,
    'S2_BAND_ABSORPTION': 90802,
    'S3_IMBALANCE': 90803,
    'S4_TAPE_MOMENTUM': 90804,
    'S5_MICRO_BREAKOUT': 90805,
}

scalp_states = {name: {'in_position': False, 'direction': None, 'entry_price': 0.0,
                        'stop_loss': 0.0, 'take_profit': 0.0, 'open_time': None}
                 for name in SCALP_MAGIC}


def _scalp_open(name, direction, entry, sl, tp):
    st = scalp_states[name]
    st.update({'in_position': True, 'direction': direction, 'entry_price': entry,
               'stop_loss': sl, 'take_profit': tp, 'open_time': datetime.now().strftime('%H:%M:%S')})
    logger.info(f"[SCALP:{name}] {direction} @ {entry:.2f} SL={sl:.2f} TP={tp:.2f}")
    mt5_place_order(direction, sl, tp, magic=SCALP_MAGIC[name], comment=f"Goldflow-{name}")


def _scalp_check_exit(name, price):
    st = scalp_states[name]
    if not st['in_position']:
        return
    is_long = st['direction'] == 'BUY'
    hit_tp = (price >= st['take_profit']) if is_long else (price <= st['take_profit'])
    hit_sl = (price <= st['stop_loss']) if is_long else (price >= st['stop_loss'])
    if hit_tp or hit_sl:
        logger.info(f"[SCALP:{name}] {'TP HIT' if hit_tp else 'SL HIT'} @ {price:.2f} "
                    f"(entry was {st['entry_price']:.2f})")
        st['in_position'] = False


def _compute_vwap_bands():
    """Same cumulative VWAP + 1.28-sigma band formula the chart itself uses,
    so 'price touches the lower band' means the same thing here as what the
    user sees on screen."""
    bars = historical_bars[-100:] if len(historical_bars) > 100 else historical_bars
    if not bars:
        return last_known_price, last_known_price, last_known_price
    cum_pv_b = 0.0
    cum_vol_b = 0.0
    cum_diffsq = 0.0
    vwap_now = bars[-1]['close']
    for b in bars:
        tp = (b['high'] + b['low'] + b['close']) / 3.0
        vol = b.get('volume', 0) or 0
        cum_pv_b += tp * vol
        cum_vol_b += vol
        vwap_now = (cum_pv_b / cum_vol_b) if cum_vol_b > 0 else tp
        cum_diffsq += ((tp - vwap_now) ** 2) * vol
    std_now = (cum_diffsq / max(cum_vol_b, 1.0)) ** 0.5
    return round(vwap_now, 2), round(vwap_now + 1.28 * std_now, 2), round(vwap_now - 1.28 * std_now, 2)


def evaluate_scalp_strategies(bar):
    if not STRATEGIES_ENABLED:
        return
    if len(historical_bars) < 6:
        return
    candle_range = bar['high'] - bar['low']
    if candle_range > SCALP_MAX_CANDLE_RANGE:
        return
    price = last_known_price
    vwap = bar.get('vwap', price)
    body_ratio = (abs(bar['close'] - bar['open']) / candle_range) if candle_range > 0 else 0.0
    buy_ratio = (bar['buy_vol'] / bar['volume']) if bar.get('volume', 0) > 0 else 0.5

    # ---- Strategy 1: VWAP-Cross Momentum ----
    st1 = scalp_states['S1_VWAP_MOMENTUM']
    if not st1['in_position']:
        is_trending = sniper_state.get('regime') == 'TRENDING_EXPANSION'
        whole_above = bar['low'] > vwap
        whole_below = bar['high'] < vwap
        if is_trending and whole_above and bar['close'] > bar['open'] and body_ratio >= 0.65 and buy_ratio >= 0.60:
            sl = round(bar['low'], 2)
            tp = round(price + (price - sl) * 1.5, 2)
            _scalp_open('S1_VWAP_MOMENTUM', 'BUY', price, sl, tp)
        elif is_trending and whole_below and bar['close'] < bar['open'] and body_ratio >= 0.65 and buy_ratio <= 0.40:
            sl = round(bar['high'], 2)
            tp = round(price - (sl - price) * 1.5, 2)
            _scalp_open('S1_VWAP_MOMENTUM', 'SELL', price, sl, tp)

    # ---- Strategy 2: Band Extreme + Absorption Reversal ----
    st2 = scalp_states['S2_BAND_ABSORPTION']
    if not st2['in_position']:
        vwap_now, upper_band, lower_band = _compute_vwap_bands()
        absorption = bar.get('absorption')
        if bar['low'] <= lower_band and absorption == 'Bullish Absorption':
            sl = round(bar['low'], 2)
            _scalp_open('S2_BAND_ABSORPTION', 'BUY', price, sl, vwap_now)
        elif bar['high'] >= upper_band and absorption == 'Bearish Absorption':
            sl = round(bar['high'], 2)
            _scalp_open('S2_BAND_ABSORPTION', 'SELL', price, sl, vwap_now)

    # ---- Strategy 3: Order-Book Imbalance Spike ----
    st3 = scalp_states['S3_IMBALANCE']
    if not st3['in_position']:
        bids_vol = sum(b['volume'] for b in order_book.get('bids', [])[:5])
        asks_vol = sum(a['volume'] for a in order_book.get('asks', [])[:5])
        if bids_vol > 0 and asks_vol > 0:
            if bids_vol >= 2 * asks_vol:
                sl = round(bar['low'], 2)
                tp = round(price + (price - sl) * 1.5, 2)
                _scalp_open('S3_IMBALANCE', 'BUY', price, sl, tp)
            elif asks_vol >= 2 * bids_vol:
                sl = round(bar['high'], 2)
                tp = round(price - (sl - price) * 1.5, 2)
                _scalp_open('S3_IMBALANCE', 'SELL', price, sl, tp)

    # ---- Strategy 4: Tape Momentum (3+ consecutive same-side block trades) ----
    st4 = scalp_states['S4_TAPE_MOMENTUM']
    if not st4['in_position'] and len(recent_tape) >= 3:
        streak_side = None
        streak_count = 0
        for t in recent_tape:
            if not t.get('is_block'):
                break
            if streak_side is None:
                streak_side = t['side']
                streak_count = 1
            elif t['side'] == streak_side:
                streak_count += 1
            else:
                break
        if streak_count >= 3:
            if streak_side == 'BUY':
                sl = round(bar['low'], 2)
                tp = round(price + (price - sl) * 1.5, 2)
                _scalp_open('S4_TAPE_MOMENTUM', 'BUY', price, sl, tp)
            else:
                sl = round(bar['high'], 2)
                tp = round(price - (sl - price) * 1.5, 2)
                _scalp_open('S4_TAPE_MOMENTUM', 'SELL', price, sl, tp)

    # ---- Strategy 5: Micro-Breakout (last 5 bars' range + volume spike) ----
    st5 = scalp_states['S5_MICRO_BREAKOUT']
    if not st5['in_position']:
        lookback = historical_bars[-6:-1]
        if lookback:
            recent_high = max(b['high'] for b in lookback)
            recent_low = min(b['low'] for b in lookback)
            avg_vol = sum(b['volume'] for b in lookback) / len(lookback)
            vol_spike = avg_vol > 0 and bar['volume'] > avg_vol * 1.5
            if vol_spike and bar['close'] > recent_high:
                sl = round(recent_low, 2)
                tp = round(price + (price - sl) * 1.5, 2)
                _scalp_open('S5_MICRO_BREAKOUT', 'BUY', price, sl, tp)
            elif vol_spike and bar['close'] < recent_low:
                sl = round(recent_high, 2)
                tp = round(price - (sl - price) * 1.5, 2)
                _scalp_open('S5_MICRO_BREAKOUT', 'SELL', price, sl, tp)


# =============================================================================
# 5C. STATISTICAL ORDER-FLOW ENGINE ("semi-HFT") - additive, independent
# position tracking, own MT5 magic number. Reacts on EVERY depth/trade event
# (not bar-close), since its whole thesis is a short (seconds) prediction
# horizon - unlike the other engines, waiting for a 1-minute bar would waste
# the entire edge window.
#
# HONEST SCOPE: this is NOT true HFT. No colocation, no genuine L3 order-book
# event stream (Binance only gives 100ms depth snapshots), and Python/MT5-
# desktop execution latency is 50-500ms. The same published academic order-
# flow mathematics (Cont-Kukanov-Stoikov OFI, microprice, Kyle's lambda,
# trade-arrival Hawkes, EV-after-cost gating) is used, just recalibrated to a
# ~2-9 second horizon our infrastructure can actually act within - the same
# way a small neural net uses the same underlying math as a large one, just
# at a different scale.
# =============================================================================
HFT_MAGIC = 90900
HFT_TIME_STOP_SECONDS = 9.0
HFT_MIN_EDGE_DOLLARS = 0.05  # required EV-after-cost to fire; else NO_TRADE

hft_state = {'in_position': False, 'direction': None, 'entry_price': 0.0,
             'stop_loss': 0.0, 'take_profit': 0.0, 'open_time': None, 'entry_ts': None}

_hft_prev_book = None
_hft_trade_times = {'BUY': deque(maxlen=300), 'SELL': deque(maxlen=300)}
_hft_impact_history = deque(maxlen=300)   # (ofi_norm, subsequent_2s_move) - calibrates Kyle's lambda
_hft_prob_history = deque(maxlen=500)     # (signal_score_bucket, did_price_go_up) - calibrates P(up)
_hft_pending = deque(maxlen=100)          # (ts, mid_price, ofi_norm, signal_score) awaiting a real outcome


def _hft_ofi_event(pb_prev, qb_prev, pb_new, qb_new, pa_prev, qa_prev, pa_new, qa_new):
    """Cont-Kukanov-Stoikov order-flow-imbalance event from consecutive
    top-of-book snapshots. We only get 100ms depth snapshots (not a true
    per-event L3 stream), so this is the standard snapshot-diff OFI
    approximation used in practice when genuine L3 data isn't available."""
    term1 = qb_new if pb_new >= pb_prev else 0.0
    term2 = qb_prev if pb_new <= pb_prev else 0.0
    term3 = qa_new if pa_new <= pa_prev else 0.0
    term4 = qa_prev if pa_new >= pa_prev else 0.0
    return term1 - term2 - term3 + term4


def _hft_microprice(bid, ask, bid_sz, ask_sz):
    if bid_sz + ask_sz <= 0:
        return (bid + ask) / 2.0, 0.0
    mid = (bid + ask) / 2.0
    micro = (ask * bid_sz + bid * ask_sz) / (bid_sz + ask_sz)
    spread = ask - bid
    mps = (micro - mid) / spread if spread > 0 else 0.0
    return micro, mps


def _hft_trade_intensity(side, window_sec=3.0):
    now = datetime.now().timestamp()
    return sum(1 for t in _hft_trade_times[side] if now - t <= window_sec) / window_sec


def hft_on_trade(is_buy):
    """Feeds the trade-arrival intensity estimate - a legitimate simplified
    Hawkes proxy (recent real-trade arrival rate, buy side vs sell side)
    since we lack genuine per-event LOB data for a full state-dependent
    self-exciting kernel fit."""
    if not STRATEGIES_ENABLED:
        return
    _hft_trade_times['BUY' if is_buy else 'SELL'].append(datetime.now().timestamp())


def _hft_dynamic_lambda():
    """Rolling Kyle's Lambda: Cov(dP, OFI) / Var(OFI) over recent history."""
    if len(_hft_impact_history) < 30:
        return None
    ofis = [x[0] for x in _hft_impact_history]
    moves = [x[1] for x in _hft_impact_history]
    n = len(ofis)
    mean_o = sum(ofis) / n
    mean_m = sum(moves) / n
    cov = sum((o - mean_o) * (m - mean_m) for o, m in zip(ofis, moves)) / n
    var = sum((o - mean_o) ** 2 for o in ofis) / n
    return (cov / var) if var > 1e-12 else None


def _hft_check_exit(price):
    if not hft_state['in_position']:
        return
    is_long = hft_state['direction'] == 'BUY'
    hit_tp = (price >= hft_state['take_profit']) if is_long else (price <= hft_state['take_profit'])
    hit_sl = (price <= hft_state['stop_loss']) if is_long else (price >= hft_state['stop_loss'])
    elapsed = (datetime.now() - hft_state['entry_ts']).total_seconds() if hft_state['entry_ts'] else 0.0
    timed_out = elapsed >= HFT_TIME_STOP_SECONDS
    if hit_tp or hit_sl or timed_out:
        reason = 'TP HIT' if hit_tp else ('SL HIT' if hit_sl else 'TIME STOP')
        logger.info(f"[HFT-ENGINE] {reason} @ {price:.2f} (entry was {hft_state['entry_price']:.2f}, held {elapsed:.1f}s)")
        hft_state['in_position'] = False


def _hft_evaluate(ofi_norm, mps, qi, intensity_imbalance, mid_price, bid, ask, signal_score):
    _hft_check_exit(mid_price)
    if hft_state['in_position']:
        return

    lam = _hft_dynamic_lambda()
    if lam is None:
        return  # still calibrating (needs ~30 resolved 2s-ahead outcomes first)

    expected_move = lam * ofi_norm
    spread = ask - bid

    bucket = round(signal_score, 1)
    similar = [outcome for sig, outcome in _hft_prob_history if abs(sig - bucket) <= 0.15]
    p_up = (sum(similar) / len(similar)) if len(similar) >= 20 else 0.5

    direction = None
    p_dir = 0.5
    if signal_score > 0.15 and expected_move > 0:
        direction, p_dir = 'BUY', p_up
    elif signal_score < -0.15 and expected_move < 0:
        direction, p_dir = 'SELL', 1.0 - p_up

    if direction is None:
        return

    ev_gross = p_dir * abs(expected_move) - (1 - p_dir) * abs(expected_move) * 0.5
    ev_net = ev_gross - spread - 0.05  # spread + assumed slippage/fee buffer
    if ev_net <= HFT_MIN_EDGE_DOLLARS:
        return  # EV-after-cost gate: NO_TRADE

    # IMPORTANT: mid_price above is derived from Binance PAXG's own depth -
    # used correctly for OFI/QI/microprice (real order-flow dynamics), but
    # PAXG can sit at a sustained few-dollar offset from the real spot price
    # our broker actually quotes (the same root cause as the earlier candle
    # upper-wick bug). Entry/SL/TP must be anchored to last_known_price
    # (iTick-driven, broker-aligned) or MT5 rejects them as invalid the
    # moment the order reaches the server.
    entry = last_known_price if last_known_price else mid_price
    sl_dist = max(0.4, spread * 3)
    tp_dist = max(0.6, sl_dist * 1.3)
    sl = round(entry - sl_dist, 2) if direction == 'BUY' else round(entry + sl_dist, 2)
    tp = round(entry + tp_dist, 2) if direction == 'BUY' else round(entry - tp_dist, 2)

    hft_state.update({'in_position': True, 'direction': direction, 'entry_price': entry,
                       'stop_loss': sl, 'take_profit': tp,
                       'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'entry_ts': datetime.now()})
    logger.info(f"[HFT-ENGINE] {direction} @ {entry:.2f} SL={sl} TP={tp} "
                f"(OFI={ofi_norm:.3f} QI={qi:.3f} MPS={mps:.3f} lambda={lam:.4f} "
                f"P={p_dir:.2f} EV_net={ev_net:.3f})")
    mt5_place_order(direction, sl, tp, magic=HFT_MAGIC, comment="Goldflow-HFTEngine")


def hft_on_depth_update(bids, asks):
    """Called on every depth snapshot from Port 9000 (~every 100ms) - this is
    the fast path; unlike evaluate_scalp_strategies/evaluate_dual_engines,
    this never waits for a bar close."""
    global _hft_prev_book
    if not STRATEGIES_ENABLED:
        return
    if not bids or not asks:
        return
    pb_new, qb_new = float(bids[0][0]), float(bids[0][1])
    pa_new, qa_new = float(asks[0][0]), float(asks[0][1])

    if _hft_prev_book is not None:
        pb_prev, qb_prev, pa_prev, qa_prev = _hft_prev_book
        ofi = _hft_ofi_event(pb_prev, qb_prev, pb_new, qb_new, pa_prev, qa_prev, pa_new, qa_new)
        depth_sum = qb_new + qa_new
        ofi_norm = (ofi / depth_sum) if depth_sum > 0 else 0.0

        micro, mps = _hft_microprice(pb_new, pa_new, qb_new, qa_new)
        qi = (qb_new - qa_new) / (qb_new + qa_new) if (qb_new + qa_new) > 0 else 0.0
        intensity_imbalance = _hft_trade_intensity('BUY') - _hft_trade_intensity('SELL')
        mid_price = (pb_new + pa_new) / 2.0
        signal_score = (2.0 * ofi_norm) + (1.0 * qi) + (1.5 * mps) + (0.5 * math.tanh(intensity_imbalance))

        now_ts = datetime.now().timestamp()
        while _hft_pending and now_ts - _hft_pending[0][0] >= 2.0:
            t0, p0, ofi0, sig0 = _hft_pending.popleft()
            move = mid_price - p0
            _hft_impact_history.append((ofi0, move))
            _hft_prob_history.append((round(sig0, 1), 1 if move > 0 else 0))
        _hft_pending.append((now_ts, mid_price, ofi_norm, signal_score))

        _hft_evaluate(ofi_norm, mps, qi, intensity_imbalance, mid_price, pb_new, pa_new, signal_score)

    _hft_prev_book = (pb_new, qb_new, pa_new, qa_new)


# =============================================================================
# 6. LIVE FEED - subscribes to Port 9000 Core Engine V2 (real ticks + real depth)
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
        hft_on_trade(is_buy)
    elif mtype == "depth":
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        with data_lock:
            update_order_book_from_depth(bids, asks)
        hft_on_depth_update(bids, asks)
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
        if "iTick" in msg.get("source", ""):
            threading.Thread(target=ingest_itick_to_questdb, args=(msg["price"],), daemon=True).start()
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
    logger.info(f"Quant Terminal V2 Live Feed Worker: subscribing to Core Engine V2 at {ENGINE_WS_URL}")
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
# 7. HIGH-LEVEL BLOOMBERG AI QUANT TERMINAL DASHBOARD UI (unchanged layout)
# =============================================================================
app = dash.Dash(
    __name__,
    title="GOLD.FLOW // AI Quant Terminal V2 (Pilot)",
    update_title=None,
    suppress_callback_exceptions=True
)


# =============================================================================
# READ-ONLY MIRROR ENDPOINT (additive only - changes nothing about this
# dashboard's own behavior/logic/UI). Exists purely so a separate full-screen
# TradingView-style chart page (Port 9081) can display the EXACT same bars,
# VWAP/bands and signals as this dashboard, instead of recalculating
# independently (which would drift on cumulative values depending on when
# each process started).
# =============================================================================
def _json_safe(v):
    """numpy bool_/int64/float64 (from pandas/numpy comparisons upstream, e.g.
    imbalance = volume > avg_vol*1.8) look like native Python types but
    aren't - Flask's JSON encoder rejects them outright."""
    if isinstance(v, (bool,)):
        return bool(v)
    if hasattr(v, 'item'):  # numpy scalar
        return v.item()
    return v

def _bar_to_json(b):
    t = b.get('time')
    out = {k: _json_safe(v) for k, v in b.items() if k != 'levels'}
    out['time'] = t.isoformat() if hasattr(t, 'isoformat') else t
    return out

@app.server.route('/api/mirror')
def mirror_endpoint():
    from flask import jsonify
    with data_lock:
        bars = [_bar_to_json(b) for b in historical_bars[-300:]]
        cur = {k: _json_safe(v) for k, v in current_bar.items() if k != 'levels'}
        if cur.get('time') is not None and hasattr(cur['time'], 'isoformat'):
            cur['time'] = cur['time'].isoformat()
        pos = {k: _json_safe(v) for k, v in trade_state.items()}
        sniper = {k: _json_safe(v) for k, v in sniper_state.items()}
    return jsonify({
        'historical_bars': bars,
        'current_bar': cur,
        'trade_state': pos,
        'sniper_state': sniper,
        'last_known_price': _json_safe(last_known_price),
        'cum_delta': _json_safe(cum_delta),
    })

app.layout = html.Div(
    id="quant-terminal-container",
    style={"backgroundColor": "#05070A", "color": "#E6EDF3", "fontFamily": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
           "minHeight": "100vh", "padding": "8px 14px", "boxSizing": "border-box"},
    children=[
        dcc.Interval(id="quant-interval", interval=1000, n_intervals=0),
        html.Div(id="quant-topbar", style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                  "backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderBottom": "2px solid #FFD700",
                  "padding": "8px 16px", "borderRadius": "6px", "marginBottom": "8px"},
            children=[
                html.Div([
                    html.Span("GOLD.FLOW ", style={"color": "#FFD700", "fontWeight": "900", "fontSize": "17px", "letterSpacing": "1.5px"}),
                    html.Span("// AI QUANT TERMINAL V2 PILOT ", style={"color": "#38BDF8", "fontWeight": "bold", "fontSize": "13px"}),
                    html.Span("[PORT 9080]", style={"color": "#00E676", "fontSize": "11px", "marginLeft": "8px", "backgroundColor": "#064E3B", "padding": "2px 8px", "borderRadius": "4px", "fontWeight": "bold"})
                ]),
                html.Div(id="quant-clocks", style={"fontSize": "11px", "color": "#94A3B8", "letterSpacing": "0.5px"}),
                html.Div(id="feed-status-header", style={"fontSize": "11px"})
            ]
        ),
        html.Div(id="ai-regime-hud", style={"display": "grid", "gridTemplateColumns": "1.2fr 2.8fr", "gap": "10px", "marginBottom": "10px"},
            children=[
                html.Div(id="ai-regime-card", style={"backgroundColor": "#0D131F", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "10px 14px", "display": "flex", "flexDirection": "column", "justifyContent": "space-between"}),
                html.Div(id="vwap-sniper-card", style={"backgroundColor": "#0D131F", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "10px 14px", "display": "flex", "flexDirection": "column", "justifyContent": "space-between"})
            ]
        ),
        html.Div(id="ticker-strip", style={"display": "grid", "gridTemplateColumns": "1.5fr 1fr 1fr 1fr 1fr 1fr", "gap": "8px", "marginBottom": "10px"},
            children=[
                html.Div(id="live-price-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="spread-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="high-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="low-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="session-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
                html.Div(id="phase-box", style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "padding": "8px 12px", "borderRadius": "6px"}),
            ]
        ),
        html.Div(style={"display": "flex", "gap": "10px", "marginBottom": "10px"},
            children=[
                html.Div(style={"flex": "7.2", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[html.Div(style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "6px"},
                        children=[dcc.Graph(id="quant-main-chart", config={"displayModeBar": False, "responsive": True}, style={"height": "560px"})])]
                ),
                html.Div(style={"flex": "2.8", "display": "flex", "flexDirection": "column", "gap": "8px"},
                    children=[
                        html.Div(style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "8px 10px", "height": "290px", "overflow": "hidden"},
                            children=[
                                html.Div(style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #1E293B", "paddingBottom": "4px", "marginBottom": "6px"},
                                    children=[html.Span("REAL DOM / DEPTH LADDER", style={"color": "#38BDF8", "fontSize": "11px", "fontWeight": "bold"}),
                                              html.Span("L2 BOOK (Binance)", style={"color": "#94A3B8", "fontSize": "10px"})]),
                                html.Div(id="dom-ladder-content")
                            ]
                        ),
                        html.Div(style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "8px 10px", "height": "260px", "overflow": "hidden"},
                            children=[
                                html.Div(style={"display": "flex", "justifyContent": "space-between", "borderBottom": "1px solid #1E293B", "paddingBottom": "4px", "marginBottom": "6px"},
                                    children=[html.Span("THE TAPE (REAL TIME & SALES)", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"}),
                                              html.Span("TICK STREAM", style={"color": "#00E676", "fontSize": "10px"})]),
                                html.Div(id="tape-content", style={"height": "230px", "overflowY": "auto"})
                            ]
                        )
                    ]
                )
            ]
        ),
        html.Div(style={"backgroundColor": "#0A0E17", "border": "1px solid #1E293B", "borderRadius": "6px", "padding": "10px 14px", "marginTop": "4px"},
            children=[
                html.Div(style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "8px", "borderBottom": "1px solid #1E293B", "paddingBottom": "6px"},
                    children=[
                        html.Div([html.Span("TRADE BLOTTER // INSTITUTIONAL EXECUTION LOG", style={"color": "#38BDF8", "fontSize": "12px", "fontWeight": "bold"}),
                                  html.Span(" (Dual Engine, Real Data)", style={"color": "#94A3B8", "fontSize": "11px"})]),
                        html.Div([
                            html.Button("MANUAL CUT (CLOSE TRADE)", id="manual-close-btn", n_clicks=0,
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


@app.callback(Output("manual-close-msg", "children"), [Input("manual-close-btn", "n_clicks")], prevent_initial_call=True)
def handle_manual_cut(n_clicks):
    if n_clicks and trade_state.get('in_position'):
        close_trade_instance(last_known_price, 'MANUAL CUT')
        return html.Span("Position Squared Off!", style={"color": "#00E676", "fontWeight": "bold"})
    elif n_clicks:
        return html.Span("No Active Position", style={"color": "#94A3B8"})
    return ""


@app.callback(
    [Output("quant-clocks", "children"), Output("feed-status-header", "children"),
     Output("ai-regime-card", "children"), Output("vwap-sniper-card", "children"),
     Output("live-price-box", "children"), Output("spread-box", "children"),
     Output("high-box", "children"), Output("low-box", "children"),
     Output("session-box", "children"), Output("phase-box", "children"),
     Output("quant-main-chart", "figure"), Output("dom-ladder-content", "children"),
     Output("tape-content", "children"), Output("blotter-summary-stats", "children"),
     Output("trade-blotter-table", "children")],
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

    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    clocks_text = f"UTC {now_utc.strftime('%H:%M:%S')}  |  NYC {(now_utc - timedelta(hours=4)).strftime('%H:%M:%S')}  |  LDN {(now_utc + timedelta(hours=1)).strftime('%H:%M:%S')}  |  IST {now.strftime('%H:%M:%S')}"

    feed_color = "#00E676" if feed_status['connected'] else "#FF3366"
    feed_text = "FEED: LIVE (Port 9000)" if feed_status['connected'] else f"FEED: RECONNECTING ({feed_status['reconnects']})"
    feed_header_children = [html.Span(feed_text, style={"color": feed_color, "fontWeight": "bold", "fontSize": "11px"})]
    if real_spot_state['price'] is not None:
        feed_header_children.append(html.Span(
            f"  |  Real Spot ({real_spot_state['source']}): ${real_spot_state['price']:.2f}",
            style={"color": "#FFD700", "fontSize": "11px", "marginLeft": "8px"}
        ))
    feed_header = html.Span(feed_header_children)

    is_chop = sniper['regime'] == 'SIDEWAYS_CHOP'
    regime_color = "#FF3366" if is_chop else "#00E676"
    regime_bg = "#2A0812" if is_chop else "#062E1C"
    regime_border = "#FF3366" if is_chop else "#00E676"
    regime_title = "SIDEWAYS CHOPPY CONSOLIDATION" if is_chop else "HIGH MOMENTUM EXPANSION"
    regime_desc = "TRADING BLOCKED - False Breakout Protection" if is_chop else "TRENDING DIRECTIONAL FLOW - Sniper Ready"
    chop_val = sniper.get('chop_index', 50.0)
    ker_val = sniper.get('efficiency_ratio', 0.5)

    regime_card_elem = html.Div([
        html.Div([html.Span("AI REGIME DETECTOR: ", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                  html.Span(regime_title, style={"color": regime_color, "fontSize": "12px", "fontWeight": "900", "letterSpacing": "0.5px"})], style={"marginBottom": "4px"}),
        html.Div([html.Span(f"STATUS: {regime_desc}", style={"color": "#F8FAFC", "fontSize": "11px", "backgroundColor": regime_bg, "padding": "3px 8px", "borderRadius": "4px", "border": f"1px solid {regime_border}", "display": "inline-block"})], style={"marginBottom": "6px"}),
        html.Div([html.Span(f"Bill Dreiss CHOP: {chop_val}/100 ", style={"color": "#FFD700", "fontSize": "10px", "marginRight": "8px"}),
                  html.Span("(>61.8=Chop)" if chop_val >= 61.8 else "(<45=Trend)", style={"color": "#94A3B8", "fontSize": "9px"}),
                  html.Span(f" | KER Efficiency: {ker_val}", style={"color": "#38BDF8", "fontSize": "10px", "marginLeft": "6px"})])
    ])

    sig_text = sniper.get('signal', 'SCANNING LIQUIDITY')
    is_buy_sig = 'BUY' in sig_text
    is_sell_sig = 'SELL' in sig_text
    sig_color = "#00E676" if is_buy_sig else ("#FF3366" if is_sell_sig else "#FFD700")

    sniper_card_elem = html.Div([
        html.Div([
            html.Div([html.Span("VWAP QUANT SNIPER ($3-$5 TP): ", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                      html.Span(sig_text, style={"color": sig_color, "fontSize": "13px", "fontWeight": "900", "letterSpacing": "1px"})]),
            html.Span(f"CONFIDENCE: {sniper.get('confidence', 80)}%", style={"color": "#FFD700", "fontSize": "11px", "fontWeight": "bold"})
        ], style={"display": "flex", "justifyContent": "space-between", "marginBottom": "4px"}),
        html.Div([
            html.Div([html.Span("ENTRY: ", style={"color": "#94A3B8", "fontSize": "10px"}), html.Span(f"${sniper.get('entry_price', price):.2f}", style={"color": "#FFFFFF", "fontSize": "12px", "fontWeight": "bold"})]),
            html.Div([html.Span("STOP LOSS: ", style={"color": "#FF3366", "fontSize": "10px"}), html.Span(f"${sniper.get('sl', price - 2.0):.2f}", style={"color": "#FF3366", "fontSize": "12px", "fontWeight": "bold"})]),
            html.Div([html.Span("TP-1: ", style={"color": "#00E676", "fontSize": "10px"}), html.Span(f"${sniper.get('tp1', price + 3.5):.2f}", style={"color": "#00E676", "fontSize": "12px", "fontWeight": "bold"})]),
            html.Div([html.Span("TP-2: ", style={"color": "#38BDF8", "fontSize": "10px"}), html.Span(f"${sniper.get('tp2', price + 5.0):.2f}", style={"color": "#38BDF8", "fontSize": "12px", "fontWeight": "bold"})]),
            html.Div([html.Span("R:R: ", style={"color": "#94A3B8", "fontSize": "10px"}), html.Span(f"{sniper.get('risk_reward', '1:2.5')}", style={"color": "#FFD700", "fontSize": "12px", "fontWeight": "bold"})])
        ], style={"display": "flex", "justifyContent": "space-between", "backgroundColor": "#070A0F", "padding": "4px 10px", "borderRadius": "4px", "border": "1px solid #1E293B", "marginBottom": "4px"}),
        html.Div([html.Span("CRITERIA: ", style={"color": "#94A3B8", "fontSize": "10px"}), html.Span(sniper.get('reason', 'Analyzing live bar...'), style={"color": "#CBD5E1", "fontSize": "10px"})])
    ])

    ch_val = price - meta.get('open', price)
    ch_pct = (ch_val / meta.get('open', price)) * 100 if meta.get('open') else 0.0
    ch_color = "#00E676" if ch_val >= 0 else "#FF3366"
    ch_sign = "+" if ch_val >= 0 else ""
    price_elem = [html.Div("XAU/USD SPOT", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                  html.Div(f"${price:.2f}", style={"color": "#FFD700", "fontSize": "18px", "fontWeight": "900"}),
                  html.Div(f"{ch_sign}{ch_val:.2f} ({ch_sign}{ch_pct:.2f}%)", style={"color": ch_color, "fontSize": "10px"})]

    spread = round(abs(meta.get('ask', price) - meta.get('bid', price)), 2)
    spread_elem = [html.Div("SPREAD / LIQUIDITY", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                   html.Div(f"${spread:.2f}", style={"color": "#38BDF8", "fontSize": "16px", "fontWeight": "bold"}),
                   html.Div("REAL BOOK SPREAD", style={"color": "#00E676", "fontSize": "10px"})]
    high_elem = [html.Div("SESSION HIGH", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                 html.Div(f"${meta.get('high', price):.2f}", style={"color": "#E2E8F0", "fontSize": "16px", "fontWeight": "bold"}),
                 html.Div("RESISTANCE LVL", style={"color": "#94A3B8", "fontSize": "10px"})]
    low_elem = [html.Div("SESSION LOW", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                html.Div(f"${meta.get('low', price):.2f}", style={"color": "#E2E8F0", "fontSize": "16px", "fontWeight": "bold"}),
                html.Div("SUPPORT LVL", style={"color": "#94A3B8", "fontSize": "10px"})]

    hour = now.hour
    session_name = "LONDON" if 13 <= hour < 17 else "NEW YORK" if 17 <= hour < 22 else "ASIAN"
    is_kz = (13 <= hour < 15) or (17 <= hour < 20)
    session_elem = [html.Div("MARKET SESSION", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                     html.Div(session_name, style={"color": "#FFD700", "fontSize": "16px", "fontWeight": "bold"}),
                     html.Div("KILL ZONE ACTIVE" if is_kz else "STANDARD SESSION", style={"color": "#00E676" if is_kz else "#94A3B8", "fontSize": "10px", "fontWeight": "bold"})]
    phase_elem = [html.Div("ORDER FLOW PHASE", style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold"}),
                  html.Div(cur_bar.get('phase', 'Institutional').upper(), style={"color": "#38BDF8", "fontSize": "15px", "fontWeight": "bold"}),
                  html.Div("CVD ALIGNED", style={"color": "#00E676", "fontSize": "10px"})]

    display_bars = bars[-100:] if len(bars) > 100 else bars
    if display_bars:
        df = pd.DataFrame(display_bars)
        df['time_str'] = df['time'].apply(lambda x: x.strftime('%H:%M') if isinstance(x, datetime) else str(x)[-8:-3])
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        cum_pv_series = (typical_price * df['volume']).cumsum()
        cum_vol_series = df['volume'].cumsum()
        df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])
        df['diff_sq'] = ((typical_price - df['vwap']) ** 2) * df['volume']
        df['vwap_std'] = np.sqrt(df['diff_sq'].cumsum() / np.maximum(cum_vol_series, 1.0))
        df['vwap_upper'] = (df['vwap'] + 1.28 * df['vwap_std']).round(2)
        df['vwap_lower'] = (df['vwap'] - 1.28 * df['vwap_std']).round(2)

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.68, 0.16, 0.16])
        # Fill colors were 30%-opacity (rgba(...,0.3)), which blended almost
        # invisibly into the dark chart background - only the solid wick/
        # border line was visible, making candles look like thin OHLC lines
        # instead of proper wide TradingView-style bodies. Full opacity fixes it.
        fig.add_trace(go.Candlestick(x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                                      name="XAUUSD", increasing_line_color="#00E676", decreasing_line_color="#FF3366",
                                      increasing_fillcolor="#00E676", decreasing_fillcolor="#FF3366", showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'], mode="lines", name="VWAP", line=dict(color="#FFD700", width=2)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap_upper'], mode="lines", name="Upper Band (+1.28σ)", line=dict(color="#FF9800", width=1.5, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap_lower'], mode="lines", name="Lower Band (-1.28σ)", line=dict(color="#00F0FF", width=1.5, dash="dash")), row=1, col=1)

        imb_df = df[df['imbalance'] == True]
        if not imb_df.empty:
            fig.add_trace(go.Scatter(x=imb_df['time_str'], y=imb_df['high'] * 1.0001, mode='markers', marker=dict(symbol='star', size=11, color='#00E5FF'), name='Imbalance'), row=1, col=1)

        if SHOW_ENTRY_SL_TP_LINES and pos.get('in_position'):
            fig.add_hline(y=pos['entry_price'], line_color="#38BDF8", line_width=2, line_dash="dash", row=1, col=1, annotation_text=f"ENTRY ${pos['entry_price']:.2f}")
            fig.add_hline(y=pos['stop_loss'], line_color="#FF3366", line_width=2, line_dash="dot", row=1, col=1, annotation_text=f"SL ${pos['stop_loss']:.2f}")
            fig.add_hline(y=pos['take_profit'], line_color="#00E676", line_width=2, line_dash="dot", row=1, col=1, annotation_text=f"TP ${pos['take_profit']:.2f}")

        delta_colors = ["#00E676" if d >= 0 else "#FF3366" for d in df['delta']]
        fig.add_trace(go.Bar(x=df['time_str'], y=df['delta'], name="Delta", marker_color=delta_colors, showlegend=False), row=2, col=1)
        fig.add_trace(go.Scatter(x=df['time_str'], y=df['cvd'], mode="lines", name="CVD", line=dict(color="#38BDF8", width=2), fill="tozeroy", fillcolor="rgba(56,189,248,0.15)", showlegend=False), row=3, col=1)

        fig.update_xaxes(type='category', showgrid=True, gridcolor="#131C2E", tickfont=dict(size=9, color="#94A3B8"))
        fig.update_yaxes(showgrid=True, gridcolor="#131C2E", tickfont=dict(size=10, color="#94A3B8"))
        fig.update_layout(template="plotly_dark", paper_bgcolor="#0A0E17", plot_bgcolor="#0A0E17", margin=dict(l=10, r=40, t=10, b=10),
                           xaxis_rangeslider_visible=False, height=550, showlegend=True,
                           legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=10, color="#94A3B8")))
    else:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", paper_bgcolor="#0A0E17", plot_bgcolor="#0A0E17")

    bids = book.get('bids', [])[:8]
    asks = book.get('asks', [])[:8]
    max_dom_vol = max([b['volume'] for b in bids + asks] + [1.0])

    dom_rows = []
    for a in reversed(asks):
        p_pct = min(100, int((a['volume'] / max_dom_vol) * 100))
        dom_rows.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "position": "relative"},
            children=[html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{p_pct}%", "backgroundColor": "rgba(255, 51, 102, 0.22)", "zIndex": 0}),
                      html.Span(f"${a['price']:.2f}", style={"color": "#FF3366", "fontWeight": "bold", "zIndex": 1}),
                      html.Span(f"{a['volume']:.3f}", style={"color": "#E2E8F0", "zIndex": 1})]))
    dom_rows.append(html.Div(f"-- SPREAD ${spread:.2f} --", style={"textAlign": "center", "color": "#FFD700", "fontSize": "10px", "padding": "3px 0", "borderTop": "1px dashed #1E293B", "borderBottom": "1px dashed #1E293B", "margin": "2px 0"}))
    for b in bids:
        p_pct = min(100, int((b['volume'] / max_dom_vol) * 100))
        dom_rows.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "position": "relative"},
            children=[html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{p_pct}%", "backgroundColor": "rgba(0, 230, 118, 0.22)", "zIndex": 0}),
                      html.Span(f"${b['price']:.2f}", style={"color": "#00E676", "fontWeight": "bold", "zIndex": 1}),
                      html.Span(f"{b['volume']:.3f}", style={"color": "#E2E8F0", "zIndex": 1})]))
    if not bids and not asks:
        dom_rows = [html.Div("Waiting for real depth data from Port 9000...", style={"color": "#555", "fontStyle": "italic", "fontSize": "11px", "padding": "10px"})]

    tape_rows = []
    for t in tape[:15]:
        side_color = "#00E676" if t['side'] == 'BUY' else "#FF3366"
        bg_col = "rgba(0, 230, 118, 0.12)" if t['side'] == 'BUY' else "rgba(255, 51, 102, 0.12)"
        tape_rows.append(html.Div(style={"display": "flex", "justifyContent": "space-between", "padding": "2px 4px", "fontSize": "11px", "borderBottom": "1px solid #111827", "backgroundColor": bg_col},
            children=[html.Span(t['time'], style={"color": "#94A3B8"}), html.Span(f"${t['price']:.2f}", style={"color": "#FFFFFF", "fontWeight": "bold"}),
                      html.Span(f"{t['size']:.3f}", style={"color": side_color, "fontWeight": "bold"}),
                      html.Span("BLOCK" if t.get('is_block') else t['side'], style={"color": "#FFD700" if t.get('is_block') else side_color, "fontSize": "9px"})]))

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
    blotter_table_rows = [html.Tr([html.Th(h, style={"padding": "6px 8px", "color": "#94A3B8", "fontSize": "10px", "textAlign": "left", "borderBottom": "1px solid #1E293B"}) for h in blotter_headers])]
    for t in history[:12]:
        dir_col = "#00E676" if "BUY" in str(t.get('dir', '')) else "#FF3366"
        st = str(t.get('status', ''))
        st_col = "#00E676" if ("WIN" in st or "TP" in st) else ("#FF3366" if ("LOSS" in st or "SL" in st) else "#FFD700")
        pnl_val = float(t.get('pnl', 0.0))
        pnl_t_col = "#00E676" if pnl_val >= 0 else "#FF3366"
        pnl_t_sign = "+" if pnl_val >= 0 else ""
        engine_name = t.get('signal_type', 'SNIPER')
        engine_badge = html.Span("VWAP-FC " + engine_name[-2:].lower() if engine_name.startswith("VWAP_FC") else ("QUANT SNIPER" if "SNIPER" in engine_name else "CONFLUENCE"),
            style={"backgroundColor": "#1E1B4B" if "SNIPER" in engine_name else "#1E293B", "color": "#38BDF8" if "SNIPER" in engine_name else "#E2E8F0", "padding": "2px 6px", "borderRadius": "3px", "fontSize": "9px", "fontWeight": "bold"})
        blotter_table_rows.append(html.Tr(style={"borderBottom": "1px solid #111827", "fontSize": "11px"}, children=[
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
        ]))
    blotter_table = html.Table(blotter_table_rows, style={"width": "100%", "borderCollapse": "collapse"})

    return (clocks_text, feed_header, regime_card_elem, sniper_card_elem, price_elem, spread_elem, high_elem, low_elem,
            session_elem, phase_elem, fig, dom_rows, tape_rows, blotter_stats, blotter_table)


# =============================================================================
# 9. SERVER ENTRY POINT (PORT 9080)
# =============================================================================
if __name__ == '__main__':
    init_db()
    load_initial_bars()
    load_trades()
    mt5_init()

    t = threading.Thread(target=live_feed_subscriber, daemon=True)
    t.start()

    logger.info("=" * 60)
    logger.info(f"AI QUANT TERMINAL V2 PILOT STARTING ON http://{DASH_HOST}:{DASH_PORT}")
    logger.info("Dual Engine: Institutional Confluence + VWAP Quant Sniper (real data)")
    logger.info("Plus: HFT-Engine (statistical order-flow)")
    logger.info("=" * 60)

    app.run(host=DASH_HOST, port=DASH_PORT, debug=False)
