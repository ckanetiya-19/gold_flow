# -*- coding: utf-8 -*-
"""
Updater script for:
1. Adding Crypto to Chart (XAUUSD, BTCUSDT, ETHUSDT, SOLUSDT switcher)
2. Fixing Candle Cut-off issue with 22% top/bottom scaleMargins & barSpacing
3. Adding Real SL (Stop Loss), Entry, and TP (Take Profit) price lines and badges
Keeping everything else (prices, reserves, forexfactory, jarvis, trinity radar) 100% untouched.
"""
import re

with open('xauusd_satellite_radar.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Update fetch_live_candles to accept symbol parameter
old_candles_fn = """def fetch_live_candles(interval='1m', limit=100):
    \"\"\"Fetch real OHLC klines from Binance PAXGUSDT (Gold Spot proxy)\"\"\"
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4.0, context=ssl_ctx) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
            candles = []
            for k in raw:
                candles.append({
                    "time": int(k[0] // 1000),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            return candles
    except Exception:
        return []"""

new_candles_fn = """def fetch_live_candles(symbol='XAUUSD', interval='1m', limit=100):
    \"\"\"Fetch real OHLC klines from Binance for Gold or Crypto\"\"\"
    sym = symbol.upper()
    if 'BTC' in sym:
        pair = 'BTCUSDT'
    elif 'ETH' in sym:
        pair = 'ETHUSDT'
    elif 'SOL' in sym:
        pair = 'SOLUSDT'
    else:
        pair = 'PAXGUSDT'
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4.0, context=ssl_ctx) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
            candles = []
            for k in raw:
                candles.append({
                    "time": int(k[0] // 1000),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            return candles
    except Exception:
        return []"""

if old_candles_fn in code:
    code = code.replace(old_candles_fn, new_candles_fn)
    print("[+] fetch_live_candles updated for multi-asset crypto.")
else:
    print("[!] old_candles_fn not found verbatim.")

# 2. Update analyze_smc to calculate exact SL, Entry, TP, and R:R
old_analyze_smc = """def analyze_smc(candles):
    \"\"\"Algorithmic Smart Money Concepts (SMC) engine: Order Blocks, FVGs, and CVD\"\"\"
    if len(candles) < 10:
        return {
            'order_block': {'low': 4350.0, 'high': 4355.0},
            'fvg': {'bottom': 4355.0, 'top': 4358.0},
            'golden_pocket': 4365.0,
            'liquidity_sweep': 4380.0,
            'cvd': 120.0,
            'pattern_label': '⚡ SMC: BULLISH ORDER BLOCK + 0.618 FIBONACCI RETEST'
        }
    
    n = len(candles)
    min_low = min(c['low'] for c in candles)
    max_high = max(c['high'] for c in candles)
    rng = max(1.0, max_high - min_low)
    
    ob_low = min_low + rng * 0.18
    ob_high = ob_low + rng * 0.08
    for i in range(1, n - 1):
        if candles[i-1]['close'] < candles[i-1]['open'] and candles[i]['close'] > candles[i]['open'] and candles[i]['close'] > candles[i-1]['high']:
            ob_low = candles[i-1]['low']
            ob_high = candles[i-1]['high']
            
    fvg_bottom = min_low + rng * 0.45
    fvg_top = fvg_bottom + rng * 0.06
    for i in range(2, n):
        if candles[i]['low'] > candles[i-2]['high']:
            fvg_bottom = candles[i-2]['high']
            fvg_top = candles[i]['low']
            
    fib_level = round(min_low + rng * 0.618, 2)
    sweep_level = round(max_high - rng * 0.05, 2)
    
    cvd = 0.0
    for c in candles[-20:]:
        c_rng = c['high'] - c['low']
        if c_rng > 0:
            ratio = (c['close'] - c['open']) / c_rng
            cvd += c['volume'] * ratio
            
    return {
        'order_block': {'low': round(ob_low, 2), 'high': round(ob_high, 2)},
        'fvg': {'bottom': round(fvg_bottom, 2), 'top': round(fvg_top, 2)},
        'golden_pocket': fib_level,
        'liquidity_sweep': sweep_level,
        'cvd': round(cvd, 2),
        'pattern_label': f"⚡ SMC ALGO: BULLISH ORDER BLOCK (${ob_low:.2f}) + 0.618 FIB RETEST"
    }"""

new_analyze_smc = """def analyze_smc(candles):
    \"\"\"Algorithmic Smart Money Concepts (SMC) engine: Order Blocks, FVGs, and exact SL/TP\"\"\"
    if len(candles) < 10:
        return {
            'order_block': {'low': 4350.0, 'high': 4355.0},
            'fvg': {'bottom': 4355.0, 'top': 4358.0},
            'golden_pocket': 4365.0,
            'liquidity_sweep': 4380.0,
            'entry': 4360.0,
            'sl': 4345.0,
            'tp': 4385.0,
            'rr': '1:2.8',
            'cvd': 120.0,
            'pattern_label': '⚡ SMC: BULLISH ORDER BLOCK + 0.618 FIBONACCI RETEST'
        }
    
    n = len(candles)
    min_low = min(c['low'] for c in candles)
    max_high = max(c['high'] for c in candles)
    rng = max(1.0, max_high - min_low)
    
    ob_low = min_low + rng * 0.18
    ob_high = ob_low + rng * 0.08
    for i in range(1, n - 1):
        if candles[i-1]['close'] < candles[i-1]['open'] and candles[i]['close'] > candles[i]['open'] and candles[i]['close'] > candles[i-1]['high']:
            ob_low = candles[i-1]['low']
            ob_high = candles[i-1]['high']
            
    fvg_bottom = min_low + rng * 0.45
    fvg_top = fvg_bottom + rng * 0.06
    for i in range(2, n):
        if candles[i]['low'] > candles[i-2]['high']:
            fvg_bottom = candles[i-2]['high']
            fvg_top = candles[i]['low']
            
    fib_level = round(min_low + rng * 0.618, 2)
    sweep_level = round(max_high - rng * 0.05, 2)
    
    current_entry = round(candles[-1]['close'], 2)
    sl_level = round(ob_low - rng * 0.04, 2)
    risk = max(0.5, current_entry - sl_level)
    tp_level = round(current_entry + risk * 2.8, 2)
    if tp_level < sweep_level:
        tp_level = sweep_level
    reward = max(0.5, tp_level - current_entry)
    rr_ratio = f"1:{round(reward / risk, 1)}"
    
    cvd = 0.0
    for c in candles[-20:]:
        c_rng = c['high'] - c['low']
        if c_rng > 0:
            ratio = (c['close'] - c['open']) / c_rng
            cvd += c['volume'] * ratio
            
    return {
        'order_block': {'low': round(ob_low, 2), 'high': round(ob_high, 2)},
        'fvg': {'bottom': round(fvg_bottom, 2), 'top': round(fvg_top, 2)},
        'golden_pocket': fib_level,
        'liquidity_sweep': sweep_level,
        'entry': current_entry,
        'sl': sl_level,
        'tp': tp_level,
        'rr': rr_ratio,
        'cvd': round(cvd, 2),
        'pattern_label': f"⚡ SMC: OB (${ob_low:.2f}) | TP: ${tp_level:.2f} | SL: ${sl_level:.2f} | R:R {rr_ratio}"
    }"""

if old_analyze_smc in code:
    code = code.replace(old_analyze_smc, new_analyze_smc)
    print("[+] analyze_smc updated with dynamic SL, Entry, TP, and R:R.")
else:
    print("[!] old_analyze_smc not found verbatim.")

# 3. Update router for symbol parameter in /api/radar/candles
old_route = """        elif self.path.startswith("/api/radar/candles"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            interval = params.get("interval", ["1m"])[0]
            limit = int(params.get("limit", ["90"])[0])
            candles = fetch_live_candles(interval=interval, limit=limit)
            smc = analyze_smc(candles) if candles else {}
            response_data = {"candles": candles, "smc": smc}"""

new_route = """        elif self.path.startswith("/api/radar/candles"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            symbol = params.get("symbol", ["XAUUSD"])[0]
            interval = params.get("interval", ["1m"])[0]
            limit = int(params.get("limit", ["90"])[0])
            candles = fetch_live_candles(symbol=symbol, interval=interval, limit=limit)
            smc = analyze_smc(candles) if candles else {}
            response_data = {"symbol": symbol, "candles": candles, "smc": smc}"""

if old_route in code:
    code = code.replace(old_route, new_route)
    print("[+] /api/radar/candles endpoint updated for symbol parameter.")

# 4. Update UI Header for Crypto Asset Buttons and SL/TP Display
old_chart_card_header = """    <!-- WIDE PANORAMIC AI VISION TRADINGVIEW CANDLESTICK ENGINE -->
    <div class="card" id="visionChartCard" style="margin-bottom:0; width:100%;">
      <div class="card-header">
        <div class="card-title" style="font-size:13px; letter-spacing:1px;">👁️ AI VISION TRADINGVIEW CANDLESTICK ENGINE (XAUUSD GOLD SPOT)</div>
        <div style="display:flex; gap:8px; align-items:center;">
          <div style="display:flex; gap:3px; background:rgba(0,0,0,0.5); padding:2px 4px; border-radius:4px; border:1px solid rgba(255,255,255,0.1);">
            <button class="btn-tf active" id="btnTf1m" onclick="switchTimeframe('1m')">1M</button>
            <button class="btn-tf" id="btnTf5m" onclick="switchTimeframe('5m')">5M</button>
            <button class="btn-tf" id="btnTf15m" onclick="switchTimeframe('15m')">15M</button>
          </div>
          <span style="font-size: 10px; color: var(--green); background: rgba(16,185,129,0.15); border:1px solid var(--green); padding:2px 6px; border-radius:4px;">● TV CANDLES: LIVE</span>
          <button class="btn-hud" style="font-size:10px; padding:3px 8px;" onclick="toggleChartFullscreen()">🔍 EXPAND</button>
        </div>
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px; font-family:'Share Tech Mono'; color:var(--text-dim); margin-bottom:6px; padding:5px 10px; background:rgba(0,0,0,0.4); border-radius:4px; border-left: 3px solid var(--gold);">
        <span id="chartPatternLabel" style="color:#06b6d4; font-weight:700;">⚡ AI PATTERN: BULLISH ORDER BLOCK + 0.618 FIBONACCI RETEST</span>
        <span id="tvOhlcBar" style="color:#fff; font-size:11px;">O: -- | H: -- | L: -- | C: --</span>
      </div>"""

new_chart_card_header = """    <!-- WIDE PANORAMIC AI VISION TRADINGVIEW CANDLESTICK ENGINE (MULTI-ASSET + SL/TP) -->
    <div class="card" id="visionChartCard" style="margin-bottom:0; width:100%;">
      <div class="card-header">
        <div class="card-title" id="chartCardTitle" style="font-size:13px; letter-spacing:1px;">👁️ AI VISION TRADINGVIEW CANDLESTICK ENGINE</div>
        <div style="display:flex; gap:8px; align-items:center;">
          <!-- Crypto & Asset Switcher -->
          <div style="display:flex; gap:3px; background:rgba(0,0,0,0.5); padding:2px 4px; border-radius:4px; border:1px solid rgba(245,158,11,0.3);">
            <button class="btn-tf active" id="btnSymGold" onclick="switchSymbol('XAUUSD')">🟡 XAU/USD</button>
            <button class="btn-tf" id="btnSymBtc" onclick="switchSymbol('BTCUSDT')">₿ BTC</button>
            <button class="btn-tf" id="btnSymEth" onclick="switchSymbol('ETHUSDT')">Ξ ETH</button>
            <button class="btn-tf" id="btnSymSol" onclick="switchSymbol('SOLUSDT')">◎ SOL</button>
          </div>
          <!-- Timeframe Switcher -->
          <div style="display:flex; gap:3px; background:rgba(0,0,0,0.5); padding:2px 4px; border-radius:4px; border:1px solid rgba(255,255,255,0.1);">
            <button class="btn-tf active" id="btnTf1m" onclick="switchTimeframe('1m')">1M</button>
            <button class="btn-tf" id="btnTf5m" onclick="switchTimeframe('5m')">5M</button>
            <button class="btn-tf" id="btnTf15m" onclick="switchTimeframe('15m')">15M</button>
          </div>
          <span style="font-size: 10px; color: var(--green); background: rgba(16,185,129,0.15); border:1px solid var(--green); padding:2px 6px; border-radius:4px;">● TV CANDLES: LIVE</span>
          <button class="btn-hud" style="font-size:10px; padding:3px 8px;" onclick="toggleChartFullscreen()">🔍 EXPAND</button>
        </div>
      </div>

      <!-- Real SL, Entry & TP Price Bar -->
      <div id="slTpBanner" style="display:flex; justify-content:space-between; align-items:center; font-size:11px; font-family:'Share Tech Mono'; color:var(--text-dim); margin-bottom:6px; padding:6px 12px; background:rgba(0,0,0,0.5); border-radius:4px; border-left: 3px solid var(--gold);">
        <div style="display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
          <span id="chartPatternLabel" style="color:#06b6d4; font-weight:700;">⚡ SMC ALGO: TARGETS & ORDER BLOCK</span>
          <span style="color:#10b981; font-weight:bold; background:rgba(16,185,129,0.15); padding:2px 6px; border-radius:3px; border:1px solid rgba(16,185,129,0.4);" id="badgeTp">🎯 TP: $--</span>
          <span style="color:#06b6d4; font-weight:bold; background:rgba(6,182,212,0.15); padding:2px 6px; border-radius:3px; border:1px solid rgba(6,182,212,0.4);" id="badgeEntry">🔵 ENTRY: $--</span>
          <span style="color:#ef4444; font-weight:bold; background:rgba(239,68,68,0.15); padding:2px 6px; border-radius:3px; border:1px solid rgba(239,68,68,0.4);" id="badgeSl">🛑 SL: $--</span>
          <span style="color:var(--gold); font-weight:bold; background:rgba(245,158,11,0.15); padding:2px 6px; border-radius:3px; border:1px solid rgba(245,158,11,0.4);" id="badgeRr">⚖️ R:R: 1:2.8</span>
        </div>
        <span id="tvOhlcBar" style="color:#fff; font-size:11px;">O: -- | H: -- | L: -- | C: --</span>
      </div>"""

if old_chart_card_header in code:
    code = code.replace(old_chart_card_header, new_chart_card_header)
    print("[+] Chart header updated with Crypto switcher and SL/TP bar.")

# 5. Update JS for initTradingViewChart (fixing candle cutting) and loadCandles (drawing SL/TP lines & crypto)
old_js_chart = """let tvChart = null;
let tvCandleSeries = null;
let currentTf = '1m';
let orderBlockLine = null;
let fibLine = null;
let liqSweepLine = null;
let lastCandle = null;

function initTradingViewChart() {
  const container = document.getElementById('tvChartContainer');
  if (!container || !window.LightweightCharts) {
    setTimeout(initTradingViewChart, 150);
    return;
  }

  tvChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: { color: '#020617' },
      textColor: '#94a3b8',
      fontSize: 11,
      fontFamily: 'Share Tech Mono, monospace'
    },
    grid: {
      vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
      horzLines: { color: 'rgba(255, 255, 255, 0.04)' }
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(6, 182, 212, 0.4)', width: 1, style: 2 },
      horzLine: { color: 'rgba(6, 182, 212, 0.4)', width: 1, style: 2 }
    },
    rightPriceScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      scaleMargins: { top: 0.12, bottom: 0.15 }
    },
    timeScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      timeVisible: true,
      secondsVisible: false
    }
  });

  tvCandleSeries = tvChart.addCandlestickSeries({
    upColor: '#10b981',
    downColor: '#ef4444',
    borderVisible: false,
    wickUpColor: '#10b981',
    wickDownColor: '#ef4444'
  });

  tvChart.subscribeCrosshairMove(param => {
    if (!param || !param.seriesData || !param.seriesData.get(tvCandleSeries)) {
      if (lastCandle) updateOhlcDisplay(lastCandle);
      return;
    }
    const data = param.seriesData.get(tvCandleSeries);
    updateOhlcDisplay(data);
  });

  loadCandles(currentTf);
}

function updateOhlcDisplay(d) {
  if (!d) return;
  const bar = document.getElementById('tvOhlcBar');
  if (!bar) return;
  const col = d.close >= d.open ? '#10b981' : '#ef4444';
  bar.innerHTML = `O: <span style="color:${col};">$${d.open.toFixed(2)}</span> | H: <span style="color:${col};">$${d.high.toFixed(2)}</span> | L: <span style="color:${col};">$${d.low.toFixed(2)}</span> | C: <span style="color:${col};">$${d.close.toFixed(2)}</span>`;
}

function loadCandles(tf) {
  fetch(`/api/radar/candles?interval=${tf}&limit=90`)
    .then(r => r.json())
    .then(res => {
      const candles = Array.isArray(res) ? res : (res.candles || []);
      const smc = (!Array.isArray(res) && res.smc) ? res.smc : null;
      if (!candles || candles.length === 0) return;
      tvCandleSeries.setData(candles);
      lastCandle = candles[candles.length - 1];
      updateOhlcDisplay(lastCandle);

      let obLevel, fibLevel, sweepLevel, patternLabel;
      if (smc && smc.order_block) {
        obLevel = smc.order_block.low;
        fibLevel = smc.golden_pocket;
        sweepLevel = smc.liquidity_sweep;
        patternLabel = smc.pattern_label || `⚡ SMC: ORDER BLOCK ($${obLevel}) + 0.618 FIBONACCI RETEST`;
      } else {
        const minLow = Math.min(...candles.map(c => c.low));
        const maxHigh = Math.max(...candles.map(c => c.high));
        const range = Math.max(1, maxHigh - minLow);
        obLevel = Number((minLow + range * 0.15).toFixed(2));
        fibLevel = Number((minLow + range * 0.618).toFixed(2));
        sweepLevel = Number((maxHigh - range * 0.05).toFixed(2));
        patternLabel = `⚡ SMC: ORDER BLOCK ($${obLevel}) + 0.618 FIBONACCI RETEST`;
      }

      const pLabelEl = document.getElementById('chartPatternLabel');
      if (pLabelEl) pLabelEl.innerText = patternLabel;

      if (orderBlockLine) tvCandleSeries.removePriceLine(orderBlockLine);
      if (fibLine) tvCandleSeries.removePriceLine(fibLine);
      if (liqSweepLine) tvCandleSeries.removePriceLine(liqSweepLine);

      orderBlockLine = tvCandleSeries.createPriceLine({
        price: obLevel,
        color: '#10b981',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: `🟢 BULLISH ORDER BLOCK ($${obLevel})`
      });

      fibLine = tvCandleSeries.createPriceLine({
        price: fibLevel,
        color: '#f59e0b',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🟡 FIB 0.618 GOLDEN POCKET ($${fibLevel})`
      });

      liqSweepLine = tvCandleSeries.createPriceLine({
        price: sweepLevel,
        color: '#ef4444',
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: `🔴 LIQUIDITY SWEEP ($${sweepLevel})`
      });

      tvCandleSeries.setMarkers([
        {
          time: lastCandle.time,
          position: 'belowBar',
          color: '#10b981',
          shape: 'arrowUp',
          text: '⚡ AI SNIPER BUY (BOS CONFIRMED)'
        }
      ]);

      tvChart.timeScale().fitContent();
    })
    .catch(e => console.log('Candles fetch error:', e));
}

function switchTimeframe(tf) {
  currentTf = tf;
  document.querySelectorAll('.btn-tf').forEach(b => b.classList.remove('active'));
  const activeBtn = document.getElementById(tf === '1m' ? 'btnTf1m' : tf === '5m' ? 'btnTf5m' : 'btnTf15m');
  if (activeBtn) activeBtn.classList.add('active');
  loadCandles(tf);
}"""

new_js_chart = """let tvChart = null;
let tvCandleSeries = null;
let currentTf = '1m';
let currentSymbol = 'XAUUSD';
let orderBlockLine = null;
let fibLine = null;
let liqSweepLine = null;
let tpLine = null;
let slLine = null;
let entryLine = null;
let lastCandle = null;

function initTradingViewChart() {
  const container = document.getElementById('tvChartContainer');
  if (!container || !window.LightweightCharts) {
    setTimeout(initTradingViewChart, 150);
    return;
  }

  tvChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: { color: '#020617' },
      textColor: '#94a3b8',
      fontSize: 11,
      fontFamily: 'Share Tech Mono, monospace'
    },
    grid: {
      vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
      horzLines: { color: 'rgba(255, 255, 255, 0.04)' }
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(6, 182, 212, 0.4)', width: 1, style: 2 },
      horzLine: { color: 'rgba(6, 182, 212, 0.4)', width: 1, style: 2 }
    },
    rightPriceScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      autoScale: true,
      scaleMargins: { top: 0.22, bottom: 0.22 }
    },
    timeScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      timeVisible: true,
      secondsVisible: false,
      barSpacing: 9,
      minBarSpacing: 4
    }
  });

  tvCandleSeries = tvChart.addCandlestickSeries({
    upColor: '#10b981',
    downColor: '#ef4444',
    borderVisible: false,
    wickUpColor: '#10b981',
    wickDownColor: '#ef4444'
  });

  tvChart.subscribeCrosshairMove(param => {
    if (!param || !param.seriesData || !param.seriesData.get(tvCandleSeries)) {
      if (lastCandle) updateOhlcDisplay(lastCandle);
      return;
    }
    const data = param.seriesData.get(tvCandleSeries);
    updateOhlcDisplay(data);
  });

  loadCandles(currentTf);
}

function updateOhlcDisplay(d) {
  if (!d) return;
  const bar = document.getElementById('tvOhlcBar');
  if (!bar) return;
  const col = d.close >= d.open ? '#10b981' : '#ef4444';
  const dec = currentSymbol === 'SOLUSDT' ? 2 : (currentSymbol === 'XAUUSD' ? 2 : (currentSymbol === 'ETHUSDT' ? 2 : 2));
  bar.innerHTML = `O: <span style="color:${col};">$${d.open.toFixed(dec)}</span> | H: <span style="color:${col};">$${d.high.toFixed(dec)}</span> | L: <span style="color:${col};">$${d.low.toFixed(dec)}</span> | C: <span style="color:${col};">$${d.close.toFixed(dec)}</span>`;
}

function loadCandles(tf) {
  fetch(`/api/radar/candles?symbol=${currentSymbol}&interval=${tf}&limit=90`)
    .then(r => r.json())
    .then(res => {
      const candles = Array.isArray(res) ? res : (res.candles || []);
      const smc = (!Array.isArray(res) && res.smc) ? res.smc : null;
      if (!candles || candles.length === 0) return;
      tvCandleSeries.setData(candles);
      lastCandle = candles[candles.length - 1];
      updateOhlcDisplay(lastCandle);

      const minLow = Math.min(...candles.map(c => c.low));
      const maxHigh = Math.max(...candles.map(c => c.high));
      const range = Math.max(1, maxHigh - minLow);

      let obLevel, fibLevel, sweepLevel, patternLabel, entryLvl, slLvl, tpLvl, rrRatio;
      if (smc && smc.order_block) {
        obLevel = smc.order_block.low;
        fibLevel = smc.golden_pocket;
        sweepLevel = smc.liquidity_sweep;
        entryLvl = smc.entry || lastCandle.close;
        slLvl = smc.sl || Number((obLevel - range * 0.04).toFixed(2));
        tpLvl = smc.tp || sweepLevel;
        rrRatio = smc.rr || '1:2.8';
        patternLabel = smc.pattern_label || `⚡ SMC: OB ($${obLevel}) | TP: $${tpLvl} | SL: $${slLvl}`;
      } else {
        obLevel = Number((minLow + range * 0.15).toFixed(2));
        fibLevel = Number((minLow + range * 0.618).toFixed(2));
        sweepLevel = Number((maxHigh - range * 0.05).toFixed(2));
        entryLvl = lastCandle.close;
        slLvl = Number((obLevel - range * 0.04).toFixed(2));
        tpLvl = sweepLevel;
        rrRatio = '1:2.8';
        patternLabel = `⚡ SMC: OB ($${obLevel}) | TP: $${tpLvl} | SL: $${slLvl}`;
      }

      // Update SL, Entry, TP badges
      const pLabelEl = document.getElementById('chartPatternLabel');
      if (pLabelEl) pLabelEl.innerText = patternLabel;
      const bTp = document.getElementById('badgeTp');
      if (bTp) bTp.innerText = `🎯 TP: $${tpLvl.toLocaleString()}`;
      const bEntry = document.getElementById('badgeEntry');
      if (bEntry) bEntry.innerText = `🔵 ENTRY: $${entryLvl.toLocaleString()}`;
      const bSl = document.getElementById('badgeSl');
      if (bSl) bSl.innerText = `🛑 SL: $${slLvl.toLocaleString()}`;
      const bRr = document.getElementById('badgeRr');
      if (bRr) bRr.innerText = `⚖️ R:R: ${rrRatio}`;

      // Remove existing lines
      if (orderBlockLine) tvCandleSeries.removePriceLine(orderBlockLine);
      if (fibLine) tvCandleSeries.removePriceLine(fibLine);
      if (liqSweepLine) tvCandleSeries.removePriceLine(liqSweepLine);
      if (tpLine) tvCandleSeries.removePriceLine(tpLine);
      if (slLine) tvCandleSeries.removePriceLine(slLine);
      if (entryLine) tvCandleSeries.removePriceLine(entryLine);

      // Plot Order Block (solid green)
      orderBlockLine = tvCandleSeries.createPriceLine({
        price: obLevel,
        color: '#10b981',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: `🟢 ORDER BLOCK ($${obLevel})`
      });

      // Plot Golden Pocket (dashed gold)
      fibLine = tvCandleSeries.createPriceLine({
        price: fibLevel,
        color: '#f59e0b',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🟡 FIB 0.618 ($${fibLevel})`
      });

      // Plot Take Profit (dashed vibrant green)
      tpLine = tvCandleSeries.createPriceLine({
        price: tpLvl,
        color: '#10b981',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🎯 TAKE PROFIT (TP): $${tpLvl}`
      });

      // Plot Sniper Entry (dotted cyan)
      entryLine = tvCandleSeries.createPriceLine({
        price: entryLvl,
        color: '#06b6d4',
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: `🔵 ENTRY: $${entryLvl}`
      });

      // Plot Stop Loss (dashed red)
      slLine = tvCandleSeries.createPriceLine({
        price: slLvl,
        color: '#ef4444',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🛑 STOP LOSS (SL): $${slLvl}`
      });

      tvCandleSeries.setMarkers([
        {
          time: lastCandle.time,
          position: 'belowBar',
          color: '#10b981',
          shape: 'arrowUp',
          text: `⚡ ${currentSymbol} BUY (SL: $${slLvl})`
        }
      ]);

      tvChart.timeScale().fitContent();
    })
    .catch(e => console.log('Candles fetch error:', e));
}

function switchSymbol(sym) {
  currentSymbol = sym;
  document.querySelectorAll('[id^=btnSym]').forEach(b => b.classList.remove('active'));
  const btnId = sym === 'XAUUSD' ? 'btnSymGold' : (sym === 'BTCUSDT' ? 'btnSymBtc' : (sym === 'ETHUSDT' ? 'btnSymEth' : 'btnSymSol'));
  const el = document.getElementById(btnId);
  if (el) el.classList.add('active');
  const titleEl = document.getElementById('chartCardTitle');
  if (titleEl) {
    titleEl.innerText = sym === 'XAUUSD' ? '👁️ AI VISION TRADINGVIEW CANDLESTICK ENGINE (XAUUSD GOLD SPOT)' :
                        `👁️ AI VISION TRADINGVIEW CANDLESTICK ENGINE (${sym} CRYPTO)`;
  }
  loadCandles(currentTf);
}

function switchTimeframe(tf) {
  currentTf = tf;
  document.querySelectorAll('[id^=btnTf]').forEach(b => b.classList.remove('active'));
  const activeBtn = document.getElementById(tf === '1m' ? 'btnTf1m' : tf === '5m' ? 'btnTf5m' : 'btnTf15m');
  if (activeBtn) activeBtn.classList.add('active');
  loadCandles(tf);
}"""

if old_js_chart in code:
    code = code.replace(old_js_chart, new_js_chart)
    print("[+] JavaScript chart engine updated with SL/TP lines, Crypto switcher, and candle spacing fix.")

# 6. Update pollTelemetry candle tick update to respect currentSymbol
old_tick = """      // 9. Update TradingView candle with live price tick
      if (tvCandleSeries && lastCandle && data.macro_radar && data.macro_radar.xauusd) {
        const liveP = data.macro_radar.xauusd.price;
        const nowSec = Math.floor(Date.now() / 1000);
        if (currentTf === '1m' && nowSec - lastCandle.time >= 60) {
          lastCandle = {
            time: Math.floor(nowSec / 60) * 60,
            open: liveP,
            high: liveP,
            low: liveP,
            close: liveP
          };
        } else {
          lastCandle.high = Math.max(lastCandle.high, liveP);
          lastCandle.low = Math.min(lastCandle.low, liveP);
          lastCandle.close = liveP;
        }
        tvCandleSeries.update(lastCandle);
        updateOhlcDisplay(lastCandle);
      }"""

new_tick = """      // 9. Update TradingView candle with live price tick (respecting active symbol)
      if (tvCandleSeries && lastCandle && data.macro_radar) {
        let liveP = data.macro_radar.xauusd.price;
        if (currentSymbol === 'BTCUSDT' && data.macro_radar.bitcoin) liveP = data.macro_radar.bitcoin.price;
        else if (currentSymbol === 'ETHUSDT' && data.macro_radar.ethereum) liveP = data.macro_radar.ethereum.price;
        
        const nowSec = Math.floor(Date.now() / 1000);
        if (currentTf === '1m' && nowSec - lastCandle.time >= 60) {
          lastCandle = {
            time: Math.floor(nowSec / 60) * 60,
            open: liveP,
            high: liveP,
            low: liveP,
            close: liveP
          };
        } else {
          lastCandle.high = Math.max(lastCandle.high, liveP);
          lastCandle.low = Math.min(lastCandle.low, liveP);
          lastCandle.close = liveP;
        }
        tvCandleSeries.update(lastCandle);
        updateOhlcDisplay(lastCandle);
      }"""

if old_tick in code:
    code = code.replace(old_tick, new_tick)
    print("[+] pollTelemetry candle ticking updated for active symbol.")

with open('xauusd_satellite_radar.py', 'w', encoding='utf-8') as f:
    f.write(code)

scratch_path = r'C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow\xauusd_satellite_radar.py'
import os
if os.path.exists(os.path.dirname(scratch_path)):
    with open(scratch_path, 'w', encoding='utf-8') as f:
        f.write(code)

print("[SUCCESS] xauusd_satellite_radar.py successfully updated!")
