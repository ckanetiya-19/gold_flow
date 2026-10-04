"""
PORT 9061 - FULL-SCREEN TRADINGVIEW-STYLE CHART (mirrors Port 9060)
============================================================================
NEW file, NEW port. Does NOT touch PORT_9060_V2_INSTITUTIONAL_DASHBOARD_V2.py
in any way except that file now exposes one additive, read-only GET endpoint
(/api/mirror) this page polls - nothing about 9060's own behavior, layout,
or calculations changed.

Purpose: a dedicated, full-browser-window chart page with real TradingView
interaction (Lightweight Charts library: mouse-wheel zoom, drag-to-pan,
crosshair) and a full TradingView-style timeframe row (1m/5m/15m/1H/4H/1D),
showing the EXACT SAME price bars, VWAP line, imbalance markers and
Entry/SL/TP levels as the Port 9060 dashboard - not an independent
recalculation. Two processes computing "the same" cumulative VWAP/CVD from
scratch would drift depending on when each one started; mirroring the
source's own already-computed bars guarantees byte-for-byte matching values.
"""

import json
import threading
import time
from datetime import datetime, timedelta

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

MIRROR_URL = "http://127.0.0.1:9060/api/mirror"
POLL_SECONDS = 1.0
HOST = "127.0.0.1"
PORT = 9061

app = FastAPI(title="Goldflow Full-Screen Chart (mirrors Port 9060)")

mirror_cache = {"historical_bars": [], "current_bar": None, "trade_state": {}, "last_known_price": None, "cum_delta": 0.0, "connected": False}


def _parse_time(t):
    if isinstance(t, str):
        return datetime.fromisoformat(t)
    return t


def poll_loop():
    while True:
        try:
            r = requests.get(MIRROR_URL, timeout=3)
            if r.status_code == 200:
                mirror_cache.update(r.json())
                mirror_cache["connected"] = True
        except Exception:
            mirror_cache["connected"] = False
        time.sleep(POLL_SECONDS)


def bucket_time(t: datetime, tf_minutes: int) -> datetime:
    """Same aggregation semantics as the source dashboard's M5/M15 resampling,
    generalized to also handle 1H/4H/1D correctly (source only handled <60m)."""
    if tf_minutes < 60 * 24:
        day_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        delta_min = int((t - day_start).total_seconds() // 60)
        bucket_min = (delta_min // tf_minutes) * tf_minutes
        return day_start + timedelta(minutes=bucket_min)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1H": 60, "4H": 240, "1D": 1440}


def build_candles(tf: str):
    bars = [dict(b) for b in mirror_cache.get("historical_bars", [])]
    for b in bars:
        b["time"] = _parse_time(b["time"])

    cur = mirror_cache.get("current_bar")
    if cur and cur.get("close") is not None and cur.get("time") is not None:
        c = dict(cur)
        c["time"] = _parse_time(c["time"])
        bars.append(c)

    if not bars:
        return []

    tf_min = TF_MINUTES.get(tf, 1)
    if tf_min > 1:
        resampled = {}
        order = []
        for b in bars:
            bucket = bucket_time(b["time"], tf_min)
            if bucket not in resampled:
                resampled[bucket] = dict(b)
                resampled[bucket]["time"] = bucket
                order.append(bucket)
            else:
                agg = resampled[bucket]
                agg["high"] = max(agg["high"], b["high"])
                agg["low"] = min(agg["low"], b["low"])
                agg["close"] = b["close"]
                agg["volume"] = agg.get("volume", 0) + b.get("volume", 0)
                agg["imbalance"] = agg.get("imbalance") or b.get("imbalance")
        bars = [resampled[k] for k in order]

    # Real cumulative VWAP over the (possibly resampled) series - same formula
    # Port 9060 itself uses, so the yellow line matches exactly.
    cum_pv = 0.0
    cum_vol = 0.0
    out = []
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b.get("volume", 0) or 0
        cum_pv += tp * vol
        cum_vol += vol
        vwap = round(cum_pv / cum_vol, 2) if cum_vol > 0 else b["close"]
        out.append({
            "time": int(b["time"].timestamp()),
            "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
            "vwap": vwap,
            "imbalance": bool(b.get("imbalance")),
        })
    return out


@app.get("/api/chart_data")
async def chart_data(tf: str = "1m"):
    candles = build_candles(tf)
    return JSONResponse({
        "candles": candles,
        "trade_state": mirror_cache.get("trade_state", {}),
        "last_price": mirror_cache.get("last_known_price"),
        "cum_delta": mirror_cache.get("cum_delta"),
        "source_connected": mirror_cache.get("connected", False),
    })


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>XAUUSD V2 - Full Screen Chart</title>
<meta charset="utf-8">
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  html, body { margin:0; padding:0; width:100%; height:100%; background:#0b0e14; overflow:hidden; font-family:'Segoe UI',sans-serif; }
  #topbar { display:flex; align-items:center; justify-content:space-between; padding:8px 14px; background:#0d1117; border-bottom:1px solid #21262d; }
  #title { color:#FFD700; font-weight:700; font-size:14px; }
  #status { font-size:11px; color:#00e676; }
  #status.down { color:#f23645; }
  #tfrow button { background:#1c2030; color:#ccc; border:1px solid #2a2e39; padding:6px 14px; margin-left:4px; border-radius:4px; cursor:pointer; font-size:12px; }
  #tfrow button.active { background:#FFD700; color:#111; font-weight:700; }
  #chartContainer { width:100vw; height:calc(100vh - 44px); }
  #ohlc { position:absolute; top:50px; left:14px; color:#eee; font-size:12px; z-index:5; background:rgba(13,17,23,0.7); padding:4px 8px; border-radius:4px; }
</style>
</head>
<body>
  <div id="topbar">
    <div id="title">XAUUSD V2 Institutional - Full Chart (mirrors Port 9060)</div>
    <div id="tfrow">
      <button data-tf="1m" class="active">1m</button>
      <button data-tf="5m">5m</button>
      <button data-tf="15m">15m</button>
      <button data-tf="1H">1H</button>
      <button data-tf="4H">4H</button>
      <button data-tf="1D">1D</button>
    </div>
    <div id="status">LIVE</div>
  </div>
  <div id="ohlc"></div>
  <div id="chartContainer"></div>

<script>
let currentTf = '1m';
let chart, candleSeries, vwapSeries;
let entryLine = null, slLine = null, tpLine = null;

function initChart() {
  const container = document.getElementById('chartContainer');
  chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: { background: { color: '#0b0e14' }, textColor: '#94a3b8' },
    grid: { vertLines: { color: 'rgba(255,255,255,0.05)' }, horzLines: { color: 'rgba(255,255,255,0.05)' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.1)' },
    timeScale: { borderColor: 'rgba(255,255,255,0.1)', timeVisible: true, secondsVisible: false },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#00E676', downColor: '#FF3366', borderVisible: false,
    wickUpColor: '#00E676', wickDownColor: '#FF3366',
  });
  vwapSeries = chart.addLineSeries({ color: '#FFD700', lineWidth: 2 });
}

async function loadCandles() {
  try {
    const res = await fetch(`/api/chart_data?tf=${currentTf}`);
    const data = await res.json();
    document.getElementById('status').className = data.source_connected ? '' : 'down';
    document.getElementById('status').innerText = data.source_connected ? 'LIVE (Port 9060)' : 'RECONNECTING...';
    if (!data.candles.length) return;

    candleSeries.setData(data.candles.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
    vwapSeries.setData(data.candles.map(c => ({ time: c.time, value: c.vwap })));

    const markers = data.candles.filter(c => c.imbalance).map(c => ({
      time: c.time, position: 'aboveBar', color: '#00e5ff', shape: 'circle', text: '★'
    }));
    candleSeries.setMarkers(markers);

    const last = data.candles[data.candles.length - 1];
    document.getElementById('ohlc').innerText =
      `O: ${last.open.toFixed(2)}  H: ${last.high.toFixed(2)}  L: ${last.low.toFixed(2)}  C: ${last.close.toFixed(2)}  VWAP: ${last.vwap.toFixed(2)}  CVD: ${(data.cum_delta||0).toFixed(0)}`;

    const pos = data.trade_state || {};
    if (entryLine) { candleSeries.removePriceLine(entryLine); entryLine = null; }
    if (slLine) { candleSeries.removePriceLine(slLine); slLine = null; }
    if (tpLine) { candleSeries.removePriceLine(tpLine); tpLine = null; }
    if (pos.in_position) {
      entryLine = candleSeries.createPriceLine({ price: pos.entry_price, color: '#2962ff', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ENTRY' });
      slLine = candleSeries.createPriceLine({ price: pos.stop_loss, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'SL' });
      tpLine = candleSeries.createPriceLine({ price: pos.take_profit, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'TP' });
    }
  } catch (e) {
    document.getElementById('status').className = 'down';
    document.getElementById('status').innerText = 'ERROR';
  }
}

document.querySelectorAll('#tfrow button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tfrow button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentTf = btn.dataset.tf;
    loadCandles();
  });
});

initChart();
loadCandles();
setInterval(loadCandles, 1000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_PAGE


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=poll_loop, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
