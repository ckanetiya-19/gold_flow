# GOLDFLOW PORT 8088 & 8095 SYSTEM - RULES & CHAT BLUEPRINT

## 1. System Overview
This folder contains the self-contained **Live MT5 Consensus Execution Engine & Master Quant Stream System**:
- **Port 8088 (`goldflow_live_engine_8088.py`)**: 5-Minute Institutional Dual-Model Consensus Trading Engine.
  - Connected directly to Equiti Broker MT5 (Account #1189847).
  - Strict 0.01 lot size.
  - Multi-tier safety shields.
- **Port 8095 (`xauusd_hybrid_stream_terminal.py`)**: Master Quant Stream Terminal (BVC - Bulk Volume Classification, OFI - Order Flow Imbalance, VWAP Analytics).
- **Local Databases**:
  - `trades_stream_8095.db`: Stream bar database for Port 8095.

---

## 2. Port 8088 Live Trading Engine Specifications

### Model A: 8086 Confluence Strategy (Magic: 808801)
- **Timeframe**: 5-Minute institutional bar.
- **TA Score Requirement**: TA Score >= 40/40 (EMA 20/50/200, 1H EMA filter, RSI 14, MACD, Bollinger Bands, Fibonacci 38.2/50/61.8, Support/Resistance).
- **Order Flow Score Requirement**: OF Score >= 30/30 (Bar Delta +/- 30, CVD 5-period trend, Volume Absorption, POC migration).
- **Trigger**: Both TA >= 40 AND OF >= 30 -> Instant 0.01 lot order with 1:2 R:R (SL: $2.50, TP: $5.00).

### Model B: Pure SMC & Real-Time Order Flow Strategy (Magic: 808802)
- **Session Liquidity Sweeps**: Asian Judas High/Low, London LSH/LSL, Previous Day High (PDH), Previous Day Low (PDL).
- **Order Flow Confirmation**: Delta reversal (+/- 30 threshold) or CVD momentum confirmation.

### Tier-2 Anti-Manipulation Safety Shield
- Compares MT5 broker mid-price against independent spot feeds.
- If broker spread > $0.60, phantom wick > $0.45, or deviation > $0.85, trades are automatically blocked.

### 1:1 Auto Break-Even Guarantee
- Once floating profit reaches +$2.50 (1:1 R:R), Stop Loss is immediately moved to Entry + $0.15, guaranteeing zero risk and fee coverage.

### Ironclad 5-Minute Candle Bar Lock (Zero Re-Entry Rule)
- If a trade was active in the current 5M candle, and that trade closes (SL hit, TP hit, or manually closed in MT5), **ABSOLUTELY NO NEW TRADE CAN BE ENTERED WITHIN THAT SAME 5-MINUTE CANDLE**.
- The engine locks and displays `🔒 LOCKED (Xm Ys)` until the next 5M bar opens.

---

## 3. Port 8095 Master Quant Stream Terminal Specifications
- Real-time tick ingestion via WebSockets.
- Millisecond Bulk Volume Classification (BVC) & Order Flow Imbalance (OFI).
- Volume Profile rolling POC, VAH, VAL, and CVD.

---

## 4. API Keys Configuration
- **Port 8088 AllTick Token**: `e4432003c7fb8ef16dce2c8fbcf1ae57-c-app` (Dedicated)
- **Port 8088 & 8095 iTick Token**: `73f7322874cd42bbb126499c62a7c4c01dd60a55460745dabba192d9528e2bc1` (Shared between 8088 & 8095)
- **Port 8095 AllTick Token**: `4925bd3c3d58234362b2481ad684a0a7-c-app` (Dedicated)
- **Backup Spot Pool**: Handled automatically via `multi_api_key_pool.py`.

---

## 5. Historical Chat Context & Agreements
- **21-Sep-2026**: Dual-Model architecture formalized (Model A 8086 Confluence + Model B SMC).
- **22-Sep-2026**: Anti-manipulation shield, Equiti broker account binding, auto break-even, and 5M bar lock implemented.
- **28-Sep-2026**: Folder-level segregation executed into `8088&8095new` to maintain complete isolation from 8070 & 8080.
