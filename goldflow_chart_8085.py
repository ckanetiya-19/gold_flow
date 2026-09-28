# =============================================================================
# 📈 GOLD.FLOW // CHART PRO — FULL-SCREEN CHART DASHBOARD (XAUUSD)
# Port: 8085 | Standalone Chart | TradingView-Style
# Elements: Candlestick, VWAP, Bands, BUY/SELL Signals, Entry/SL/TP Lines
# Zoom: Mouse Wheel | Timeframes: 1M / 5M / 15M / 30M / 1H / 4H
# =============================================================================

import dash
from dash import dcc, html, Input, Output, State
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import requests
import threading
import time
import random
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

try:
    import multi_api_key_pool
    _POOL_OK = True
except ImportError:
    _POOL_OK = False

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
    format='%(asctime)s - [CHART_8085] - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'port_8085.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('CHART_8085')

data_lock      = threading.Lock()
bars_store     = {'1m': [], '5m': [], '15m': [], '30m': [], '1h': [], '4h': []}
TF_MINUTES     = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240}
current_tick_bar = {k: {'time': None,'open': None,'high': -1e9,'low': 1e9,'close': None,'volume': 0.0,'buy_vol': 0.0,'sell_vol': 0.0} for k in TF_MINUTES}
cum_delta = 0.0
last_price    = 3350.0
market_meta   = {'bid': 3349.8,'ask': 3350.2,'high': 3360.0,'low': 3340.0,'open': 3345.0,'source': 'Live'}
signals_list  = []
active_position = {'in_position': False,'direction': None,'entry': 0.0,'sl': 0.0,'tp': 0.0}
is_running    = True

def floor_time(dt, tf_key):
    mins = TF_MINUTES[tf_key]
    f = dt.replace(second=0, microsecond=0)
    return f.replace(minute=(f.minute // mins) * mins)

def _snap_bar(cb, cvd):
    return {'time': cb['time'],'open': cb['open'],'high': cb['high'],'low': cb['low'],'close': cb['close'],'volume': cb['volume'],'buy_vol': cb['buy_vol'],'sell_vol': cb['sell_vol'],'delta': cb['buy_vol'] - cb['sell_vol'],'cvd': cvd,'imbalance': cb['volume'] > 180}

def _detect_signal(bar):
    global active_position, signals_list
    bs = bars_store['1m']
    if len(bs) < 5: return
    closes = [b['close'] for b in bs[-5:]]
    deltas = [b['delta'] for b in bs[-5:]]
    vols   = [b['volume'] for b in bs[-5:]]
    price  = bar['close']
    avg_vol = max(np.mean(vols), 1.0)
    if (closes[-1] > closes[-3] and deltas[-1] > 0 and bar['volume'] > avg_vol * 1.3 and not active_position['in_position']):
        sl = round(price - 2.0, 2); tp = round(price + 4.0, 2)
        signals_list.append({'time': bar['time'],'price': price,'direction': 'BUY','entry': price,'sl': sl,'tp': tp})
        active_position.update({'in_position': True,'direction': 'BUY','entry': price,'sl': sl,'tp': tp})
        logger.info(f'BUY Signal @ {price:.2f}')
        if len(signals_list) > 200: signals_list[:] = signals_list[-200:]
    elif (closes[-1] < closes[-3] and deltas[-1] < 0 and bar['volume'] > avg_vol * 1.3 and not active_position['in_position']):
        sl = round(price + 2.0, 2); tp = round(price - 4.0, 2)
        signals_list.append({'time': bar['time'],'price': price,'direction': 'SELL','entry': price,'sl': sl,'tp': tp})
        active_position.update({'in_position': True,'direction': 'SELL','entry': price,'sl': sl,'tp': tp})
        logger.info(f'SELL Signal @ {price:.2f}')
        if len(signals_list) > 200: signals_list[:] = signals_list[-200:]
    if active_position['in_position']:
        pos = active_position
        if pos['direction'] == 'BUY' and (price >= pos['tp'] or price <= pos['sl']):
            active_position.update({'in_position': False,'direction': None,'entry': 0.0,'sl': 0.0,'tp': 0.0})
        elif pos['direction'] == 'SELL' and (price <= pos['tp'] or price >= pos['sl']):
            active_position.update({'in_position': False,'direction': None,'entry': 0.0,'sl': 0.0,'tp': 0.0})

def process_tick(price, volume, is_buy):
    global cum_delta, last_price
    last_price = price
    market_meta['high'] = max(market_meta.get('high', price), price)
    market_meta['low']  = min(market_meta.get('low', price), price)
    buy_v = volume if is_buy else 0.0
    sell_v = volume if not is_buy else 0.0
    cum_delta += buy_v - sell_v
    now = datetime.now()
    for tf_key in TF_MINUTES:
        bar_time = floor_time(now, tf_key)
        cb = current_tick_bar[tf_key]
        if cb['time'] is None or bar_time > cb['time']:
            if cb['time'] is not None and cb['open'] is not None:
                finished = _snap_bar(cb, cum_delta)
                bars_store[tf_key].append(finished)
                if len(bars_store[tf_key]) > 500: bars_store[tf_key] = bars_store[tf_key][-500:]
                if tf_key == '1m': _detect_signal(finished)
            current_tick_bar[tf_key] = {'time': bar_time,'open': price,'high': price,'low': price,'close': price,'volume': volume,'buy_vol': buy_v,'sell_vol': sell_v}
        else:
            cb['high'] = max(cb['high'], price); cb['low'] = min(cb['low'], price)
            cb['close'] = price; cb['volume'] += volume; cb['buy_vol'] += buy_v; cb['sell_vol'] += sell_v

def live_feed_worker():
    global is_running
    logger.info('Chart Pro Live Feed Worker starting...')
    while is_running:
        price = None
        try:
            if _POOL_OK:
                spot = multi_api_key_pool.get_spot_gold_live()
                if spot and spot.get('price', 0) > 0:
                    price = float(spot['price'])
                    market_meta['bid'] = float(spot.get('bid', price - 0.1))
                    market_meta['ask'] = float(spot.get('ask', price + 0.1))
                    market_meta['source'] = spot.get('source', 'Multi-API')
        except Exception: pass
        if not price:
            try:
                r = requests.get('https://api.binance.com/api/v3/ticker/bookTicker', params={'symbol': 'PAXGUSDT'}, timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    bid = float(d['bidPrice']); ask = float(d['askPrice'])
                    price = (bid + ask) / 2
                    market_meta['bid'] = bid; market_meta['ask'] = ask; market_meta['source'] = 'Binance PAXG'
            except Exception: pass
        if price and price > 0:
            vol = float(random.randint(50, 160))
            is_buy = price >= market_meta.get('ask', price) or price > last_price
            with data_lock: process_tick(price, vol, is_buy)
        for _ in range(2):
            if not is_running: break
            time.sleep(1.0)
            drift = random.choice([-0.05, -0.02, 0.0, 0.02, 0.05])
            with data_lock: process_tick(round(last_price + drift, 2), float(random.randint(15, 65)), drift >= 0)

MAX_BARS = {'1m': 120,'5m': 100,'15m': 80,'30m': 60,'1h': 60,'4h': 48}

def build_chart(tf, bars, meta, sigs, pos):
    if not bars:
        fig = go.Figure()
        fig.update_layout(template='plotly_dark', paper_bgcolor='#05070A', plot_bgcolor='#070C14',
            annotations=[dict(text='Waiting for live data...', x=0.5, y=0.5, xref='paper', yref='paper',
                              font=dict(color='#94A3B8', size=20), showarrow=False)])
        return fig
    n = MAX_BARS.get(tf, 100)
    display = bars[-n:] if len(bars) > n else bars
    df = pd.DataFrame(display)
    df['time_str'] = df['time'].apply(lambda x: x.strftime('%m/%d %H:%M') if isinstance(x, datetime) else str(x))
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    cpv = (tp * df['volume']).cumsum(); cvs = df['volume'].cumsum()
    df['vwap'] = np.where(cvs > 0, (cpv / cvs).round(2), df['close'])
    df['diff_sq'] = ((tp - df['vwap']) ** 2) * df['volume']
    df['vwap_std'] = np.sqrt(df['diff_sq'].cumsum() / np.maximum(cvs, 1.0))
    df['vwap_upper'] = (df['vwap'] + 1.28 * df['vwap_std']).round(2)
    df['vwap_lower'] = (df['vwap'] - 1.28 * df['vwap_std']).round(2)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.78, 0.22])

    fig.add_trace(go.Candlestick(x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        name='XAUUSD', increasing_line_color='#00E676', decreasing_line_color='#FF3366',
        increasing_fillcolor='rgba(0,230,118,0.35)', decreasing_fillcolor='rgba(255,51,102,0.35)',
        showlegend=False, line=dict(width=1.5)), row=1, col=1)

    fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap'], mode='lines', name='VWAP',
        line=dict(color='#FFD700', width=2.5), showlegend=True), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap_upper'], mode='lines', name='+1.28σ Band',
        line=dict(color='#FF9800', width=1.5, dash='dot'), showlegend=True), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['time_str'], y=df['vwap_lower'], mode='lines', name='-1.28σ Band',
        line=dict(color='#00F0FF', width=1.5, dash='dash'), showlegend=True), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=pd.concat([df['time_str'], df['time_str'][::-1]]),
        y=pd.concat([df['vwap_upper'], df['vwap_lower'][::-1]]),
        fill='toself', fillcolor='rgba(255,152,0,0.05)', line=dict(color='rgba(0,0,0,0)'),
        showlegend=False, hoverinfo='skip'), row=1, col=1)

    imb_df = df[df['imbalance'] == True] if 'imbalance' in df.columns else pd.DataFrame()
    if not imb_df.empty:
        fig.add_trace(go.Scatter(x=imb_df['time_str'], y=imb_df['high'] * 1.0003, mode='markers',
            marker=dict(symbol='star', size=14, color='#00E5FF', line=dict(color='#00BCD4', width=1)),
            name='⚡ Imbalance', showlegend=True), row=1, col=1)

    time_strs = set(df['time_str'].tolist())
    def fmt(t): return floor_time(t, tf).strftime('%m/%d %H:%M') if isinstance(t, datetime) else str(t)
    buy_sigs  = [(fmt(s['time']), s['price'] - 0.8) for s in sigs if s['direction'] == 'BUY'  and fmt(s['time']) in time_strs]
    sell_sigs = [(fmt(s['time']), s['price'] + 0.8) for s in sigs if s['direction'] == 'SELL' and fmt(s['time']) in time_strs]

    if buy_sigs:
        fig.add_trace(go.Scatter(x=[x[0] for x in buy_sigs], y=[x[1] for x in buy_sigs], mode='markers',
            marker=dict(symbol='triangle-up', size=16, color='#00E676', line=dict(color='#00C853', width=1.5)),
            name='▲ BUY Signal', showlegend=True), row=1, col=1)
    if sell_sigs:
        fig.add_trace(go.Scatter(x=[x[0] for x in sell_sigs], y=[x[1] for x in sell_sigs], mode='markers',
            marker=dict(symbol='triangle-down', size=16, color='#FF3366', line=dict(color='#C62828', width=1.5)),
            name='▼ SELL Signal', showlegend=True), row=1, col=1)

    if pos.get('in_position'):
        fig.add_hline(y=pos['entry'], line_color='#38BDF8', line_width=2, line_dash='dash',
            annotation_text=f"  ENTRY ", annotation_font=dict(color='#38BDF8', size=12),
            annotation_position='right', row=1, col=1)
        fig.add_hline(y=pos['sl'], line_color='#FF3366', line_width=2, line_dash='dot',
            annotation_text=f"  SL ", annotation_font=dict(color='#FF3366', size=12),
            annotation_position='right', row=1, col=1)
        fig.add_hline(y=pos['tp'], line_color='#00E676', line_width=2, line_dash='dot',
            annotation_text=f"  TP ", annotation_font=dict(color='#00E676', size=12),
            annotation_position='right', row=1, col=1)

    if 'delta' in df.columns:
        delta_colors = ['#00E676' if d >= 0 else '#FF3366' for d in df['delta']]
        fig.add_trace(go.Bar(x=df['time_str'], y=df['delta'], name='Delta',
            marker_color=delta_colors, showlegend=False), row=2, col=1)
        fig.add_hline(y=0, line_color='#334155', line_width=1, row=2, col=1)

    fig.update_xaxes(type='category', showgrid=True, gridcolor='#0F172A', gridwidth=1,
        tickfont=dict(size=10, color='#64748B'), showline=True, linecolor='#1E293B', rangeslider_visible=False)
    fig.update_yaxes(showgrid=True, gridcolor='#0F172A', gridwidth=1,
        tickfont=dict(size=11, color='#94A3B8'), tickprefix='$', showline=True, linecolor='#1E293B', tickformat='.2f')
    fig.update_yaxes(title_text='DELTA', row=2, col=1, tickfont=dict(size=9, color='#64748B'), tickprefix='')

    fig.update_layout(template='plotly_dark', paper_bgcolor='#05070A', plot_bgcolor='#070C14',
        margin=dict(l=5, r=90, t=5, b=5), autosize=True, xaxis_rangeslider_visible=False,
        showlegend=True, legend=dict(orientation='h', yanchor='bottom', y=1.005, xanchor='left', x=0,
            font=dict(size=11, color='#94A3B8'), bgcolor='rgba(5,7,10,0.7)', bordercolor='#1E293B', borderwidth=1),
        dragmode='pan', uirevision='chart')
    return fig

TF_LABELS = [('1m','1M'),('5m','5M'),('15m','15M'),('30m','30M'),('1h','1H'),('4h','4H')]

app = dash.Dash(__name__, title='GOLD.FLOW // Chart Pro (XAUUSD)', update_title=None, suppress_callback_exceptions=True)

app.layout = html.Div(style={'backgroundColor':'#05070A','color':'#E6EDF3','fontFamily':"'JetBrains Mono','Consolas',monospace",'height':'100vh','display':'flex','flexDirection':'column','overflow':'hidden','margin':'0','padding':'0','boxSizing':'border-box'},
    children=[
        dcc.Interval(id='chart-interval', interval=2000, n_intervals=0),
        dcc.Store(id='tf-store', data='1m'),
        html.Div(style={'display':'flex','alignItems':'center','justifyContent':'space-between','backgroundColor':'#0A0E17','borderBottom':'2px solid #FFD700','padding':'6px 16px','flexShrink':'0'},
            children=[
                html.Div(style={'display':'flex','alignItems':'center','gap':'12px'},children=[
                    html.Span('⚡ GOLD.FLOW', style={'color':'#FFD700','fontWeight':'900','fontSize':'18px','letterSpacing':'1.5px'}),
                    html.Span('// CHART PRO', style={'color':'#38BDF8','fontWeight':'bold','fontSize':'13px'}),
                    html.Span('[PORT 8085]', style={'color':'#00E676','fontSize':'10px','fontWeight':'bold','backgroundColor':'#064E3B','padding':'2px 8px','borderRadius':'4px'}),
                    html.Span('XAUUSD', style={'color':'#FFFFFF','fontWeight':'bold','fontSize':'14px','marginLeft':'6px'}),
                ]),
                html.Div(id='chart-price-display', style={'textAlign':'center'}),
                html.Div(id='chart-clock', style={'fontSize':'11px','color':'#64748B','textAlign':'right'})
            ]),
        html.Div(style={'display':'flex','alignItems':'center','backgroundColor':'#080C14','borderBottom':'1px solid #1E293B','padding':'5px 16px','gap':'6px','flexShrink':'0'},
            children=[
                html.Span('TIMEFRAME:', style={'color':'#475569','fontSize':'10px','fontWeight':'bold','marginRight':'4px'}),
                *[html.Button(label, id=f'tf-btn-{tf}', n_clicks=0,
                    style={'backgroundColor':'#0F172A','color':'#94A3B8','border':'1px solid #1E293B','padding':'4px 14px',
                           'borderRadius':'4px','cursor':'pointer','fontFamily':'inherit','fontSize':'12px','fontWeight':'bold'})
                  for tf, label in TF_LABELS],
                html.Span('🖱️  Scroll = Zoom  |  Drag = Pan', style={'color':'#334155','fontSize':'10px','marginLeft':'auto'})
            ]),
        html.Div(id='chart-status-strip', style={'display':'flex','gap':'20px','backgroundColor':'#07090F','borderBottom':'1px solid #0F172A','padding':'4px 16px','flexShrink':'0','fontSize':'11px','flexWrap':'wrap'}),
        html.Div(style={'flex':'1','overflow':'hidden','padding':'4px'},children=[
            dcc.Graph(id='main-chart-figure',
                config={'displayModeBar':True,'displaylogo':False,'modeBarButtonsToRemove':['select2d','lasso2d','autoScale2d'],'scrollZoom':True,'responsive':True,'toImageButtonOptions':{'filename':'goldflow_chart_8085'}},
                style={'height':'100%','width':'100%'},
                figure=go.Figure(layout=dict(template='plotly_dark',paper_bgcolor='#05070A',plot_bgcolor='#070C14')))
        ])
    ])

@app.callback(Output('tf-store','data'),
    [Input(f'tf-btn-{tf}','n_clicks') for tf,_ in TF_LABELS],
    [State('tf-store','data')], prevent_initial_call=True)
def select_timeframe(*args):
    from dash import ctx
    triggered = ctx.triggered_id
    if not triggered: return args[-1] or '1m'
    for tf, _ in TF_LABELS:
        if triggered == f'tf-btn-{tf}': return tf
    return args[-1] or '1m'

@app.callback([Output('main-chart-figure','figure'),Output('chart-price-display','children'),Output('chart-clock','children'),Output('chart-status-strip','children')],
    [Input('chart-interval','n_intervals'),Input('tf-store','data')])
def update_chart(n, tf_key):
    tf = tf_key or '1m'
    with data_lock:
        bars = list(bars_store.get(tf, [])); price = last_price
        meta = dict(market_meta); sigs = list(signals_list); pos = dict(active_position)
    fig = build_chart(tf, bars, meta, sigs, pos)
    open_p = meta.get('open', price); ch = price - open_p
    ch_pct = (ch / open_p * 100) if open_p else 0.0
    ch_col = '#00E676' if ch >= 0 else '#FF3366'; ch_s = '+' if ch >= 0 else ''
    price_elem = html.Div([html.Span(f'', style={'color':'#FFD700','fontSize':'22px','fontWeight':'900'}),
        html.Span(f'  {ch_s}{ch:.2f} ({ch_s}{ch_pct:.2f}%)', style={'color':ch_col,'fontSize':'12px','marginLeft':'8px'})],
        style={'display':'flex','alignItems':'center'})
    now = datetime.now(); utc = datetime.now(timezone.utc)
    clock_elem = html.Div([html.Div(f"IST {now.strftime('%H:%M:%S')}", style={'color':'#CBD5E1','fontWeight':'bold','fontSize':'12px'}),
        html.Div(f"UTC {utc.strftime('%H:%M:%S')}  |  NYC {(utc-timedelta(hours=4)).strftime('%H:%M:%S')}", style={'color':'#475569','fontSize':'10px','marginTop':'2px'})])
    bid=meta.get('bid',price-0.1); ask=meta.get('ask',price+0.1); sprd=round(abs(ask-bid),2)
    def stat(l,v,c='#CBD5E1'): return html.Div([html.Span(f'{l}: ',style={'color':'#475569'}),html.Span(v,style={'color':c,'fontWeight':'bold'})])
    pos_t = 'ACTIVE' if pos.get('in_position') else 'FLAT'; pos_c = '#FFD700' if pos.get('in_position') else '#475569'
    bc = sum(1 for s in sigs if s['direction']=='BUY'); sc = sum(1 for s in sigs if s['direction']=='SELL')
    strip=[stat('BID',f'','#FF3366'),stat('ASK',f'','#00E676'),stat('SPREAD',f'','#38BDF8'),
        stat('HIGH',f''),stat('LOW',f''),
        stat('BARS',str(len(bars)),'#94A3B8'),stat('▲ BUY',str(bc),'#00E676'),stat('▼ SELL',str(sc),'#FF3366'),
        stat('POS',pos_t,pos_c),stat('SOURCE',meta.get('source','Live'),'#38BDF8')]
    return fig, price_elem, clock_elem, strip

if __name__ == '__main__':
    logger.info('='*60)
    logger.info('GOLDFLOW CHART PRO — PORT 8085 Starting...')
    logger.info('Full-Screen | Scroll Zoom | 6 Timeframes | All Signals')
    logger.info('='*60)
    threading.Thread(target=live_feed_worker, daemon=True).start()
    logger.info('Serving Chart Pro on http://0.0.0.0:8085')
    app.run(host='0.0.0.0', port=8085, debug=False)
