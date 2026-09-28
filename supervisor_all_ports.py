"""
===============================================================================
GOLDFLOW 24/7 DAEMON SUPERVISOR (ALL LOCALHOST PORTS: 8050, 8060, 8070, 8080, 8090)
===============================================================================
Features:
- Spawns all 5 port servers in independent subprocesses
- Continuously monitors port health and auto-revives any crashed/stopped server
- Zero-downtime auto-healing supervisor
===============================================================================
"""

import subprocess
import time
import os
import sys
import logging

# Fix Windows console encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

LOG_DIR = r"c:\Users\ckane\Desktop\goldflow1\server_logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "supervisor.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Supervisor")

SERVERS = [
    {
        "port": 8050,
        "name": "Port 8050 (V1 Legacy Order Flow)",
        "script": r"C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow\xauusd_pro.py",
        "cwd": r"C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow",
        "process": None
    },
    {
        "port": 8060,
        "name": "Port 8060 (V2 Institutional Dashboard)",
        "script": r"C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow\xauusd_v2_pro.py",
        "cwd": r"C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow",
        "process": None
    },
    {
        "port": 8070,
        "name": "Port 8070 (Bloomberg Web Terminal)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\8080&8070new\xauusd_terminal.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1\8080&8070new",
        "process": None
    },
    {
        "port": 8080,
        "name": "Port 8080 (AI Quant Sniper Terminal)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\8080&8070new\xauusd_quant_terminal.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1\8080&8070new",
        "process": None
    },
    {
        "port": 8086,
        "name": "Port 8086 (Confluence Backtest Lab)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\goldflow_backtest_8086.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1",
        "process": None
    },
    {
        "port": 8088,
        "name": "Port 8088 (GoldFlow Live Engine Model B Sweep Reset)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\8088&8095new\goldflow_live_engine_8088.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1\8088&8095new",
        "process": None
    },
    {
        "port": 8090,
        "name": "Port 8090 (Unified Trinity Radar & JARVIS)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\xauusd_satellite_radar.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1",
        "process": None
    },
    {
        "port": 8095,
        "name": "Port 8095 (Quant Stream Terminal BVC/OFI)",
        "script": r"c:\Users\ckane\Desktop\goldflow1\8088&8095new\xauusd_hybrid_stream_terminal.py",
        "cwd": r"c:\Users\ckane\Desktop\goldflow1\8088&8095new",
        "process": None
    }
]

# ─────────────────────────────────────────────────────────────────────────────
# NOTE: To make a clean restart of any individual port, simply kill its process.
#       The supervisor loop (every 4 seconds) will detect the dead process and
#       auto-revive it.  Port 8080 is intentionally NOT connected to MT5.
# ─────────────────────────────────────────────────────────────────────────────

def start_server(srv):
    port = srv["port"]
    out_file = open(os.path.join(LOG_DIR, f"port_{port}.log"), "a", encoding="utf-8")
    logger.info(f"[STARTING] {srv['name']} on port {port}...")
    proc = subprocess.Popen(
        [sys.executable, srv["script"]],
        cwd=srv["cwd"],
        stdout=out_file,
        stderr=subprocess.STDOUT
    )
    srv["process"] = proc
    logger.info(f"[ONLINE] {srv['name']} started (PID: {proc.pid})")
    return proc

def main():
    logger.info("=======================================================")
    logger.info("[SUPERVISOR] GOLDFLOW MASTER 24/7 DAEMON ACTIVATED")
    logger.info("=======================================================")
    
    # 1. Start all servers
    for srv in SERVERS:
        start_server(srv)
        time.sleep(1.5)

    # 2. Continuous Health Monitoring & Auto-Revival Loop
    last_health_log = time.time()
    while True:
        try:
            time.sleep(4)
            now = time.time()
            log_health = (now - last_health_log >= 60)
            if log_health:
                last_health_log = now

            for srv in SERVERS:
                proc = srv["process"]
                if proc is None or proc.poll() is not None:
                    exit_code = proc.poll() if proc else "NONE"
                    logger.warning(f"[REVIVING] {srv['name']} died (Exit code: {exit_code}). Auto-reviving NOW...")
                    start_server(srv)
                elif log_health:
                    logger.info(f"[HEALTH OK] {srv['name']} (PID: {proc.pid}) running normally")

        except KeyboardInterrupt:
            logger.info("[STOP] Supervisor stopped by user. Shutting down all children...")
            for srv in SERVERS:
                if srv["process"] and srv["process"].poll() is None:
                    srv["process"].terminate()
            break
        except Exception as e:
            logger.error(f"Supervisor error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
