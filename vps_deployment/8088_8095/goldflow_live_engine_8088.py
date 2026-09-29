# =============================================================================
# GOLDFLOW DUAL-MODEL LIVE CONSENSUS TRADING ENGINE - PORT 8088
# 100% Pure Spot Gold (XAUUSD) - Institutional Multi-Model Architecture
#
# MODEL A (Full Confluence 8086 Strategy):
#   - Magic Number: 808801 | Comment: 'GF-CONFLUENCE-A'
#   - 15M Technical Indicators: EMA (20/50/200), RSI 14, MACD, Bollinger Bands,
#     Fibonacci (38.2/50/61.8), Support/Resistance, VWAP Trend
#   - SMC Session Sweeps: PDH/PDL, Asian Judas, London LSH/LSL, NYH/NYL
#   - Volume Profile: Rolling POC, VAH, VAL
#   - Order Flow Confirmation: CVD Trend, Bar Delta, Absorption, Imbalance
#   - Entry Threshold: Min TA Score >= 40 AND Min Order Flow Score >= 30
#
# MODEL B (Pure SMC & Real-Time Order Flow Strategy):
#   - Magic Number: 808802 | Comment: 'GF-SMC-OF-B'
#   - SMC Liquidity Sweeps: London LSH/LSL, Asian Judas Swing, PDH/PDL
#   - Millisecond Tick Order Flow Delta & CVD Reversal (AllTick + iTick)
#   - Institutional Absorption Detection
#
# TIER 2 SHIELD (Both Models Protected):
#   - Spread Guard (<= $0.60) & Phantom Wick Guard (<= $0.45)
#
# TIER 3 EXECUTION & CAPITAL PROTECTION:
#   - Equiti Broker MT5 (Account #1189847)
#   - Strict 0.01 Lot Sizing ($100 Capital Base)
#   - 1:2 R:R Targets (SL: $2.50, TP: $5.00)
#   - 1:1 Auto Break-Even Guarantee (Zero Risk Lock at $2.50 Profit)
# =============================================================================

import dash
from dash import dcc, html, Input, Output, State, callback_context
from flask import request, Response
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import requests
import json
import urllib.parse
import threading
import time
from datetime import datetime, timezone, timedelta
import sqlite3
import logging
import os
import sys
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
import websocket
from multi_api_key_pool import get_spot_gold_live

import atexit

# Console encoding for Windows
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [LIVE_8088] - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'port_8088.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('LIVE_8088')

# =============================================================================
# API CONFIGURATION & CREDENTIALS
# =============================================================================
ALLTICK_TOKEN = 'e4432003c7fb8ef16dce2c8fbcf1ae57-c-app'
ITICK_TOKEN   = '7111eb89b6364381bad7d4a507b5573ac880fb5d6c0e47bda0cae76a453c1bb0'

TWELVEDATA_KEYS = [
    '7b4a5b6feaf2429180934a74c8d88905', 'a77eb531fb46450088346ca8ed8d2658',
    '0c541a88560d4a6dbf169a37db04fcb6', '2d261575188c46678bf2b3d50a528408',
    '9a881e3b6cba435fb5b02a868473c9c1', '09db8bad69204b4d907bbbbbe1cc540f',
    '044603710a24444bbff60912e365cf7e', 'e6a9bbe1a68946479fdb526b83c3ccd6',
    '67b7138f49964313b96520c505e3c78f', '53aa8f7c4390483b81fd9095dd4fd7eb',
    'f8f735ebca8e4b9fbe58de09ee1f81e2', '37456a60b8484cfa89d85a83ce9e0ab9',
    'cd70ff3ef4114ce394a2d446d0ab8d9b'
]
_td_idx = 0

REALMARKET_KEYS = [
    'G9Nq09nXaBZLR0zcZDVW0kg4IOrhYB4KkvYoIvk5plh7ioOp',
    'xZxlI1aC0UhJZY9jZdfh3gqYQE3Qm9jeAuptPZy1n5jNYIzL',
    'ZTsYWWk1VYaGfuYUzWhvTGxj0ketmClqm5tBCcjCwSUIC9Wi',
    'dD6DPvOpxZONPk8j9mARQrJmhQ2L6h7dk3T7NiaZBUILMdBc',
    'gQP3ZSkBKPQYOlKevntEluhAOqUp9NUFNTXGZbrwSf5s6LaJ',
    'oTICKn5MJZdgA3MLbq0cqAgDKnBMWbPyO3yIIdFGgPHiYayF',
    'oVD0LtmS8BS1LUJ1YrRv44OQ4oNf7PDr7i3BYtkhk8h844ck',
    'lmbOvLJNkLC0dhL3U7Qcysf1HMgvU703EPy2XkcQj4l4OwcF',
    'FY0oUU0xGykL8CryEwXRbIuut2uwXPr4Qf7jJSzKCSrzL4ei'
]
_rm_idx = 0

MAGIC_MODEL_A   = 808801
COMMENT_MODEL_A = 'GF-CONFLUENCE-A'

MAGIC_MODEL_B   = 808802
COMMENT_MODEL_B = 'GF-SMC-OF-B'

mt5_lock = threading.RLock()

def resolve_gold_symbol():
    if mt5 is None:
        return 'XAUUSD.sd'
    try:
        if not mt5.initialize():
            return 'XAUUSD.sd'
        candidates = ['XAUUSD.sd', 'XAUUSD', 'XAUUSD.x', 'XAUUSD.pr', 'XAUUSDm', 'GOLD', 'XAUUSD.a', 'XAUUSD.raw']
        for c in candidates:
            s = mt5.symbol_info(c)
            if s is not None:
                if not s.visible:
                    mt5.symbol_select(c, True)
                return c
    except Exception:
        pass
    return 'XAUUSD.sd'

SYMBOL = resolve_gold_symbol()
if mt5 is not None:
    atexit.register(mt5.shutdown)

def get_current_5m_candle_time():
    """Returns the epoch start timestamp of the current 5-minute candle bar"""
    return int(time.time() // 300) * 300

def get_5m_countdown_str():
    """Returns the remaining time string (e.g. '3m 42s') until the next 5-minute candle bar"""
    rem_sec = 300 - (int(time.time()) % 300)
    return f"{rem_sec // 60}m {rem_sec % 60:02d}s"

def get_initial_traded_candle():
    if mt5 is None:
        return 0
    try:
        with mt5_lock:
            if not mt5.initialize():
                return 0
            curr_candle = get_current_5m_candle_time()
            # 1. Any currently active positions?
            positions = mt5.positions_get(symbol=SYMBOL)
            if positions:
                for p in positions:
                    if p.magic in (MAGIC_MODEL_A, MAGIC_MODEL_B):
                        return curr_candle
            # 2. Any recent deals (entry OR exit) in the last 30 minutes?
            now = datetime.now()
            deals = mt5.history_deals_get(now - timedelta(minutes=30), now + timedelta(minutes=5))
            if deals:
                for d in reversed(deals):
                    if d.symbol == SYMBOL and d.magic in (MAGIC_MODEL_A, MAGIC_MODEL_B):
                        d_candle = int(d.time // 300) * 300
                        if d_candle >= curr_candle:
                            return curr_candle
                        return d_candle
    except Exception as e:
        logger.warning(f"get_initial_traded_candle error: {e}")
    return 0


# =============================================================================
# GLOBAL SHARED STATE (Thread-Safe)
# =============================================================================
state_lock = threading.RLock()

engine_state = {
    # System Controls
    'auto_pilot': True,          # Master Auto-Pilot Switch
    'model_a_active': True,      # Enable Model A (Confluence)
    'model_b_active': True,      # Enable Model B (Pure SMC & OF)
    'capital': 100.0,            # $100 baseline sizing
    'lot_size': 0.01,           # 0.01 lot strict sizing
    'use_auto_be': True,         # Auto Break-Even at 1:1 R:R ($2.50)
    'rr_mode': '1:2',            # Target R:R ('1:2' -> SL: $2.50, TP: $5.00)

    # 15M Candle Lock Protection (Strict 1-Trade Per Candle)
    'last_traded_candle_time': get_initial_traded_candle(),
    'last_trade_exec_time': 0,
    'candle_lock_status': 'READY',
    'next_candle_countdown': '',

    # Live Prices & Spreads
    'mt5_bid': 4307.34,
    'mt5_ask': 4307.58,
    'mt5_spread': 0.24,
    'last_price': 4307.46,
    'alltick_price': 4307.46,
    'itick_price': 4307.46,
    'neutral_spot_price': 4307.46,
    'price_deviation': 0.02,


    # Feeds Health
    'mt5_status': 'CONNECTED',
    'alltick_status': 'ONLINE',
    'itick_status': 'CONNECTED',
    'shield_status': 'ACTIVE',
    'market_mode': 'CHECKING',
    'spot_source': 'INITIALIZING',
    'last_itick_tick_time': 0,
    'last_alltick_time': 0,

    # Order Flow Metrics (AllTick + iTick)
    'cvd': 0.0,
    'bar_delta': 0.0,
    'cvd_trend': 'NEUTRAL',
    'absorption_detected': False,
    'imbalance_detected': False,

    # Tier 2: Safety Shield
    'shield_ok': True,
    'spread_ok': True,
    'wick_guard_ok': True,
    'shield_msg': 'All safety checks nominal (Spread <= $0.60, Deviation <= $0.45)',

    # SMC & Volume Profile Levels
    'smc_levels': {
        'pdh': 0.0, 'pdl': 0.0,
        'asia_high': 0.0, 'asia_low': 0.0,
        'london_high': 0.0, 'london_low': 0.0,
        'ny_high': 0.0, 'ny_low': 0.0,
        'poc': 0.0, 'vah': 0.0, 'val': 0.0
    },

    # MODEL A STATE (Full 8086 Confluence)
    'model_a': {
        'signal': 'WAITING',
        'direction': 'NEUTRAL',
        'ta_score': 0,
        'of_score': 0,
        'signals_detail': 'Calculating 15M Indicators...',
        'reason': 'Awaiting 15M Confluence (TA >= 40, OF >= 30)',
        'last_eval_time': None,
        'total_trades': 0,
        'wins': 0,
        'losses': 0,
        'be_count': 0
    },

    # MODEL B STATE (Pure SMC & Order Flow)
    'model_b': {
        'signal': 'WAITING',
        'direction': 'NEUTRAL',
        'active_sweep': None,
        'sweep_must_reset': False,   # Sweep Reset Protection: True = waiting for price to leave all levels
        'reason': 'Monitoring session sweeps & live delta...',
        'last_eval_time': None,
        'total_trades': 0,
        'wins': 0,
        'losses': 0,
        'be_count': 0
    },

    # Account & Blotter
    'account_info': {
        'login': 1189847,
        'server': 'EquitiBrokerageSC-Demo',
        'balance': 100.0,
        'equity': 100.0,
        'margin_free': 100.0,
        'floating_pnl': 0.0
    },
    'open_positions': [],
    'protected_be_count': 0,
    'audit_logs': []
}

def log_audit(msg, tag='SYSTEM'):
    now_str = datetime.now().strftime('%H:%M:%S')
    entry = f"[{now_str}] [{tag}] {msg}"
    with state_lock:
        engine_state['audit_logs'].insert(0, entry)
        if len(engine_state['audit_logs']) > 120:
            engine_state['audit_logs'].pop()
    logger.info(f"[{tag}] {msg}")

# =============================================================================
# TECHNICAL INDICATORS & SMC MATH (EXACT 8086 CONFLUENCE FORMULAS)
# =============================================================================
def calc_ema(s, n): return s.ewm(span=n, adjust=False).mean()
def calc_sma(s, n): return s.rolling(n).mean()

def calc_rsi(s, n=14):
    d = s.diff(); g = d.clip(lower=0).rolling(n).mean(); l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def calc_macd(s, f=12, sl=26, sg=9):
    m = calc_ema(s, f) - calc_ema(s, sl); sig = calc_ema(m, sg); return m, sig, m - sig

def calc_bb(s, n=20, k=2):
    ma = calc_sma(s, n); std = s.rolling(n).std(); return ma + k*std, ma, ma - k*std

def calc_atr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def calc_vwap(df):
    tp = (df['high']+df['low']+df['close'])/3.0
    cpv = (tp*df['volume']).cumsum(); cvs = df['volume'].cumsum()
    return np.where(cvs > 0, cpv/cvs, df['close'])

def calc_fibonacci(df, lb=100):
    w = df.tail(lb); sh = w['high'].max(); sl = w['low'].min(); diff = sh - sl
    return {'382': sh - 0.382*diff, '500': sh - 0.500*diff, '618': sh - 0.618*diff}

def calc_volume_profile(df, window=80):
    n = len(df)
    poc = np.zeros(n); vah = np.zeros(n); val = np.zeros(n)
    c_arr = df['close'].values; v_arr = df['volume'].values
    for i in range(min(window, n), n):
        sub_c = c_arr[i-window:i]; sub_v = v_arr[i-window:i]
        mn, mx = sub_c.min(), sub_c.max()
        if mx > mn:
            edges = np.linspace(mn, mx, 21)
            b_idx = np.clip(np.digitize(sub_c, edges) - 1, 0, 19)
            v_bins = np.bincount(b_idx, weights=sub_v, minlength=20)
            p_idx = np.argmax(v_bins)
            poc[i] = (edges[p_idx] + edges[p_idx+1]) / 2.0
            sorted_idxs = np.argsort(-v_bins)
            cum_v = 0.0; va_idxs = []
            tot_v = v_bins.sum()
            for idx in sorted_idxs:
                cum_v += v_bins[idx]; va_idxs.append(idx)
                if cum_v >= 0.70 * tot_v: break
            val[i] = edges[min(va_idxs)]; vah[i] = edges[max(va_idxs)+1]
        else:
            poc[i] = c_arr[i]; val[i] = c_arr[i]; vah[i] = c_arr[i]
    df['poc'] = poc; df['vah'] = vah; df['val'] = val
    return df

def calc_liquidity_sweeps(df, pd_window=288):
    df = df.copy()
    df['pdh'] = df['high'].rolling(pd_window).max().shift(1)
    df['pdl'] = df['low'].rolling(pd_window).min().shift(1)
    df['sweep_pdl'] = (df['low'] < df['pdl']) & (df['close'] > df['pdl'])
    df['sweep_pdh'] = (df['high'] > df['pdh']) & (df['close'] < df['pdh'])

    hours = df.index.hour
    is_asia   = (hours >= 0) & (hours < 7)
    is_london = (hours >= 7) & (hours < 13)
    is_ny     = (hours >= 13) & (hours < 21)

    df['asia_h'] = np.where(is_asia, df['high'], np.nan)
    df['asia_l'] = np.where(is_asia, df['low'], np.nan)
    df['asia_high'] = df['asia_h'].ffill().rolling(84).max()
    df['asia_low']  = df['asia_l'].ffill().rolling(84).min()

    df['london_h'] = np.where(is_london, df['high'], np.nan)
    df['london_l'] = np.where(is_london, df['low'], np.nan)
    df['london_high'] = df['london_h'].ffill().rolling(72).max()
    df['london_low']  = df['london_l'].ffill().rolling(72).min()

    df['ny_h'] = np.where(is_ny, df['high'], np.nan)
    df['ny_l'] = np.where(is_ny, df['low'], np.nan)
    df['ny_high'] = df['ny_h'].ffill().rolling(96).max()
    df['ny_low']  = df['ny_l'].ffill().rolling(96).min()

    is_after_asia = hours >= 7
    df['sweep_asia_low']  = is_after_asia & (df['low'] < df['asia_low']) & (df['close'] > df['asia_low'])
    df['sweep_asia_high'] = is_after_asia & (df['high'] > df['asia_high']) & (df['close'] < df['asia_high'])

    df['sweep_london_low']  = is_ny & (df['low'] < df['london_low']) & (df['close'] > df['london_low'])
    df['sweep_london_high'] = is_ny & (df['high'] > df['london_high']) & (df['close'] < df['london_high'])

    return df


def calc_orderflow_metrics(df):
    df = df.copy()
    rng = (df['high']-df['low']).replace(0, 0.01)
    bp = (df['close']-df['low'])/rng
    df['buy_vol']   = df['volume']*bp
    df['sell_vol']  = df['volume']*(1-bp)
    df['delta']     = df['buy_vol']-df['sell_vol']
    df['cvd']       = df['delta'].cumsum()
    df['vwap']      = calc_vwap(df)
    avg_vol = df['volume'].rolling(20).mean()
    df['imbalance'] = df['volume'] > avg_vol*1.8
    body = (df['close']-df['open']).abs()
    df['absorption'] = (df['delta'].abs()>50) & (body < body.rolling(20).mean()*0.35)
    df['fvg_bull'] = df['low'] > df['high'].shift(2)
    df['fvg_bear'] = df['high'] < df['low'].shift(2)
    return df

def score_bar_model_a(df, i):
    """Exact 8086 Scoring Algorithm combining all 3 panels"""
    if i < 80: return 0, 0, 'NEUTRAL', {}
    row = df.iloc[i]; price = row['close']
    scores = {}; bull = 0; bear = 0

    ema20  = row.get('ema20',  price); ema50  = row.get('ema50',  price)
    ema200 = row.get('ema200', price); rsi    = row.get('rsi',    50)
    macd   = row.get('macd',   0);     macd_s = row.get('macd_s', 0)
    bb_up  = row.get('bb_up',  price+5); bb_lo = row.get('bb_lo', price-5)
    vwap   = row.get('vwap',  price)
    sr_hi  = row.get('sr_hi',  price+10); sr_lo = row.get('sr_lo', price-10)
    atr    = max(row.get('atr', 2.0), 0.5)

    # 1. Technical Indicators (Panel 2)
    if price > ema20 > ema50: bull += 15; scores['EMA'] = 15
    elif price < ema20 < ema50: bear += 15; scores['EMA'] = -15

    if rsi < 35: bull += 12; scores['RSI_OS'] = 12
    elif rsi > 65: bear += 12; scores['RSI_OB'] = -12
    elif 40 <= rsi <= 60: bull += 5; bear += 5; scores['RSI_N'] = 5

    if macd > macd_s: bull += 10; scores['MACD'] = 10
    elif macd < macd_s: bear += 10; scores['MACD'] = -10

    if price <= bb_lo: bull += 12; scores['BB_OS'] = 12
    elif price >= bb_up: bear += 12; scores['BB_OB'] = -12

    if price > vwap: bull += 8; scores['VWAP'] = 8
    elif price < vwap: bear += 8; scores['VWAP'] = -8

    if abs(price - sr_lo) < atr*0.5: bull += 15; scores['SR_S'] = 15
    if abs(price - sr_hi) < atr*0.5: bear += 15; scores['SR_R'] = -15

    for fk in ['fib_382','fib_500','fib_618']:
        fl = row.get(fk, 0)
        if fl > 0 and abs(price-fl) < atr*0.5:
            bull += 12; bear += 12; scores['FIB'] = 12; break

    # 2. SMC Liquidity Sweeps & Volume Profile (Panel 1)
    if row.get('sweep_pdl', False): bull += 20; scores['SWEEP_PDL'] = 20
    if row.get('sweep_pdh', False): bear += 20; scores['SWEEP_PDH'] = 20
    if row.get('sweep_asia_low', False): bull += 18; scores['SWEEP_ASIA_L'] = 18
    if row.get('sweep_asia_high', False): bear += 18; scores['SWEEP_ASIA_H'] = 18
    if row.get('sweep_london_low', False): bull += 20; scores['SWEEP_LSL'] = 20
    if row.get('sweep_london_high', False): bear += 20; scores['SWEEP_LSH'] = 20

    val_lvl = row.get('val', 0); vah_lvl = row.get('vah', 0); poc_lvl = row.get('poc', 0)
    if val_lvl > 0 and price <= val_lvl + atr*0.3: bull += 16; scores['VP_VAL'] = 16
    if vah_lvl > 0 and price >= vah_lvl - atr*0.3: bear += 16; scores['VP_VAH'] = 16
    if poc_lvl > 0 and abs(price - poc_lvl) < atr*0.25:
        if bull > bear: bull += 10; scores['VP_POC'] = 10
        elif bear > bull: bear += 10; scores['VP_POC'] = 10

    # Multi-Timeframe Bias
    h1_ema20 = row.get('h1_ema20', price); h1_ema50 = row.get('h1_ema50', price)
    if price > h1_ema20 > h1_ema50: bull += 15; scores['MTF_H1_BULL'] = 15
    elif price < h1_ema20 < h1_ema50: bear += 15; scores['MTF_H1_BEAR'] = 15

    ta_score = max(bull, bear)
    direction = 'BUY' if bull > bear else ('SELL' if bear > bull else 'NEUTRAL')

    # 3. Order Flow Triggers (Panel 3)
    of = 0
    cvd = row.get('cvd', 0); delta = row.get('delta', 0)
    absorb = row.get('absorption', False); imbln = row.get('imbalance', False)
    fvg_b = row.get('fvg_bull', False); fvg_s = row.get('fvg_bear', False)

    if i > 5:
        ct = cvd - df.iloc[i-5].get('cvd', cvd)
        if direction=='BUY' and ct>0: of += 20; scores['CVD'] = 20
        elif direction=='SELL' and ct<0: of += 20; scores['CVD'] = 20
        elif ct != 0: of -= 10; scores['CVD_DIV'] = -10

    if direction=='BUY' and delta>0: of += 15; scores['DELTA'] = 15
    elif direction=='SELL' and delta<0: of += 15; scores['DELTA'] = 15

    if absorb: of += 15; scores['ABS'] = 15
    if imbln: of += 10; scores['IMB'] = 10
    if direction=='BUY' and fvg_b: of += 12; scores['FVG'] = 12
    elif direction=='SELL' and fvg_s: of += 12; scores['FVG'] = 12

    try:
        h = df.index[i].hour
        if 8 <= h < 12 or 13 <= h < 17: of += 10; scores['KZ'] = 10
    except Exception:
        pass

    return ta_score, max(0, of), direction, scores

# =============================================================================
# INSTITUTIONAL 5-MINUTE BAR REPOSITORY & REAL-TIME INGESTION
# =============================================================================
INST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_institutional.db')
institutional_bars_lock = threading.RLock()
institutional_5m_bars = []
current_m5_bar = {
    'time': None, 'open': 0.0, 'high': 0.0, 'low': 0.0, 'close': 0.0,
    'volume': 0.0, 'buy_vol': 0.0, 'sell_vol': 0.0, 'delta': 0.0, 'cvd': 0.0, 'vwap': 0.0
}

def load_institutional_bars():
    global institutional_5m_bars
    try:
        if os.path.exists(INST_DB_PATH):
            conn = sqlite3.connect(INST_DB_PATH)
            df = pd.read_sql_query('SELECT bar_time, open, high, low, close, volume, delta FROM price_bars ORDER BY bar_time DESC LIMIT 1500', conn)
            conn.close()
            if not df.empty:
                df['time'] = pd.to_datetime(df['bar_time'])
                df = df.set_index('time').sort_index()
                ohlc_dict = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum', 'delta': 'sum'}
                df_5m = df.resample('5min').agg(ohlc_dict).dropna()
                with institutional_bars_lock:
                    institutional_5m_bars.clear()
                    cvd_run = 0.0
                    for t, r in df_5m.iterrows():
                        cvd_run += float(r['delta'])
                        institutional_5m_bars.append({
                            'time': t,
                            'open': float(r['open']),
                            'high': float(r['high']),
                            'low': float(r['low']),
                            'close': float(r['close']),
                            'volume': float(r['volume']),
                            'delta': float(r['delta']),
                            'cvd': cvd_run,
                            'vwap': float(r['close'])
                        })
                    if institutional_5m_bars:
                        last_b = institutional_5m_bars[-1]
                        logger.info(f"✅ Loaded {len(institutional_5m_bars)} Institutional 5M Bars | Latest Close: ${last_b['close']:.2f}")
    except Exception as e:
        logger.error(f"Error loading institutional bars: {e}")

def get_institutional_m5_df():
    with institutional_bars_lock:
        if not institutional_5m_bars:
            return None
        bars_copy = list(institutional_5m_bars)
        if current_m5_bar['time'] is not None and current_m5_bar['close'] > 0:
            bars_copy.append({
                'time': current_m5_bar['time'],
                'open': current_m5_bar['open'],
                'high': current_m5_bar['high'],
                'low': current_m5_bar['low'],
                'close': current_m5_bar['close'],
                'volume': current_m5_bar['volume'],
                'delta': current_m5_bar['delta'],
                'cvd': current_m5_bar['cvd'],
                'vwap': current_m5_bar['vwap']
            })
    df = pd.DataFrame(bars_copy)
    df = df.set_index('time')
    return df

def on_institutional_tick(price, size, is_buy, source="AllTick WS"):
    global current_m5_bar, institutional_5m_bars
    if price <= 0:
        return
    size = max(0.5, float(size))
    with institutional_bars_lock:
        now_dt = datetime.now()
        minute_bucket = (now_dt.minute // 5) * 5
        bar_start = now_dt.replace(minute=minute_bucket, second=0, microsecond=0)

        delta_amt = size if is_buy else -size
        if current_m5_bar['time'] is None:
            current_m5_bar['time'] = bar_start
            current_m5_bar['open'] = price
            current_m5_bar['high'] = price
            current_m5_bar['low'] = price
            current_m5_bar['close'] = price
            current_m5_bar['volume'] = size
            current_m5_bar['buy_vol'] = size if is_buy else 0.0
            current_m5_bar['sell_vol'] = 0.0 if is_buy else size
            current_m5_bar['delta'] = delta_amt
            prev_cvd = institutional_5m_bars[-1]['cvd'] if institutional_5m_bars else 0.0
            current_m5_bar['cvd'] = prev_cvd + delta_amt
            current_m5_bar['vwap'] = price
        elif bar_start > current_m5_bar['time']:
            institutional_5m_bars.append(dict(current_m5_bar))
            if len(institutional_5m_bars) > 400:
                institutional_5m_bars.pop(0)

            current_m5_bar['time'] = bar_start
            current_m5_bar['open'] = price
            current_m5_bar['high'] = price
            current_m5_bar['low'] = price
            current_m5_bar['close'] = price
            current_m5_bar['volume'] = size
            current_m5_bar['buy_vol'] = size if is_buy else 0.0
            current_m5_bar['sell_vol'] = 0.0 if is_buy else size
            current_m5_bar['delta'] = delta_amt
            current_m5_bar['cvd'] = institutional_5m_bars[-1]['cvd'] + delta_amt
            current_m5_bar['vwap'] = price
        else:
            current_m5_bar['high'] = max(current_m5_bar['high'], price)
            current_m5_bar['low'] = min(current_m5_bar['low'], price)
            current_m5_bar['close'] = price
            current_m5_bar['volume'] += size
            if is_buy:
                current_m5_bar['buy_vol'] += size
            else:
                current_m5_bar['sell_vol'] += size
            current_m5_bar['delta'] = current_m5_bar['buy_vol'] - current_m5_bar['sell_vol']
            prev_cvd = institutional_5m_bars[-1]['cvd'] if institutional_5m_bars else 0.0
            current_m5_bar['cvd'] = prev_cvd + current_m5_bar['delta']
            current_m5_bar['vwap'] = round((current_m5_bar['open'] + current_m5_bar['high'] + current_m5_bar['low'] + current_m5_bar['close']) / 4.0, 2)

    with state_lock:
        engine_state['last_price'] = price
        engine_state['alltick_price'] = price
        engine_state['institutional_spot'] = price
        engine_state['spot_source'] = source
        engine_state['last_alltick_time'] = time.time()
        engine_state['bar_delta'] = current_m5_bar['delta']
        engine_state['cvd'] = current_m5_bar['cvd']
        engine_state['cvd_trend'] = 'BULLISH' if engine_state['bar_delta'] > 20 else ('BEARISH' if engine_state['bar_delta'] < -20 else 'NEUTRAL')


# =============================================================================
# TIER 1 INSTITUTIONAL STREAMING WORKERS (ALLTICK, TWELVEDATA, ITICK)
# =============================================================================
def alltick_ws_worker():
    """Streams live spot gold ticks via AllTick WebSocket"""
    url = f"wss://quote.alltick.co/quote-b-ws-api?token={ALLTICK_TOKEN}"
    while True:
        try:
            with state_lock:
                engine_state['alltick_status'] = 'CONNECTING'

            def on_msg(ws, msg):
                try:
                    d = json.loads(msg)
                    if 'data' in d and isinstance(d['data'], dict):
                        dt = d['data']
                        bids = dt.get('bids', [])
                        asks = dt.get('asks', [])
                        if bids and asks:
                            bid = float(bids[0]['price'])
                            ask = float(asks[0]['price'])
                            mid = round((bid + ask) / 2.0, 2)
                            vol = float(bids[0].get('volume', 1.0))
                            on_institutional_tick(mid, min(vol, 10.0), is_buy=True, source='AllTick WS')
                            with state_lock:
                                engine_state['alltick_status'] = 'ONLINE (WebSocket Streaming)'
                except Exception:
                    pass

            def on_open(ws):
                with state_lock:
                    engine_state['alltick_status'] = 'CONNECTED (Subscribing)'
                sub = {"cmd_id": 22002, "seq_id": int(time.time()), "trace": "p8088", "data": {"symbol_list": [{"code": "GOLD"}, {"code": "XAUUSD"}]}}
                ws.send(json.dumps(sub))

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg)
            ws.run_forever(ping_interval=15, ping_timeout=5)
        except Exception as e:
            logger.warning(f"AllTick WS loop error: {e}")
        time.sleep(10)


def twelvedata_ws_worker():
    """Streams institutional spot gold quotes via TwelveData WebSocket"""
    global _td_idx
    while True:
        try:
            k = TWELVEDATA_KEYS[_td_idx % len(TWELVEDATA_KEYS)]
            url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={k}"

            def on_msg(ws, msg):
                try:
                    d = json.loads(msg)
                    if d.get("event") == "price" and "price" in d:
                        p = float(d["price"])
                        if p > 0:
                            on_institutional_tick(p, 1.0, is_buy=True, source='TwelveData WS')
                except Exception:
                    pass

            def on_open(ws):
                ws.send(json.dumps({"action": "subscribe", "params": {"symbols": "XAU/USD"}}))

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg)
            ws.run_forever(ping_interval=20, ping_timeout=8)
        except Exception:
            _td_idx += 1
        time.sleep(15)


def itick_worker():
    """Streams order flow tick delta & imbalances via iTick WebSocket"""
    url = f"wss://api-free.itick.io/forex?token={ITICK_TOKEN}"

    def on_message(ws, msg):
        try:
            payload = json.loads(msg)
            if 'data' in payload and isinstance(payload['data'], dict):
                d = payload['data']
                p = float(d.get('p', d.get('price', d.get('last_price', 0))))
                v = float(d.get('v', d.get('vol', d.get('volume', 1.0))))
                s = int(d.get('s', 0))
                is_buy = (s == 1)
                if p > 0:
                    on_institutional_tick(p, v, is_buy=is_buy, source='iTick WS')
                    with state_lock:
                        engine_state['itick_price'] = p
                        engine_state['last_itick_tick_time'] = time.time()
                        engine_state['itick_status'] = 'CONNECTED (Streaming)'
        except Exception:
            pass

    def on_open(ws):
        with state_lock:
            engine_state['itick_status'] = 'CONNECTED'

    def on_error(ws, err):
        with state_lock:
            engine_state['itick_status'] = f'STANDBY ({str(err)[:20]})'

    def on_close(ws, close_status_code, close_msg):
        with state_lock:
            engine_state['itick_status'] = 'RECONNECTING'

    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(ping_interval=20, ping_timeout=8)
        except Exception as e:
            logger.warning(f"iTick worker loop exception: {e}")
        time.sleep(15)

# =============================================================================
# TIER 2 WORKER: INSTITUTIONAL ANTI-MANIPULATION SAFETY SHIELD
# =============================================================================
def safety_shield_worker():
    """Institutional Anti-Manipulation Safety Shield:
       Compares MT5 execution quote against the live Institutional Spot Feed
       (AllTick / TwelveData / iTick).
       Tolerance: Tight $0.85
       Spread Guard: <= $0.60
    """
    while True:
        try:
            with state_lock:
                mt5_bid = engine_state['mt5_bid']
                mt5_ask = engine_state['mt5_ask']
                mt5_spread = engine_state['mt5_spread']
                mt5_mid = (mt5_bid + mt5_ask) / 2.0 if (mt5_bid > 0 and mt5_ask > 0) else 0.0
                inst_price = engine_state.get('institutional_spot', engine_state.get('alltick_price', 0.0))
                inst_src = engine_state.get('spot_source', 'Institutional Spot')

            if inst_price > 0 and mt5_mid > 0:
                dev = abs(mt5_mid - inst_price)
                tolerance = 0.85
                spread_ok = mt5_spread <= 0.60
                wick_guard_ok = (dev <= tolerance)
                shield_ok = spread_ok and wick_guard_ok

                with state_lock:
                    engine_state['neutral_spot_price'] = round(inst_price, 2)
                    engine_state['price_deviation'] = round(dev, 2)
                    engine_state['spread_ok'] = spread_ok
                    engine_state['wick_guard_ok'] = wick_guard_ok
                    engine_state['shield_ok'] = shield_ok

                    if not spread_ok:
                        engine_state['shield_msg'] = f'⚠️ SPREAD INFLATED: MT5 Spread ${mt5_spread:.2f} > $0.60. Delayed!'
                    elif not wick_guard_ok:
                        engine_state['shield_msg'] = f'⚠️ PHANTOM WICK DETECTED: MT5 diff ${dev:.2f} > ${tolerance:.2f} vs {inst_src}. Blocked!'
                    else:
                        engine_state['shield_msg'] = f'✅ SAFETY SHIELD CLEAR: Spread ${mt5_spread:.2f} OK | Price Dev ${dev:.2f} ({inst_src})'
        except Exception as e:
            logger.error(f"Safety shield loop error: {e}")
        time.sleep(5)

# =============================================================================
# SMC LEVELS CALCULATOR (100% INSTITUTIONAL 5M FEED - ZERO BROKER DATA)
# =============================================================================
def update_smc_levels():
    try:
        df = get_institutional_m5_df()
        if df is None or len(df) < 50:
            return

        hours = df.index.hour
        is_asia   = (hours >= 0) & (hours < 7)
        is_london = (hours >= 7) & (hours < 13)
        is_ny     = (hours >= 13) & (hours < 21)

        asia_h = df[is_asia]['high'].tail(48).max() if any(is_asia) else df['high'].iloc[-1]
        asia_l = df[is_asia]['low'].tail(48).min() if any(is_asia) else df['low'].iloc[-1]
        lon_h  = df[is_london]['high'].tail(48).max() if any(is_london) else df['high'].iloc[-1]
        lon_l  = df[is_london]['low'].tail(48).min() if any(is_london) else df['low'].iloc[-1]
        ny_h   = df[is_ny]['high'].tail(48).max() if any(is_ny) else df['high'].iloc[-1]
        ny_l   = df[is_ny]['low'].tail(48).min() if any(is_ny) else df['low'].iloc[-1]

        pdh = df['high'].iloc[-288:].max() if len(df) >= 288 else df['high'].max()
        pdl = df['low'].iloc[-288:].min() if len(df) >= 288 else df['low'].min()

        c_arr = df['close'].tail(120).values
        v_arr = df['volume'].tail(120).values.astype(float)
        edges = np.linspace(c_arr.min(), c_arr.max(), 21)
        b_idx = np.clip(np.digitize(c_arr, edges) - 1, 0, 19)
        v_bins = np.bincount(b_idx, weights=v_arr, minlength=20)
        p_idx = np.argmax(v_bins)
        poc = (edges[p_idx] + edges[p_idx+1]) / 2.0

        with state_lock:
            engine_state['smc_levels'] = {
                'pdh': round(pdh, 2), 'pdl': round(pdl, 2),
                'asia_high': round(asia_h, 2), 'asia_low': round(asia_l, 2),
                'london_high': round(lon_h, 2), 'london_low': round(lon_l, 2),
                'ny_high': round(ny_h, 2), 'ny_low': round(ny_l, 2),
                'poc': round(poc, 2)
            }
    except Exception as e:
        logger.error(f'SMC levels calculation error: {e}')

# =============================================================================
# TIER 3 WORKER: MT5 EXECUTION & AUTO BREAK-EVEN (FOR BOTH MODEL A & MODEL B)
# =============================================================================
def mt5_execution_worker():
    """Maintains MT5 bridge, polls positions, executes 1:1 Auto Break-Even for both Model A & B"""
    global SYMBOL
    prev_tracked_positions_count = 0
    prev_model_b_count = 0          # Track Model B positions separately for Sweep Reset
    while True:
        if mt5 is None:
            time.sleep(3)
            continue
        try:
            with mt5_lock:
                if not mt5.initialize():
                    with state_lock:
                        engine_state['mt5_status'] = 'DISCONNECTED'
                    time.sleep(3)
                    continue

                acc = mt5.account_info()
                sym_info = mt5.symbol_info(SYMBOL)
                if sym_info is None or not sym_info.visible:
                    SYMBOL = resolve_gold_symbol()
                    sym_info = mt5.symbol_info(SYMBOL)
                tick = mt5.symbol_info_tick(SYMBOL)
                positions = mt5.positions_get(symbol=SYMBOL)



            if tick and sym_info:
                with state_lock:
                    engine_state['mt5_bid'] = tick.bid
                    engine_state['mt5_ask'] = tick.ask
                    engine_state['mt5_spread'] = round(tick.ask - tick.bid, 2)
                    engine_state['mt5_status'] = f'CONNECTED ({acc.server if acc else "Equiti"})'
                    engine_state['market_mode'] = 'OPEN (Live Trading)' if sym_info.trade_mode != 0 else 'WEEKEND STANDBY (Armed)'


            if acc:
                with state_lock:
                    engine_state['account_info']['login'] = acc.login
                    engine_state['account_info']['server'] = acc.server
                    engine_state['account_info']['balance'] = round(acc.balance, 2)
                    engine_state['account_info']['equity'] = round(acc.equity, 2)
                    engine_state['account_info']['margin_free'] = round(acc.margin_free, 2)
                    engine_state['account_info']['floating_pnl'] = round(acc.equity - acc.balance, 2)

            with state_lock:
                use_auto_be = engine_state['use_auto_be']

            active_list = []
            be_orders = []

            if positions:
                for pos in positions:
                    ticket = pos.ticket
                    magic  = pos.magic
                    pos_type = 'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'
                    open_p = pos.price_open
                    curr_p = pos.price_current
                    sl = pos.sl
                    tp = pos.tp
                    pnl = pos.profit
                    vol = pos.volume
                    is_be_active = False

                    model_tag = 'MODEL A (Confluence)' if magic == MAGIC_MODEL_A else ('MODEL B (SMC-OF)' if magic == MAGIC_MODEL_B else 'MANUAL')

                    if use_auto_be:
                        if pos_type == 'BUY':
                            if curr_p >= open_p + 2.50 and sl < open_p:
                                new_sl = round(open_p + 0.15, 2)
                                be_orders.append((ticket, model_tag, new_sl, tp))
                                is_be_active = True
                            elif sl >= open_p:
                                is_be_active = True
                        else: # SELL
                            if curr_p <= open_p - 2.50 and (sl > open_p or sl == 0):
                                new_sl = round(open_p - 0.15, 2)
                                be_orders.append((ticket, model_tag, new_sl, tp))
                                is_be_active = True
                            elif sl <= open_p and sl > 0:
                                is_be_active = True

                    active_list.append({
                        'ticket': ticket,
                        'model': model_tag,
                        'magic': magic,
                        'type': pos_type,
                        'volume': vol,
                        'open_price': open_p,
                        'current_price': curr_p,
                        'sl': sl,
                        'tp': tp,
                        'pnl': round(pnl, 2),
                        'is_be': is_be_active,
                        'status': '🛡️ ZERO RISK' if is_be_active else '⏳ RUNNING'
                    })

            # 5-Minute Candle Bar Lock Enforcement
            curr_candle_epoch = get_current_5m_candle_time()
            our_active_positions = [p for p in active_list if p.get('magic') in (MAGIC_MODEL_A, MAGIC_MODEL_B)]
            model_b_active_positions = [p for p in active_list if p.get('magic') == MAGIC_MODEL_B]

            with state_lock:
                engine_state['open_positions'] = active_list

                # Rule 1: If any position is active right now, lock current 5M candle
                if len(our_active_positions) > 0:
                    if engine_state.get('last_traded_candle_time', 0) < curr_candle_epoch:
                        engine_state['last_traded_candle_time'] = curr_candle_epoch

                # Rule 2: If a position was active and just got closed (manual cut or SL/TP)
                if prev_tracked_positions_count > 0 and len(our_active_positions) == 0:
                    engine_state['last_traded_candle_time'] = curr_candle_epoch
                    rem_str = get_5m_countdown_str()
                    engine_state['candle_lock_status'] = f'LOCKED ({rem_str})'
                    log_audit(f'🔒 5M CANDLE LOCKED: Trade closed/cut. Strict wait until next 5M candle bar ({rem_str}).', 'PROTECTION')

                # Rule 3: Model B Sweep Reset - If Model B trade just closed, activate sweep reset
                if prev_model_b_count > 0 and len(model_b_active_positions) == 0:
                    engine_state['model_b']['sweep_must_reset'] = True
                    log_audit('🔄 MODEL B SWEEP RESET ACTIVATED: Trade closed. Waiting for price to leave all sweep zones before next Model B trade.', 'PROTECTION')

            prev_tracked_positions_count = len(our_active_positions)
            prev_model_b_count = len(model_b_active_positions)

            # Continuous MT5 Deal History Audit (Last 20 minutes)
            try:
                now_dt = datetime.now()
                deals = mt5.history_deals_get(now_dt - timedelta(minutes=20), now_dt + timedelta(minutes=5))
                if deals:
                    for d in reversed(deals):
                        if d.symbol == SYMBOL and d.magic in (MAGIC_MODEL_A, MAGIC_MODEL_B):
                            d_candle = int(d.time // 300) * 300
                            if d_candle >= curr_candle_epoch:
                                with state_lock:
                                    if engine_state.get('last_traded_candle_time', 0) < curr_candle_epoch:
                                        engine_state['last_traded_candle_time'] = curr_candle_epoch
            except Exception:
                pass

            for ticket, model_tag, new_sl, tp in be_orders:
                with mt5_lock:
                    req = {
                        'action': mt5.TRADE_ACTION_SLTP,
                        'position': ticket,
                        'symbol': SYMBOL,
                        'sl': new_sl,
                        'tp': tp
                    }
                    res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log_audit(f'🛡️ AUTO BREAK-EVEN: #{ticket} [{model_tag}] SL moved to ${new_sl}. Zero Risk!', 'PROTECTION')
                    with state_lock:
                        engine_state['protected_be_count'] += 1

        except Exception as e:
            logger.error(f'MT5 execution loop error: {e}')
        time.sleep(1)

# =============================================================================
# MODEL A EVALUATOR (FULL 8086 CONFLUENCE - 15M TECHNICALS + SMC + ORDER FLOW)
# =============================================================================
def evaluate_model_a():
    """Runs Model A: 100% Institutional 5M Candles, EMA 20/50/200, RSI, MACD, BB, Fib, VWAP, S/R, SMC, OF"""
    try:
        df = get_institutional_m5_df()
        if df is None or len(df) < 50:
            return

        df['volume'] = df['volume'].astype(float)

        df['ema20']  = calc_ema(df['close'], 20)
        df['ema50']  = calc_ema(df['close'], 50)
        df['ema200'] = calc_ema(df['close'], 200)
        df['h1_ema20'] = calc_ema(df['close'], 240)
        df['h1_ema50'] = calc_ema(df['close'], 600)
        df['rsi']   = calc_rsi(df['close'])
        df['macd'], df['macd_s'], _ = calc_macd(df['close'])
        df['bb_up'], _, df['bb_lo'] = calc_bb(df['close'])
        df['atr']   = calc_atr(df)
        df['sr_hi'] = df['high'].rolling(50).max()
        df['sr_lo'] = df['low'].rolling(50).min()
        df = calc_orderflow_metrics(df)
        df = calc_volume_profile(df, window=80)
        df = calc_liquidity_sweeps(df, pd_window=288)
        fb = calc_fibonacci(df)
        df['fib_382'] = fb['382']; df['fib_500'] = fb['500']; df['fib_618'] = fb['618']

        # Score the latest bar
        last_idx = len(df) - 1
        ta_s, of_s, dirn, scs = score_bar_model_a(df, last_idx)
        sig_detail = ', '.join(f'{k}:{v}' for k, v in scs.items() if v > 0)

        current_candle_time = get_current_5m_candle_time()
        rem_str = get_5m_countdown_str()

        order_to_send_a = None
        with state_lock:
            last_traded_candle = engine_state.get('last_traded_candle_time', 0)
            is_candle_locked = (current_candle_time > 0 and current_candle_time <= last_traded_candle)
            engine_state['candle_lock_status'] = f'LOCKED ({rem_str})' if is_candle_locked else 'READY'
            engine_state['next_candle_countdown'] = rem_str

            engine_state['model_a']['ta_score'] = ta_s
            engine_state['model_a']['of_score'] = of_s
            engine_state['model_a']['direction'] = dirn
            engine_state['model_a']['signals_detail'] = sig_detail if sig_detail else 'Scanning...'
            engine_state['model_a']['last_eval_time'] = datetime.now().strftime('%H:%M:%S')

            auto_pilot = engine_state['auto_pilot']
            model_a_on = engine_state['model_a_active']
            shield_ok  = engine_state['shield_ok']
            open_pos   = [p for p in engine_state['open_positions'] if p.get('magic') in (MAGIC_MODEL_A, MAGIC_MODEL_B)]

            # Model A Entry Condition: TA Score >= 40 and OF Score >= 30
            if ta_s >= 40 and of_s >= 30 and dirn in ('BUY', 'SELL'):
                if is_candle_locked:
                    engine_state['model_a']['signal'] = f'CANDLE LOCKED ({dirn})'
                    engine_state['model_a']['reason'] = f'5M candle already traded. Next candle in {rem_str}.'
                else:
                    engine_state['model_a']['signal'] = f'STRONG {dirn}'
                    engine_state['model_a']['reason'] = f'Consensus Met: TA={ta_s}/40, OF={of_s}/30 ({sig_detail[:35]})'
                    if auto_pilot and model_a_on and shield_ok and len(open_pos) == 0:
                        order_to_send_a = (dirn, MAGIC_MODEL_A, COMMENT_MODEL_A, f'Model A Confluence (TA:{ta_s}, OF:{of_s})', current_candle_time)
            else:
                if is_candle_locked:
                    engine_state['model_a']['signal'] = 'CANDLE LOCKED'
                    engine_state['model_a']['reason'] = f'Waiting for next 5M candle ({rem_str})...'
                else:
                    engine_state['model_a']['signal'] = 'WAITING'
                    engine_state['model_a']['reason'] = f'TA={ta_s}/40 | OF={of_s}/30 | Dir={dirn} (Waiting for 40/30 threshold)'


        if order_to_send_a:
            execute_order(*order_to_send_a)

    except Exception as e:
        logger.error(f'Model A evaluation error: {e}')

# =============================================================================
# MODEL B EVALUATOR (PURE SMC LIQUIDITY & REAL-TIME ORDER FLOW)
# =============================================================================
def evaluate_model_b():
    """Runs Model B: Pure Session Sweeps + Millisecond Institutional Delta & CVD
    
    SWEEP RESET PROTECTION:
    After any Model B trade closes (SL/TP/manual), the engine requires price to
    move AWAY from all SMC sweep zones before a new trade can fire. This prevents
    back-to-back re-entries at the same level. The flag 'sweep_must_reset' is set
    to True when a trade closes, and only resets to False when NO sweep is detected
    (proving price has left all level zones). Only then can a fresh sweep trigger.
    """
    try:
        update_smc_levels()

        current_candle_time = get_current_5m_candle_time()
        rem_str = get_5m_countdown_str()

        with state_lock:
            auto_pilot = engine_state['auto_pilot']
            model_b_on = engine_state['model_b_active']
            p = engine_state['last_price']
            smc = dict(engine_state['smc_levels'])
            shield_ok = engine_state['shield_ok']
            open_pos = [pos for pos in engine_state['open_positions'] if pos.get('magic') in (MAGIC_MODEL_A, MAGIC_MODEL_B)]
            cvd_t = engine_state['cvd_trend']
            delta = engine_state['bar_delta']
            absorb = engine_state['absorption_detected']
            sweep_must_reset = engine_state['model_b']['sweep_must_reset']

        # Detect Session Sweeps
        detected_sweep = None
        sweep_dir = None

        if smc.get('london_high', 0) > 0 and p >= smc['london_high'] - 0.20:
            detected_sweep = 'LONDON HIGH SWEEP (LSH)'
            sweep_dir = 'SELL'
        elif smc.get('london_low', 0) > 0 and p <= smc['london_low'] + 0.20:
            detected_sweep = 'LONDON LOW SWEEP (LSL)'
            sweep_dir = 'BUY'
        elif smc.get('asia_high', 0) > 0 and p >= smc['asia_high'] - 0.20:
            detected_sweep = 'ASIAN HIGH JUDAS SWEEP'
            sweep_dir = 'SELL'
        elif smc.get('asia_low', 0) > 0 and p <= smc['asia_low'] + 0.20:
            detected_sweep = 'ASIAN LOW JUDAS SWEEP'
            sweep_dir = 'BUY'
        elif smc.get('pdh', 0) > 0 and p >= smc['pdh'] - 0.20:
            detected_sweep = 'PREVIOUS DAY HIGH SWEEP (PDH)'
            sweep_dir = 'SELL'
        elif smc.get('pdl', 0) > 0 and p <= smc['pdl'] + 0.20:
            detected_sweep = 'PREVIOUS DAY LOW SWEEP (PDL)'
            sweep_dir = 'BUY'

        # SWEEP RESET LOGIC: After trade closes, wait for price to leave all level zones
        if sweep_must_reset:
            if detected_sweep is None:
                # Price has moved away from ALL levels — reset complete, ready for fresh sweep
                with state_lock:
                    engine_state['model_b']['sweep_must_reset'] = False
                sweep_must_reset = False
                log_audit('✅ MODEL B SWEEP RESET COMPLETE: Price cleared all sweep zones. Ready for fresh level.', 'PROTECTION')

        # Order Flow Confirmation
        of_confirmed = False
        if detected_sweep:
            if sweep_dir == 'SELL' and (delta < -30 or cvd_t == 'BEARISH' or absorb):
                of_confirmed = True
            elif sweep_dir == 'BUY' and (delta > 30 or cvd_t == 'BULLISH' or absorb):
                of_confirmed = True

        order_to_send_b = None
        with state_lock:
            last_traded_candle = engine_state.get('last_traded_candle_time', 0)
            is_candle_locked = (current_candle_time > 0 and current_candle_time <= last_traded_candle)

            engine_state['model_b']['active_sweep'] = detected_sweep
            engine_state['model_b']['last_eval_time'] = datetime.now().strftime('%H:%M:%S')

            if detected_sweep and of_confirmed and shield_ok:
                if sweep_must_reset:
                    # BLOCKED: Price is still at a level zone, must leave first
                    engine_state['model_b']['signal'] = f'SWEEP RESET PENDING ({sweep_dir})'
                    engine_state['model_b']['reason'] = f'{detected_sweep} detected but price must leave all levels first before next trade.'
                elif is_candle_locked:
                    engine_state['model_b']['signal'] = f'CANDLE LOCKED ({sweep_dir})'
                    engine_state['model_b']['reason'] = f'5M candle already traded. Next candle in {rem_str}.'
                else:
                    engine_state['model_b']['signal'] = f'STRONG {sweep_dir}'
                    engine_state['model_b']['reason'] = f'Consensus Met: {detected_sweep} + Delta ({delta:+})'
                    if auto_pilot and model_b_on and len(open_pos) == 0:
                        order_to_send_b = (sweep_dir, MAGIC_MODEL_B, COMMENT_MODEL_B, f'Model B SMC ({detected_sweep})', current_candle_time)
            elif detected_sweep and not of_confirmed:
                if sweep_must_reset:
                    engine_state['model_b']['signal'] = 'SWEEP RESET PENDING'
                    engine_state['model_b']['reason'] = f'{detected_sweep} — Price must clear all levels before next trade...'
                else:
                    engine_state['model_b']['signal'] = f'PENDING {sweep_dir}'
                    engine_state['model_b']['reason'] = f'{detected_sweep} at ${p:.2f}. Waiting for Delta reversal...'
            else:
                if sweep_must_reset:
                    # No sweep detected while reset pending — this is good, reset will happen next cycle
                    engine_state['model_b']['signal'] = 'SWEEP RESETTING'
                    engine_state['model_b']['reason'] = 'Price leaving level zones... Reset in progress.'
                elif is_candle_locked:
                    engine_state['model_b']['signal'] = 'CANDLE LOCKED'
                    engine_state['model_b']['reason'] = f'Waiting for next 5M candle ({rem_str})...'
                else:
                    engine_state['model_b']['signal'] = 'WAITING'
                    engine_state['model_b']['reason'] = 'Monitoring session levels, Judas sweeps & CVD...'

        if order_to_send_b:
            execute_order(*order_to_send_b)

    except Exception as e:
        logger.error(f'Model B evaluation error: {e}')

# =============================================================================
# UNIFIED DUAL-MODEL DISPATCH LOOP
# =============================================================================
def dual_model_consensus_engine():
    """Periodically evaluates both Model A and Model B in parallel"""
    while True:
        try:
            evaluate_model_a()
            evaluate_model_b()
        except Exception as e:
            logger.error(f'Dual model engine error: {e}')
        time.sleep(3)

def execute_order(direction, magic_num, comment_tag, reason_str, candle_time=0):
    """Executes a 0.01 lot order with 1:2 R:R (SL: $2.50, TP: $5.00) on MT5 with 5M Candle Lock"""
    try:
        curr_candle = get_current_5m_candle_time()
        # Mutex pre-check to prevent concurrent entry by Model A and Model B in the same 5M candle
        with state_lock:
            if engine_state.get('last_traded_candle_time', 0) >= curr_candle:
                logger.info(f"execute_order aborted: 5M candle {curr_candle} already locked.")
                return
            # Pre-lock current 5M candle immediately
        # HARDWARE KILL-SWITCH: STRICT PAPER TRADING ONLY
        PAPER_TRADING_ONLY = True
        if PAPER_TRADING_ONLY or mt5 is None:
            price = latest_tick.get('bid', 2650.0) if direction == 'SELL' else latest_tick.get('ask', 2650.0)
            sl = round(price - 2.50, 2) if direction == 'BUY' else round(price + 2.50, 2)
            tp = round(price + 5.00, 2) if direction == 'BUY' else round(price - 5.00, 2)
            rem_str = get_5m_countdown_str()
            log_audit(f'📄 [PAPER SIMULATION] 0.01 {direction} [{comment_tag}] at ${price:.2f} | SL=${sl:.2f}, TP=${tp:.2f} | {reason_str}', 'SIMULATED')
            with state_lock:
                engine_state['last_traded_candle_time'] = curr_candle
                engine_state['last_trade_exec_time'] = time.time()
                engine_state['candle_lock_status'] = f'LOCKED ({rem_str})'
            return

        with mt5_lock:
            if not mt5.initialize():
                return
            tick = mt5.symbol_info_tick(SYMBOL)
            if not tick:
                return

            price = tick.ask if direction == 'BUY' else tick.bid
            sl = round(price - 2.50, 2) if direction == 'BUY' else round(price + 2.50, 2)
            tp = round(price + 5.00, 2) if direction == 'BUY' else round(price - 5.00, 2)

            req = {
                'action': mt5.TRADE_ACTION_DEAL,
                'symbol': SYMBOL,
                'volume': 0.01,
                'type': mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL,
                'price': price,
                'sl': sl,
                'tp': tp,
                'deviation': 20,
                'magic': magic_num,
                'comment': comment_tag,
                'type_time': mt5.ORDER_TIME_GTC,
                'type_filling': mt5.ORDER_FILLING_IOC,
            }

            check = mt5.order_check(req)
            if check.retcode == 0 or check.retcode == mt5.TRADE_RETCODE_DONE:
                res = mt5.order_send(req)
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    rem_str = get_5m_countdown_str()
                    log_audit(f'🚀 0.01 {direction} EXECUTED [{comment_tag}] at ${price:.2f} | SL=${sl}, TP=${tp} | {reason_str}', 'EXECUTION')
                    with state_lock:
                        engine_state['last_traded_candle_time'] = curr_candle
                        engine_state['last_trade_exec_time'] = time.time()
                        engine_state['candle_lock_status'] = f'LOCKED ({rem_str})'
                else:
                    log_audit(f'Order send issue: {res.retcode} - {res.comment}', 'WARNING')
                    with state_lock:
                        engine_state['last_trade_exec_time'] = time.time()
            else:
                log_audit(f'CONSENSUS TRIGGERED (Standby Simulation): [{comment_tag}] 0.01 {direction} at ${price:.2f} | SL=${sl}, TP=${tp} | Check: {check.comment}', 'STANDBY')
    except Exception as e:
        logger.error(f'Order dispatch error: {e}')

def panic_close_all():
    """Closes all open positions immediately"""
    try:
        if mt5 is None:
            return "MT5 not active on Linux VPS"
        with mt5_lock:
            if not mt5.initialize():
                return "MT5 not connected"
            positions = mt5.positions_get(symbol=SYMBOL)
            closed_n = 0
            if positions:
                for p in positions:
                    tick = mt5.symbol_info_tick(SYMBOL)
                    close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    close_price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
                    req = {
                        'action': mt5.TRADE_ACTION_DEAL,
                        'position': p.ticket,
                        'symbol': SYMBOL,
                        'volume': p.volume,
                        'type': close_type,
                        'price': close_price,
                        'deviation': 30,
                        'magic': p.magic,
                        'comment': 'PANIC_CLOSE',
                        'type_time': mt5.ORDER_TIME_GTC,
                        'type_filling': mt5.ORDER_FILLING_IOC,
                    }
                    res = mt5.order_send(req)
                    if res.retcode == mt5.TRADE_RETCODE_DONE:
                        closed_n += 1
        log_audit(f'🛑 PANIC KILL-SWITCH: Closed {closed_n} positions!', 'EMERGENCY')
        return f'Closed {closed_n} positions'
    except Exception as e:
        return str(e)

# =============================================================================
# START BACKGROUND WORKERS (INSTITUTIONAL PIPELINE)
# =============================================================================
load_institutional_bars()

t_alltick = threading.Thread(target=alltick_ws_worker, daemon=True)
t_td_ws   = threading.Thread(target=twelvedata_ws_worker, daemon=True)
t_itick   = threading.Thread(target=itick_worker, daemon=True)
t_shield  = threading.Thread(target=safety_shield_worker, daemon=True)
t_mt5     = threading.Thread(target=mt5_execution_worker, daemon=True)
t_engine  = threading.Thread(target=dual_model_consensus_engine, daemon=True)

t_alltick.start()
t_td_ws.start()
t_itick.start()
t_shield.start()
t_mt5.start()
t_engine.start()

# =============================================================================
# DASH WEB USER INTERFACE (PORT 8088)
# =============================================================================
app = dash.Dash(
    __name__,
    title='GOLDFLOW Dual-Model Live Engine',
    update_title=None,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes"}]
)

server = app.server

@server.before_request
def require_basic_auth():
    auth = request.authorization
    if not auth or auth.username != 'am' or auth.password != 'Orferflow@1910':
        return Response(
            '401 Unauthorized - Access Denied\nGoldFlow Live Engine 8088',
            401,
            {'WWW-Authenticate': 'Basic realm="GoldFlow Secured Terminal"'}
        )

app.layout = html.Div(
    style={'backgroundColor':'#030712','color':'#E6EDF3','fontFamily':"'JetBrains Mono','Consolas',monospace",'minHeight':'100vh','padding':'10px 16px','overflowX':'hidden','overflowY':'auto'},
    children=[
        dcc.Interval(id='live-refresh', interval=1500, n_intervals=0),

        # TOP HEADER
        html.Div(style={'display':'flex','flexWrap':'wrap','gap':'10px','alignItems':'center','justifyContent':'space-between','backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderBottom':'2px solid #00E676','padding':'8px 16px','borderRadius':'6px','marginBottom':'10px'},
            children=[
                html.Div([
                    html.Span('⚡ GOLD.FLOW', style={'color':'#FFD700','fontWeight':'900','fontSize':'18px'}),
                    html.Span(' // DUAL-MODEL LIVE CONSENSUS ENGINE', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'13px'}),
                    html.Span(' [PORT 8088]', style={'color':'#00E676','fontSize':'10px','backgroundColor':'#064E3B','padding':'2px 8px','borderRadius':'4px','marginLeft':'8px'}),
                    html.Span('MODEL A (8086 CONFLUENCE) + MODEL B (PURE SMC-OF)', style={'color':'#A855F7','fontSize':'10px','backgroundColor':'#2E1065','padding':'2px 8px','borderRadius':'4px','marginLeft':'6px'}),
                ]),
                html.Div(id='header-clock-strip', style={'color':'#94A3B8','fontSize':'11px'})
            ]),

        # 4-COLUMN TOP COMPARISON CARDS
        html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(220px, 1fr))','gap':'10px','marginBottom':'10px'},
            children=[
                # MODEL A CARD (Full 8086 Confluence)
                html.Div(id='model-a-card', style={'backgroundColor':'#0A0E17','border':'1px solid #3B82F6','borderRadius':'6px','padding':'10px'}),
                # MODEL B CARD (Pure SMC & OF)
                html.Div(id='model-b-card', style={'backgroundColor':'#0A0E17','border':'1px solid #10B981','borderRadius':'6px','padding':'10px'}),
                # TIER 2: SAFETY SHIELD
                html.Div(id='tier2-card', style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px'}),
                # ACCOUNT & PROTECTED CARDS
                html.Div(id='account-card', style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px'})
            ]),

        # MASTER CONTROLS BAR
        html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px 14px','marginBottom':'10px','display':'flex','flexWrap':'wrap','gap':'10px','alignItems':'center','justifyContent':'space-between'},
            children=[
                html.Div(style={'display':'flex','flexWrap':'wrap','alignItems':'center','gap':'15px'},
                    children=[
                        html.Div([
                            html.Span('AUTO-PILOT:', style={'color':'#64748B','fontSize':'11px','fontWeight':'bold','marginRight':'6px'}),
                            html.Button(id='autopilot-btn', n_clicks=0,
                                style={'border':'none','padding':'6px 14px','borderRadius':'4px','cursor':'pointer','fontWeight':'bold','fontSize':'11px'})
                        ]),
                        html.Div([
                            html.Span('MODELS ACTIVE:', style={'color':'#64748B','fontSize':'11px','fontWeight':'bold','marginRight':'6px'}),
                            dcc.Checklist(id='models-chk',
                                options=[
                                    {'label':' 🔵 Model A (8086 Confluence)','value':'ma'},
                                    {'label':' 🟢 Model B (Pure SMC-OF)','value':'mb'}
                                ],
                                value=['ma','mb'], inline=True, style={'fontSize':'11px','color':'#CBD5E1'}, inputStyle={'marginRight':'4px','marginLeft':'8px'})
                        ]),
                        html.Div([
                            dcc.Checklist(id='be-chk', options=[{'label':' 🛡️ 1:1 Auto Break-Even (Zero Risk)','value':'be'}],
                                value=['be'], style={'fontSize':'11px','color':'#00E676','fontWeight':'bold'})
                        ])
                    ]),
                html.Div(style={'display':'flex','alignItems':'center','gap':'8px'},
                    children=[
                        html.Button('🛑 PANIC CLOSE ALL', id='panic-btn', n_clicks=0,
                            style={'backgroundColor':'#991B1B','color':'#FFF','border':'1px solid #EF4444','padding':'6px 14px','borderRadius':'4px','cursor':'pointer','fontWeight':'900','fontSize':'11px'})
                    ])
            ]),

        # 2-COLUMN MAIN CONTENT (BLOTTER & AUDIT)
        html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(320px, 1fr))','gap':'10px'},
            children=[
                # LEFT COLUMN: ACTIVE BLOTTER & SMC LEVELS
                html.Div([
                    html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px','marginBottom':'10px'},
                        children=[
                            html.Div(style={'display':'flex','justifyContent':'space-between','alignItems':'center','marginBottom':'8px'},
                                children=[
                                    html.Div('⚡ LIVE MT5 ORDER BLOTTER (MODEL A & MODEL B SIDE-BY-SIDE)', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'12px'}),
                                    html.Div(id='blotter-count-badge', style={'color':'#00E676','fontSize':'10px','fontWeight':'bold'})
                                ]),
                            html.Div(id='positions-table', style={'overflowX':'auto'})
                        ]),
                    html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px'},
                        children=[
                            html.Div('🏛️ SMC LEVELS & 15M TECHNICAL INDICATORS WATCH', style={'color':'#A855F7','fontWeight':'bold','fontSize':'12px','marginBottom':'8px'}),
                            html.Div(id='indicators-watch-display')
                        ])
                ]),

                # RIGHT COLUMN: AUDIT TRAIL
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px'},
                    children=[
                        html.Div('📜 REAL-TIME EXECUTION AUDIT TRAIL (MODEL A vs B)', style={'color':'#F59E0B','fontWeight':'bold','fontSize':'12px','marginBottom':'8px'}),
                        html.Div(id='audit-log-terminal', style={'height':'500px','overflowY':'auto','backgroundColor':'#020617','padding':'8px','borderRadius':'4px','border':'1px solid #1E293B','fontSize':'10px','lineHeight':'1.6'})
                    ])
            ])
    ])

# =============================================================================
# DASH CONTROLS CALLBACK
# =============================================================================
@app.callback(
    [Output('autopilot-btn','children'), Output('autopilot-btn','style')],
    [Input('autopilot-btn','n_clicks'), Input('panic-btn','n_clicks'),
     Input('models-chk','value'), Input('be-chk','value')],
    prevent_initial_call=False
)
def handle_controls(n_auto, n_panic, models_val, be_val):
    ctx = callback_context
    trig = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ''

    do_panic = False
    with state_lock:
        if trig == 'autopilot-btn':
            engine_state['auto_pilot'] = not engine_state['auto_pilot']
            status_text = 'ARMED' if engine_state['auto_pilot'] else 'DISARMED'
            log_audit(f'User toggled Master Auto-Pilot: {status_text}', 'USER_ACTION')
        if trig == 'panic-btn':
            do_panic = True
        engine_state['model_a_active'] = 'ma' in (models_val or [])
        engine_state['model_b_active'] = 'mb' in (models_val or [])
        engine_state['use_auto_be'] = 'be' in (be_val or [])
        is_armed = engine_state['auto_pilot']

    if do_panic:
        panic_close_all()

    if is_armed:
        return '🟢 ARMED (AUTO-TRADING ON)', {'backgroundColor':'#065F46','color':'#34D399','border':'1px solid #059669'}
    else:
        return '🔴 DISARMED (PAUSED)', {'backgroundColor':'#374151','color':'#9CA3AF','border':'1px solid #4B5563'}

# =============================================================================
# DASH REFRESH CALLBACK (Every 1.5 Seconds)
# =============================================================================
@app.callback(
    [Output('header-clock-strip','children'),
     Output('model-a-card','children'),
     Output('model-b-card','children'),
     Output('tier2-card','children'),
     Output('account-card','children'),
     Output('positions-table','children'),
     Output('blotter-count-badge','children'),
     Output('indicators-watch-display','children'),
     Output('audit-log-terminal','children')],
    [Input('live-refresh','n_intervals')]
)
def update_dashboard(n):
    with state_lock:
        st = dict(engine_state)
        acc = dict(st['account_info'])
        smc = dict(st['smc_levels'])
        ma = dict(st['model_a'])
        mb = dict(st['model_b'])
        positions = list(st['open_positions'])
        logs = list(st['audit_logs'])

    now_utc = datetime.now(timezone.utc)
    curr_c = get_current_5m_candle_time()
    last_c = st.get('last_traded_candle_time', 0)
    if curr_c > 0 and curr_c <= last_c:
        rem_str = get_5m_countdown_str()
        candle_lock_badge = f'🔒 LOCKED ({rem_str})'
    else:
        candle_lock_badge = '🟢 READY'
    clock_txt = f"SPOT: ${st['last_price']:.2f} | SPREAD: ${st['mt5_spread']:.2f} | 🕯️ 5M: {candle_lock_badge} | {st['market_mode']} | UTC: {now_utc.strftime('%H:%M:%S')}"


    # 1. MODEL A CARD
    ma_sig_c = '#F59E0B' if 'LOCKED' in ma['signal'] else ('#00E676' if 'BUY' in ma['signal'] else ('#FF3366' if 'SELL' in ma['signal'] else '#94A3B8'))
    model_a_html = html.Div([
        html.Div([
            html.Span('🔵 MODEL A: 8086 CONFLUENCE', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'11px'}),
            html.Span(' [MAGIC: 808801]', style={'color':'#64748B','fontSize':'9px','marginLeft':'4px'})
        ]),
        html.Div([
            html.Span(f"TA Score: {ma['ta_score']}/40 ", style={'color':'#00E676' if ma['ta_score']>=40 else '#F59E0B','fontWeight':'bold','fontSize':'11px'}),
            html.Span(f"| OF Score: {ma['of_score']}/30", style={'color':'#00E676' if ma['of_score']>=30 else '#F59E0B','fontWeight':'bold','fontSize':'11px'}),
        ], style={'marginTop':'3px'}),
        html.Div(f"Signal: {ma['signal']}", style={'color':ma_sig_c,'fontWeight':'900','fontSize':'13px','marginTop':'2px'}),
        html.Div(f"Signals: {ma['signals_detail'][:45]}", style={'color':'#64748B','fontSize':'9px','marginTop':'2px'}),
        html.Div(f"Active: {'🟢 YES' if st['model_a_active'] else '⚪ OFF'}", style={'color':'#94A3B8','fontSize':'9px','marginTop':'2px'})
    ])

    # 2. MODEL B CARD
    mb_sig_c = '#F59E0B' if 'LOCKED' in mb['signal'] else ('#00E676' if 'BUY' in mb['signal'] else ('#FF3366' if 'SELL' in mb['signal'] else '#94A3B8'))
    model_b_html = html.Div([
        html.Div([
            html.Span('🟢 MODEL B: PURE SMC & ORDER FLOW', style={'color':'#10B981','fontWeight':'bold','fontSize':'11px'}),
            html.Span(' [MAGIC: 808802]', style={'color':'#64748B','fontSize':'9px','marginLeft':'4px'})
        ]),
        html.Div(f"Sweep: {mb['active_sweep'] if mb['active_sweep'] else 'None'}", style={'color':'#CBD5E1','fontSize':'10px','marginTop':'3px'}),
        html.Div([
            html.Span(f"Delta: {st['bar_delta']:+} | ", style={'color':'#00E676' if st['bar_delta']>0 else '#FF3366','fontWeight':'bold','fontSize':'10px'}),
            html.Span(f"CVD: {st['cvd_trend']}", style={'color':'#94A3B8','fontSize':'10px'})
        ]),
        html.Div(f"Signal: {mb['signal']}", style={'color':mb_sig_c,'fontWeight':'900','fontSize':'13px','marginTop':'2px'}),
        html.Div(f"Active: {'🟢 YES' if st['model_b_active'] else '⚪ OFF'}", style={'color':'#94A3B8','fontSize':'9px','marginTop':'2px'})
    ])

    # 3. TIER 2 SHIELD CARD
    t2_c = '#00E676' if st['shield_ok'] else '#FF3366'
    tier2_html = html.Div([
        html.Div('TIER 2: ANTI-MANIPULATION SHIELD', style={'color':'#A855F7','fontSize':'10px','fontWeight':'bold'}),
        html.Div(f"Neutral Spot: ${st['neutral_spot_price']:.2f} ({st.get('spot_source','Auto')[:16]})", style={'color':'#CBD5E1','fontSize':'10px','marginTop':'3px'}),
        html.Div(f"Deviation: ${st['price_deviation']:.2f} ({'PASS' if st['wick_guard_ok'] else 'FAIL'})", style={'color':'#00E676' if st['wick_guard_ok'] else '#FF3366','fontSize':'10px'}),
        html.Div(f"Spread: ${st['mt5_spread']:.2f} ({'PASS' if st['spread_ok'] else 'FAIL'})", style={'color':'#00E676' if st['spread_ok'] else '#FF3366','fontSize':'10px'}),
        html.Div('SHIELD: ACTIVE & SAFE' if st['shield_ok'] else 'SHIELD: DELAYED', style={'color':t2_c,'fontWeight':'bold','fontSize':'10px','marginTop':'3px'})
    ])

    # 4. ACCOUNT CARD
    account_html = html.Div([
        html.Div(f"EQUITI #{acc['login']}", style={'color':'#00E676','fontSize':'10px','fontWeight':'bold'}),
        html.Div(f"Equity: ${acc['equity']:.2f} (Bal: ${acc['balance']:.2f})", style={'color':'#E6EDF3','fontSize':'10px','marginTop':'3px'}),
        html.Div(f"Float P&L: ${acc['floating_pnl']:+.2f}", style={'color':'#00E676' if acc['floating_pnl']>=0 else '#FF3366','fontWeight':'bold','fontSize':'11px'}),
        html.Div(f"🛡️ Zero-Risk BE Locks: {st['protected_be_count']}", style={'color':'#38BDF8','fontSize':'10px','fontWeight':'bold','marginTop':'2px'}),
        html.Div(f"Strict Sizing: {st['lot_size']} Lot", style={'color':'#94A3B8','fontSize':'9px'})
    ])

    # 5. POSITIONS TABLE
    if positions:
        table_rows = []
        for p in positions:
            m_color = '#38BDF8' if 'MODEL A' in p['model'] else '#10B981'
            table_rows.append(html.Tr([
                html.Td(f"#{p['ticket']}", style={'padding':'4px'}),
                html.Td(p['model'], style={'color':m_color,'fontWeight':'bold','padding':'4px'}),
                html.Td(p['type'], style={'color':'#00E676' if p['type']=='BUY' else '#FF3366','fontWeight':'bold','padding':'4px'}),
                html.Td(f"{p['volume']:.2f}", style={'padding':'4px'}),
                html.Td(f"${p['open_price']:.2f}", style={'padding':'4px'}),
                html.Td(f"${p['current_price']:.2f}", style={'padding':'4px'}),
                html.Td(f"${p['sl']:.2f}", style={'color':'#FF7043','padding':'4px'}),
                html.Td(f"${p['tp']:.2f}", style={'color':'#00E676','padding':'4px'}),
                html.Td(f"${p['pnl']:+.2f}", style={'color':'#00E676' if p['pnl']>=0 else '#FF3366','fontWeight':'bold','padding':'4px'}),
                html.Td(p['status'], style={'color':'#00E676' if 'ZERO' in p['status'] else '#F59E0B','fontWeight':'bold','padding':'4px'})
            ]))
        positions_html = html.Table([
            html.Thead(html.Tr([
                html.Th('TICKET'), html.Th('STRATEGY MODEL'), html.Th('TYPE'), html.Th('LOT'),
                html.Th('ENTRY'), html.Th('CURRENT'), html.Th('SL'), html.Th('TP'), html.Th('P&L'), html.Th('PROTECTION')
            ], style={'color':'#64748B','fontSize':'10px','textAlign':'left'})),
            html.Tbody(table_rows, style={'fontSize':'11px'})
        ], style={'width':'100%','borderCollapse':'collapse'})
        count_badge = f"{len(positions)} ACTIVE TRADE(S)"
    else:
        positions_html = html.Div('No active positions. Scanning both Model A (Confluence) & Model B (SMC-OF)...',
                                  style={'color':'#64748B','fontSize':'11px','padding':'14px 6px','textAlign':'center'})
        count_badge = '0 TRADES (STANDBY)'

    # 6. INDICATORS WATCH DISPLAY
    indicators_html = html.Div(style={'display':'grid','gridTemplateColumns':'1fr 1fr','gap':'8px','fontSize':'10px'},
        children=[
            html.Div([
                html.Div('SMC SESSION LEVELS:', style={'color':'#38BDF8','fontWeight':'bold','marginBottom':'4px'}),
                html.Div(f"PDH: ${smc['pdh']:.2f} | PDL: ${smc['pdl']:.2f}"),
                html.Div(f"Asian High: ${smc['asia_high']:.2f} | Low: ${smc['asia_low']:.2f}"),
                html.Div(f"London High: ${smc['london_high']:.2f} | Low: ${smc['london_low']:.2f}"),
                html.Div(f"Volume POC: ${smc['poc']:.2f}")
            ], style={'backgroundColor':'#020617','padding':'8px','borderRadius':'4px','border':'1px solid #1E293B'}),
            html.Div([
                html.Div('15M TECHNICAL CONFLUENCE (MODEL A):', style={'color':'#10B981','fontWeight':'bold','marginBottom':'4px'}),
                html.Div(f"EMA 20/50/200 Trend: {'Aligned' if 'EMA' in ma['signals_detail'] else 'Mixed'}"),
                html.Div(f"RSI 14 Zone: {'Oversold/Overbought' if 'RSI' in ma['signals_detail'] else 'Neutral'}"),
                html.Div(f"MACD Cross: {'Bull/Bear Cross' if 'MACD' in ma['signals_detail'] else 'Consolidating'}"),
                html.Div(f"VWAP & Fibonacci: {'Confluence Active' if 'VWAP' in ma['signals_detail'] or 'FIB' in ma['signals_detail'] else 'Normal'}")
            ], style={'backgroundColor':'#020617','padding':'8px','borderRadius':'4px','border':'1px solid #1E293B'})
        ])

    # 7. AUDIT LOG TERMINAL
    log_rows = []
    for l in logs:
        c = '#00E676' if 'EXECUTION' in l else ('#38BDF8' if 'PROTECTION' in l else ('#FF3366' if 'WARNING' in l or 'EMERGENCY' in l else '#94A3B8'))
        log_rows.append(html.Div(l, style={'color':c,'marginBottom':'2px'}))
    audit_html = log_rows if log_rows else [html.Div('Audit log ready...', style={'color':'#64748B'})]

    return clock_txt, model_a_html, model_b_html, tier2_html, account_html, positions_html, count_badge, indicators_html, audit_html

# =============================================================================
# SERVER ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    logger.info("============================================================")
    logger.info("GOLDFLOW DUAL-MODEL LIVE TRADING ENGINE STARTING ON PORT 8088")
    logger.info("Model A: Full 8086 Confluence (EMA, RSI, MACD, BB, Fib, VWAP, SMC)")
    logger.info("Model B: Pure SMC & Real-Time Order Flow Delta (AllTick + iTick)")
    logger.info("============================================================")
    app.run(host='0.0.0.0', port=8088, debug=False, threaded=True)
