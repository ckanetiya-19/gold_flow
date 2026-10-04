"""
GOLDFLOW TRADE REPORT GENERATOR
============================================================================
Builds an Excel report with two parts:
  Part 1: Full historical trade list from each port's own SQLite database
          (9050, 9060, 9070, 9080 dual-engine). No date is stored in these
          tables (time-of-day only), so this is the complete history each
          DB currently holds, not filtered to "today" specifically.
  Part 2: TODAY's trades only (9060 + 9080, including 9080's 5 scalp
          strategies), reconstructed from the live log files (which DO
          carry full date-time) since the last restart at ~12:11 IST today.
          For every SL-hit trade, the Max Favorable Excursion (MFE) - how
          far price moved in the trade's favor before reversing to hit SL -
          is reconstructed from QuestDB's stored tick history.

CAVEAT: QuestDB's ticks_v2 table stores real Binance PAXG trade ticks only
(the volume/CVD source). It does NOT store iTick's real spot XAUUSD ticks
(iTick is broadcast live but never persisted). So MFE figures here are
computed against the PAXG price series, which can differ from the real
spot price the candles/SL are now driven by - treat MFE as approximate,
not an exact match to what a broker would have shown.

Run: python generate_trade_report.py
Output: Goldflow_Trade_Report_<date>.xlsx in this same folder.
"""

import sqlite3
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime
import pandas as pd

BASE_DIR = r"C:\Users\ckane\Desktop\claud order flow"
QUESTDB_URL = "http://127.0.0.1:9010/exec"

DB_FILES = {
    "9050 (Legacy Order Flow)": "trades_v2_9050.db",
    "9060 (Institutional)": "trades_v2_9060.db",
    "9070 (Bloomberg Terminal)": "trades_terminal_v2_9070.db",
    "9080 (AI Quant Terminal)": "trades_quant_v2_9080.db",
}

LOG_FILES = {
    "9060": r"C:\Users\ckane\AppData\Local\Temp\claude\C--Users-ckane-Desktop-claud-order-flow\502feb35-cdbb-4c96-937b-cd8715b8e50f\scratchpad\9060.log",
    "9080": r"C:\Users\ckane\AppData\Local\Temp\claude\C--Users-ckane-Desktop-claud-order-flow\502feb35-cdbb-4c96-937b-cd8715b8e50f\scratchpad\9080.log",
}


def questdb_query(sql):
    url = QUESTDB_URL + "?" + urllib.parse.urlencode({"query": sql})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("dataset", [])
    except Exception as e:
        print(f"QuestDB query failed: {e}")
        return []


def compute_mfe(direction, entry, open_ts, close_ts):
    """direction: BUY/LONG or SELL/SHORT. Returns (mfe_dollars, tick_count) or (None, 0)."""
    sql = (
        f"SELECT price FROM ticks_v2 WHERE symbol='XAUUSD' "
        f"AND timestamp >= '{open_ts}' AND timestamp <= '{close_ts}'"
    )
    rows = questdb_query(sql)
    if not rows:
        return None, 0
    prices = [r[0] for r in rows]
    is_long = direction in ("BUY", "LONG")
    if is_long:
        mfe = max(prices) - entry
    else:
        mfe = entry - min(prices)
    return round(mfe, 2), len(prices)


# =============================================================================
# PART 1: full historical trade list per port, straight from each SQLite DB
# =============================================================================
def build_part1():
    sheets = {}
    for label, fname in DB_FILES.items():
        path = f"{BASE_DIR}\\{fname}"
        try:
            conn = sqlite3.connect(path)
            df = pd.read_sql_query("SELECT * FROM trades ORDER BY id", conn)
            conn.close()
            sheets[label] = df
        except Exception as e:
            print(f"Could not read {fname}: {e}")
    return sheets


# =============================================================================
# PART 2: today's trades (since last restart ~12:11 IST) with MFE for SL hits
# =============================================================================
TS_RE = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+"


def parse_9060_log(path):
    trades = []
    pending = None
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    for line in lines:
        m = re.search(TS_RE + r" - \[V2 PAPER\] (BUY|SELL) ([\d.]+) XAUUSD @ \$([\d.]+) \| SL: \$([\d.]+) \| TP: \$([\d.]+)", line)
        if m:
            ts, direction, lot, entry, sl, tp = m.groups()
            pending = {
                "Port": "9060", "Strategy": "Confluence", "Direction": direction,
                "Lot": float(lot), "Entry": float(entry), "SL": float(sl), "TP": float(tp),
                "Open_Time": ts, "Close_Time": None, "Exit": None, "PnL": None,
            }
            continue
        m = re.search(TS_RE + r" - \[(TP|SL) HIT\] P/L: \$(-?[\d.]+)", line)
        if m and pending is not None:
            ts, reason, pnl = m.groups()
            pending["Close_Time"] = ts
            pending["Exit"] = f"{reason} HIT"
            pending["PnL"] = float(pnl)
            trades.append(pending)
            pending = None
    if pending is not None:
        trades.append(pending)  # still open
    return trades


def parse_9080_log(path):
    trades = []
    pending_main = None
    pending_scalp = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    for line in lines:
        m = re.search(TS_RE + r" - .*?\[QUANT SNIPER (BUY|SELL)\] Entry: \$([\d.]+) \| SL: \$([\d.]+) \| TP: \$([\d.]+)", line)
        if m:
            ts, direction, entry, sl, tp = m.groups()
            pending_main = {
                "Port": "9080", "Strategy": "Quant Sniper", "Direction": direction,
                "Lot": 0.01, "Entry": float(entry), "SL": float(sl), "TP": float(tp),
                "Open_Time": ts, "Close_Time": None, "Exit": None, "PnL": None,
            }
            continue

        m = re.search(TS_RE + r" - .*?\[TRADE CLOSED: (.+?)\] Exit: \$([\d.]+) \| PnL: \$(-?[\d.]+)", line)
        if m and pending_main is not None:
            ts, reason, exit_price, pnl = m.groups()
            pending_main["Close_Time"] = ts
            pending_main["Exit"] = reason
            pending_main["PnL"] = float(pnl)
            trades.append(pending_main)
            pending_main = None
            continue

        m = re.search(TS_RE + r" - .*?\[SCALP:(\w+)\] (BUY|SELL) @ ([\d.]+) SL=([\d.]+) TP=([\d.]+)", line)
        if m:
            ts, name, direction, entry, sl, tp = m.groups()
            pending_scalp[name] = {
                "Port": "9080", "Strategy": f"Scalp:{name}", "Direction": direction,
                "Lot": 0.01, "Entry": float(entry), "SL": float(sl), "TP": float(tp),
                "Open_Time": ts, "Close_Time": None, "Exit": None, "PnL": None,
            }
            continue

        m = re.search(TS_RE + r" - .*?\[SCALP:(\w+)\] (TP|SL) HIT @ ([\d.]+) \(entry was ([\d.]+)\)", line)
        if m and m.group(1) in pending_scalp:
            ts, name, reason, exit_price, _entry = m.groups()
            rec = pending_scalp.pop(name)
            rec["Close_Time"] = ts
            rec["Exit"] = f"{reason} HIT"
            is_long = rec["Direction"] == "BUY"
            exit_price = float(exit_price)
            rec["PnL"] = round((exit_price - rec["Entry"]) if is_long else (rec["Entry"] - exit_price), 2)
            trades.append(rec)

    if pending_main is not None:
        trades.append(pending_main)
    for rec in pending_scalp.values():
        trades.append(rec)
    return trades


def build_part2():
    today_trades = parse_9060_log(LOG_FILES["9060"]) + parse_9080_log(LOG_FILES["9080"])
    for t in today_trades:
        if t["Close_Time"] and t["Exit"] and "SL" in t["Exit"]:
            mfe, n = compute_mfe(t["Direction"], t["Entry"], t["Open_Time"], t["Close_Time"])
            t["MFE_$_before_SL"] = mfe if mfe is not None else "N/A (no ticks)"
            t["MFE_ticks_used"] = n
        else:
            t["MFE_$_before_SL"] = ""
            t["MFE_ticks_used"] = ""
    return pd.DataFrame(today_trades)


def main():
    part1_sheets = build_part1()
    part2_df = build_part2()

    today_str = datetime.now().strftime("%Y-%m-%d")
    out_path = f"{BASE_DIR}\\Goldflow_Trade_Report_{today_str}.xlsx"

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        part2_df.to_excel(writer, sheet_name="Today (since 12-11 restart)", index=False)
        for label, df in part1_sheets.items():
            safe_name = label.split(" ")[0] + "_full_history"
            df.to_excel(writer, sheet_name=safe_name[:31], index=False)

    print(f"Report written: {out_path}")
    print(f"Today's trades found: {len(part2_df)}")
    if not part2_df.empty:
        sl_rows = part2_df[part2_df["Exit"].astype(str).str.contains("SL", na=False)]
        print(f"SL-hit trades with MFE computed: {len(sl_rows)}")


if __name__ == "__main__":
    main()
