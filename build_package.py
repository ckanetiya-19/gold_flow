import os
import shutil
import zipfile

BASE_DIR = r"C:\Users\ckane\Desktop\GOLDFLOW_PRO_SYSTEM"
ZIP_OUTPUT = r"C:\Users\ckane\Desktop\GOLDFLOW_COMPLETE_FINAL_SYSTEM.zip"

# 1. Batch Launcher Scripts
START_BAT_CONTENT = """@echo off
title GOLDFLOW Institutional Trading System - Launcher
color 0A
cls
echo ===============================================================================
echo                ⚡ GOLDFLOW INSTITUTIONAL TRADING SYSTEM ⚡
echo                   Automated Multi-Port Launch Sequence
echo ===============================================================================
echo.
echo [1/5] Launching Port 8070: Bloomberg Web Terminal (1M VWAP Cross & Retest)...
start "Port 8070 - Bloomberg Web Terminal" /B python 01_ACTIVE_TRADING_ENGINES\\PORT_8070_BLOOMBERG_WEB_TERMINAL.py

echo [2/5] Launching Port 8080: AI Quant Sniper Terminal (Multi-Layer Scoring)...
start "Port 8080 - AI Quant Terminal" /B python 01_ACTIVE_TRADING_ENGINES\\PORT_8080_AI_QUANT_TERMINAL.py

echo [3/5] Launching Port 8088: Dual-Model Live Engine (5M Bar Lock & SMC OF)...
start "Port 8088 - Live Engine" /B python 01_ACTIVE_TRADING_ENGINES\\PORT_8088_GOLDFLOW_LIVE_ENGINE.py

echo [4/5] Launching Port 8090: Unified Trinity Radar (Session Sweeps & Heatmap)...
start "Port 8090 - Trinity Radar" /B python 01_ACTIVE_TRADING_ENGINES\\PORT_8090_UNIFIED_TRINITY_RADAR.py

echo [5/5] Launching Port 8095: Master Stream Terminal (Ultra-Low Latency WebSocket)...
start "Port 8095 - Master Stream Terminal" /B python 01_ACTIVE_TRADING_ENGINES\\PORT_8095_MASTER_STREAM_TERMINAL.py

echo.
echo ===============================================================================
echo                     ✅ ALL PORTS SUCCESSFULLY STARTED!
echo ===============================================================================
echo Access your live dashboards in any browser:
echo.
echo   * Bloomberg Web Terminal:      http://localhost:8070
echo   * AI Quant Sniper Terminal:    http://localhost:8080
echo   * Dual-Model Live Engine:      http://localhost:8088
echo   * Unified Trinity Radar:       http://localhost:8090
echo   * Master Stream Terminal:      http://localhost:8095
echo.
echo To stop all ports, run STOP_ALL_PORTS.bat
echo ===============================================================================
pause
"""

STOP_BAT_CONTENT = """@echo off
title GOLDFLOW - Stop All Ports
color 0C
cls
echo ===============================================================================
echo                🛑 STOPPING GOLDFLOW INSTITUTIONAL SYSTEM 🛑
echo ===============================================================================
echo Terminating running Python engine processes...

powershell -Command "Get-Process python*, pythonw* -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*PORT_*' -or $_.CommandLine -like '*goldflow*' -or $_.CommandLine -like '*xauusd*' } | Stop-Process -Force -ErrorAction SilentlyContinue"

echo.
echo ===============================================================================
echo                 ✅ All GOLDFLOW port processes stopped!
echo ===============================================================================
pause
"""

# Write batch files
for p in [os.path.join(BASE_DIR, "START_ALL_PORTS.bat"), os.path.join(BASE_DIR, "04_STARTUP_SCRIPTS", "START_ALL_PORTS.bat")]:
    with open(p, "w", encoding="utf-8") as f:
        f.write(START_BAT_CONTENT)

for p in [os.path.join(BASE_DIR, "STOP_ALL_PORTS.bat"), os.path.join(BASE_DIR, "04_STARTUP_SCRIPTS", "STOP_ALL_PORTS.bat")]:
    with open(p, "w", encoding="utf-8") as f:
        f.write(STOP_BAT_CONTENT)

# 2. Comprehensive Master Manual
README_CONTENT = """# GOLDFLOW INSTITUTIONAL TRADING SYSTEM // COMPLETE MASTER MANUAL
**Version:** 3.5.0-Final (Desktop Edition)  
**Target Asset:** XAU/USD (Gold Spot / Equiti Brokerage `XAUUSD.sd`)  
**Architecture:** Microservices Multi-Port Real-Time Engine (WebSocket + AsyncIO + Dash + Plotly)  
**Author:** Antigravity AI & Master Quantitative Engineering Team  

---

## 1. Executive Summary & Core Philosophy

**GOLDFLOW** is an enterprise-grade quantitative trading and order flow intelligence platform designed specifically for institutional Gold (XAU/USD) trading. It bridges institutional dark pool analytics, session liquidity sweeps (SMC), Volume Weighted Average Price (VWAP), and order book absorption directly with live MetaTrader 5 execution.

Unlike retail indicators that lag price action, GOLDFLOW processes millisecond-level tick data from multiple institutional data feeds (AllTick, TwelveData, iTick) combined with broker execution feeds to identify true institutional footprints.

---

## 2. System Architecture & Port Directory

The platform runs as a coordinated cluster of dedicated microservices, each operating on its own isolated localhost port:

| Port | Service Name | Source File | Description | Strategy Rules |
| :--- | :--- | :--- | :--- | :--- |
| **8070** | Bloomberg Web Terminal | `PORT_8070_BLOOMBERG_WEB_TERMINAL.py` | Order Flow, Delta, Tape, DOM Ladder & Blotter | 1M VWAP Cross & Retest + Strict Wick Filter + 1:2 R:R |
| **8080** | AI Quant Sniper Terminal | `PORT_8080_AI_QUANT_TERMINAL.py` | Multi-Layer Algorithmic Scoring & Quant Metrics | 1M VWAP Cross & Retest + Strict Wick Filter + 1:2 R:R |
| **8088** | Dual-Model Live Engine | `PORT_8088_GOLDFLOW_LIVE_ENGINE.py` | 5M Dual-Engine Consensus + Anti-Manipulation Shield | Model A 8086 Confluence + Model B SMC & OF + **Ironclad 5M Bar Lock** |
| **8090** | Unified Trinity Radar | `PORT_8090_UNIFIED_TRINITY_RADAR.py` | Liquidity Radar, Session Levels & Heatmap | Asian/London/NY Sweeps, POC, Multi-TF Bias |
| **8095** | Master Stream Terminal | `PORT_8095_MASTER_STREAM_TERMINAL.py` | Ultra-Low Latency High-Frequency WebSocket Stream | 1M VWAP Cross & Retest + Strict Wick Filter + 1:2 R:R |
| **8085** | Multi-TF Institutional Chart | `PORT_8085_MULTITIMEFRAME_CHART.py` | Interactive Live Candlestick Canvas & Indicators | Multi-Timeframe Visualization |
| **8086** | Confluence Backtester | `PORT_8086_CONFLUENCE_BACKTESTER.py` | Historical Strategy Simulator & Monte Carlo Engine | 8086 Confluence Engine Backtesting |
| **8000** | Core WebSocket Engine | `PORT_8000_CORE_WEBSOCKET_ENGINE.py` | Central Real-time Feed Aggregator | Multi-Feed Distribution |
| **8050** | V1 Order Flow Dashboard | `PORT_8050_V1_LEGACY_ORDERFLOW.py` | Legacy V1 Order Flow Footprint | Tape & Delta History |
| **8060** | V2 Institutional Dashboard | `PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py` | V2 Absorption & Volume Profile Blotter | Institutional Footprint Analysis |

---

## 3. Deep-Dive Strategy Specifications

### A. 1-Minute VWAP Strict Proper Close & Wick-Filter Strategy (Ports 8070, 8080, 8095)
Applied strictly across Ports 8070, 8080, and 8095 on the 1-Minute timeframe:

1. **BUY Setup (Green Candle):**
   - **Candle Color:** Green (`Close > Open`).
   - **Proper Close:** The candle must close strictly above the live VWAP line (`Close > VWAP`).
   - **Strict Wick Filter:** The lower wick must **NOT** pierce below the VWAP line (`Low >= VWAP`). If any part of the lower shadow touches or crosses below VWAP, the candle is completely disqualified.
   - **Entry:** Instant BUY on the open of the immediately following candle.
   - **Stop Loss (SL):** Exactly $1.00 below the live VWAP line (`SL = VWAP - 1.00`).
   - **Take Profit (TP):** Strict 1:2 Risk-to-Reward ratio (`TP = Entry + 2 * (Entry - SL)`).

2. **SELL Setup (Red Candle):**
   - **Candle Color:** Red (`Close < Open`).
   - **Proper Close:** The candle must close strictly below the live VWAP line (`Close < VWAP`).
   - **Strict Wick Filter:** The upper wick must **NOT** pierce above the VWAP line (`High <= VWAP`). If any part of the upper shadow touches or crosses above VWAP, the candle is completely disqualified.
   - **Entry:** Instant SELL on the open of the immediately following candle.
   - **Stop Loss (SL):** Exactly $1.00 above the live VWAP line (`SL = VWAP + 1.00`).
   - **Take Profit (TP):** Strict 1:2 Risk-to-Reward ratio (`TP = Entry - 2 * (SL - Entry)`).

3. **Protection & Trailing:**
   - Trailing Stop: Once profit reaches +$2.00, $1.00 profit is locked, and SL ratchets $1.00 behind price peak/trough.

---

### B. 5-Minute Dual-Model Consensus Engine with Ironclad Candle Lock (Port 8088)
Runs on Port 8088 on the 5-Minute institutional timeframe:

1. **Model A (8086 Confluence - Magic 808801):**
   - **Technical Score (TA >= 40/40):** Evaluates EMA 20/50/200 trend alignment, 1-Hour EMA 20/50 trend filter, RSI 14 (Oversold < 30 / Overbought > 70), MACD Signal Cross, Bollinger Band Squeeze/Expansion, Fibonacci Retracement Levels (0.382, 0.500, 0.618), Dynamic S/R zones.
   - **Order Flow Score (OF >= 30/30):** Evaluates Bar Delta (+/- 30 threshold), CVD 5-period Trend, Cumulative Volume Absorption, POC migrations.
   - **Trigger:** Confluence Met (TA >= 40 and OF >= 30) -> 0.01 lot order with 1:2 R:R (SL: $2.50, TP: $5.00).

2. **Model B (Pure SMC & Real-Time Order Flow - Magic 808802):**
   - **Session Liquidity Sweeps:** Asian High Judas Sweep (Sell), Asian Low Judas Sweep (Buy), London High Sweep (Sell), London Low Sweep (Buy), Previous Day High (PDH) Sweep (Sell), Previous Day Low (PDL) Sweep (Buy).
   - **Order Flow Confirmation:** Triggered only when accompanied by Delta reversal (+/- 30) or CVD Bullish/Bearish confirmation.

3. **Tier 2 Anti-Manipulation Safety Shield:**
   - Compares MT5 quote mid-price against independent spot feeds (AllTick/TwelveData/iTick).
   - If deviation > $0.85 (broker wick manipulation) or broker spread > $0.60, trades are instantly blocked.

4. **1:1 Auto Break-Even (Zero Risk Protection):**
   - When floating profit reaches 1:1 R:R ($2.50 gain on 0.01 lot), Stop Loss is automatically moved to Entry + $0.15 (guaranteeing zero loss and fee coverage).

5. **Ironclad 5-Minute Candle Bar Lock (Zero Re-Entry Rule):**
   - **The Rule:** If a trade was active in the current 5-minute candle, and that trade closes (either by hitting SL, TP, or being manually closed/cut by the user in MT5), **ABSOLUTELY NO NEW TRADE CAN BE ENTERED WITHIN THAT SAME 5-MINUTE CANDLE**.
   - **Next Bar Enforcement:** The engine continuously locks the current 5M candle epoch and displays a live countdown (`🔒 LOCKED (Xm Ys)`). No order can be sent until the current 5-minute bar completes and the next 5-minute bar opens.

---

## 4. MetaTrader 5 (MT5) Setup & Configuration

- **Supported Brokers:** Equiti Brokerage, IC Markets, Pepperstone, OANDA, FXCM.
- **Symbol Auto-Detection:** The bridge (`mt5_bridge.py`) automatically discovers the broker's specific Gold symbol, prioritizing `XAUUSD.sd`, `XAUUSD`, `XAUUSD.pr`, or `GOLD`.
- **Order Magic Numbers:**
  - `807001` -> Port 8070 Bloomberg Web Terminal
  - `808001` -> Port 8080 AI Quant Sniper Terminal
  - `808801` -> Port 8088 Live Engine (Model A Confluence)
  - `808802` -> Port 8088 Live Engine (Model B SMC & OF)
  - `809501` -> Port 8095 Master Quant Stream Terminal
- **Filling Mode:** Automatically auto-detects `ORDER_FILLING_IOC` (Immediate-or-Cancel), `ORDER_FILLING_FOK`, or `ORDER_FILLING_RETURN`.

---

## 5. Directory Structure of This Package

```
GOLDFLOW_PRO_SYSTEM/
│
├── START_ALL_PORTS.bat                         <- One-click launch for all 5 active ports
├── STOP_ALL_PORTS.bat                          <- One-click shutdown for all ports
├── requirements.txt                            <- Python dependency specifications
│
├── 01_ACTIVE_TRADING_ENGINES/
│   ├── PORT_8070_BLOOMBERG_WEB_TERMINAL.py    <- Port 8070 Bloomberg Web Terminal
│   ├── PORT_8080_AI_QUANT_TERMINAL.py         <- Port 8080 AI Quant Sniper Terminal
│   ├── PORT_8088_GOLDFLOW_LIVE_ENGINE.py      <- Port 8088 Dual-Model Live Engine
│   ├── PORT_8090_UNIFIED_TRINITY_RADAR.py     <- Port 8090 Unified Trinity Radar
│   ├── PORT_8095_MASTER_STREAM_TERMINAL.py    <- Port 8095 Master Quant Stream Terminal
│   ├── PORT_8085_MULTITIMEFRAME_CHART.py      <- Port 8085 MTF Institutional Chart
│   └── PORT_8086_CONFLUENCE_BACKTESTER.py     <- Port 8086 Strategy Backtester
│
├── 02_CORE_SYSTEM_ARCHITECTURE/
│   ├── PORT_8000_CORE_WEBSOCKET_ENGINE.py     <- Port 8000 Core WebSocket Feed Hub
│   ├── PORT_8050_V1_LEGACY_ORDERFLOW.py       <- Port 8050 V1 Order Flow Dashboard
│   └── PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py <- Port 8060 V2 Absorption Dashboard
│
├── 03_CONNECTORS_AND_LIBRARIES/
│   ├── mt5_bridge.py                          <- High-Speed MT5 Bridge & Trade Executor
│   ├── multi_api_key_pool.py                  <- TwelveData & AllTick API Quota Manager
│   ├── supervisor_all_ports.py                <- Background Health Check Watchdog
│   └── goldflow_tradingview_quant_sniper.pine <- TradingView PineScript v5 Indicator
│
├── 04_STARTUP_SCRIPTS/
│   ├── START_ALL_PORTS.bat                    <- Port launcher batch script
│   ├── STOP_ALL_PORTS.bat                     <- Process terminator batch script
│   └── launch_all_ports_startup.vbs           <- Background silent VBScript launcher
│
├── 05_DOCUMENTATION_AND_BLUEPRINTS/
│   ├── README_MASTER_MANUAL.md                <- This master manual
│   ├── ALL_LOCALHOST_PORTS_SYSTEM_BLUEPRINT.html <- Interactive architecture blueprint
│   ├── ALL_LOCALHOST_PORTS_SYSTEM_BLUEPRINT.pdf  <- Printable visual architecture blueprint
│   ├── COMPLETE_GOLDFLOW_CHAT_HISTORY.html    <- Complete development transcript (HTML)
│   └── COMPLETE_GOLDFLOW_CHAT_HISTORY.pdf     <- Complete development transcript (PDF)
│
└── root_copies_for_direct_execution/          <- Canonical filename copies for CLI execution
    ├── xauusd_terminal.py
    ├── xauusd_quant_terminal.py
    ├── xauusd_hybrid_stream_terminal.py
    ├── goldflow_live_engine_8088.py
    ├── xauusd_satellite_radar.py
    ├── goldflow_chart_8085.py
    ├── goldflow_backtest_8086.py
    ├── mt5_bridge.py
    └── multi_api_key_pool.py
```

---

## 6. Installation & Quickstart Guide

### Step 1: Install Python Dependencies
Open PowerShell or Command Prompt in the project folder and run:
```bash
pip install -r requirements.txt
```

### Step 2: Open MetaTrader 5
1. Launch MetaTrader 5 on your Windows desktop.
2. Log into your Demo or Live account.
3. In MT5, go to **Tools -> Options -> Expert Advisors** and ensure **"Allow algorithmic trading"** is checked.
4. Ensure `XAUUSD.sd` (or your broker's Gold symbol) is visible in the Market Watch window.

### Step 3: Launch the Entire Platform
Double-click `START_ALL_PORTS.bat`.

All 5 core ports will start in the background. Within 5-10 seconds, open your browser and navigate to:
- **Port 8070:** `http://localhost:8070`
- **Port 8080:** `http://localhost:8080`
- **Port 8088:** `http://localhost:8088`
- **Port 8090:** `http://localhost:8090`
- **Port 8095:** `http://localhost:8095`

---

## 7. Operational Best Practices & Troubleshooting

1. **Clean Shutdown:** Always use `STOP_ALL_PORTS.bat` to cleanly terminate all processes before shutting down your PC.
2. **Candle Lock Countdown:** In Port 8088, when a trade is closed, do not attempt to force a manual order—the engine's header will clearly display `🔒 LOCKED (Xm Ys)`. Once the timer reaches `0m 00s`, the system automatically transitions to `🟢 READY` for the next 5M bar.
3. **Wick Filter Verification:** In Ports 8070, 8080, and 8095, notice that green candles whose lower wicks cross below VWAP will be ignored. Only clean bodies that close above with shadows entirely above VWAP will trigger trades.

---
*© 2026 GOLDFLOW Institutional Technologies. All Rights Reserved.*
"""

for p in [os.path.join(BASE_DIR, "README_MASTER_MANUAL.md"), os.path.join(BASE_DIR, "05_DOCUMENTATION_AND_BLUEPRINTS", "README_MASTER_MANUAL.md")]:
    with open(p, "w", encoding="utf-8") as f:
        f.write(README_CONTENT.strip())

print("Created README_MASTER_MANUAL.md in root and documentation folders.")

# 3. Create Complete ZIP Archive
print(f"Compressing {BASE_DIR} into {ZIP_OUTPUT}...")
file_count = 0
total_uncompressed_bytes = 0

with zipfile.ZipFile(ZIP_OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
    for root, dirs, files in os.walk(BASE_DIR):
        for file in files:
            file_path = os.path.join(root, file)
            arcname = os.path.relpath(file_path, os.path.dirname(BASE_DIR))
            zipf.write(file_path, arcname)
            file_count += 1
            total_uncompressed_bytes += os.path.getsize(file_path)

zip_size = os.path.getsize(ZIP_OUTPUT)
print(f"SUCCESS! Packaged {file_count} files.")
print(f"Uncompressed size: {total_uncompressed_bytes:,} bytes ({total_uncompressed_bytes / (1024*1024):.2f} MB)")
print(f"Final ZIP size:    {zip_size:,} bytes ({zip_size / (1024*1024):.2f} MB)")
print(f"ZIP Location:      {ZIP_OUTPUT}")
