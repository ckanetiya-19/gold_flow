"""
bar_store.py - tiny SQLite persistence for 1-minute chart bars so a port's
candle history SURVIVES a restart (9060/9080 previously kept bars only in
memory; 9100/9900 already had their own persistence). Reusable, no deps.

Persists the essential fields; reconstructs the extra dashboard fields with
safe defaults on load so the existing chart/callbacks never KeyError.
"""
import sqlite3
from datetime import datetime, timezone, timedelta

_COLS = ("time", "open", "high", "low", "close", "volume",
         "buy_vol", "sell_vol", "delta", "cvd", "vwap", "poc")


def connect(dbfile):
    conn = sqlite3.connect(dbfile, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS bars (
        time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
        volume REAL, buy_vol REAL, sell_vol REAL, delta REAL, cvd REAL,
        vwap REAL, poc REAL)""")
    conn.commit()
    return conn


def _epoch(t):
    if t is None:
        return None
    return int(t.timestamp()) if hasattr(t, "timestamp") else int(t)


def save(conn, bar):
    """Save/replace one finalized bar (keyed by its minute)."""
    e = _epoch(bar.get("time"))
    if e is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (e, bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"),
             float(bar.get("volume", 0) or 0), float(bar.get("buy_vol", 0) or 0),
             float(bar.get("sell_vol", 0) or 0), float(bar.get("delta", 0) or 0),
             float(bar.get("cvd", 0) or 0), float(bar.get("vwap", 0) or 0),
             float(bar.get("poc", bar.get("close", 0)) or 0)))
        conn.commit()
    except Exception:
        pass


def save_many(conn, bars):
    for b in bars:
        save(conn, b)


def load(conn, retention_days=14):
    """Return bars (oldest->newest) as full dashboard dicts."""
    try:
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp())
        conn.execute("DELETE FROM bars WHERE time < ?", (cutoff,))
        conn.commit()
        rows = conn.execute(
            "SELECT time,open,high,low,close,volume,buy_vol,sell_vol,delta,cvd,vwap,poc "
            "FROM bars ORDER BY time ASC").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        t, o, h, l, c, v, bv, sv, dl, cv, vw, poc = r
        out.append({
            "time": datetime.fromtimestamp(t), "open": o, "high": h, "low": l, "close": c,
            "volume": v, "buy_vol": bv, "sell_vol": sv, "delta": dl, "cvd": cv,
            "vwap": vw, "poc": poc,
            # dashboard extras rebuilt with safe defaults
            "imbalance": False, "fvg": None, "phase": "Neutral", "confidence": 50,
            "absorption": None, "cvd_divergence": None, "signal_type": "NONE",
            "session": "", "levels": {},
        })
    return out
