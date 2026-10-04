# ------------------------------------------------------------
# XAUUSD ORDER FLOW DASHBOARD - V2 HEDGE-FUND PILOT UPGRADE
# NEW file, NEW port (9050). PORT_8050_V1_LEGACY_ORDERFLOW.py and its live
# port 8050 are completely untouched and keep running independently.
#
# Upgrades over V1 (PORT_8050_V1_LEGACY_ORDERFLOW.py):
#   1. Real trade volume + real buy/sell side, sourced from the hardened
#      PORT_9000_CORE_WEBSOCKET_ENGINE_V2.py feed (ws://127.0.0.1:9000/ws).
#      V1 used random.randint(60,180) / random.randint(12,35) for every
#      tick's "volume" - none of that is real. V2 has zero random-volume
#      ticks: every bar is built from genuine Binance aggTrade prints.
#   2. Historical bootstrap uses REAL Binance 1m klines (open/high/low/
#      close/taker-buy-volume) instead of a random.uniform() walk.
#   3. Finalized 1-minute bars are additionally persisted to QuestDB
#      (durable, queryable, backtest-ready) alongside the local SQLite
#      trade journal.
#   4. No hardcoded API key. GoldAPI fallback is opt-in via env var only;
#      by default this dashboard depends solely on the free, keyless
#      Binance feed (relayed through Port 9000).
#   5. Binds to 127.0.0.1 only (V1 bound 0.0.0.0, reachable from the LAN).
#   6. Resilient reconnect-with-backoff to the Port 9000 engine, with a
#      visible feed-status indicator in the UI instead of silent failure.
#
# Trade auto-execution logic (VWAP/CVD/phase/confidence scoring) is kept
# as-is from V1 - it was already genuine rule-based logic, not random.
# Still PAPER trading only (no real broker order placement).
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
from datetime import datetime, timedelta
from websockets.sync.client import connect as ws_connect
from questdb import Sender, TimestampNanos

# Fix Windows console UTF-8 encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ---------- CONFIG (env vars, zero hardcoded secrets) ----------
CONFIG = {
    "GOLDAPI_KEY": os.getenv("GOLDFLOW_GOLDAPI_KEY", ""),  # optional, unset by default
    "GOLDAPI_URL": "https://www.goldapi.io/api/XAU/USD",
    "DATA_MODE": "Port 9000 Core Engine (real Binance aggTrade relay)",
    "TRADE_MODE": "PAPER",
    "FIXED_LOT": 0.01,
    "RISK_REWARD_RATIO": 2.0,
}
BINANCE_SYMBOL = os.getenv("GOLDFLOW_BINANCE_SYMBOL", "PAXGUSDT")
ENGINE_WS_URL = os.getenv("GOLDFLOW_ENGINE_WS_URL", "ws://127.0.0.1:9000/ws")
QUESTDB_ILP_CONF = os.getenv("GOLDFLOW_QUESTDB_ILP_CONF", "tcp::addr=127.0.0.1:9009;")
ENGINE_HOST = os.getenv("GOLDFLOW_DASH_HOST", "127.0.0.1")
ENGINE_PORT = int(os.getenv("GOLDFLOW_DASH_PORT", "9050"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("OrderFlowV2")

# ---------- GLOBAL DATA ----------
data_lock = threading.Lock()
historical_bars = []
current_bar = {
    'time': None, 'open': None, 'high': -float('inf'), 'low': float('inf'), 'close': None,
    'volume': 0.0, 'buy_vol': 0.0, 'sell_vol': 0.0, 'delta': 0.0, 'cvd': 0.0, 'vwap': 4318.0,
    'poc': 4318.0, 'phase': 'Neutral', 'confidence': 60, 'imbalance': False, 'fvg': None,
    'levels': {}
}
cum_vol = 0.0
cum_pv = 0.0
cum_delta = 0.0
is_running = True
last_known_price = 4318.0
last_price_update = datetime.now()
market_meta = {'open': 4329.0, 'high': 4335.0, 'low': 4282.0, 'bid': 4317.5, 'ask': 4318.3, 'ch': -10.8}

feed_status = {'connected': False, 'reconnects': 0, 'last_message': None}
real_spot_state = {'price': None, 'bid': None, 'ask': None, 'source': None}

trade_state = {'in_position': False, 'entry_price': 0, 'stop_loss': 0, 'take_profit': 0, 'direction': None, 'pnl': 0.0}
trade_history = []

# ---------- SQLite Persistence (new DB file - does not touch trades.db) ----------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_v2_9050.db')

# ---------- QuestDB durable bar storage (new tables, shared instance from Port 9000 pilot) ----------
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
            "bars_v2_9050",
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
        pnl REAL
    )''')
    conn.commit()
    conn.close()
    logger.info(f"Database initialized: {DB_PATH}")


def save_trade_to_db(trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''INSERT OR REPLACE INTO trades (id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (trade['id'], trade['time'], trade.get('exit_time', '--'), trade['dir'],
                      trade['entry'], trade.get('exit_price', 0.0), trade['sl'], trade['tp'], trade['status'], trade['pnl']))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB save error: {e}")


def update_trade_in_db(trade_id, status, pnl, exit_time=None, exit_price=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        if exit_time is not None and exit_price is not None:
            conn.execute('UPDATE trades SET status=?, pnl=?, exit_time=?, exit_price=? WHERE id=?', (status, pnl, exit_time, exit_price, trade_id))
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
        cursor = conn.execute('SELECT id, time, exit_time, dir, entry, exit_price, sl, tp, status, pnl FROM trades ORDER BY id')
        trade_history = [{
            'id': r[0], 'time': r[1], 'exit_time': r[2] if r[2] else '--', 'dir': r[3], 'entry': r[4],
            'exit_price': r[5] if r[5] else 0.0, 'sl': r[6], 'tp': r[7], 'status': r[8], 'pnl': r[9]
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


# ---------- 1. ORDER FLOW ENGINE (unchanged real math, now fed real ticks) ----------
def update_mark_price(price):
    """PAXGUSDT can go minutes without a real trade print while the order
    book keeps moving. Without this, the chart/price freeze during those
    gaps. This extends the current bar's high/low/close from the real
    bid/ask mid-price WITHOUT touching volume/delta/CVD - those stay
    strictly tied to real trade ticks, only the displayed price keeps moving."""
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
            if current_bar['time'] is None or current_bar['time'] < current_minute:
                if current_bar['close'] is not None and current_bar['time'] is not None:
                    final_bar = finalize_bar(current_bar, vwap)
                    historical_bars.append(final_bar)
                    threading.Thread(target=ingest_bar_to_questdb, args=(final_bar,), daemon=True).start()
                    if len(historical_bars) > 150:
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
    avg_vol = np.mean([b['volume'] for b in historical_bars[-10:]]) if len(historical_bars) >= 10 else bar['volume']
    imbalance = bar['volume'] > (avg_vol * 2.2) if avg_vol > 0 else False

    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2['high'] < bar['low']: fvg = "Bullish"
        elif prev2['low'] > bar['high']: fvg = "Bearish"

    phase = "Neutral"
    if len(historical_bars) >= 5:
        cvd_trend = np.mean([b.get('cvd', 0) for b in historical_bars[-5:]])
        if cvd_trend > 0 and bar['close'] >= vwap: phase = "Distribution"
        elif cvd_trend < 0 and bar['close'] <= vwap: phase = "Accumulation"
        else: phase = "Manipulation"

    buy_score = 50
    if cum_delta > 0: buy_score += 15
    if bar['close'] > vwap: buy_score += 10
    if imbalance: buy_score += 10
    if phase == "Distribution": buy_score += 15
    if fvg == "Bullish": buy_score += 10
    buy_conf = max(5, min(95, buy_score))

    sell_score = 50
    if cum_delta < 0: sell_score += 15
    if bar['close'] < vwap: sell_score += 10
    if imbalance: sell_score += 10
    if phase == "Accumulation": sell_score += 15
    if fvg == "Bearish": sell_score += 10
    sell_conf = max(5, min(95, sell_score))

    if phase == "Distribution":
        confidence = buy_conf
    elif phase == "Accumulation":
        confidence = sell_conf
    else:
        confidence = max(buy_conf, sell_conf)

    return {
        'time': bar['time'], 'open': bar['open'], 'high': bar['high'], 'low': bar['low'], 'close': bar['close'],
        'volume': bar['volume'], 'delta': delta, 'cvd': cum_delta,
        'vwap': round(vwap, 2), 'poc': round(poc, 2), 'imbalance': imbalance,
        'fvg': fvg, 'phase': phase, 'confidence': confidence
    }


# ---------- 2. TRADING EXECUTION (paper only, unchanged rule-based logic) ----------
def calculate_sl_tp(price, direction, vwap, atr):
    if atr == 0: atr = 2.0
    if direction == "LONG":
        sl = min(price - atr * 1.5, vwap - atr * 0.5)
        tp = price + (price - sl) * CONFIG["RISK_REWARD_RATIO"]
    else:
        sl = max(price + atr * 1.5, vwap + atr * 0.5)
        tp = price - (sl - price) * CONFIG["RISK_REWARD_RATIO"]
    return round(sl, 2), round(tp, 2)


def execute_order(symbol, action, lot, sl, tp):
    global trade_history
    logger.info(f"[PAPER] {action} {lot} {symbol} @ Market (${last_known_price:.2f}) | SL: ${sl:.2f} | TP: ${tp:.2f}")
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
        'exit_time': 'RUNNING',
        'dir': 'BUY' if direction == 'LONG' else 'SELL',
        'entry': round(last_known_price, 2),
        'exit_price': round(last_known_price, 2),
        'sl': sl,
        'tp': tp,
        'status': 'OPEN',
        'pnl': 0.0
    })
    if len(trade_history) > 50:
        trade_history.pop(0)
    save_trade_to_db(trade_history[-1])
    return True


# Strategy master switch: entry strategy is OFF unless GOLDFLOW_STRATEGIES_ENABLED=1.
STRATEGIES_ENABLED = os.getenv("GOLDFLOW_STRATEGIES_ENABLED", "0") == "1"
if not STRATEGIES_ENABLED:
    logger.info("STRATEGIES DISABLED (master switch off) - order-flow analytics only, no signals/trades.")


def check_and_execute_trade(bar):
    global trade_state, trade_history
    if trade_state['in_position']:
        current_price = bar['close']
        now_time = datetime.now().strftime('%H:%M:%S')
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

    if not STRATEGIES_ENABLED: return
    if bar['confidence'] < 65: return
    if len(historical_bars) > 5:
        prices = [b['close'] for b in historical_bars[-5:]]
        atr = np.mean([abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]) * 1.5
    else: atr = 2.0

    if bar['phase'] == "Distribution" and bar['delta'] > 0 and bar['close'] > bar['vwap']:
        sl, tp = calculate_sl_tp(bar['close'], "LONG", bar['vwap'], atr)
        logger.info(f"[SIGNAL] LONG @ ${bar['close']:.2f} | Conf: {bar['confidence']}%")
        execute_order("XAUUSD", "BUY", CONFIG["FIXED_LOT"], sl, tp)
    elif bar['phase'] == "Accumulation" and bar['delta'] < 0 and bar['close'] < bar['vwap']:
        sl, tp = calculate_sl_tp(bar['close'], "SHORT", bar['vwap'], atr)
        logger.info(f"[SIGNAL] SHORT @ ${bar['close']:.2f} | Conf: {bar['confidence']}%")
        execute_order("XAUUSD", "SELL", CONFIG["FIXED_LOT"], sl, tp)


def _close_trade(status, now_time, current_price):
    trade_state['in_position'] = False
    if trade_history:
        trade_history[-1]['status'] = status
        trade_history[-1]['exit_time'] = now_time
        trade_history[-1]['exit_price'] = round(current_price, 2)
        trade_history[-1]['pnl'] = trade_state['pnl']
        update_trade_in_db(trade_history[-1]['id'], status, trade_state['pnl'], now_time, round(current_price, 2))
    logger.info(f"[{status}] P/L: ${trade_state['pnl']}")


# ---------- 3. REAL HISTORICAL BOOTSTRAP (Binance klines - no synthetic data) ----------
def _fetch_live_price():
    """Fetch live gold price: Binance PAXG (free, keyless). Optional GoldAPI fallback if env key set."""
    try:
        r = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={BINANCE_SYMBOL}', timeout=3)
        if r.status_code == 200:
            d = r.json()
            bid = float(d['bidPrice'])
            ask = float(d['askPrice'])
            mid = round((bid + ask) / 2, 2)
            if mid > 0:
                return mid, bid, ask, "Binance PAXG"
    except Exception as e:
        logger.warning(f"Binance price fetch failed: {e}")

    if CONFIG['GOLDAPI_KEY']:
        try:
            headers = {'x-access-token': CONFIG['GOLDAPI_KEY'], 'Content-Type': 'application/json'}
            r = requests.get(CONFIG['GOLDAPI_URL'], headers=headers, timeout=4)
            if r.status_code == 200:
                d = r.json()
                p = float(d.get('price', 0))
                if p > 0:
                    return p, float(d.get('bid', p - 0.3)), float(d.get('ask', p + 0.3)), "GoldAPI"
        except Exception as e:
            logger.warning(f"GoldAPI fallback failed: {e}")
    return None, None, None, None


def initialize_historical_bars():
    """Bootstrap chart with REAL Binance 1m klines (real OHLCV + real taker-buy-volume delta)."""
    global historical_bars, cum_vol, cum_pv, cum_delta, last_known_price, market_meta

    logger.info("Bootstrapping historical bars from real Binance klines...")
    price, bid, ask, source = _fetch_live_price()
    if price and price > 0:
        last_known_price = price
        market_meta['bid'] = bid
        market_meta['ask'] = ask
        logger.info(f"Live Anchor [{source}]: ${price:.2f}")

    try:
        r = requests.get(
            'https://api.binance.com/api/v3/klines',
            params={'symbol': BINANCE_SYMBOL, 'interval': '1m', 'limit': 60},
            timeout=10,
        )
        klines = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"Kline bootstrap fetch failed: {e}")
        klines = []

    with data_lock:
        historical_bars.clear()
        cum_vol = 0.0
        cum_pv = 0.0
        cum_delta = 0.0
        for k in klines:
            bar_time = datetime.fromtimestamp(k[0] / 1000).replace(second=0, microsecond=0)
            open_p, high_p, low_p, close_p = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5])
            taker_buy_vol = float(k[9])  # REAL data from Binance - not synthetic
            delta = taker_buy_vol - (vol - taker_buy_vol)

            cum_vol += vol
            cum_pv += ((open_p + high_p + low_p + close_p) / 4) * vol
            cum_delta += delta
            vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else close_p

            historical_bars.append({
                'time': bar_time, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                'volume': vol, 'delta': delta, 'cvd': cum_delta,
                'vwap': vwap, 'poc': round((high_p + low_p) / 2, 2),
                'imbalance': False, 'fvg': None,
                'phase': "Distribution" if delta > 0 else "Accumulation",
                'confidence': 50,  # neutral - no fabricated bootstrap confidence
            })
        if historical_bars:
            market_meta['open'] = historical_bars[0]['open']
            market_meta['high'] = max(b['high'] for b in historical_bars)
            market_meta['low'] = min(b['low'] for b in historical_bars)
            if not last_known_price:
                last_known_price = historical_bars[-1]['close']

    logger.info(f"Bootstrapped {len(historical_bars)} REAL bars from Binance | Last: ${last_known_price:.2f}")


# ---------- 4. LIVE FEED - subscribes to Port 9000 Core Engine V2 (real ticks, real depth) ----------
def handle_engine_message(raw):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    feed_status['last_message'] = datetime.now()

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
    """Subscribes to the hardened Port 9000 engine feed. Zero random data - every
    tick/volume/side here is real, relayed from genuine Binance aggTrade prints."""
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


# ---------- 5. DASH UI LAYOUT ----------
app = dash.Dash(__name__, external_stylesheets=['https://codepen.io/chriddyp/pen/bWLwgP.css'])
app.title = "XAUUSD Pro Order Flow V2 (Pilot)"

app.layout = html.Div([
    html.H1("XAUUSD Order Flow V2 - Hedge-Fund Pilot Upgrade", style={'textAlign': 'center', 'color': '#FFD700', 'marginBottom': '5px'}),
    html.H6(f"Data: {CONFIG['DATA_MODE']} | Mode: {CONFIG['TRADE_MODE']} | Lot: {CONFIG['FIXED_LOT']} | RR: 1:{CONFIG['RISK_REWARD_RATIO']} | Port 9050",
            style={'textAlign': 'center', 'color': '#aaa', 'marginBottom': '15px'}),

    html.Iframe(
        src="https://s.tradingview.com/widgetembed/?symbol=OANDA%3AXAUUSD&interval=1&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en&hide_side_toolbar=0&allow_symbol_change=1&studies=[]",
        width="100%", height="480", style={'border': 'none', 'borderRadius': '8px', 'marginBottom': '15px'}
    ),

    html.Hr(style={'borderColor': '#333'}),
    html.H4("Order Flow Analysis (real Binance data via Port 9000)", style={'textAlign': 'center', 'color': '#00d4ff', 'marginBottom': '10px'}),

    html.Div([
        html.Label("Time Frame:", style={'color': '#aaa', 'display': 'inline-block', 'marginRight': '10px', 'fontWeight': 'bold'}),
        dcc.Dropdown(
            id='tf-select',
            options=[{'label': 'M1', 'value': 'M1'}, {'label': 'M5', 'value': 'M5'}, {'label': 'M15', 'value': 'M15'}],
            value='M1', clearable=False, searchable=False,
            style={'width': '180px', 'display': 'inline-block', 'color': '#111', 'verticalAlign': 'middle'}),
    ], style={'textAlign': 'center', 'marginBottom': '10px'}),

    dcc.Interval(id='live-update', interval=1000, n_intervals=0),
    html.Div(id='feed-status-banner', style={'marginBottom': '8px'}),
    html.Div(id='v1-signal-banner', style={'marginBottom': '12px'}),
    dcc.Graph(id='live-orderflow-chart', style={'height': '55vh'}),

    html.Hr(style={'borderColor': '#333', 'marginTop': '30px'}),
    html.H4("Trade History (Paper)", style={'textAlign': 'center', 'color': '#FFD700', 'marginBottom': '15px', 'marginTop': '15px'}),
    html.Div(id='trade-history-table', style={'overflowX': 'auto', 'marginBottom': '30px'}),

], style={'backgroundColor': '#111', 'padding': '20px', 'color': '#eee', 'minHeight': '100vh'})


@app.callback(Output('feed-status-banner', 'children'), [Input('live-update', 'n_intervals')])
def update_feed_status_banner(n):
    connected = feed_status['connected']
    color = '#089981' if connected else '#f23645'
    text = "LIVE FEED CONNECTED (Port 9000)" if connected else f"FEED DISCONNECTED - reconnecting... ({feed_status['reconnects']} attempts)"
    children = [html.Div(text, style={
        'textAlign': 'center', 'color': color, 'fontSize': '12px', 'fontWeight': 'bold',
        'padding': '4px', 'border': f'1px solid {color}', 'borderRadius': '4px', 'maxWidth': '400px', 'margin': '0 auto',
    })]
    if real_spot_state['price'] is not None:
        children.append(html.Div(
            f"Real Spot XAUUSD ({real_spot_state['source']}): ${real_spot_state['price']:.2f}  |  "
            f"Bid ${real_spot_state['bid']:.2f} / Ask ${real_spot_state['ask']:.2f}",
            style={'textAlign': 'center', 'color': '#FFD700', 'fontSize': '11px', 'marginTop': '4px'}
        ))
    return children


@app.callback(Output('v1-signal-banner', 'children'), [Input('live-update', 'n_intervals')])
def update_v1_signal_banner(n):
    with data_lock:
        last_bar = historical_bars[-1] if historical_bars else {}
    phase = last_bar.get('phase', 'Neutral')
    conf = last_bar.get('confidence', 50)
    if trade_state.get('in_position'):
        d = trade_state['direction']
        running_pnl = (last_known_price - trade_state['entry_price']) if d == "LONG" else (trade_state['entry_price'] - last_known_price)
        return html.Div(
            f"LIVE POSITION: {d} @ ${trade_state['entry_price']:.2f} | SL: ${trade_state['stop_loss']:.2f} | TP: ${trade_state['take_profit']:.2f} | P/L: ${running_pnl:+.2f}",
            style={'backgroundColor': 'rgba(8, 153, 129, 0.2)', 'border': '1px solid #089981', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px'}
        )
    elif conf >= 65:
        dirn = "LONG / BUY" if phase == "Distribution" else "SHORT / SELL"
        return html.Div(
            f"AI SIGNAL: {dirn} @ ${last_known_price:.2f} | Confidence: {conf}% | Phase: {phase}",
            style={'backgroundColor': 'rgba(255, 215, 0, 0.15)', 'border': '1px solid #FFD700', 'color': '#FFD700', 'padding': '10px 18px', 'borderRadius': '6px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '14px'}
        )
    return html.Div()


@app.callback(Output('trade-history-table', 'children'), [Input('live-update', 'n_intervals')])
def update_trade_table(n):
    header_style = {'backgroundColor': '#1a1a2e', 'color': '#FFD700', 'padding': '10px 14px', 'textAlign': 'center', 'fontWeight': '700', 'fontSize': '12px', 'textTransform': 'uppercase', 'borderBottom': '2px solid #FFD700'}
    cell_style = {'backgroundColor': '#0d0d1a', 'color': '#d1d4dc', 'padding': '9px 14px', 'textAlign': 'center', 'fontSize': '13px', 'borderBottom': '1px solid #1a1a2e'}
    headers = ['#', 'Open Time', 'Exit Time', 'Type', 'Open Price', 'Exit Price', 'SL', 'TP', 'P/L ($)', 'Status']
    thead = html.Thead(html.Tr([html.Th(h, style=header_style) for h in headers]))

    rows = []
    with data_lock:
        display_trades = list(reversed(trade_history[-30:]))
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
        ]))

    if not rows:
        rows = [html.Tr([html.Td('No trades yet - waiting for signals...', colSpan=10, style={**cell_style, 'color': '#555', 'fontStyle': 'italic', 'padding': '20px'})])]

    table = html.Table([thead, html.Tbody(rows)], style={'width': '100%', 'borderCollapse': 'collapse', 'borderRadius': '8px', 'overflow': 'hidden', 'border': '1px solid #1a1a2e'})
    return table


@app.callback(Output('live-orderflow-chart', 'figure'), [Input('live-update', 'n_intervals'), Input('tf-select', 'value')])
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
                'phase': bars[-1]['phase'] if bars else "Neutral", 'confidence': bars[-1]['confidence'] if bars else 65
            }
        else:
            curr = None

    all_bars = bars + ([curr] if curr else [])
    if not all_bars:
        initialize_historical_bars()
        all_bars = [dict(b) for b in historical_bars]

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

    df = pd.DataFrame(all_bars)
    df['delta'] = pd.to_numeric(df.get('delta', 0), errors='coerce').fillna(0.0)
    df['cvd'] = pd.to_numeric(df.get('cvd', 0), errors='coerce').fillna(cum_delta)
    df['confidence'] = pd.to_numeric(df.get('confidence', 50), errors='coerce').fillna(50)

    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cum_pv_series = (typical_price * df['volume']).cumsum()
    cum_vol_series = df['volume'].cumsum()
    df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])
    df['poc'] = pd.to_numeric(df.get('poc', df['close']), errors='coerce').fillna(df['close'])
    df['time_str'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')

    last = df.iloc[-1]
    last_price = float(last['close']); cur_delta = float(last['delta']); cur_cvd = float(cum_delta)
    cur_vwap = float(last['vwap']); cur_phase = str(last.get('phase', 'Neutral')); cur_conf = int(last['confidence'])
    live_timestamp = datetime.now().strftime('%H:%M:%S')

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.42, 0.18, 0.20, 0.20],
        subplot_titles=(
            f"LIVE [{live_timestamp}] XAUUSD: ${last_price:.2f} | VWAP: ${cur_vwap:.2f} | Phase: {cur_phase}",
            f"Volume Delta ({cur_delta:+.0f}) & POC",
            f"CVD ({cur_cvd:+.0f})",
            f"Confidence ({cur_conf}%)"
        )
    )
    fig.add_trace(go.Candlestick(x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                                  name="XAUUSD", increasing_line_color='#089981', decreasing_line_color='#f23645', showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'], mode='lines', name='VWAP', line=dict(color='#ffd700', width=2)), row=1, col=1)

    imb_mask = df.get('imbalance', pd.Series([False]*len(df))) == True
    if imb_mask.any():
        fig.add_trace(go.Scatter(x=df[imb_mask]['time_str'], y=df[imb_mask]['high'] * 1.0002, mode='markers',
                                  marker=dict(symbol='star', size=12, color='#00e5ff'), name='Imbalance'), row=1, col=1)

    if trade_state.get('in_position'):
        fig.add_hline(y=trade_state['entry_price'], line_color='#2962ff', line_width=2, line_dash='dash', row=1, col=1, annotation_text=f"Entry ${trade_state['entry_price']:.2f}")
        fig.add_hline(y=trade_state['stop_loss'], line_color='#f23645', line_width=2, line_dash='dot', row=1, col=1, annotation_text=f"SL ${trade_state['stop_loss']:.2f}")
        fig.add_hline(y=trade_state['take_profit'], line_color='#089981', line_width=2, line_dash='dot', row=1, col=1, annotation_text=f"TP ${trade_state['take_profit']:.2f}")

    colors_delta = ['#089981' if d >= 0 else '#f23645' for d in df['delta']]
    fig.add_trace(go.Bar(x=df['time_str'], y=df['delta'], name='Delta', marker_color=colors_delta), row=2, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['poc'], mode='lines', name='POC', line=dict(color='#ff9800', dash='dot')), row=2, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['cvd'], mode='lines', name='CVD', line=dict(color='#00e5ff', width=2), fill='tozeroy', fillcolor='rgba(0,229,255,0.15)'), row=3, col=1)
    fig.add_trace(go.Bar(x=df['time_str'], y=df['confidence'], name='Confidence %', marker_color=df['confidence'], marker_colorscale='RdYlGn',
                          text=[f"{int(c)}%" for c in df['confidence']], textposition='outside'), row=4, col=1)
    fig.add_hline(y=65, line_dash="dash", line_color="#ffd700", row=4, col=1, annotation_text="Entry Threshold (65%)")

    fig.update_layout(template='plotly_dark', height=850, showlegend=True, hovermode='x unified', plot_bgcolor='#111', paper_bgcolor='#111',
                       font=dict(color='#d1d4dc'), margin=dict(t=70, b=30, l=50, r=50), legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1))
    fig.update_xaxes(type='category', rangeslider_visible=False, gridcolor='#222', nticks=15)
    fig.update_yaxes(gridcolor='#222')
    return fig


# ---------- 6. STARTUP ----------
if __name__ == '__main__':
    logger.info("Starting XAUUSD Order Flow V2 (Hedge-Fund Pilot Upgrade)...")

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(lambda: logger.info("V2: atexit cleanup complete"))

    init_db()
    load_trades_from_db()
    initialize_historical_bars()

    feed_thread = threading.Thread(target=live_feed_subscriber, daemon=True)
    feed_thread.start()

    logger.info(f"LIVE Dashboard (localhost-only): http://{ENGINE_HOST}:{ENGINE_PORT}")
    app.run(debug=False, use_reloader=False, host=ENGINE_HOST, port=ENGINE_PORT)
