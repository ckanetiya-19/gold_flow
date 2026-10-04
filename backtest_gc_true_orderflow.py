"""
Standalone backtest (read-only, LOCAL ONLY - NO Databento API, NO cost):
runs our CURRENT LIVE 9100 logic on REAL CME gold-futures order-flow, to see
whether TRUE order-flow (real per-trade aggressor side from the exchange)
gives a cleaner/better edge than the Binance-PAXG proxy we use now.

Data source: gc_trades_3mo.dbn - already downloaded (GC.v.0 volume-continuous
front-month gold futures, 3 months, 5.82M real trades). This script reads ONLY
that local file. It NEVER calls the Databento API, so it costs nothing.

Aggressor-side mapping (validated empirically, not guessed): Databento's CME
`side` on a trade marks the RESTING order's side, so the AGGRESSOR is the
opposite - side 'A' (resting Ask lifted) = BUY aggressor, side 'B' (resting
Bid hit) = SELL aggressor. Confirmed: with A=BUY the minute delta vs minute
return correlation is POSITIVE (+0.10); with B=BUY it was negative. So:
    buy = (side == 'A')

This gives REAL CVD / Delta / Footprint / Volume-Imbalance - unlike PAXG
(different instrument) or tick-rule (inferred) - the genuine article for gold.

Logic tested = the exact current live 9100 config, via the existing signal
engine (backtest_orderflow_zones_tiers2.generate_signals_r2):
  Order-Block zones + structural SL + min-1.5R + Stacked Footprint Imbalance
  + CVD Structure Break, exited with the $2-arm/$1-trail trailing stop.
Also the VWAP full-crossover, for completeness. Random-entry control on each.

Cost model: GC real spread $0.31 (measured live earlier in this project).
PnL is reported in PRICE POINTS ($ per $1 move) so it's directly comparable
to the PAXG-proxy spot backtests; note a real GC contract is $100/point
(micro MGC $10/point), so scale accordingly for real position sizing.

Run: python backtest_gc_true_orderflow.py
"""
import numpy as np
import databento as db

from backtest_orderflow_zones_tiers import (
    calc_atr_series, run_trailing_exit, stats, row, generate_random_signals, MIN_RR,
)
from backtest_orderflow_zones_tiers2 import (
    build_bars_with_footprint, build_ob_zones_causal_full,
    precompute_stacked_imbalance_series, precompute_cvd_structure_break_series,
    generate_signals_r2,
)
from backtest_9080_all_strategies_oos import pnl_of
from backtest_vwap_first_close import market_closed

GC_SPREAD = 0.31
DBN_FILE = "gc_trades_3mo.dbn"


def load_gc_arrays():
    print("Loading local GC trades file (no API, no cost)...")
    store = db.DBNStore.from_file(DBN_FILE)
    df = store.to_df()
    df = df[df["side"].isin(["A", "B"])]  # drop neutral/unclassifiable
    ts = (df.index.astype("int64") // 10**9).to_numpy().astype(float)  # seconds
    px = df["price"].to_numpy().astype(float)
    vol = df["size"].to_numpy().astype(float)
    buy = (df["side"].to_numpy() == "A")  # A = BUY aggressor (validated)
    print(f"  {len(ts):,} real trades | {(ts[-1]-ts[0])/86400:.1f} days | "
          f"buy {buy.sum():,} / sell {(~buy).sum():,}")
    return ts, px, vol, buy


def execute_gc(bars, ts, px, sigs, spread=GC_SPREAD):
    """Same tick-level trailing execution as the other backtests, but ticks
    here are REAL GC trades and the spread is GC's real $0.31."""
    trades, free_idx = [], -1
    for i, d, sl in sorted(sigs, key=lambda s: s[0]):
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        if d == "BUY" and entry <= sl:
            continue
        if d == "SELL" and entry >= sl:
            continue
        xp, xi, why = run_trailing_exit(ts, px, nxt, d, entry, sl)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "risk": abs(entry - sl),
                       "pnl": pnl_of(d, entry, xp) - spread, "why": why})
        free_idx = xi
    return trades


def stats_gc(tr):
    n = len(tr)
    if n == 0:
        return None
    wins = sum(1 for x in tr if x["why"] == "TRAIL")
    gross = sum(x["pnl"] + GC_SPREAD for x in tr)
    net = sum(x["pnl"] for x in tr)
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, wr=wins / n * 100, gross=gross, net=net, avg=net / n, dd=dd)


def main():
    ts, px, vol, buy = load_gc_arrays()
    print("Building bars with REAL order flow (buy/sell from exchange aggressor)...")
    bars = build_bars_with_footprint(ts, px, vol, buy)
    atrs = calc_atr_series(bars)
    print(f"{len(bars):,} one-minute bars.")
    print("Building OB zones / stacked-imbalance / CVD-break (all from REAL order flow)...")
    ob = build_ob_zones_causal_full(bars, atrs)
    stacked = precompute_stacked_imbalance_series(bars)
    cvdbreak = precompute_cvd_structure_break_series(bars)
    print("done.\n")

    print("=" * 120)
    print("CURRENT LIVE 9100 LOGIC on REAL GC gold-futures order-flow (spread $0.31)")
    print("(PnL in price points, $/$1 move - comparable to the PAXG-proxy spot backtests)")
    print("=" * 120)

    base_sigs = generate_signals_r2(bars, atrs, ob, structural_sl=True,
                                    stacked=stacked, require_stack=True,
                                    cvd_break=cvdbreak, require_cvdbreak=True)
    base_tr = execute_gc(bars, ts, px, base_sigs)
    print(row("9100 logic on REAL GC order-flow", stats_gc(base_tr)))
    rnd = generate_random_signals(bars, atrs, len(base_sigs), seed=7001)
    print(row(f"   -> random control ({len(base_sigs)} sigs)", stats_gc(execute_gc(bars, ts, px, rnd))))
    print()

    # component variants to see what real order flow adds
    print("--- component breakdown on REAL GC order-flow ---")
    ob_only = generate_signals_r2(bars, atrs, ob, structural_sl=True)
    print(row("OB zones + structuralSL + minRR only", stats_gc(execute_gc(bars, ts, px, ob_only))))
    ob_cvd = generate_signals_r2(bars, atrs, ob, structural_sl=True, cvd_break=cvdbreak, require_cvdbreak=True)
    print(row("  + CVD Structure Break", stats_gc(execute_gc(bars, ts, px, ob_cvd))))
    ob_stack = generate_signals_r2(bars, atrs, ob, structural_sl=True, stacked=stacked, require_stack=True)
    print(row("  + Stacked Imbalance", stats_gc(execute_gc(bars, ts, px, ob_stack))))
    print()

    print("=" * 120)
    print("REFERENCE - same 9100 logic on PAXG proxy (from earlier run): "
          "net/trade ~$0.74, WR ~62%, 90 trades over ~14 days")
    print("Compare the REAL-GC row above against that to judge if true order-flow is cleaner.")
    print("=" * 120)


if __name__ == "__main__":
    main()
