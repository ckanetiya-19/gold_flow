================================================================================
          GOLDFLOW ECOSYSTEM - ALL LOCALHOST PORTS MASTER CODE ARCHIVE
================================================================================

This directory contains individual standalone source code files for every 
port developed in the Goldflow trading ecosystem.

FOLDER LOCATION: C:\Users\ckane\Desktop\GOLDFLOW_ALL_PORTS_CODE

--------------------------------------------------------------------------------
1. PORT 8000: CORE WEBSOCKET DATA ENGINE
--------------------------------------------------------------------------------
- File Name    : PORT_8000_CORE_WEBSOCKET_ENGINE.py (Original: app.py)
- Web / Socket : ws://localhost:8000/
- Role         : Multi-source live tick aggregation, PAXG/USDT stream, normalization.
- Run Command  : python PORT_8000_CORE_WEBSOCKET_ENGINE.py

--------------------------------------------------------------------------------
2. PORT 8050: V1 LEGACY ORDER FLOW DASHBOARD
--------------------------------------------------------------------------------
- File Name    : PORT_8050_V1_LEGACY_ORDERFLOW.py (Original: xauusd_pro.py)
- Web Browser  : http://localhost:8050/
- Role         : Classic 1m Candlesticks, Delta Volume Bars, TradingView iframe.
- Run Command  : python PORT_8050_V1_LEGACY_ORDERFLOW.py

--------------------------------------------------------------------------------
3. PORT 8060: V2 INSTITUTIONAL CONFLUENCE DASHBOARD
--------------------------------------------------------------------------------
- File Name    : PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py (Original: xauusd_v2_pro.py)
- Web Browser  : http://localhost:8060/
- Role         : 0-100% Confluence, True Dynamic VWAP, Value Area Bands, Auto Gap Backfill.
- Run Command  : python PORT_8060_V2_INSTITUTIONAL_DASHBOARD.py

--------------------------------------------------------------------------------
4. PORT 8070: BLOOMBERG WEB TERMINAL V1
--------------------------------------------------------------------------------
- File Name    : PORT_8070_BLOOMBERG_WEB_TERMINAL.py (Original: xauusd_terminal.py)
- Web Browser  : http://localhost:8070/
- Role         : Depth of Market (DOM) Ladder, Time & Sales Tape, Footprint, Blotter.
- Run Command  : python PORT_8070_BLOOMBERG_WEB_TERMINAL.py

--------------------------------------------------------------------------------
5. PORT 8080: AI QUANT EXECUTION TERMINAL
--------------------------------------------------------------------------------
- File Name    : PORT_8080_AI_QUANT_TERMINAL.py (Original: xauusd_quant_terminal.py)
- Web Browser  : http://localhost:8080/
- Role         : Dual Engine, CHOP >= 60 Sideways Filter, - TP Sniper,
                 Instant 1s Execution, Dedicated Red [ ⛔ MANUAL CUT ] Button.
- Run Command  : python PORT_8080_AI_QUANT_TERMINAL.py

--------------------------------------------------------------------------------
6. PORT 8090: UNIFIED TRINITY MACRO RADAR & JARVIS AI COMMAND CENTER
--------------------------------------------------------------------------------
- File Name    : PORT_8090_UNIFIED_TRINITY_RADAR_JARVIS.py (Original: xauusd_satellite_radar.py)
- Web Browser  : http://localhost:8090/
- Role         : 5 Autonomous AI Agents (with Risk Sentinel VETO), 
                 26+ Central Banks WGC Radar, ForexFactory 30-60s Leak Scanner,
                 JARVIS v2 Voice Brain, Panoramic TradingView SL/TP Lines & Crypto Switcher.
- Run Command  : python PORT_8090_UNIFIED_TRINITY_RADAR_JARVIS.py

================================================================================
Generated for ckane | DeepMind Antigravity AI Engineering Team
================================================================================
