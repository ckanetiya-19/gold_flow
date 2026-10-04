"""Replay real ticks through the LIVE module (vwap_first_close.py, FINAL 6 variants, 1-min only)
into a temp DB and compare with the standalone backtest scripts on the same ticks.
Read-only w.r.t. live ports."""
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import vwap_first_close as vfc
from backtest_9080_all_strategies_oos import load_ticks
from backtest_vwap_first_close import build_bars
from backtest_vwap_first_close_be import execute as be_exec
from backtest_vwap_first_vwapsl import execute as vsl_exec
from backtest_vwap_retest import make_signals as ms
from backtest_vwap_retest_vwapsl import execute as rt_vwap_exec
from backtest_vwap_trailing import execute_trailing

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("replay")

ts, px, vol, buy = load_ticks()
tmp = os.path.join(tempfile.mkdtemp(), "replay.db")
daily = vfc.DailyVwap()
strats = [vfc.make_variant(v, "REPLAY", 1, daily, log, None, tmp) for v in vfc.VARIANTS]
for s in strats:
    s.used = False     # replay must not treat the first bar as "unknown history"

cur = None
for i in range(len(ts)):
    now = datetime.fromtimestamp(ts[i], timezone.utc)
    minute = int(ts[i] // 60) * 60
    if cur is not None and minute > cur["m"]:
        start = datetime.fromtimestamp(cur["m"], timezone.utc)
        for s in strats:
            s.on_bar(cur["o"], cur["h"], cur["l"], cur["c"], start, px[i], now)
        cur = None
    for s in strats:
        s.on_price(px[i], now)
    daily.update(px[i], vol[i], now)
    if cur is None:
        cur = {"m": minute, "o": px[i], "h": px[i], "l": px[i], "c": px[i]}
    else:
        cur["h"] = max(cur["h"], px[i]); cur["l"] = min(cur["l"], px[i]); cur["c"] = px[i]

conn = sqlite3.connect(tmp)
bars = build_bars(ts, px, vol, 60)
first = ms(bars, 0.0, True, False)
retest_b = ms(bars, 0.5, False, True)
expected = {
    "V1": be_exec(bars, ts, px, [(i, d) for i, d, k in first], use_be=True),
    "V4": vsl_exec(bars, ts, px, first, 0.5),
    "V10": rt_vwap_exec(bars, ts, px, retest_b, 0.5),
    "T1": execute_trailing(bars, ts, px, first),
    "T2": execute_trailing(bars, ts, px, first, 0.5),
    "T3": execute_trailing(bars, ts, px, retest_b, 0.5),
}

print(f"{'Variant':5s} | {'REPLAY n':>8s} {'net':>9s} | {'BACKTEST n':>10s} {'net':>9s} | description")
for v, cfg in vfc.VARIANTS.items():
    r = conn.execute("SELECT COUNT(*), ROUND(SUM(net),2) FROM trades WHERE tf=1 AND variant=? AND outcome NOT IN ('OPEN')", (v,)).fetchone()
    e = expected[v]
    en = len(e)
    enet = round(sum(x["pnl"] for x in e) - 0.24 * en, 2)
    print(f"{v:5s} | {r[0]:8d} {r[1] if r[1] is not None else 0:9.2f} | {en:10d} {enet:9.2f} | {cfg['desc']}")
