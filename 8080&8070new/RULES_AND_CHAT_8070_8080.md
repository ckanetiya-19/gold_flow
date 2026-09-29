# GOLDFLOW PORT 8070 & 8080 SYSTEM - RULES & CHAT BLUEPRINT

## 1. System Overview
This folder contains the complete, self-contained **Pure Spot Gold Order Flow & AI Quant System**:
- **Port 8070 (`xauusd_terminal.py`)**: Bloomberg Web Terminal (Order Flow, DOM / Depth Ladder, The Tape, Real-time Delta, CVD).
- **Port 8080 (`xauusd_quant_terminal.py`)**: AI Quant Sniper Terminal (Multi-Layer Quant Scoring & Analytics).
- **Local Databases**:
  - `trades_terminal.db`: Historical 1M candles (5,050+ bars preserved).
  - `trades_quant.db`: Historical 1M quant bars (21,700+ bars preserved).

---

## 2. Core Strategy Rules (1-Minute Institutional VWAP)

### A. BUY Setup (Green Candle)
1. **Candle Color**: Green (`Close > Open`).
2. **Proper Close**: The candle must close strictly above the live VWAP line (`Close > VWAP`).
3. **Strict Wick Filter**: The lower wick must **NOT** pierce below the VWAP line (`Low >= VWAP`). If any shadow touches or crosses below VWAP, disqualified.
4. **Entry**: Open of the next candle.
5. **Stop Loss (SL)**: Exactly $1.00 below the VWAP line (`SL = VWAP - 1.00`).
6. **Take Profit (TP)**: Strict 1:2 Risk-to-Reward ratio (`TP = Entry + 2 * (Entry - SL)`).

### B. SELL Setup (Red Candle)
1. **Candle Color**: Red (`Close < Open`).
2. **Proper Close**: The candle must close strictly below the live VWAP line (`Close < VWAP`).
3. **Strict Wick Filter**: The upper wick must **NOT** pierce above the VWAP line (`High <= VWAP`). If any shadow touches or crosses above VWAP, disqualified.
4. **Entry**: Open of the next candle.
5. **Stop Loss (SL)**: Exactly $1.00 above the VWAP line (`SL = VWAP + 1.00`).
6. **Take Profit (TP)**: Strict 1:2 Risk-to-Reward ratio (`TP = Entry - 2 * (SL - Entry)`).

### C. Trade Execution & Level Lock
1. **Single Trade Per Level**: Only 1 trade is permitted per level/cycle.
2. **TP Win Lock**: If TP is hit, all further trade entries are completely **LOCKED (STOPPED)** until price crosses to the opposite side of VWAP and forms a brand new fresh level.
3. **SL Retry Rule**: If a trade is stopped out (SL hit), only the **immediate next candle** (Bar index = SL Bar + 1) is allowed to re-enter if it meets the full setup. If the next candle does not qualify, the setup is forfeited.

---

## 3. Pure Spot Data Ingestion Rules (Crucial Agreement)
1. **MT5 Ticks Excluded From Candlesticks**:
   - MT5 broker ticks must **NEVER** be used to generate the OHLC candlestick bars on 8070 or 8080.
   - MT5 is connected strictly for viewing account balances, open positions, and blotter.
2. **Candlesticks Generated Purely from Spot Feeds**:
   - **Primary**: AllTick & iTick WebSockets.
   - **Backup**: TwelveData (13 keys) and RealMarket (9 keys) Rotating Pool (`multi_api_key_pool.py`).
3. **DOM & The Tape**:
   - Updates tick-by-tick from WebSocket depth and prints institutional block trades (> 80 oz).

---

## 4. API Keys Configuration
- **AllTick Token (Port 8070)**: `de36ba2fd50be697d72d9336d249ec8d-c-app` (Dedicated)
- **AllTick Token (Port 8080)**: `ee8db5ca115f423a9debb36a8947eddb-c-app` (Dedicated)
- **iTick Token (Shared 8070 & 8080)**: `475ba01817e945f5920509a34db9305cf0ab0dea0e934212aee138cbdbd92cae` (Validated)
- **TwelveData & RealMarket**: Handled automatically via `multi_api_key_pool.py` (22 rotating keys).

---

## 5. Historical Chat Context & Agreements
- **25-Sep-2026 11:43 AM**: User requested pure spot candle creation without broker manipulation wicks, zero rate-limit exhaustion, and clean un-cluttered visual charts.
- **26-Sep-2026**: High-precision `⚡ABS` (Institutional Absorption) indicator refined, historical candle databases verified and preserved across all reboots.
- **28-Sep-2026**: Physical folder-level segregation executed into `8080&8070new` to ensure 100% independence.
