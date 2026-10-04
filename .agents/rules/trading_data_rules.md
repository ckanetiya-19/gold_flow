# INSTITUTIONAL TRADING DATA & ORDER FLOW ARCHITECTURE RULES

## CRITICAL IMMUTABLE PRINCIPLES

1. **NEVER USE MT5 FOR CANDLES, ORDER FLOW, OR TECHNICAL INDICATORS:**
   - Broker MT5 feeds (`XAUUSD.sd`, etc.) contain broker-specific spread markups, simulated tick volume, stop-loss hunting wicks, and phantom price spikes.
   - MT5 data must NEVER be used to calculate:
     - Candlestick bars (OHLCV)
     - Cumulative Volume Delta (CVD)
     - Order Flow Delta / Imbalance
     - Volume Profile & Point of Control (POC)
     - Volume Weighted Average Price (VWAP)
     - Technical indicators (EMA, RSI, MACD, Bollinger Bands, ATR)
     - Smart Money Concepts (SMC) levels: Asian/London/NY Session Highs/Lows, PDH, PDL, Liquidity Sweeps

2. **INSTITUTIONAL DATA SOURCES ONLY:**
   - All candlesticks and order flow metrics MUST be generated strictly from institutional, external interbank feeds:
     - **AllTick WebSocket / REST (`GOLD` / `XAUUSD`)**
     - **TwelveData WebSocket / REST (`XAU/USD`)**
     - **iTick WebSocket / REST (`XAUUSD`)**
     - **RealMarketAPI**

3. **ROLE OF MT5:**
   - MT5 is strictly and exclusively the **Execution Bridge**:
     - Submitting market orders (`mt5.order_send`)
     - Setting Stop Loss and Take Profit targets
     - Moving Stop Loss to Break-Even (`TRADE_ACTION_SLTP`)
     - Querying live Account Balance, Equity, and Floating PnL
     - Polling open positions to sync trade blotter

4. **NO CRYPTO ASSETS FOR GOLD (PURGE BINANCE):**
   - Binance PAXG/USDT is a crypto token traded in Tether (USDT), not physical/spot Gold (XAUUSD).
   - Never use Binance or crypto tokens as a baseline or fallback for Gold deviation shields or order flow.

5. **DEVIATION SHIELD:**
   - The Deviation Shield verifies that the MT5 broker quote does not artificially deviate from the true Institutional Spot price.
   - Tolerance is tight ($0.85 to $1.00 max).
