@echo off
title GOLDFLOW 8088 & 8095 SYSTEM
cd /d "%~dp0"
set "PY=C:\Users\ckane\AppData\Local\Programs\Python\Python314\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"

echo Starting Port 8088 (Dual-Model Live Engine)...
start /B "" "%PY%" goldflow_live_engine_8088.py

timeout /t 2 /nobreak >nul

echo Starting Port 8095 (Master Quant Stream Terminal)...
start /B "" "%PY%" xauusd_hybrid_stream_terminal.py

echo Done! Both Port 8088 and 8095 are running in background.
