"""Report of the FINAL 6 VWAP paper variants (1-minute only), read-only.
Uses port 9080 only (which runs all 6 variants; 9060 runs the same 6 and produces the same V1 trades).
Run: python vwap_fc_daily_report.py                 (all days, per variant)
     python vwap_fc_daily_report.py 2026-09-22      (one UTC day)
     python vwap_fc_daily_report.py 2026-09-22 T1   (one day, one variant, with trade list)
PnL = $ per 0.01 lot ($1 per $1 move); NET = after $0.24 spread per trade."""
import sqlite3
import sys

import vwap_first_close as vfc

day = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-" else None
only = sys.argv[2] if len(sys.argv) > 2 else None
conn = sqlite3.connect(vfc.DB_FILE)
variant_list = ",".join(f"'{v}'" for v in vfc.VARIANTS)
where = f"port='9080' AND tf=1 AND COALESCE(variant,'V1') IN ({variant_list}) AND outcome NOT IN ('OPEN','ABORTED_RESTART')"
if day:
    where += f" AND substr(open_utc,1,10)='{day}'"
if only:
    where += f" AND COALESCE(variant,'V1')='{only}'"

rows = conn.execute(f"""SELECT COALESCE(variant,'V1') v, COUNT(*), SUM(outcome IN ('WIN','TRAIL')), SUM(outcome='BE'), SUM(outcome='LOSS'),
    ROUND(SUM(gross),2), ROUND(SUM(net),2), ROUND(AVG(net),3), ROUND(AVG(risk),2), ROUND(AVG(hold_min),0)
    FROM trades WHERE {where} GROUP BY v""").fetchall()
by_v = {r[0]: r for r in rows}
print(f"Period: {day or 'ALL days'}   (port 9080, unique trades, 1-min only)\n")
print(f"{'Var':4s} {'Description':40s} {'Trades':>6s} {'Win':>4s} {'BE':>3s} {'Loss':>4s} {'Gross$':>8s} {'NET$':>8s} {'Net/tr':>7s} {'AvgSL$':>7s} {'Hold':>5s}")
tot = 0.0
for v, cfg in vfc.VARIANTS.items():
    r = by_v.get(v)
    if r is None:
        print(f"{v:4s} {cfg['desc']:40s}      0    -   -    -        -        -       -       -     -")
        continue
    print(f"{r[0]:4s} {cfg['desc']:40s} {r[1]:6d} {r[2]:4d} {r[3]:3d} {r[4]:4d} {r[5]:8.2f} {r[6]:8.2f} {r[7]:7.3f} {r[8]:7.2f} {r[9]:5.0f}")
    tot += r[6]
print(f"\nAll 6 variants combined NET: ${tot:.2f}")

print("\nOpen now:")
for r in conn.execute(f"SELECT COALESCE(variant,'V1'), port, dir, open_utc, entry, sl_initial, tp FROM trades "
                       f"WHERE outcome='OPEN' AND port='9080' AND tf=1 AND COALESCE(variant,'V1') IN ({variant_list}) ORDER BY id DESC LIMIT 20"):
    print("  ", r)
if only:
    print(f"\nTrades of {only}:")
    for r in conn.execute(f"SELECT dir, open_utc, entry, exit, outcome, ROUND(net,2), ROUND(hold_min,0) FROM trades WHERE {where} ORDER BY id"):
        print("  ", r)
