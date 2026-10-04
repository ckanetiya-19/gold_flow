# ------------------------------------------------------------
# XAUUSD V2 INSTITUTIONAL TERMINAL - HEDGE-FUND PILOT UPGRADE
# NEW file, NEW port (9060). PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py and its
# live port 8060 are completely untouched and keep running independently.
#
# Upgrades over V1 (PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py):
#   1. Real trade volume + real buy/sell side from the hardened
#      PORT_9000_CORE_WEBSOCKET_ENGINE_V2.py feed (ws://127.0.0.1:9000/ws)
#      instead of random.randint(60,180) per tick.
#   2. Zero hardcoded API keys. V1 had a live paid REALMARKET_KEY and a
#      GOLDAPI_KEY committed in plain text - both now come from env vars,
#      unset by default (this dashboard runs fine on the free Binance
#      feed alone).
#   3. Synthetic random-walk fallback (_generate_synthetic_bars) removed.
#      If Binance klines are unavailable, the dashboard says so instead
#      of fabricating candles.
#   4. Finalized bars persisted to QuestDB (bars_v2_9060) for durable,
#      queryable, backtest-ready storage - on top of the local SQLite
#      trade journal.
#   5. Binds to 127.0.0.1 only (V1 bound 0.0.0.0).
#   6. Resilient reconnect-with-backoff to Port 9000, visible feed-status
#      indicator instead of silent failure.
#
# Session/Kill-Zone detection, Risk Manager, Confluence scoring, FVG,
# Absorption and CVD-Divergence detection are kept as-is from V1 - all of
# that was already genuine rule-based logic, not random. Still PAPER
# trading only (no real broker order placement).
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

# ---------- CONFIG (env vars, zero hardcoded secrets) ----------
CONFIG = {
    "REALMARKET_KEY": os.getenv("GOLDFLOW_REALMARKET_KEY", ""),   # optional, unset by default
    "REALMARKET_URL": "https://api.realmarketapi.com/api/v1/price",
    "GOLDAPI_KEY": os.getenv("GOLDFLOW_GOLDAPI_KEY", ""),          # optional, unset by default
    "GOLDAPI_URL": "https://www.goldapi.io/api/XAU/USD",
    "DATA_MODE": "Port 9000 Core Engine (real Binance aggTrade relay)",
    "TRADE_MODE": "PAPER",
    "FIXED_LOT": 0.01,
    "RISK_REWARD_RATIO": 2.0,
    "ACCOUNT_SIZE": 10000.0,
    "MAX_RISK_PCT": 1.0,
    "MAX_DAILY_LOSS_PCT": 3.0,
    "MAX_CONSECUTIVE_LOSSES": 3,
}
BINANCE_SYMBOL = os.getenv("GOLDFLOW_BINANCE_SYMBOL", "PAXGUSDT")
ENGINE_WS_URL = os.getenv("GOLDFLOW_ENGINE_WS_URL", "ws://127.0.0.1:9000/ws")
QUESTDB_ILP_CONF = os.getenv("GOLDFLOW_QUESTDB_ILP_CONF", "tcp::addr=127.0.0.1:9009;")
DASH_HOST = os.getenv("GOLDFLOW_DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.getenv("GOLDFLOW_DASH_PORT", "9060"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("XAUUSD_V2_Pilot")

# Temporarily hides the Entry/SL/TP price lines on this dashboard's own chart
# (user request, 2026-09-16) - flip back to True to restore them. Does not
# affect trade_state, signals, or MT5 execution - display only.
SHOW_ENTRY_SL_TP_LINES = False

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
MT5_MAGIC = 90600
_mt5_ready = False
# Master switch: MT5 order placement is OFF unless GOLDFLOW_MT5_TRADING_ENABLED=1.
# Paper trading (signals, trade log, DB) keeps running; only MT5 is never touched.
MT5_TRADING_ENABLED = os.getenv("GOLDFLOW_MT5_TRADING_ENABLED", "0") == "1"
# Strategy master switch: entry strategy is OFF unless GOLDFLOW_STRATEGIES_ENABLED=1.
# Order-flow analytics/charts keep running.
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
        result = None
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": MT5_SYMBOL, "volume": MT5_LOT,
                "type": order_type, "price": price, "sl": float(sl), "tp": float(tp) if tp else 0.0,
                "deviation": 20, "magic": magic or MT5_MAGIC, "comment": comment or "Goldflow-9060-Auto",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[MT5 DEMO ORDER] {direction} {MT5_LOT} {MT5_SYMBOL} @ {price} "
                            f"SL={sl} TP={tp} ticket=#{result.order} comment={comment or 'Goldflow-9060-Auto'}")
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
last_known_price = 0.0
last_price_update = datetime.now()
market_meta = {'open': 0, 'high': 0, 'low': 0, 'bid': 0, 'ask': 0, 'ch': 0}
feed_status = {'connected': False, 'reconnects': 0}
real_spot_state = {'price': None, 'bid': None, 'ask': None, 'source': None}

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0}
trade_history = []

# ---------- SQLite Persistence (new DB file - does not touch trades_v2.db) ----------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_v2_9060.db')

# ---------- QuestDB durable bar storage ----------
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
            "bars_v2_9060",
            symbols={"symbol": "XAUUSD", "phase": str(bar.get('phase', 'Neutral')), "session": str(bar.get('session', ''))},
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


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, time TEXT, exit_time TEXT, dir TEXT, entry REAL,
        exit_price REAL, sl REAL, tp REAL, status TEXT, pnl REAL,
        confidence INTEGER, phase TEXT, session TEXT, signal_type TEXT
    )''')
    conn.commit()
    conn.close()
    logger.info(f"V2 Pilot Database initialized: {DB_PATH}")


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
        logger.info(f"Loaded {len(trade_history)} trades from database")
    except Exception as e:
        logger.error(f"DB load error: {e}")


def graceful_shutdown(signum=None, frame=None):
    global is_running
    logger.info("Graceful shutdown initiated...")
    is_running = False
    if _questdb_sender is not None:
        try:
            _questdb_sender.close()
        except Exception:
            pass
    sys.exit(0)


# ============================================================
# SESSION / KILL ZONE DETECTION (unchanged real logic)
# ============================================================
def get_session_info():
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
# RISK MANAGER (unchanged real logic)
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
            "can_trade": can, "reason": reason, "daily_pnl": round(self.daily_pnl, 2),
            "consecutive_losses": self.consecutive_losses, "trades_today": self.trades_today,
        }

risk_manager = RiskManager()


# ============================================================
# ORDER FLOW ENGINE (unchanged real math, now fed real ticks)
# ============================================================
# ============================================================
# VWAP FIRST-CLOSE PAPER STRATEGY (1-min and 5-min), shared module vwap_first_close.py.
# Paper only (never touches MT5). Validated by backtest_vwap_first_close_be.py.
# ============================================================
import vwap_first_close as vfc
VWAP_FC_ENABLED = os.getenv("GOLDFLOW_VWAP_FC_ENABLED", "1") == "1"


def _vwap_fc_record(rec):
    """Show a closed VWAP-FC paper trade in this dashboard's trade table + DB (P/L = NET $ per 0.01 lot)."""
    global trade_history
    status = {'WIN': 'TP HIT', 'BE': 'BE EXIT', 'LOSS': 'SL HIT'}.get(rec['outcome'], rec['outcome'])
    fmt = '%Y-%m-%d %H:%M:%S'
    next_id = (max(t['id'] for t in trade_history) + 1) if trade_history else 1
    record = {
        'id': next_id, 'time': rec['open_utc'].astimezone().strftime(fmt),
        'exit_time': rec['close_utc'].astimezone().strftime(fmt), 'dir': rec['dir'],
        'entry': round(rec['entry'], 2), 'exit_price': round(rec['exit'], 2), 'sl': round(rec['sl'], 2),
        'tp': round(rec['tp'], 2), 'status': status, 'pnl': round(rec['net'], 2), 'confidence': 0,
        'phase': 'VWAP-FC', 'session': 'VWAP-FC ' + str(rec['tf']) + 'm', 'signal_type': 'VWAP_FC_' + str(rec['tf']) + 'M'}
    trade_history.append(record)
    if len(trade_history) > 100:
        trade_history.pop(0)
    save_trade_to_db(record)


vwap_fc_daily = vfc.DailyVwap()
# V1/V4/V10 MT5 trading turned OFF on demo (user request) - paper-trading/DB logging continues.
# Portfolio (RSI14/VOLIMB/ADXTREND) keeps its own separate MT5 wiring, unaffected.
VWAP_FC_MT5_ENABLED = os.getenv("GOLDFLOW_VWAP_FC_MT5_ENABLED", "0") == "1"
_vwap_fc_mt5_open = mt5_place_order if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else None
_vwap_fc_mt5_modify = mt5_modify_position if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else None
vwap_fc_strats = [vfc.make_variant(v, "9060", 1, vwap_fc_daily, logger, _vwap_fc_record if v == "V1" else None,
                                    mt5_open=_vwap_fc_mt5_open, mt5_modify=_vwap_fc_mt5_modify)
                  for v in vfc.VARIANTS] if VWAP_FC_ENABLED else []
if VWAP_FC_ENABLED:
    logger.info(f"VWAP paper strategies ENABLED (FINAL 6, 1-min only), MT5 live={'YES (demo)' if (MT5_TRADING_ENABLED and VWAP_FC_MT5_ENABLED) else 'no, paper only'}: "
                f"{len(vwap_fc_strats)} variant instances "
                f"({', '.join(vfc.VARIANTS)}); only V1 shows in the dashboard table, all logged to vwap_fc_paper.db (magic 90810-90815, comment GF-<variant>-9060).")


def _rolling_chart_vwap(window_bars):
    """Same formula the chart itself plots (update_chart): cumulative Typical
    Price (H+L+C)/3 weighted by volume, over the same last-90-bar window
    the chart displays - NOT the process-lifetime cumulative bar['vwap']."""
    if not window_bars:
        return None
    tp_sum = sum(((b['high'] + b['low'] + b['close']) / 3.0) * b.get('volume', 0.0) for b in window_bars)
    vol_sum = sum(b.get('volume', 0.0) for b in window_bars)
    return round(tp_sum / vol_sum, 2) if vol_sum > 0 else window_bars[-1]['close']


def _vwap_fc_on_bar(bar, price):
    try:
        now_utc = datetime.now(timezone.utc)
        start_utc = bar['time'].astimezone(timezone.utc)
        chart_vwap = _rolling_chart_vwap(historical_bars[-90:])
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
portfolio_engine = pf.PortfolioEngine("9060", logger, mt5_open=_portfolio_mt5_open,
                                       mt5_modify=_portfolio_mt5_modify) if PORTFOLIO_ENABLED else None
if PORTFOLIO_ENABLED:
    logger.info(f"Portfolio strategies ENABLED (RSI14, VOLIMB, ADXTREND), MT5 live="
                f"{'YES (demo)' if MT5_TRADING_ENABLED else 'no, paper only'} - magic 90820/90821/90823, "
                f"comment GF-P-<strategy>-9060, logged to portfolio_paper.db.")


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
vwapcross_engine = vxc.VwapFullCrossover("9060", logger, mt5_open=_vwapcross_mt5_open,
                                          mt5_modify=_vwapcross_mt5_modify) if VWAPCROSS_ENABLED else None
if VWAPCROSS_ENABLED:
    logger.info(f"VWAP Full-Crossover ENABLED (paper), MT5 live="
                f"{'YES (demo)' if (MT5_TRADING_ENABLED and VWAPCROSS_MT5_ENABLED) else 'no, paper only'} - "
                f"magic 90830, comment GF-VWAPX-9060, logged to vwap_full_crossover_paper.db. "
                f"NOTE: NOT backtest-validated (went net negative in full-period test) - deployed live at user's explicit request to observe.")


def _vwapcross_on_bar(bar, price):
    if not vwapcross_engine:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        chart_vwap = _rolling_chart_vwap(historical_bars[-90:])
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


def update_mark_price(price):
    """iTick (real spot XAUUSD) - or, when iTick is briefly stale, Port 9000's
    own Binance bid/ask-mid fallback - is now the SOLE driver of the candle's
    open/high/low/close, and owns bar creation/finalization. Previously PAXG
    trade ticks also set open/close directly in process_tick; since PAXG can
    trade at a sustained few-dollar offset from real spot gold, every bar
    showed an artificial wick toward whichever source last updated. Volume/
    delta/CVD stay strictly tied to real PAXG trade ticks (see process_tick) -
    only the price path moved here."""
    global current_bar, historical_bars, last_known_price, last_price_update
    price = float(price)
    last_known_price = price
    last_price_update = datetime.now()
    now = datetime.now()
    current_minute = now.replace(second=0, microsecond=0)
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else price

    with data_lock:
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
                if len(historical_bars) > 200:
                    historical_bars.pop(0)
                check_and_execute_trade(final_bar)
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
    """Real Binance PAXG trade ticks now feed ONLY volume/delta/CVD into
    whichever bar update_mark_price (real-spot-driven) currently has open -
    PAXG's own trade price no longer touches open/high/low/close (see
    update_mark_price for why). The chart's displayed price/candle is driven
    entirely by the real spot feed; PAXG contributes real trade volume only."""
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
        logger.error(f"Tick Error: {e}")


def finalize_bar(bar, vwap):
    delta = float(bar['buy_vol'] - bar['sell_vol'])
    poc = max(bar['levels'], key=bar['levels'].get) if bar['levels'] else bar['close']
    avg_vol = np.mean([b['volume'] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar['volume']
    imbalance = bar['volume'] > (avg_vol * 2.2) if avg_vol > 0 else False

    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']:
            fvg = "Bullish"
        elif prev2['low'] > bar['high']:
            fvg = "Bearish"

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
            phase = "Bearish Divergence"
        elif cvd_trend < 0 and price_trend > 0:
            phase = "Bullish Divergence"
        else:
            phase = "Manipulation"

    absorption = None
    if len(historical_bars) >= 3:
        recent_3 = historical_bars[-3:]
        total_vol = sum(b['volume'] for b in recent_3)
        price_range = max(b['high'] for b in recent_3) - min(b['low'] for b in recent_3)
        if total_vol > avg_vol * 3 and price_range < 1.0:
            absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption"

    cvd_divergence = None
    if len(historical_bars) >= 15:
        prices_recent = [b['close'] for b in historical_bars[-15:]]
        cvds_recent = [b.get('cvd', 0) for b in historical_bars[-15:]]
        price_low_idx = np.argmin(prices_recent)
        price_high_idx = np.argmax(prices_recent)
        if price_low_idx > 8:
            prev_low_idx = np.argmin(prices_recent[:price_low_idx])
            if (prices_recent[price_low_idx] < prices_recent[prev_low_idx] and
                cvds_recent[price_low_idx] > cvds_recent[prev_low_idx]):
                cvd_divergence = "Bullish"
        if price_high_idx > 8:
            prev_high_idx = np.argmax(prices_recent[:price_high_idx])
            if (prices_recent[price_high_idx] > prices_recent[prev_high_idx] and
                cvds_recent[price_high_idx] < cvds_recent[prev_high_idx]):
                cvd_divergence = "Bearish"

    score = 0
    signal_type = "NONE"
    session = get_session_info()

    if delta > 0:
        score += 25; signal_type = "BUY"
    elif delta < 0:
        score += 25; signal_type = "SELL"

    if signal_type == "BUY" and bar['close'] > vwap: score += 20
    elif signal_type == "SELL" and bar['close'] < vwap: score += 20

    if session['kill_zone']: score += 15
    elif session['quality'] == "MEDIUM": score += 8

    if cvd_divergence == "Bullish" and signal_type == "BUY": score += 20
    elif cvd_divergence == "Bearish" and signal_type == "SELL": score += 20

    if fvg == "Bullish" and signal_type == "BUY": score += 10
    elif fvg == "Bearish" and signal_type == "SELL": score += 10

    if absorption == "Bullish Absorption" and signal_type == "BUY": score += 10
    elif absorption == "Bearish Absorption" and signal_type == "SELL": score += 10

    if phase == "Bullish Divergence": signal_type = "BUY"
    elif phase == "Bearish Divergence": signal_type = "SELL"

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
# TRADING EXECUTION (paper only, unchanged rule-based logic)
# ============================================================
def calculate_sl_tp(price, direction, vwap, atr):
    if atr == 0: atr = 2.0
    if direction == "LONG":
        sl = min(price - atr * 1.5, vwap - atr * 0.5)
        tp = price + (price - sl) * CONFIG["RISK_REWARD_RATIO"]
    else:
        sl = max(price + atr * 1.5, vwap + atr * 0.5)
        tp = price - (sl - price) * CONFIG["RISK_REWARD_RATIO"]
    return round(sl, 2), round(tp, 2)


def execute_order(symbol, action, lot, sl, tp, confidence, phase, session_name, signal_type):
    global trade_history
    logger.info(f"[V2 PAPER] {action} {lot} {symbol} @ ${last_known_price:.2f} | SL: ${sl:.2f} | TP: ${tp:.2f} | Conf: {confidence}% | Session: {session_name}")
    mt5_place_order(action, sl, tp)
    trade_state['in_position'] = True
    trade_state['entry_price'] = last_known_price
    trade_state['stop_loss'] = sl
    trade_state['take_profit'] = tp
    direction = "LONG" if "BUY" in action else "SHORT"
    trade_state['direction'] = direction

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    trade_history.append({
        'id': len(trade_history) + 1, 'time': now_str, 'exit_time': 'RUNNING',
        'dir': 'BUY' if direction == 'LONG' else 'SELL',
        'entry': round(last_known_price, 2), 'exit_price': round(last_known_price, 2),
        'sl': sl, 'tp': tp, 'status': 'OPEN', 'pnl': 0.0,
        'confidence': confidence, 'phase': phase, 'session': session_name, 'signal_type': signal_type
    })
    if len(trade_history) > 100:
        trade_history.pop(0)
    save_trade_to_db(trade_history[-1])
    return True


def check_and_execute_trade(bar):
    global trade_state, trade_history
    if trade_state['in_position']:
        current_price = bar['close']
        now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if trade_state['direction'] == "LONG":
            trade_state['pnl'] = round(current_price - trade_state['entry_price'], 2)
            if current_price <= trade_state['stop_loss']:
                _close_trade('SL HIT', now_time, current_price)
            elif current_price >= trade_state['take_profit']:
                _close_trade('TP HIT', now_time, current_price)
        else:
            trade_state['pnl'] = round(trade_state['entry_price'] - current_price, 2)
            if current_price >= trade_state['stop_loss']:
                _close_trade('SL HIT', now_time, current_price)
            elif current_price <= trade_state['take_profit']:
                _close_trade('TP HIT', now_time, current_price)
        if trade_state['in_position'] and trade_history:
            trade_history[-1]['pnl'] = trade_state['pnl']
            trade_history[-1]['exit_price'] = round(current_price, 2)
        return

    if not STRATEGIES_ENABLED:
        return

    can_trade, reason = risk_manager.can_trade()
    if not can_trade:
        logger.info(f"[RISK MANAGER] Trade blocked: {reason}")
        return

    session = get_session_info()
    min_confidence = 55 if session['kill_zone'] else 65
    if bar['confidence'] < min_confidence:
        return

    if len(historical_bars) > 5:
        prices = [b['close'] for b in historical_bars[-5:]]
        atr = np.mean([abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]) * 1.5
    else:
        atr = 2.0

    signal_type = bar.get('signal_type', 'NONE')
    if signal_type == "BUY" and bar['delta'] > 0:
        sl, tp = calculate_sl_tp(bar['close'], "LONG", bar['vwap'], atr)
        lot = risk_manager.calculate_position_size(bar['close'], sl)
        logger.info(f"[V2 SIGNAL] LONG @ ${bar['close']:.2f} | Conf: {bar['confidence']}% | Session: {session['name']}")
        execute_order("XAUUSD", "BUY", lot, sl, tp, bar['confidence'], bar['phase'], session['name'], signal_type)
    elif signal_type == "SELL" and bar['delta'] < 0:
        sl, tp = calculate_sl_tp(bar['close'], "SHORT", bar['vwap'], atr)
        lot = risk_manager.calculate_position_size(bar['close'], sl)
        logger.info(f"[V2 SIGNAL] SHORT @ ${bar['close']:.2f} | Conf: {bar['confidence']}% | Session: {session['name']}")
        execute_order("XAUUSD", "SELL", lot, sl, tp, bar['confidence'], bar['phase'], session['name'], signal_type)


def _close_trade(status, now_time, current_price):
    trade_state['in_position'] = False
    risk_manager.record_trade_result(trade_state['pnl'])
    if trade_history:
        trade_history[-1]['status'] = status
        trade_history[-1]['exit_time'] = now_time
        trade_history[-1]['exit_price'] = round(current_price, 2)
        trade_history[-1]['pnl'] = trade_state['pnl']
        update_trade_in_db(trade_history[-1]['id'], status, trade_state['pnl'], now_time, round(current_price, 2))
    logger.info(f"[{status}] P/L: ${trade_state['pnl']}")


# ============================================================
# REAL HISTORICAL DATA (Binance Klines only - no synthetic fallback)
# ============================================================
def initialize_historical_bars():
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta

    saved_bars = []  # V2 pilot starts fresh each run; QuestDB holds the durable history instead

    logger.info("Fetching REAL historical bars from Binance PAXG Klines...")
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                         params={'symbol': BINANCE_SYMBOL, 'interval': '1m', 'limit': 120}, timeout=10)
        klines = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"Binance Klines fetch failed: {e}")
        klines = []

    if not klines:
        logger.error("No historical data available (Binance unreachable). Chart will populate as live ticks arrive.")
        return

    with data_lock:
        historical_bars.clear()
        cum_vol = 0.0
        cum_pv = 0.0
        cum_delta = 0.0
        for k in klines:
            bar_time = datetime.fromtimestamp(k[0] / 1000).replace(second=0, microsecond=0)
            open_p, high_p, low_p, close_p = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5])
            taker_buy_vol = float(k[9])
            taker_sell_vol = vol - taker_buy_vol
            delta = taker_buy_vol - taker_sell_vol  # REAL delta from actual exchange data

            cum_delta += delta
            cum_vol += vol
            cum_pv += ((open_p + high_p + low_p + close_p) / 4) * vol
            vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else close_p

            historical_bars.append({
                'time': bar_time, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                'volume': vol, 'delta': delta, 'cvd': cum_delta,
                'vwap': vwap, 'poc': round((high_p + low_p) / 2, 2),
                'imbalance': False, 'fvg': None,
                'phase': "Distribution" if delta > 0 else "Accumulation",
                'confidence': 50, 'absorption': None, 'cvd_divergence': None,
                'signal_type': 'NONE', 'session': ''
            })

        last_known_price = historical_bars[-1]['close']
        market_meta['open'] = historical_bars[0]['open']
        market_meta['high'] = max(b['high'] for b in historical_bars)
        market_meta['low'] = min(b['low'] for b in historical_bars)
        market_meta['bid'] = last_known_price - 0.2
        market_meta['ask'] = last_known_price + 0.2

    logger.info(f"Loaded {len(historical_bars)} REAL bars from Binance | Last: ${last_known_price:.2f}")


# ============================================================
# LIVE FEED - subscribes to Port 9000 Core Engine V2 (real ticks, real depth)
# ============================================================
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
        market_meta['source'] = "Port 9000 (real Binance aggTrade)"
        process_tick(price, volume, is_buy)
    elif mtype == "depth":
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        if bids:
            market_meta['bid'] = float(bids[0][0])
        if asks:
            market_meta['ask'] = float(asks[0][0])
        if bids and asks:
            market_meta['spread'] = round(float(asks[0][0]) - float(bids[0][0]), 2)
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
    logger.info(f"Live Feed Worker: subscribing to Core Engine V2 at {ENGINE_WS_URL}")
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


# ============================================================
# DASH UI LAYOUT (unchanged from V1, port/title updated)
# ============================================================
v2_app = dash.Dash(__name__, external_stylesheets=['https://codepen.io/chriddyp/pen/bWLwgP.css'])
v2_app.title = "XAUUSD V2 Institutional Terminal - Pilot"


# =============================================================================
# READ-ONLY MIRROR ENDPOINT (additive only - changes nothing about this
# dashboard's own behavior/logic/UI). Exists purely so a separate full-screen
# TradingView-style chart page (Port 9061) can display the EXACT same bars,
# VWAP and signals as this dashboard, instead of recalculating independently
# (which would drift on cumulative values like CVD/VWAP depending on when
# each process started).
# =============================================================================
def _json_safe(v):
    """numpy bool_/int64/float64 (from pandas/numpy comparisons upstream, e.g.
    imbalance = volume > avg_vol*2.2) look like native Python types but
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

@v2_app.server.route('/api/mirror')
def mirror_endpoint():
    from flask import jsonify
    with data_lock:
        bars = [_bar_to_json(b) for b in historical_bars[-300:]]
        cur = {k: _json_safe(v) for k, v in current_bar.items() if k != 'levels'}
        if cur.get('time') is not None and hasattr(cur['time'], 'isoformat'):
            cur['time'] = cur['time'].isoformat()
        pos = {k: _json_safe(v) for k, v in trade_state.items()}
    return jsonify({
        'historical_bars': bars,
        'current_bar': cur,
        'trade_state': pos,
        'last_known_price': _json_safe(last_known_price),
        'cum_delta': _json_safe(cum_delta),
    })

v2_app.layout = html.Div([
    html.Div([
        html.Div([
            html.Span("⚡", style={'fontSize': '28px', 'marginRight': '10px'}),
            html.Span("XAUUSD V2 PILOT", style={'fontSize': '22px', 'fontWeight': '800', 'color': '#FFD700'}),
            html.Span(" — Institutional Order Flow Terminal (Port 9060)", style={'fontSize': '14px', 'color': '#888', 'marginLeft': '8px'}),
        ], style={'display': 'flex', 'alignItems': 'center'}),
        html.Div(id='session-badge', style={'display': 'flex', 'alignItems': 'center', 'gap': '10px'}),
    ], style={'display': 'flex', 'justifyContent': 'space-between', 'alignItems': 'center', 'padding': '12px 24px',
              'background': 'linear-gradient(135deg, #0d0d1a 0%, #1a1a2e 100%)', 'borderBottom': '2px solid #FFD700'}),

    html.Div(id='feed-status-banner', style={'textAlign': 'center', 'padding': '4px'}),

    html.Div([
        html.Div([
            html.Div("LIVE PRICE", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-live-price', children="$0.00", style={'fontSize': '28px', 'fontWeight': '800', 'color': '#FFD700'}),
            html.Div(id='v2-bid-ask', children="Bid/Ask: --", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '160px'}),
        html.Div([
            html.Div("SESSION", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-session-name', children="--", style={'fontSize': '18px', 'fontWeight': '700', 'color': '#00e5ff'}),
            html.Div(id='v2-session-quality', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '140px'}),
        html.Div([
            html.Div("CVD FLOW", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-cvd-val', children="0", style={'fontSize': '18px', 'fontWeight': '700'}),
            html.Div(id='v2-phase', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '130px'}),
        html.Div([
            html.Div("CONFIDENCE", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-confidence', children="0%", style={'fontSize': '18px', 'fontWeight': '700'}),
            html.Div(id='v2-signal-type', children="--", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '120px'}),
        html.Div([
            html.Div("RISK MANAGER", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-risk-status', children="OK", style={'fontSize': '16px', 'fontWeight': '700', 'color': '#089981'}),
            html.Div(id='v2-daily-pnl', children="P/L: $0.00", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '140px'}),
        html.Div([
            html.Div("TRADES TODAY", style={'fontSize': '10px', 'color': '#787b86', 'textTransform': 'uppercase'}),
            html.Div(id='v2-trades-count', children="0", style={'fontSize': '18px', 'fontWeight': '700', 'color': '#d1d4dc'}),
            html.Div(id='v2-consec-losses', children="Losses: 0", style={'fontSize': '11px', 'color': '#555'}),
        ], style={'background': '#1c2030', 'padding': '15px 20px', 'borderRadius': '8px', 'border': '1px solid #2a2e39', 'minWidth': '120px'}),
    ], style={'display': 'flex', 'gap': '12px', 'padding': '15px 24px', 'overflowX': 'auto', 'backgroundColor': '#111'}),

    html.Iframe(
        src="https://s.tradingview.com/widgetembed/?symbol=OANDA%3AXAUUSD&interval=1&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en&hide_side_toolbar=0&allow_symbol_change=1",
        width="100%", height="420", style={'border': 'none', 'borderRadius': '8px', 'margin': '0 24px', 'maxWidth': 'calc(100% - 48px)'}
    ),

    html.Hr(style={'borderColor': '#2a2e39', 'margin': '10px 24px'}),
    html.Div([
        html.Label("V2 Order Flow Analysis (real data)", style={'color': '#00e5ff', 'fontWeight': '700', 'fontSize': '16px', 'marginRight': '20px'}),
        html.Label("Time Frame:", style={'color': '#aaa', 'marginRight': '8px'}),
        dcc.Dropdown(id='v2-tf-select',
            options=[{'label': 'M1', 'value': 'M1'}, {'label': 'M5', 'value': 'M5'}, {'label': 'M15', 'value': 'M15'}],
            value='M1', clearable=False, searchable=False,
            style={'width': '100px', 'display': 'inline-block', 'color': '#111', 'verticalAlign': 'middle'}),
    ], style={'padding': '10px 24px', 'display': 'flex', 'alignItems': 'center'}),

    dcc.Interval(id='v2-live-update', interval=1000, n_intervals=0),
    html.Div(id='v2-signal-banner', style={'margin': '0 24px 10px 24px'}),
    dcc.Graph(id='v2-chart', style={'height': '55vh', 'margin': '0 24px'}),

    html.Hr(style={'borderColor': '#2a2e39', 'margin': '20px 24px'}),
    html.H4("V2 Trade History (Paper)", style={'textAlign': 'center', 'color': '#FFD700', 'marginBottom': '15px', 'fontWeight': 'bold'}),
    html.Div(id='v2-trade-table', style={'overflowX': 'auto', 'margin': '0 24px 30px'}),
], style={'backgroundColor': '#0b0e14', 'padding': '0', 'color': '#d1d4dc', 'minHeight': '100vh', 'fontFamily': '-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif'})


@v2_app.callback(Output('feed-status-banner', 'children'), [Input('v2-live-update', 'n_intervals')])
def update_feed_status(n):
    connected = feed_status['connected']
    color = '#089981' if connected else '#f23645'
    text = "LIVE FEED CONNECTED (Port 9000)" if connected else f"FEED DISCONNECTED - reconnecting... ({feed_status['reconnects']} attempts)"
    badge = html.Span(text, style={'color': color, 'fontSize': '11px', 'fontWeight': 'bold', 'display': 'inline-block',
                                  'padding': '3px 10px', 'border': f'1px solid {color}', 'borderRadius': '4px'})
    if real_spot_state['price'] is not None:
        spot_badge = html.Span(
            f"  Real Spot ({real_spot_state['source']}): ${real_spot_state['price']:.2f}",
            style={'color': '#FFD700', 'fontSize': '11px', 'marginLeft': '10px'}
        )
        return html.Div([badge, spot_badge])
    return badge


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
    session_quality = f"Quality: {session['quality']}" + (" KILL ZONE" if session['kill_zone'] else "")

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

    risk_str = "UNLOCKED" if risk['can_trade'] else f"BLOCKED: {risk['reason']}"
    risk_color = '#089981' if risk['can_trade'] else '#f23645'
    risk_style = {'fontSize': '16px', 'fontWeight': '700', 'color': risk_color}
    daily_pnl_str = f"Daily P/L: ${risk['daily_pnl']:+.2f}"

    trades_str = str(risk['trades_today'])
    losses_str = f"Losses: {risk['consecutive_losses']}"

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


@v2_app.callback(Output('v2-signal-banner', 'children'), [Input('v2-live-update', 'n_intervals')])
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
            f"LIVE POSITION: {d} @ ${trade_state['entry_price']:.2f} | SL: ${trade_state['stop_loss']:.2f} | TP: ${trade_state['take_profit']:.2f} | P/L: ${running_pnl:+.2f}",
            style={'backgroundColor': 'rgba(8, 153, 129, 0.15)', 'border': '1px solid #089981', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px'}
        )
    elif conf >= 55:
        dirn = "LONG" if phase in ["Distribution", "Bullish Divergence"] else "SHORT"
        return html.Div(
            f"ACTIVE AI SIGNAL: {dirn} @ ${last_known_price:.2f} | Confidence: {conf}% | {session['emoji']} {session['name']}",
            style={'backgroundColor': 'rgba(255, 215, 0, 0.12)', 'border': '1px solid #FFD700', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px'}
        )
    return html.Div()


@v2_app.callback(Output('v2-trade-table', 'children'), [Input('v2-live-update', 'n_intervals')])
def update_trade_table(n):
    header_style = {'backgroundColor': '#131722', 'color': '#FFD700', 'padding': '11px 14px', 'textAlign': 'center', 'fontWeight': '700', 'fontSize': '11px', 'textTransform': 'uppercase', 'borderBottom': '2px solid #FFD700'}
    cell_style = {'backgroundColor': '#0b0e14', 'color': '#d1d4dc', 'padding': '9px 12px', 'textAlign': 'center', 'fontSize': '12px', 'borderBottom': '1px solid #1a1e29'}
    headers = ['#', 'Open Time', 'Exit Time', 'Type', 'Open Price', 'Exit Price', 'SL', 'TP', 'P/L ($)', 'Status', 'Session']
    thead = html.Thead(html.Tr([html.Th(h, style=header_style) for h in headers]))

    rows = []
    with data_lock:
        display_trades = list(reversed(trade_history[-40:]))
    for t in display_trades:
        pnl_val = t['pnl']
        pnl_color = '#089981' if pnl_val > 0 else ('#f23645' if pnl_val < 0 else '#aaa')
        pnl_text = f"+${pnl_val:.2f}" if pnl_val > 0 else (f"-${abs(pnl_val):.2f}" if pnl_val < 0 else "$0.00")
        status = t.get('status', 'OPEN')
        status_color = '#089981' if 'TP' in status else ('#f23645' if 'SL' in status else '#FFD700')
        dir_color = '#089981' if 'BUY' in t['dir'] else '#f23645'
        exit_tm = t.get('exit_time', '--')
        exit_pr_str = f"${last_known_price:.2f}" if 'OPEN' in status else f"${t.get('exit_price', t['entry']):.2f}"

        rows.append(html.Tr([
            html.Td(t['id'], style={**cell_style, 'color': '#787b86'}),
            html.Td(t.get('time', '--'), style=cell_style),
            html.Td(exit_tm, style=cell_style),
            html.Td(t['dir'], style={**cell_style, 'color': dir_color, 'fontWeight': '700'}),
            html.Td(f"${t['entry']:.2f}", style=cell_style),
            html.Td(exit_pr_str, style=cell_style),
            html.Td(f"${t['sl']:.2f}", style={**cell_style, 'color': '#f23645'}),
            html.Td(f"${t['tp']:.2f}", style={**cell_style, 'color': '#089981'}),
            html.Td(pnl_text, style={**cell_style, 'color': pnl_color, 'fontWeight': '700'}),
            html.Td(status, style={**cell_style, 'color': status_color, 'fontWeight': '700'}),
            html.Td(t.get('session', '-'), style={**cell_style, 'color': '#00e5ff', 'fontSize': '11px'}),
        ]))

    if not rows:
        rows = [html.Tr([html.Td('No trades recorded yet - waiting for market signals...', colSpan=11, style={**cell_style, 'color': '#666', 'fontStyle': 'italic', 'padding': '24px'})])]

    table = html.Table([thead, html.Tbody(rows)], style={'width': '100%', 'borderCollapse': 'collapse', 'borderRadius': '8px', 'overflow': 'hidden', 'border': '1px solid #1e222d'})
    return table


@v2_app.callback(Output('v2-chart', 'figure'), [Input('v2-live-update', 'n_intervals'), Input('v2-tf-select', 'value')])
def update_chart(n, tf='M1'):
    if tf is None: tf = 'M1'
    with data_lock:
        if not historical_bars:
            initialize_historical_bars()
        bars = [dict(b) for b in historical_bars]
        if current_bar['close'] is not None:
            curr = {
                'time': current_bar['time'] if current_bar['time'] else datetime.now().replace(second=0, microsecond=0),
                'open': current_bar['open'], 'high': current_bar['high'], 'low': current_bar['low'], 'close': current_bar['close'],
                'volume': current_bar['volume'], 'delta': current_bar['buy_vol'] - current_bar['sell_vol'], 'cvd': cum_delta,
                'vwap': current_bar.get('vwap', last_known_price), 'poc': current_bar.get('poc', last_known_price),
                'imbalance': current_bar['volume'] > 1200, 'fvg': None,
                'phase': bars[-1]['phase'] if bars else "Neutral", 'confidence': bars[-1]['confidence'] if bars else 50,
                'absorption': None, 'cvd_divergence': None, 'signal_type': 'NONE', 'session': ''
            }
        else:
            curr = None

    all_bars = bars + ([curr] if curr else [])
    if not all_bars:
        return go.Figure(layout=dict(template='plotly_dark', height=850, plot_bgcolor='#0b0e14', paper_bgcolor='#0b0e14',
                                      annotations=[dict(text="Waiting for live data from Port 9000...", showarrow=False, font=dict(size=20, color="#888"))]))

    if tf and tf != 'M1' and all_bars:
        mins = int(tf[1:])
        resampled = []
        for b in all_bars:
            t = b['time'].replace(minute=(b['time'].minute // mins) * mins, second=0)
            if resampled and resampled[-1]['time'] == t:
                l = resampled[-1]
                l['high'] = max(l['high'], b['high']); l['low'] = min(l['low'], b['low'])
                l['close'] = b['close']; l['volume'] = l['volume'] + b['volume']
            else:
                nb = dict(b); nb['time'] = t; resampled.append(nb)
        all_bars = resampled

    # Show only the most recent ~90 candles - without this, all 200 stored
    # bars were crammed into the same chart width, squeezing each candle so
    # narrow its body became invisible (looked like thin OHLC lines instead
    # of proper wide TradingView-style candles).
    if len(all_bars) > 90:
        all_bars = all_bars[-90:]

    df = pd.DataFrame(all_bars)
    df['delta'] = pd.to_numeric(df.get('delta', 0), errors='coerce').fillna(0.0)
    df['cvd'] = pd.to_numeric(df.get('cvd', 0), errors='coerce').fillna(cum_delta)
    df['confidence'] = pd.to_numeric(df.get('confidence', 50), errors='coerce').fillna(50)

    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cum_pv_series = (typical_price * df['volume']).cumsum()
    cum_vol_series = df['volume'].cumsum()
    df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])
    df['time_str'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')

    last = df.iloc[-1]
    last_price = float(last['close']); cur_delta = float(last['delta']); cur_cvd = float(cum_delta)
    cur_vwap = float(last['vwap']); cur_phase = str(last.get('phase', 'Neutral')); cur_conf = int(last['confidence'])
    session = get_session_info()
    live_ts = datetime.now().strftime('%H:%M:%S')

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=[0.42, 0.18, 0.20, 0.20],
        subplot_titles=(
            f"LIVE [{live_ts}] ${last_price:.2f} | VWAP: ${cur_vwap:.2f} | {cur_phase} | {session['emoji']} {session['name']}",
            f"Volume Delta ({cur_delta:+.0f}) & POC", f"CVD ({cur_cvd:+.0f})", f"Confluence Score ({cur_conf}%)"))

    fig.add_trace(go.Candlestick(x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                                  name="XAUUSD", increasing_line_color='#00E676', decreasing_line_color='#FF3366',
                                  increasing_fillcolor='#00E676', decreasing_fillcolor='#FF3366', showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'], mode='lines', name='VWAP', line=dict(color='#ffd700', width=2)), row=1, col=1)

    imb_mask = df.get('imbalance', pd.Series([False]*len(df))) == True
    if imb_mask.any():
        fig.add_trace(go.Scatter(x=df[imb_mask]['time_str'], y=df[imb_mask]['high'] * 1.0002, mode='markers',
                                  marker=dict(symbol='star', size=12, color='#00e5ff'), name='Imbalance'), row=1, col=1)

    if SHOW_ENTRY_SL_TP_LINES and trade_state.get('in_position'):
        fig.add_hline(y=trade_state['entry_price'], line_color='#2962ff', line_width=2, line_dash='dash', row=1, col=1, annotation_text=f"Entry ${trade_state['entry_price']:.2f}")
        fig.add_hline(y=trade_state['stop_loss'], line_color='#f23645', line_width=2, line_dash='dot', row=1, col=1, annotation_text=f"SL ${trade_state['stop_loss']:.2f}")
        fig.add_hline(y=trade_state['take_profit'], line_color='#089981', line_width=2, line_dash='dot', row=1, col=1, annotation_text=f"TP ${trade_state['take_profit']:.2f}")

    colors_delta = ['#089981' if d >= 0 else '#f23645' for d in df['delta']]
    fig.add_trace(go.Bar(x=df['time_str'], y=df['delta'], name='Delta', marker_color=colors_delta), row=2, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['cvd'], mode='lines', name='CVD', line=dict(color='#00e5ff', width=2), fill='tozeroy', fillcolor='rgba(0,229,255,0.15)'), row=3, col=1)
    # BUG FIX: `marker_color=df['confidence'], marker_colorscale='RdYlGn'` passed the raw
    # confidence NUMBERS (e.g. 40, 60, 70) straight through as if they were color strings -
    # Plotly logged "Invalid color specifier: 40/60/70" (confirmed via browser console) and
    # defaulted the WHOLE figure to black-on-black, making every candle invisible. Fixed by
    # computing real hex colors ourselves (red->yellow->green by confidence), the same safe
    # pattern colors_delta already uses two lines above, instead of relying on Plotly to
    # infer a colorscale from a raw pandas Series.
    def _confidence_color(c):
        c = max(0, min(100, float(c)))
        if c < 50:
            t = c / 50.0
            r, g, b = 242, int(60 + t * (163 - 60)), int(69 + t * (0 - 69))
        else:
            t = (c - 50) / 50.0
            r, g, b = int(255 + t * (8 - 255)), int(160 + t * (153 - 160)), int(0 + t * (129 - 0))
        return f'rgb({r},{g},{b})'
    colors_conf = [_confidence_color(c) for c in df['confidence']]
    fig.add_trace(go.Bar(x=df['time_str'], y=df['confidence'], name='Confidence %', marker_color=colors_conf,
                          text=[f"{int(c)}%" for c in df['confidence']], textposition='outside'), row=4, col=1)
    fig.add_hline(y=65, line_dash="dash", line_color="#ffd700", row=4, col=1, annotation_text="Entry Threshold")
    fig.add_hline(y=55, line_dash="dot", line_color="#ff9800", row=4, col=1, annotation_text="Kill Zone Threshold")

    fig.update_layout(template='plotly_dark', height=850, showlegend=True, hovermode='x unified', plot_bgcolor='#0b0e14', paper_bgcolor='#0b0e14',
                       font=dict(color='#d1d4dc'), margin=dict(t=70, b=30, l=50, r=50), legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1))
    fig.update_xaxes(type='category', rangeslider_visible=False, gridcolor='#1a1a2e', nticks=15)
    fig.update_yaxes(gridcolor='#1a1a2e')
    return fig


# ============================================================
# STARTUP
# ============================================================
if __name__ == '__main__':
    logger.info("Starting XAUUSD V2 Pilot - Institutional Order Flow Terminal...")

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(lambda: logger.info("V2 Pilot: atexit cleanup complete"))

    init_db()
    load_trades_from_db()
    initialize_historical_bars()
    mt5_init()

    feed_thread = threading.Thread(target=live_feed_subscriber, daemon=True)
    feed_thread.start()

    logger.info(f"V2 Pilot Dashboard (localhost-only): http://{DASH_HOST}:{DASH_PORT}")
    v2_app.run(debug=False, use_reloader=False, host=DASH_HOST, port=DASH_PORT)
