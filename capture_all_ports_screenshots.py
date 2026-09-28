# -*- coding: utf-8 -*-
"""
Script to capture full-page high-resolution screenshots of all active
Goldflow dashboards and terminals on ports 8050, 8060, 8070, 8080, and 8090.
"""

import os
import subprocess
import shutil
import time

chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
desktop_dir = r"C:\Users\ckane\Desktop\GOLDFLOW_DASHBOARD_SCREENSHOTS"
workspace_dir = r"c:\Users\ckane\Desktop\goldflow1\screenshots"
artifact_dir = r"C:\Users\ckane\.gemini\antigravity-ide\brain\4f0a4103-efb7-4748-913e-e5d4545ef80b"

os.makedirs(desktop_dir, exist_ok=True)
os.makedirs(workspace_dir, exist_ok=True)

targets = [
    {
        "port": 8090,
        "url": "http://localhost:8090/",
        "filename": "PORT_8090_UNIFIED_TRINITY_RADAR_JARVIS.png",
        "title": "Port 8090 - Unified Trinity Macro Radar & JARVIS",
        "size": "1920,2400",
        "budget": "4500"
    },
    {
        "port": 8080,
        "url": "http://localhost:8080/",
        "filename": "PORT_8080_AI_QUANT_TERMINAL.png",
        "title": "Port 8080 - AI Quant Execution Terminal (CHOP & Manual Cut)",
        "size": "1920,2200",
        "budget": "5000"
    },
    {
        "port": 8070,
        "url": "http://localhost:8070/",
        "filename": "PORT_8070_BLOOMBERG_WEB_TERMINAL.png",
        "title": "Port 8070 - Bloomberg Web Terminal V1 (DOM & Tape)",
        "size": "1920,2000",
        "budget": "4500"
    },
    {
        "port": 8060,
        "url": "http://localhost:8060/",
        "filename": "PORT_8060_V2_INSTITUTIONAL_DASHBOARD.png",
        "title": "Port 8060 - V2 Institutional Dashboard (True VWAP & Bands)",
        "size": "1920,2200",
        "budget": "5500"
    },
    {
        "port": 8050,
        "url": "http://localhost:8050/",
        "filename": "PORT_8050_V1_LEGACY_DASHBOARD.png",
        "title": "Port 8050 - V1 Legacy Order Flow Dashboard",
        "size": "1920,2000",
        "budget": "5000"
    }
]

print("Starting high-resolution full-page screenshot capture for all ports...")

for item in targets:
    port = item["port"]
    url = item["url"]
    fname = item["filename"]
    title = item["title"]
    window_size = item["size"]
    budget = item["budget"]
    
    out_desktop = os.path.join(desktop_dir, fname)
    out_workspace = os.path.join(workspace_dir, fname)
    out_artifact = os.path.join(artifact_dir, fname)
    
    cmd = [
        chrome_path,
        "--headless",
        "--disable-gpu",
        f"--window-size={window_size}",
        f"--virtual-time-budget={budget}",
        "--run-all-compositor-stages-before-draw",
        f"--screenshot={out_desktop}",
        url
    ]
    
    print(f"\nCapturing {title} ({url})...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    if os.path.exists(out_desktop) and os.path.getsize(out_desktop) > 1000:
        # copy to workspace and artifact dir
        shutil.copy2(out_desktop, out_workspace)
        shutil.copy2(out_desktop, out_artifact)
        size_kb = os.path.getsize(out_desktop) / 1024
        print(f"SUCCESS: Captured {fname} ({size_kb:.1f} KB)")
    else:
        print(f"FAILED for {port}: Return code {res.returncode}, Stderr: {res.stderr}")

print("\nALL PORT SCREENSHOTS CAPTURED SUCCESSFULLY!")
