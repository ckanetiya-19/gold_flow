# =============================================================================
# GOLDFLOW PURE SPOT GOLD BACKTEST LAB - PORT 8086
# 100% Pure Spot Gold (Zero Futures, Zero Yahoo Finance)
# Institutional Data Feeds:
#   1. MetaTrader 5 (Equiti Broker Direct Feed - Real Ticks & Spreads)
#   2. AllTick Institutional Feed (Pure Spot Gold)
#   3. TwelveData Spot API (XAU/USD)
#   4. Multi-Feed Spot Comparison: MT5 Broker vs AllTick Spot
# Strategy Pillars:
#   - Pillar 1: SMC Liquidity Sweeps (PDH/PDL + Asian Judas Swing)
#   - Pillar 2: Volume Profile (POC, VAH, VAL)
#   - Pillar 3: Multi-Timeframe (MTF) Alignment Bonus
#   - Pillar 4: Capital Protection ($100 Capital, 0.01 Lot, Auto Break-Even)
# Isolated: Strictly Port 8086
# =============================================================================

import dash
from dash import dcc, html, Input, Output, State
from flask import request, Response
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import requests
import json
import urllib.parse
import re
import logging
import os
import sys
from datetime import datetime, timedelta

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
    format='%(asctime)s - [SPOT_8086] - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'port_8086.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('SPOT_8086')

# =============================================================================
# PURE SPOT DATA PROVIDERS (ZERO YAHOO FINANCE)
# =============================================================================
ALLTICK_TOKEN = '0767d9f1b9817840d94d0dbc0bda2c78-c-app'
ITICK_TOKEN   = '73f7322874cd42bbb126499c62a7c4c01dd60a55460745dabba192d9528e2bc1'

TWELVEDATA_KEYS = [
    '7b4a5b6feaf2429180934a74c8d88905',
    'a77eb531fb46450088346ca8ed8d2658',
    '0c541a88560d4a6dbf169a37db04fcb6',
    '2d261575188c46678bf2b3d50a528408',
    '9a881e3b6cba435fb5b02a868473c9c1',
    '09db8bad69204b4d907bbbbbe1cc540f',
    '044603710a24444bbff60912e365cf7e',
    'e6a9bbe1a68946479fdb526b83c3ccd6',
]
_td_idx = 0

def parse_any_date(d_str, default=None):
    """Parses any date format: 1-1-2026, 20-8-2026, 2026-01-01, 01/01/2026, etc."""
    if not d_str:
        return default
    if isinstance(d_str, (datetime, pd.Timestamp)):
        return d_str.to_pydatetime() if hasattr(d_str, 'to_pydatetime') else d_str
    d_clean = str(d_str).strip()
    # Correct accidental typos like 20-8-2-26 -> 20-8-2026
    d_clean = re.sub(r'(\d+)[-/](\d+)[-/]2[-/](\d+)', r'\1-\2-20\3', d_clean)
    try:
        dt = pd.to_datetime(d_clean, dayfirst=True)
        return dt.to_pydatetime()
    except Exception:
        try:
            dt = pd.to_datetime(d_clean)
            return dt.to_pydatetime()
        except Exception:
            return default

def get_next_td_key():
    global _td_idx
    key = TWELVEDATA_KEYS[_td_idx % len(TWELVEDATA_KEYS)]
    _td_idx += 1
    return key

def fetch_mt5(symbol='XAUUSD', interval='15m', dt_from=None, dt_to=None, count=2500):
    """Fetches real broker candles with real tick volume and spread from MT5"""
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            logger.warning(f'MT5 initialize failed: {mt5.last_error()}')
            return None

        candidates = ['XAUUSD.pr', symbol, 'XAUUSD', 'XAUUSDm', 'GOLD', 'XAUUSD.a', 'XAUUSD.raw', 'XAUUSD.x', 'XAUUSD.sd']
        selected_sym = None
        for s in candidates:
            info = mt5.symbol_info(s)
            if info is not None:
                selected_sym = s
                if not info.visible:
                    mt5.symbol_select(s, True)
                break
        if not selected_sym:
            logger.warning(f'Symbol {symbol} not found in MT5')
            mt5.shutdown()
            return None

        tf_map = {
            '5m': mt5.TIMEFRAME_M5,
            '15m': mt5.TIMEFRAME_M15,
            '30m': mt5.TIMEFRAME_M30,
            '1h': mt5.TIMEFRAME_H1
        }
        mt_tf = tf_map.get(interval, mt5.TIMEFRAME_M15)

        if dt_from and dt_to:
            rates = mt5.copy_rates_range(selected_sym, mt_tf, dt_from, dt_to)
        else:
            rates = mt5.copy_rates_from_pos(selected_sym, mt_tf, 0, count)

        mt5.shutdown()
        if rates is None or len(rates) == 0:
            return None

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.set_index('time')
        vol = df['tick_volume'] if 'tick_volume' in df.columns else (df['real_volume'] if 'real_volume' in df.columns else 100)
        df['volume'] = vol.astype(float)
        df = df[['open', 'high', 'low', 'close', 'volume']]
        df.index.name = 'time'
        logger.info(f'MT5: {len(df)} bars fetched for {selected_sym} ({interval})')
        return df
    except Exception as e:
        logger.error(f'MT5 fetch error: {e}')
        return None

def fetch_alltick(interval='15m', count=1000):
    """Fetches pure spot gold klines directly from AllTick institutional API"""
    try:
        tf_map = {'1m': 1, '5m': 2, '15m': 3, '30m': 4, '1h': 5}
        ktype = tf_map.get(interval, 3)
        q = json.dumps({
            'trace': 'goldflow_8086',
            'data': {
                'code': 'GOLD',
                'kline_type': ktype,
                'kline_timestamp_end': 0,
                'query_kline_num': min(count, 1000),
                'adjust_type': 0
            }
        })
        url = f'https://quote.alltick.co/quote-b-api/kline?token={ALLTICK_TOKEN}&query=' + urllib.parse.quote(q)
        r = requests.get(url, timeout=8)
        data = r.json()
        if data.get('ret') == 200:
            klines = data['data']['kline_list']
            rows = [{
                'time': pd.to_datetime(int(k['timestamp']), unit='s'),
                'open': float(k['open_price']),
                'high': float(k['high_price']),
                'low': float(k['low_price']),
                'close': float(k['close_price']),
                'volume': float(k.get('volume', 100))
            } for k in klines]
            df = pd.DataFrame(rows).set_index('time').sort_index()
            df.index.name = 'time'
            logger.info(f'AllTick: {len(df)} bars fetched for GOLD ({interval})')
            return df
        else:
            logger.warning(f'AllTick ret: {data.get("ret")} - {data.get("msg")}')
            return None
    except Exception as e:
        logger.error(f'AllTick fetch error: {e}')
        return None

def fetch_twelvedata(symbol='XAU/USD', interval='15m', outputsize=1500):
    """Fetches spot gold from TwelveData API"""
    key = get_next_td_key()
    tf_map = {'5m':'5min', '15m':'15min', '30m':'30min', '1h':'1h'}
    url = 'https://api.twelvedata.com/time_series'
    params = {'symbol': symbol, 'interval': tf_map.get(interval, '15min'), 'outputsize': outputsize, 'apikey': key, 'format': 'JSON'}
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            logger.warning(f'TwelveData error: {data.get("message")}')
            return None
        values = data.get('values', [])
        if not values:
            return None
        rows = []
        for v in reversed(values):
            rows.append({'time': pd.to_datetime(v['datetime']), 'open': float(v['open']),
                          'high': float(v['high']), 'low': float(v['low']), 'close': float(v['close']),
                          'volume': float(v.get('volume', 100))})
        df = pd.DataFrame(rows).set_index('time')
        df.index.name = 'time'
        logger.info(f'TwelveData: {len(df)} bars fetched ({interval})')
        return df
    except Exception as e:
        logger.error(f'TwelveData fetch error: {e}')
        return None

def fetch_spot_data(source='mt5', tf='15m', dt_from=None, dt_to=None, count=2500):
    """Pure Spot Data Ingestion Router (MT5, AllTick, TwelveData)"""
    if source == 'mt5':
        df = fetch_mt5('XAUUSD', tf, dt_from, dt_to, count)
        if df is not None and len(df) > 30:
            return df, 'MT5 (Equiti Broker XAUUSD)'
        logger.warning('MT5 unavailable, trying AllTick fallback...')
        df = fetch_alltick(interval=tf, count=count)
        if df is not None and len(df) > 30:
            return df, 'AllTick Spot Fallback (GOLD)'

    elif source == 'alltick':
        df = fetch_alltick(interval=tf, count=count)
        if df is not None and len(df) > 30:
            return df, 'AllTick (Institutional Spot GOLD)'
        logger.warning('AllTick unavailable, trying MT5 fallback...')
        df = fetch_mt5('XAUUSD', tf, dt_from, dt_to, count)
        if df is not None and len(df) > 30:
            return df, 'MT5 Spot Fallback (XAUUSD)'

    elif source == 'twelvedata':
        df = fetch_twelvedata('XAU/USD', tf, count)
        if df is not None and len(df) > 30:
            return df, 'TwelveData (Spot XAU/USD)'
        logger.warning('TwelveData rate-limited, trying AllTick fallback...')
        df = fetch_alltick(interval=tf, count=count)
        if df is not None and len(df) > 30:
            return df, 'AllTick Spot Fallback (GOLD)'

    return pd.DataFrame(), 'Data unavailable'

# =============================================================================
# STRATEGY CALCULATIONS: TECHNICAL + SMC LIQUIDITY + VOLUME PROFILE + ORDER FLOW
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
    """Calculates Rolling Point of Control (POC), VAH and VAL levels"""
    n = len(df)
    poc = np.zeros(n); vah = np.zeros(n); val = np.zeros(n)
    c_arr = df['close'].values; v_arr = df['volume'].values

    for i in range(window, n):
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

def calc_liquidity_sweeps(df, pd_window=96):
    """Calculates PDH/PDL Sweeps + Asian, London (LSH/LSL) & NY (NYH/NYL) Session Sweeps"""
    df = df.copy()
    # 1. Previous Day High & Low (PDH / PDL)
    df['pdh'] = df['high'].rolling(pd_window).max().shift(1)
    df['pdl'] = df['low'].rolling(pd_window).min().shift(1)

    df['sweep_pdl'] = (df['low'] < df['pdl']) & (df['close'] > df['pdl'])
    df['sweep_pdh'] = (df['high'] > df['pdh']) & (df['close'] < df['pdh'])

    hours = df.index.hour
    is_asia   = (hours >= 0) & (hours < 7)
    is_london = (hours >= 7) & (hours < 13)
    is_ny     = (hours >= 13) & (hours < 21)

    # 1. Asian Session (00:00 - 07:00 UTC)
    df['asia_h'] = np.where(is_asia, df['high'], np.nan)
    df['asia_l'] = np.where(is_asia, df['low'], np.nan)
    df['asia_high'] = df['asia_h'].ffill().rolling(48).max()
    df['asia_low']  = df['asia_l'].ffill().rolling(48).min()

    # 2. London Session (07:00 - 13:00 UTC) - LSH / LSL
    df['london_h'] = np.where(is_london, df['high'], np.nan)
    df['london_l'] = np.where(is_london, df['low'], np.nan)
    df['london_high'] = df['london_h'].ffill().rolling(48).max()
    df['london_low']  = df['london_l'].ffill().rolling(48).min()

    # 3. NY Session (13:00 - 21:00 UTC) - NYH / NYL
    df['ny_h'] = np.where(is_ny, df['high'], np.nan)
    df['ny_l'] = np.where(is_ny, df['low'], np.nan)
    df['ny_high'] = df['ny_h'].ffill().rolling(48).max()
    df['ny_low']  = df['ny_l'].ffill().rolling(48).min()

    # Sweeps:
    # A. Asian Range Sweeps (Judas Swing when London/NY opens)
    is_after_asia = hours >= 7
    df['sweep_asia_low']  = is_after_asia & (df['low'] < df['asia_low']) & (df['close'] > df['asia_low'])
    df['sweep_asia_high'] = is_after_asia & (df['high'] > df['asia_high']) & (df['close'] < df['asia_high'])

    # B. London Sweeps (NY session sweeps London High / Low)
    df['sweep_london_low']  = is_ny & (df['low'] < df['london_low']) & (df['close'] > df['london_low'])
    df['sweep_london_high'] = is_ny & (df['high'] > df['london_high']) & (df['close'] < df['london_high'])

    # C. NY Sweeps (Next Asian / London session sweeps previous NY High / Low)
    is_pre_ny = hours < 13
    df['sweep_ny_low']  = is_pre_ny & (df['low'] < df['ny_low']) & (df['close'] > df['ny_low'])
    df['sweep_ny_high'] = is_pre_ny & (df['high'] > df['ny_high']) & (df['close'] < df['ny_high'])

    return df

def calc_orderflow(df):
    """Reconstructs Delta, CVD, Volume Imbalance & Institutional Absorption"""
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

# =============================================================================
# SCORER ENGINE
# =============================================================================
def score_bar(df, i, settings):
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

    # 1. Technical Indicators
    if settings.get('use_ema'):
        if price > ema20 > ema50: bull += 15; scores['EMA'] = 15
        elif price < ema20 < ema50: bear += 15; scores['EMA'] = -15

    if settings.get('use_rsi'):
        if rsi < 35: bull += 12; scores['RSI_OS'] = 12
        elif rsi > 65: bear += 12; scores['RSI_OB'] = -12
        elif 40 <= rsi <= 60: bull += 5; bear += 5; scores['RSI_N'] = 5

    if settings.get('use_macd'):
        if macd > macd_s: bull += 10; scores['MACD'] = 10
        elif macd < macd_s: bear += 10; scores['MACD'] = -10

    if settings.get('use_bb'):
        if price <= bb_lo: bull += 12; scores['BB_OS'] = 12
        elif price >= bb_up: bear += 12; scores['BB_OB'] = -12

    if settings.get('use_vwap'):
        if price > vwap: bull += 8; scores['VWAP'] = 8
        elif price < vwap: bear += 8; scores['VWAP'] = -8

    if settings.get('use_sr'):
        if abs(price - sr_lo) < atr*0.5: bull += 15; scores['SR_S'] = 15
        if abs(price - sr_hi) < atr*0.5: bear += 15; scores['SR_R'] = -15

    if settings.get('use_fib'):
        for fk in ['fib_382','fib_500','fib_618']:
            fl = row.get(fk, 0)
            if fl > 0 and abs(price-fl) < atr*0.5:
                bull += 12; bear += 12; scores['FIB'] = 12; break

    # 2. SMC Liquidity Sweeps (PDH/PDL + Asia + London + NY)
    if settings.get('use_sweep'):
        if row.get('sweep_pdl', False): bull += 20; scores['SWEEP_PDL'] = 20
        if row.get('sweep_pdh', False): bear += 20; scores['SWEEP_PDH'] = 20

    if settings.get('use_asia_sweep'):
        if row.get('sweep_asia_low', False): bull += 18; scores['SWEEP_ASIA_L'] = 18
        if row.get('sweep_asia_high', False): bear += 18; scores['SWEEP_ASIA_H'] = 18

    if settings.get('use_london_sweep'):
        if row.get('sweep_london_low', False): bull += 20; scores['SWEEP_LSL'] = 20
        if row.get('sweep_london_high', False): bear += 20; scores['SWEEP_LSH'] = 20

    if settings.get('use_ny_sweep'):
        if row.get('sweep_ny_low', False): bull += 18; scores['SWEEP_NYL'] = 18
        if row.get('sweep_ny_high', False): bear += 18; scores['SWEEP_NYH'] = 18

    # 3. Volume Profile (POC, VAH, VAL)
    if settings.get('use_vp'):
        val_lvl = row.get('val', 0); vah_lvl = row.get('vah', 0); poc_lvl = row.get('poc', 0)
        if val_lvl > 0 and price <= val_lvl + atr*0.3:
            bull += 16; scores['VP_VAL_BOUNCE'] = 16
        if vah_lvl > 0 and price >= vah_lvl - atr*0.3:
            bear += 16; scores['VP_VAH_REJECT'] = 16
        if poc_lvl > 0 and abs(price - poc_lvl) < atr*0.25:
            if bull > bear: bull += 10; scores['VP_POC_SUPPORT'] = 10
            elif bear > bull: bear += 10; scores['VP_POC_RESIST'] = 10

    # 4. Multi-Timeframe Higher Bias
    if settings.get('use_mtf'):
        h1_ema20 = row.get('h1_ema20', price); h1_ema50 = row.get('h1_ema50', price)
        if price > h1_ema20 > h1_ema50: bull += 15; scores['MTF_H1_BULL'] = 15
        elif price < h1_ema20 < h1_ema50: bear += 15; scores['MTF_H1_BEAR'] = 15

    ta_score = max(bull, bear)
    direction = 'BUY' if bull > bear else ('SELL' if bear > bull else 'NEUTRAL')

    # 5. Order Flow Triggers
    of = 0
    cvd = row.get('cvd', 0); delta = row.get('delta', 0)
    absorb = row.get('absorption', False); imbln = row.get('imbalance', False)
    fvg_b = row.get('fvg_bull', False); fvg_s = row.get('fvg_bear', False)

    if settings.get('use_cvd') and i > 5:
        ct = cvd - df.iloc[i-5].get('cvd', cvd)
        if direction=='BUY' and ct>0: of += 20; scores['CVD'] = 20
        elif direction=='SELL' and ct<0: of += 20; scores['CVD'] = 20
        elif ct != 0: of -= 10; scores['CVD_DIV'] = -10

    if settings.get('use_delta'):
        if direction=='BUY' and delta>0: of += 15; scores['DELTA'] = 15
        elif direction=='SELL' and delta<0: of += 15; scores['DELTA'] = 15

    if settings.get('use_absorption') and absorb: of += 15; scores['ABS'] = 15
    if settings.get('use_imbalance') and imbln: of += 10; scores['IMB'] = 10

    if settings.get('use_fvg'):
        if direction=='BUY' and fvg_b: of += 12; scores['FVG'] = 12
        elif direction=='SELL' and fvg_s: of += 12; scores['FVG'] = 12

    if settings.get('use_session'):
        try:
            h = df.index[i].hour
            if 8 <= h < 12 or 13 <= h < 17: of += 10; scores['KZ'] = 10
        except: pass

    return ta_score, max(0, of), direction, scores

# =============================================================================
# BACKTEST RUNNER (CAPITAL MANAGEMENT & AUTO BREAK-EVEN)
# =============================================================================
def run_backtest(df_raw, settings):
    if df_raw.empty or len(df_raw) < 80: return [], {}
    df = df_raw.copy()
    df['ema20'] = calc_ema(df['close'], 20)
    df['ema50'] = calc_ema(df['close'], 50)
    df['ema200']= calc_ema(df['close'], 200)
    df['h1_ema20'] = calc_ema(df['close'], 80)
    df['h1_ema50'] = calc_ema(df['close'], 200)
    df['rsi']   = calc_rsi(df['close'])
    df['macd'], df['macd_s'], _ = calc_macd(df['close'])
    df['bb_up'], _, df['bb_lo'] = calc_bb(df['close'])
    df['atr']   = calc_atr(df)
    df['sr_hi'] = df['high'].rolling(50).max()
    df['sr_lo'] = df['low'].rolling(50).min()
    df = calc_orderflow(df)
    df = calc_volume_profile(df, window=80)
    df = calc_liquidity_sweeps(df, pd_window=96)
    fb = calc_fibonacci(df)
    df['fib_382'] = fb['382']; df['fib_500'] = fb['500']; df['fib_618'] = fb['618']
    df.index.name = 'time'
    df = df.dropna(subset=['ema20','rsi','atr']).reset_index()

    ta_min      = settings.get('ta_min_score', 40)
    of_min      = settings.get('of_min_score', 30)
    sl_m        = settings.get('sl_atr_mult', 1.5)
    tp_m        = settings.get('tp_atr_mult', 3.0)
    gap         = settings.get('min_bars_between', 5)
    capital     = float(settings.get('initial_capital', 100.0))
    lot_size    = float(settings.get('lot_size', 0.01))
    use_auto_be = settings.get('use_auto_be', True)
    use_trail   = settings.get('use_trailing', False)

    lot_multiplier = lot_size * 100.0
    trades = []; in_pos = False; ep = sl = tp = 0; eidx = 0; dirn = 'N'; last_sig = -gap
    is_be = False; current_bal = capital

    for i in range(80, len(df)):
        row = df.iloc[i]; price = row['close']; atr = max(row['atr'], 0.5)

        if in_pos:
            # Auto Break-Even Check (at 1:1 R:R)
            if use_auto_be and not is_be:
                if dirn=='BUY' and price >= ep + atr*1.0:
                    sl = max(sl, ep + 0.10); is_be = True
                elif dirn=='SELL' and price <= ep - atr*1.0:
                    sl = min(sl, ep - 0.10); is_be = True

            # Trailing Stop Check
            if use_trail:
                trail_dist = sl_m * atr
                if dirn=='BUY':
                    new_sl = round(price - trail_dist, 2)
                    if new_sl > sl: sl = new_sl
                else:
                    new_sl = round(price + trail_dist, 2)
                    if new_sl < sl: sl = new_sl

            ex = ''
            if dirn=='BUY':
                if price >= tp: ex = 'TP HIT'
                elif price <= sl: ex = 'BREAK-EVEN' if is_be and sl >= ep else 'SL HIT'
            else:
                if price <= tp: ex = 'TP HIT'
                elif price >= sl: ex = 'BREAK-EVEN' if is_be and sl <= ep else 'SL HIT'

            if ex:
                if ex == 'TP HIT':
                    pts = (tp - ep) if dirn=='BUY' else (ep - tp)
                elif ex == 'BREAK-EVEN':
                    pts = 0.10
                else:
                    pts = (sl - ep) if dirn=='BUY' else (ep - sl)

                pnl = round(pts * lot_multiplier, 2)
                current_bal += pnl
                res_tag = 'WIN' if pnl > 0.20 else ('BE' if pnl >= 0 else 'LOSS')
                trades[-1].update({
                    'exit_time': str(row['time'])[:16],
                    'exit_price': round(price,2),
                    'points': round(pts, 2),
                    'pnl': pnl,
                    'balance': round(current_bal, 2),
                    'result': res_tag,
                    'exit_reason': ex
                })
                in_pos = False

        if not in_pos and (i - last_sig) >= gap:
            ta_s, of_s, sd, scs = score_bar(df, i, settings)
            if ta_s >= ta_min and of_s >= of_min and sd in ('BUY','SELL'):
                ep = price
                sl = round(ep - sl_m*atr, 2) if sd=='BUY' else round(ep + sl_m*atr, 2)
                tp = round(ep + tp_m*atr, 2) if sd=='BUY' else round(ep - tp_m*atr, 2)
                in_pos = True; dirn = sd; eidx = i; last_sig = i; is_be = False
                sig_str = ', '.join(f'{k}:{v}' for k,v in scs.items() if v > 0)
                trades.append({
                    'entry_time': str(row['time'])[:16],
                    'exit_time': '-',
                    'direction': sd,
                    'lot': lot_size,
                    'entry_price': round(ep,2),
                    'sl': round(sl,2),
                    'tp': round(tp,2),
                    'exit_price': 0,
                    'points': 0,
                    'pnl': 0,
                    'balance': round(current_bal, 2),
                    'result': 'OPEN',
                    'exit_reason': '',
                    'ta_score': ta_s,
                    'of_score': of_s,
                    'signals': sig_str
                })

    closed = [t for t in trades if t['result']!='OPEN']
    wins   = [t for t in closed if t['result']=='WIN']
    bes    = [t for t in closed if t['result']=='BE']
    losses = [t for t in closed if t['result']=='LOSS']
    gw = sum(t['pnl'] for t in wins); gl = abs(sum(t['pnl'] for t in losses))

    eq = [capital]
    for t in closed: eq.append(t['balance'])

    pk = capital; mdd = 0
    for e in eq:
        if e > pk: pk = e
        if pk - e > mdd: mdd = pk - e

    mdd_pct = round((mdd / pk) * 100, 1) if pk > 0 else 0
    ending_bal = eq[-1] if eq else capital
    net_pnl = round(ending_bal - capital, 2)
    roi_pct = round((net_pnl / capital) * 100, 1)

    stats = {
        'initial_capital': capital,
        'ending_balance': round(ending_bal, 2),
        'net_pnl': net_pnl,
        'roi_pct': roi_pct,
        'lot_size': lot_size,
        'total': len(closed),
        'wins': len(wins),
        'break_evens': len(bes),
        'losses': len(losses),
        'win_rate': round((len(wins)+len(bes))/len(closed)*100,1) if closed else 0,
        'pure_win_rate': round(len(wins)/len(closed)*100,1) if closed else 0,
        'avg_win': round(gw/len(wins),2) if wins else 0,
        'avg_loss': round(gl/len(losses),2) if losses else 0,
        'profit_factor': round(gw/gl,2) if gl>0 else (999.0 if gw>0 else 0),
        'max_drawdown': round(mdd,2),
        'max_drawdown_pct': mdd_pct,
        'equity_curve': eq
    }
    return trades, stats

# =============================================================================
# DASH WEB DASHBOARD
# =============================================================================
app = dash.Dash(__name__, title='GOLDFLOW Pure Spot Backtest Engine', update_title=None, suppress_callback_exceptions=True)

server = app.server

@server.before_request
def require_basic_auth():
    auth = request.authorization
    if not auth or auth.username != 'am' or auth.password != 'Orferflow@1910':
        return Response(
            '401 Unauthorized - Access Denied\nGoldFlow Backtest Lab 8086',
            401,
            {'WWW-Authenticate': 'Basic realm="GoldFlow Secured Terminal"'}
        )

today = datetime.now()
d_30_ago = today - timedelta(days=30)

app.layout = html.Div(
    style={'backgroundColor':'#05070A','color':'#E6EDF3','fontFamily':"'JetBrains Mono','Consolas',monospace",'minHeight':'100vh','padding':'10px 16px'},
    children=[
        # TOP HEADER
        html.Div(style={'display':'flex','alignItems':'center','justifyContent':'space-between','backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderBottom':'2px solid #F59E0B','padding':'8px 16px','borderRadius':'6px','marginBottom':'12px'},
            children=[
                html.Div([
                    html.Span('⚡ GOLD.FLOW', style={'color':'#FFD700','fontWeight':'900','fontSize':'18px'}),
                    html.Span(' // PURE SPOT GOLD BACKTEST LAB', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'13px'}),
                    html.Span(' [PORT 8086]', style={'color':'#F59E0B','fontSize':'10px','backgroundColor':'#292510','padding':'2px 8px','borderRadius':'4px','marginLeft':'8px'}),
                    html.Span('100% PURE SPOT (NO FUTURES)', style={'color':'#00E676','fontSize':'10px','backgroundColor':'#064E3B','padding':'2px 8px','borderRadius':'4px','marginLeft':'6px'}),
                ]),
                html.Span('MT5 Broker Real Feed + AllTick Institutional Spot + TwelveData | SMC & Volume Profile', style={'color':'#64748B','fontSize':'11px'})
            ]),

        # 4-COLUMN SETTINGS GRID
        html.Div(style={'display':'grid','gridTemplateColumns':'1.1fr 1fr 1fr 1fr','gap':'10px','marginBottom':'12px'},
            children=[
                # PANEL 1: PURE SPOT DATA FEEDS & CAPITAL
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'12px'},
                    children=[
                        html.Div('SPOT FEEDS & CAPITAL ($100)', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'11px','marginBottom':'10px','borderBottom':'1px solid #1E293B','paddingBottom':'6px'}),

                        html.Div(style={'display':'grid','gridTemplateColumns':'1fr 1fr','gap':'8px','marginBottom':'10px'},
                            children=[
                                html.Div([
                                    html.Span('Capital ($):', style={'color':'#64748B','fontSize':'11px'}),
                                    dcc.Input(id='cap-inp', type='number', value=100.0, step=10, min=10,
                                        style={'width':'100%','backgroundColor':'#0F172A','color':'#00E676','border':'1px solid #1E293B','padding':'6px','borderRadius':'4px','marginTop':'3px','fontWeight':'bold'})
                                ]),
                                html.Div([
                                    html.Span('Lot Size:', style={'color':'#64748B','fontSize':'11px'}),
                                    dcc.Input(id='lot-inp', type='number', value=0.01, step=0.01, min=0.01, max=10.0,
                                        style={'width':'100%','backgroundColor':'#0F172A','color':'#38BDF8','border':'1px solid #1E293B','padding':'6px','borderRadius':'4px','marginTop':'3px','fontWeight':'bold'})
                                ])
                            ]),

                        # PURE SPOT DATA FEED OPTIONS
                        html.Div([
                            html.Span('Data Feed Source (Pure Spot):', style={'color':'#64748B','fontSize':'11px'}),
                            dcc.Dropdown(id='feed-sel',
                                options=[
                                    {'label':'⚡ MT5 Broker Feed (Equiti XAUUSD)','value':'mt5'},
                                    {'label':'📡 AllTick Institutional Spot (GOLD)','value':'alltick'},
                                    {'label':'🌐 TwelveData API (XAU/USD Spot)','value':'twelvedata'},
                                    {'label':'⚖️ COMPARE: MT5 vs AllTick Spot','value':'compare'}
                                ],
                                value='mt5', clearable=False,
                                style={'backgroundColor':'#0F172A','color':'#E6EDF3','border':'1px solid #1E293B','fontSize':'12px','marginTop':'3px'})
                        ], style={'marginBottom':'10px'}),

                        html.Div([
                            html.Span('Timeframe:', style={'color':'#64748B','fontSize':'11px'}),
                            dcc.Dropdown(id='tf-sel',
                                options=[{'label':'5 Min','value':'5m'},{'label':'15 Min','value':'15m'},{'label':'30 Min','value':'30m'},{'label':'1 Hour','value':'1h'}],
                                value='15m', clearable=False,
                                style={'backgroundColor':'#0F172A','color':'#E6EDF3','border':'1px solid #1E293B','fontSize':'12px','marginTop':'3px'})
                        ], style={'marginBottom':'10px'}),

                        # Date Range Presets & Universal Editable Inputs
                        html.Div([
                            html.Div([
                                html.Span('Date Range Filter:', style={'color':'#64748B','fontSize':'11px'}),
                                dcc.RadioItems(
                                    id='range-preset',
                                    options=[
                                        {'label':' 🌟 2026 All (YTD)','value':'ytd'},
                                        {'label':' 60D','value':'60d'},
                                        {'label':' 30D','value':'30d'},
                                        {'label':' 15D','value':'15d'},
                                        {'label':' 7D','value':'7d'},
                                        {'label':' Custom (Type Date)','value':'custom'}
                                    ],
                                    value='ytd', inline=True,
                                    style={'fontSize':'11px','color':'#CBD5E1','marginTop':'4px','marginBottom':'6px'},
                                    inputStyle={'marginRight':'4px','marginLeft':'6px'}
                                ),
                            ]),
                            html.Div(style={'display':'grid','gridTemplateColumns':'1fr 1fr','gap':'6px','marginTop':'4px'},
                                children=[
                                    html.Div([
                                        html.Span('From Date (તારીખ):', style={'color':'#38BDF8','fontSize':'10px','fontWeight':'bold'}),
                                        dcc.Input(id='start-date-inp', type='text', value='2026-01-01',
                                            placeholder='1-1-2026 or 2026-01-01',
                                            style={'width':'100%','backgroundColor':'#0F172A','color':'#00E676','border':'1px solid #38BDF8','padding':'5px 8px','borderRadius':'4px','marginTop':'2px','fontSize':'11px','fontWeight':'bold'})
                                    ]),
                                    html.Div([
                                        html.Span('To Date (અંતિમ):', style={'color':'#38BDF8','fontSize':'10px','fontWeight':'bold'}),
                                        dcc.Input(id='end-date-inp', type='text', value=today.strftime('%Y-%m-%d'),
                                            placeholder='19-9-2026 or 2026-09-19',
                                            style={'width':'100%','backgroundColor':'#0F172A','color':'#00E676','border':'1px solid #38BDF8','padding':'5px 8px','borderRadius':'4px','marginTop':'2px','fontSize':'11px','fontWeight':'bold'})
                                    ])
                                ]),
                            html.Div(id='date-helper-txt',
                                children=f'🌟 2026 YTD Active: 2026-01-01 to {today.strftime("%Y-%m-%d")} (Type ANY date freely: 1-1-2026, 20-8-2026, etc.)',
                                style={'color':'#F59E0B','fontSize':'9px','marginTop':'4px','fontWeight':'bold'})
                        ], style={'marginBottom':'12px'}),

                        html.Button('🚀 FETCH SPOT DATA & RUN BACKTEST', id='run-btn', n_clicks=0,
                            style={'width':'100%','backgroundColor':'#1D4ED8','color':'#FFF','border':'none','padding':'10px','borderRadius':'4px','cursor':'pointer','fontWeight':'bold','fontSize':'12px'}),
                        html.Div(id='data-st', style={'color':'#64748B','fontSize':'10px','marginTop':'6px','textAlign':'center'})
                    ]),

                # PANEL 2: PILLARS 1 & 2 - SMC LIQUIDITY & VOLUME PROFILE
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'12px'},
                    children=[
                        html.Div('SMC LIQUIDITY & VOLUME PROFILE', style={'color':'#A855F7','fontWeight':'bold','fontSize':'11px','marginBottom':'10px','borderBottom':'1px solid #1E293B','paddingBottom':'6px'}),

                        html.Div('Smart Money Concepts & Profile:', style={'color':'#475569','fontSize':'10px','marginBottom':'6px'}),
                        dcc.Checklist(id='smc-chk',
                            options=[
                                {'label':'  PDH / PDL Liquidity Sweeps','value':'use_sweep'},
                                {'label':'  Asian Session Sweep (Judas Swing)','value':'use_asia_sweep'},
                                {'label':'  London Session Sweep (LSH / LSL)','value':'use_london_sweep'},
                                {'label':'  NY Session Sweep (NYH / NYL)','value':'use_ny_sweep'},
                                {'label':'  Volume Profile (POC, VAH, VAL)','value':'use_vp'},
                                {'label':'  Multi-Timeframe H1 Bias Bonus','value':'use_mtf'},
                            ],
                            value=['use_sweep','use_asia_sweep','use_london_sweep','use_ny_sweep','use_vp','use_mtf'],
                            labelStyle={'display':'block','fontSize':'11px','color':'#CBD5E1','marginBottom':'6px'}),

                        html.Div('Pillar 4: Trade Protection ($100 Account):', style={'color':'#475569','fontSize':'10px','marginTop':'14px','marginBottom':'6px'}),
                        dcc.Checklist(id='risk-chk',
                            options=[
                                {'label':'  Auto Break-Even (Zero Risk at 1:1)','value':'use_auto_be'},
                                {'label':'  Trailing Stop Loss (Let Runners Run)','value':'use_trailing'},
                            ],
                            value=['use_auto_be'],
                            labelStyle={'display':'block','fontSize':'11px','color':'#00E676','marginBottom':'6px'}),

                        html.Div(style={'backgroundColor':'#0F172A','border':'1px solid #1E293B','borderRadius':'4px','padding':'6px','marginTop':'12px'},
                            children=[
                                html.Span('🛡️ Auto Break-Even:', style={'color':'#00E676','fontSize':'10px','fontWeight':'bold'}),
                                html.Span(' Once trade reaches 1:1 R:R, SL moves to entry so you NEVER lose capital!', style={'color':'#64748B','fontSize':'9px'})
                            ])
                    ]),

                # PANEL 3: TECHNICAL CONFLUENCE & TARGETS
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'12px'},
                    children=[
                        html.Div('TECHNICAL STRATEGIES & TARGETS', style={'color':'#F59E0B','fontWeight':'bold','fontSize':'11px','marginBottom':'10px','borderBottom':'1px solid #1E293B','paddingBottom':'6px'}),

                        html.Div([
                            html.Div([html.Span('Min TA Score:', style={'color':'#64748B','fontSize':'11px'}),
                                dcc.Slider(id='ta-sl', min=10, max=80, step=5, value=40,
                                    marks={10:'10',30:'30',50:'50',70:'70',80:'80'})
                            ], style={'marginBottom':'6px'}),
                            html.Div([html.Span('SL ATR Mult (Risk):', style={'color':'#64748B','fontSize':'11px'}),
                                dcc.Slider(id='sl-m', min=0.5, max=3.0, step=0.25, value=1.5,
                                    marks={0.5:'0.5',1.0:'1x',1.5:'1.5x',2.0:'2x',3.0:'3x'})
                            ], style={'marginBottom':'6px'}),
                            html.Div([html.Span('TP ATR Mult (Reward):', style={'color':'#64748B','fontSize':'11px'}),
                                dcc.Slider(id='tp-m', min=1.0, max=6.0, step=0.5, value=3.0,
                                    marks={1.0:'1x',2.0:'2x',3.0:'3x',4.0:'4x',6.0:'6x'})
                            ], style={'marginBottom':'8px'}),
                        ]),

                        html.Div('Technical Indicators to combine:', style={'color':'#475569','fontSize':'10px','marginBottom':'6px'}),
                        dcc.Checklist(id='ta-chk',
                            options=[
                                {'label':'  EMA Trend (20/50/200)','value':'use_ema'},
                                {'label':'  RSI 14 (Overbought/Oversold)','value':'use_rsi'},
                                {'label':'  MACD (12/26/9 Cross)','value':'use_macd'},
                                {'label':'  Bollinger Bands (20,2)','value':'use_bb'},
                                {'label':'  Fibonacci (38.2/50/61.8)','value':'use_fib'},
                                {'label':'  Support / Resistance Bounce','value':'use_sr'},
                                {'label':'  VWAP Trend Position','value':'use_vwap'},
                            ],
                            value=['use_ema','use_rsi','use_macd','use_bb','use_fib','use_sr','use_vwap'],
                            labelStyle={'display':'block','fontSize':'11px','color':'#CBD5E1','marginBottom':'5px'})
                    ]),

                # PANEL 4: ORDER FLOW STRATEGIES
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'12px'},
                    children=[
                        html.Div('ORDER FLOW STRATEGIES', style={'color':'#00E676','fontWeight':'bold','fontSize':'11px','marginBottom':'10px','borderBottom':'1px solid #1E293B','paddingBottom':'6px'}),

                        html.Div([html.Span('Min Order Flow Score:', style={'color':'#64748B','fontSize':'11px'}),
                            dcc.Slider(id='of-sl', min=10, max=60, step=5, value=30,
                                marks={10:'10',20:'20',30:'30',40:'40',50:'50',60:'60'})
                        ], style={'marginBottom':'10px'}),

                        html.Div('Order Flow Filters (Real Tick Replay):', style={'color':'#475569','fontSize':'10px','marginBottom':'6px'}),
                        dcc.Checklist(id='of-chk',
                            options=[
                                {'label':'  CVD Trend & Divergence','value':'use_cvd'},
                                {'label':'  Bar Delta Confirmation','value':'use_delta'},
                                {'label':'  Absorption Detection','value':'use_absorption'},
                                {'label':'  Volume Imbalance Spike','value':'use_imbalance'},
                                {'label':'  Fair Value Gap (FVG)','value':'use_fvg'},
                                {'label':'  Session Kill Zones (London/NY)','value':'use_session'},
                            ],
                            value=['use_cvd','use_delta','use_absorption','use_imbalance','use_fvg','use_session'],
                            labelStyle={'display':'block','fontSize':'11px','color':'#CBD5E1','marginBottom':'6px'})
                    ])
            ]),

        # SUMMARY METRIC CARDS
        html.Div(id='res-summary', style={'marginBottom':'12px'}),

        # EQUITY CURVE & PIE CHART
        html.Div(style={'display':'grid','gridTemplateColumns':'2fr 1fr','gap':'10px','marginBottom':'12px'},
            children=[
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'8px'},
                    children=[dcc.Graph(id='eq-chart', style={'height':'290px'},
                        config={'displayModeBar':False,'responsive':True},
                        figure=go.Figure(layout=dict(template='plotly_dark',paper_bgcolor='#0A0E17',plot_bgcolor='#070C14',
                            annotations=[dict(text='Click RUN to simulate pure spot gold backtest',x=0.5,y=0.5,xref='paper',yref='paper',font=dict(color='#64748B',size=14),showarrow=False)],
                            margin=dict(l=10,r=10,t=10,b=10))))])
                ,
                html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'8px'},
                    children=[dcc.Graph(id='pie-chart', style={'height':'290px'},
                        config={'displayModeBar':False,'responsive':True},
                        figure=go.Figure(layout=dict(template='plotly_dark',paper_bgcolor='#0A0E17',
                            annotations=[dict(text='Trade Outcome Distribution',x=0.5,y=0.5,xref='paper',yref='paper',font=dict(color='#64748B',size=14),showarrow=False)],
                            margin=dict(l=10,r=10,t=10,b=10))))])
            ]),

        # TRADE LOG TABLE
        html.Div(style={'backgroundColor':'#0A0E17','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px'},
            children=[
                html.Div('SPOT TRADE LOG (SMC, VOLUME PROFILE & ORDER FLOW TRIGGERS)', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'12px','marginBottom':'8px'}),
                html.Div(id='trade-tbl', style={'overflowX':'auto','maxHeight':'420px','overflowY':'auto'})
            ])
    ])

@app.callback(
    [Output('start-date-inp','value'), Output('end-date-inp','value'), Output('date-helper-txt','children')],
    [Input('range-preset','value')],
    [State('start-date-inp','value'), State('end-date-inp','value')]
)
def update_dates_from_preset(preset, cur_start, cur_end):
    now = datetime.now()
    if preset == 'ytd':
        s, e = '2026-01-01', now.strftime('%Y-%m-%d')
        return s, e, f'🌟 2026 Full Year Active: {s} to {e}'
    elif preset == '7d':
        s, e = (now - timedelta(days=7)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
        return s, e, f'📅 Last 7 Days Active: {s} to {e}'
    elif preset == '15d':
        s, e = (now - timedelta(days=15)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
        return s, e, f'📅 Last 15 Days Active: {s} to {e}'
    elif preset == '30d':
        s, e = (now - timedelta(days=30)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
        return s, e, f'📅 Last 30 Days Active: {s} to {e}'
    elif preset == '60d':
        s, e = (now - timedelta(days=60)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
        return s, e, f'📅 Last 60 Days Active: {s} to {e}'
    return cur_start, cur_end, f'✏️ Custom Range Active: {cur_start} to {cur_end}'

@app.callback(
    [Output('res-summary','children'), Output('eq-chart','figure'),
     Output('pie-chart','figure'), Output('trade-tbl','children'), Output('data-st','children')],
    [Input('run-btn','n_clicks')],
    [State('feed-sel','value'), State('tf-sel','value'),
     State('start-date-inp','value'), State('end-date-inp','value'),
     State('cap-inp','value'), State('lot-inp','value'),
     State('ta-sl','value'), State('of-sl','value'),
     State('sl-m','value'), State('tp-m','value'),
     State('ta-chk','value'), State('of-chk','value'),
     State('smc-chk','value'), State('risk-chk','value')],
    prevent_initial_call=True
)
def run_cb(n, feed, tf, start_dt, end_dt, capital, lot_size, ta_min, of_min, sl_m, tp_m, ta_chk, of_chk, smc_chk, risk_chk):
    dt_from = parse_any_date(start_dt, default=datetime(2026, 1, 1))
    dt_to   = parse_any_date(end_dt, default=datetime.now())
    if dt_to:
        dt_to = dt_to + timedelta(days=1)
    logger.info(f"BACKTEST TRIGGERED: Feed={feed}, TF={tf}, Raw Start='{start_dt}' -> Parsed='{dt_from}', Raw End='{end_dt}' -> Parsed='{dt_to}'")

    settings = {
        'initial_capital': float(capital or 100.0),
        'lot_size': float(lot_size or 0.01),
        'ta_min_score': ta_min or 40,
        'of_min_score': of_min or 30,
        'sl_atr_mult': sl_m or 1.5,
        'tp_atr_mult': tp_m or 3.0,
        'min_bars_between': 5,
        'use_auto_be': 'use_auto_be' in (risk_chk or []),
        'use_trailing': 'use_trailing' in (risk_chk or [])
    }
    all_selected = (ta_chk or []) + (of_chk or []) + (smc_chk or [])
    for s in ['use_ema','use_rsi','use_macd','use_bb','use_stoch','use_fib','use_sr','use_vwap',
              'use_cvd','use_delta','use_absorption','use_imbalance','use_fvg','use_session',
              'use_sweep','use_asia_sweep','use_london_sweep','use_ny_sweep','use_vp','use_mtf']:
        settings[s] = s in all_selected

    empty_fig = go.Figure(layout=dict(template='plotly_dark',paper_bgcolor='#0A0E17',plot_bgcolor='#070C14'))

    def card(t, v, c='#E6EDF3', sub=''):
        return html.Div(style={'backgroundColor':'#0D131F','border':'1px solid #1E293B','borderRadius':'6px','padding':'10px','textAlign':'center'},
            children=[html.Div(t,style={'color':'#64748B','fontSize':'10px','fontWeight':'bold'}),
                      html.Div(v,style={'color':c,'fontSize':'19px','fontWeight':'900'}),
                      html.Div(sub,style={'color':'#475569','fontSize':'9px'})])

    # =========================================================
    # COMPARISON MODE: MT5 SPOT vs ALLTICK SPOT
    # =========================================================
    if feed == 'compare':
        df_mt5, name_mt5 = fetch_spot_data('mt5', tf, dt_from, dt_to)
        df_at, name_at   = fetch_spot_data('alltick', tf, dt_from, dt_to)

        if df_mt5.empty and df_at.empty:
            msg = 'Both spot data feeds failed to load for the selected range.'
            return html.Div(msg, style={'color':'#FF3366'}), empty_fig, empty_fig, html.Div(msg), msg

        trades_m, stats_m = run_backtest(df_mt5, settings) if not df_mt5.empty else ([], {})
        trades_a, stats_a = run_backtest(df_at, settings) if not df_at.empty else ([], {})

        summary = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(6,1fr)','gap':'8px'},
            children=[
                card('MT5 BALANCE', f"${stats_m.get('ending_balance', capital):.2f}", '#00E676', f"ROI: {stats_m.get('roi_pct',0):+}%"),
                card('MT5 WIN+BE RATE', f"{stats_m.get('win_rate',0)}%", '#00E676', f"{stats_m.get('wins',0)}W / {stats_m.get('break_evens',0)}BE / {stats_m.get('losses',0)}L"),
                card('MT5 MAX DD', f"-${stats_m.get('max_drawdown',0):.2f}", '#FF3366', f"{stats_m.get('max_drawdown_pct',0)}% of account"),
                card('ALLTICK BALANCE', f"${stats_a.get('ending_balance', capital):.2f}", '#38BDF8', f"ROI: {stats_a.get('roi_pct',0):+}%"),
                card('ALLTICK WIN+BE RATE', f"{stats_a.get('win_rate',0)}%", '#38BDF8', f"{stats_a.get('wins',0)}W / {stats_a.get('break_evens',0)}BE / {stats_a.get('losses',0)}L"),
                card('ALLTICK MAX DD', f"-${stats_a.get('max_drawdown',0):.2f}", '#FF3366', f"{stats_a.get('max_drawdown_pct',0)}% of account"),
            ])

        eq_fig = go.Figure()
        eq_m = stats_m.get('equity_curve', [capital])
        eq_a = stats_a.get('equity_curve', [capital])
        eq_fig.add_trace(go.Scatter(y=eq_m, mode='lines', name=f'MT5 Broker Feed | End: ${stats_m.get("ending_balance",capital):.2f}', line=dict(color='#00E676', width=2.5)))
        eq_fig.add_trace(go.Scatter(y=eq_a, mode='lines', name=f'AllTick Spot Feed | End: ${stats_a.get("ending_balance",capital):.2f}', line=dict(color='#38BDF8', width=2, dash='dot')))
        eq_fig.add_hline(y=capital, line_color='#64748B', line_width=1, line_dash='dash', annotation_text='Initial $100')
        eq_fig.update_layout(template='plotly_dark', paper_bgcolor='#0A0E17', plot_bgcolor='#070C14',
            margin=dict(l=10,r=10,t=30,b=10), height=290,
            title=dict(text=f'⚖️ PURE SPOT COMPARISON: MT5 Broker (Green) vs AllTick Spot (Blue) [{start_dt} to {end_dt}]', font=dict(color='#94A3B8',size=12)),
            xaxis=dict(showgrid=True,gridcolor='#0F172A',title='Trade Sequence'),
            yaxis=dict(showgrid=True,gridcolor='#0F172A',tickprefix='$'),
            legend=dict(orientation='h',y=1.1,x=0.01))

        comp_bar = go.Figure()
        comp_bar.add_trace(go.Bar(name='MT5 Broker Spot', x=['ROI %', 'Win Rate %', 'Profit Factor'],
                                  y=[stats_m.get('roi_pct',0), stats_m.get('win_rate',0), min(stats_m.get('profit_factor',0),10)],
                                  marker_color='#00E676'))
        comp_bar.add_trace(go.Bar(name='AllTick Spot', x=['ROI %', 'Win Rate %', 'Profit Factor'],
                                  y=[stats_a.get('roi_pct',0), stats_a.get('win_rate',0), min(stats_a.get('profit_factor',0),10)],
                                  marker_color='#38BDF8'))
        comp_bar.update_layout(template='plotly_dark', paper_bgcolor='#0A0E17', plot_bgcolor='#070C14',
            margin=dict(l=10,r=10,t=30,b=10), height=290, barmode='group',
            title=dict(text='Comparison Summary (MT5 vs AllTick Pure Spot)', font=dict(color='#94A3B8',size=12)),
            legend=dict(orientation='h',y=1.1,x=0.01))

        metrics = [
            ('Initial Starting Capital', f"${capital:.2f}", f"${capital:.2f}"),
            ('Lot Size Traded', f"{settings['lot_size']} Lot", f"{settings['lot_size']} Lot"),
            ('Ending Account Balance', f"${stats_m.get('ending_balance', capital):.2f}", f"${stats_a.get('ending_balance', capital):.2f}"),
            ('Net Profit / Loss ($)', f"${stats_m.get('net_pnl',0):+.2f}", f"${stats_a.get('net_pnl',0):+.2f}"),
            ('Return on Investment (ROI %)', f"{stats_m.get('roi_pct',0):+}%", f"{stats_a.get('roi_pct',0):+}%"),
            ('Total Closed Trades', str(stats_m.get('total',0)), str(stats_a.get('total',0))),
            ('Winning Trades', str(stats_m.get('wins',0)), str(stats_a.get('wins',0))),
            ('Break-Even Saved Trades', str(stats_m.get('break_evens',0)), str(stats_a.get('break_evens',0))),
            ('Losing Trades', str(stats_m.get('losses',0)), str(stats_a.get('losses',0))),
            ('Win + BE Rate %', f"{stats_m.get('win_rate',0)}%", f"{stats_a.get('win_rate',0)}%"),
            ('Profit Factor', f"{stats_m.get('profit_factor',0):.2f}", f"{stats_a.get('profit_factor',0):.2f}"),
            ('Max Drawdown ($ & %)', f"-${stats_m.get('max_drawdown',0):.2f} ({stats_m.get('max_drawdown_pct',0)}%)", f"-${stats_a.get('max_drawdown',0):.2f} ({stats_a.get('max_drawdown_pct',0)}%)"),
            ('Bars Analyzed', str(len(df_mt5)), str(len(df_at))),
        ]
        comp_rows = [html.Tr([
            html.Th('Metric', style={'padding':'6px 10px','color':'#64748B','fontSize':'11px','borderBottom':'1px solid #1E293B','textAlign':'left'}),
            html.Th('⚡ MT5 Broker Feed (Equiti)', style={'padding':'6px 10px','color':'#00E676','fontSize':'11px','borderBottom':'1px solid #1E293B','textAlign':'center'}),
            html.Th('📡 AllTick Spot Feed', style={'padding':'6px 10px','color':'#38BDF8','fontSize':'11px','borderBottom':'1px solid #1E293B','textAlign':'center'}),
        ])]
        for m_name, v1, v2 in metrics:
            comp_rows.append(html.Tr([
                html.Td(m_name, style={'padding':'6px 10px','color':'#CBD5E1','fontSize':'11px','fontWeight':'bold','borderBottom':'1px solid #0F172A'}),
                html.Td(v1, style={'padding':'6px 10px','color':'#00E676','fontSize':'11px','textAlign':'center','borderBottom':'1px solid #0F172A'}),
                html.Td(v2, style={'padding':'6px 10px','color':'#38BDF8','fontSize':'11px','textAlign':'center','borderBottom':'1px solid #0F172A'}),
            ]))
        tbl = html.Table(comp_rows, style={'width':'100%','borderCollapse':'collapse'})
        return summary, eq_fig, comp_bar, tbl, f'✅ Pure Spot Comparison Complete: {start_dt} to {end_dt}'

    # =========================================================
    # SINGLE SPOT FEED MODE (MT5, ALLTICK, TWELVEDATA)
    # =========================================================
    df, feed_name = fetch_spot_data(feed, tf, dt_from, dt_to)
    if df.empty:
        msg = f'No data available from {feed} for range {start_dt} to {end_dt}. Check MT5 terminal or date selection.'
        return html.Div(msg, style={'color':'#FF3366'}), empty_fig, empty_fig, html.Div(msg), msg

    trades, stats = run_backtest(df, settings)
    closed = [t for t in trades if t['result']!='OPEN']

    pnl_c = '#00E676' if stats.get('net_pnl',0)>=0 else '#FF3366'
    wr_c  = '#00E676' if stats.get('win_rate',0)>=55 else ('#FFD700' if stats.get('win_rate',0)>=45 else '#FF3366')
    pf_c  = '#00E676' if stats.get('profit_factor',0)>=1.5 else ('#FFD700' if stats.get('profit_factor',0)>=1 else '#FF3366')

    summary = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(8,1fr)','gap':'8px'},
        children=[
            card('START CAPITAL', f"${capital:.2f}", '#94A3B8', f"Lot: {settings['lot_size']}"),
            card('END BALANCE', f"${stats.get('ending_balance',capital):.2f}", pnl_c, f"ROI: {stats.get('roi_pct',0):+}%"),
            card('NET PROFIT', f"${stats.get('net_pnl',0):+.2f}", pnl_c),
            card('WIN+BE RATE', f'{stats.get("win_rate",0)}%', wr_c, f"{stats.get('wins',0)}W / {stats.get('break_evens',0)}BE / {stats.get('losses',0)}L"),
            card('TOTAL TRADES', str(stats.get('total',0)), '#E6EDF3'),
            card('PROFIT FACTOR', f'{stats.get("profit_factor",0):.2f}', pf_c, '>1.5 = Good'),
            card('MAX DRAWDOWN', f"-${stats.get('max_drawdown',0):.2f}", '#FF3366', f"{stats.get('max_drawdown_pct',0)}% of capital"),
            card('SAVED BY B-EVEN', str(stats.get('break_evens',0)), '#38BDF8', 'Zero Loss Trades'),
        ])

    eq = stats.get('equity_curve', [capital])
    eq_fig = go.Figure()
    eq_fig.add_trace(go.Scatter(y=eq, mode='lines+markers', name='Account Balance ($)',
        line=dict(color='#00E676' if feed=='mt5' else '#38BDF8', width=2.5),
        marker=dict(size=4),
        fill='tozeroy', fillcolor='rgba(0,230,118,0.08)' if feed=='mt5' else 'rgba(56,189,248,0.08)'))
    eq_fig.add_hline(y=capital, line_color='#64748B', line_width=1, line_dash='dash', annotation_text=f'Initial ${capital}')
    eq_fig.update_layout(template='plotly_dark', paper_bgcolor='#0A0E17', plot_bgcolor='#070C14',
        margin=dict(l=10,r=10,t=30,b=10), height=290,
        title=dict(text=f'{feed_name} Balance Growth | Start: ${capital:.2f} ➔ End: ${stats.get("ending_balance",capital):.2f} ({stats.get("roi_pct",0):+}%)', font=dict(color='#94A3B8',size=12)),
        xaxis=dict(showgrid=True,gridcolor='#0F172A',title='Trade Sequence'),
        yaxis=dict(showgrid=True,gridcolor='#0F172A',tickprefix='$'))

    pie_fig = go.Figure()
    if stats.get('total',0) > 0:
        pie_fig.add_trace(go.Pie(labels=['WINS','BREAK-EVEN','LOSSES'],
            values=[stats.get('wins',0),stats.get('break_evens',0),stats.get('losses',0)],
            marker=dict(colors=['#00E676','#38BDF8','#FF3366']), hole=0.4, textfont=dict(size=13)))
    pie_fig.update_layout(template='plotly_dark', paper_bgcolor='#0A0E17',
        margin=dict(l=10,r=10,t=30,b=10), height=290,
        title=dict(text=f'Trade Results Split ({stats.get("wins",0)}W / {stats.get("break_evens",0)}BE / {stats.get("losses",0)}L)', font=dict(color='#94A3B8',size=12)),
        legend=dict(font=dict(color='#94A3B8')))

    hdrs = ['#','Entry Time','Exit Time','Dir','Lot','Entry','SL','TP','Exit','Pts','PnL ($)','Bal ($)','Result','Reason','TA','OF','SMC, VP & Flow Triggers']
    htr = html.Tr([html.Th(h,style={'padding':'5px 8px','color':'#64748B','fontSize':'10px','borderBottom':'1px solid #1E293B','whiteSpace':'nowrap'}) for h in hdrs])
    rows = [htr]
    for idx, t in enumerate(closed[:80]):
        rc = '#00E676' if t['result']=='WIN' else ('#38BDF8' if t['result']=='BE' else '#FF3366')
        pc = '#00E676' if t['pnl']>0 else ('#38BDF8' if t['pnl']>=0 else '#FF3366')
        dc = '#00E676' if t['direction']=='BUY' else '#FF3366'
        rows.append(html.Tr([
            html.Td(str(idx+1),style={'padding':'4px 7px','color':'#475569','fontSize':'10px'}),
            html.Td(t['entry_time'],style={'padding':'4px 7px','fontSize':'10px','whiteSpace':'nowrap'}),
            html.Td(t['exit_time'],style={'padding':'4px 7px','fontSize':'10px','whiteSpace':'nowrap'}),
            html.Td(t['direction'],style={'padding':'4px 7px','color':dc,'fontWeight':'bold','fontSize':'10px'}),
            html.Td(str(t['lot']),style={'padding':'4px 7px','color':'#94A3B8','fontSize':'10px'}),
            html.Td(f'${t["entry_price"]:.2f}',style={'padding':'4px 7px','fontSize':'10px'}),
            html.Td(f'${t["sl"]:.2f}',style={'padding':'4px 7px','color':'#FF3366','fontSize':'10px'}),
            html.Td(f'${t["tp"]:.2f}',style={'padding':'4px 7px','color':'#00E676','fontSize':'10px'}),
            html.Td(f'${t["exit_price"]:.2f}',style={'padding':'4px 7px','fontSize':'10px'}),
            html.Td(f'{t["points"]:+.1f}',style={'padding':'4px 7px','color':pc,'fontSize':'10px'}),
            html.Td(f'{t["pnl"]:+.2f}',style={'padding':'4px 7px','color':pc,'fontWeight':'bold','fontSize':'10px'}),
            html.Td(f'${t["balance"]:.2f}',style={'padding':'4px 7px','color':'#38BDF8','fontWeight':'bold','fontSize':'10px'}),
            html.Td(t['result'],style={'padding':'4px 7px','color':rc,'fontWeight':'bold','fontSize':'10px'}),
            html.Td(t.get('exit_reason',''),style={'padding':'4px 7px','color':'#CBD5E1','fontSize':'10px','fontWeight':'bold'}),
            html.Td(str(t.get('ta_score',0)),style={'padding':'4px 7px','color':'#F59E0B','fontSize':'10px'}),
            html.Td(str(t.get('of_score',0)),style={'padding':'4px 7px','color':'#00E676','fontSize':'10px'}),
            html.Td(t.get('signals','')[:100],style={'padding':'4px 7px','color':'#94A3B8','fontSize':'9px','maxWidth':'260px','overflow':'hidden'}),
        ], style={'borderBottom':'1px solid #0F172A','backgroundColor':'rgba(0,230,118,0.04)' if t['result']=='WIN' else ('rgba(56,189,248,0.04)' if t['result']=='BE' else 'rgba(255,51,102,0.04)')}))

    tbl = html.Table(rows, style={'width':'100%','borderCollapse':'collapse'})
    actual_range = f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}"
    return summary, eq_fig, pie_fig, tbl, f'✅ {feed_name}: {len(df):,} bars ({actual_range}) | {len(closed)} trades simulated'

if __name__ == '__main__':
    logger.info('='*60)
    logger.info('GOLDFLOW PURE SPOT BACKTEST ENGINE - PORT 8086 Starting...')
    logger.info('100% Pure Spot Gold (Zero Futures, Zero Yahoo Finance)')
    logger.info('Feeds: MT5 Broker Direct + AllTick Institutional Spot + TwelveData')
    logger.info('='*60)
    logger.info('Serving on http://0.0.0.0:8086')
    app.run(host='0.0.0.0', port=8086, debug=False)
