@echo off
title GOLDFLOW 8070 & 8080 SYSTEM
cd /d "%~dp0"
set "PY=C:\Users\ckane\AppData\Local\Programs\Python\Python314\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"

echo Starting Port 8070 (Bloomberg Web Terminal)...
start /B "" "%PY%" xauusd_terminal.py

timeout /t 2 /nobreak >nul

echo Starting Port 8080 (AI Quant Sniper Terminal)...
start /B "" "%PY%" xauusd_quant_terminal.py

echo Done! Both Port 8070 and 8080 are running in background.
