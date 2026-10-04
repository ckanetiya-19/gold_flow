"""
Standalone backtest (read-only): does Value Area (VAH/VAL) help the CURRENT
LIVE 9100 logic? Requested by the user - "current logic sathe VA backtest
karo, match jovo, ane VA sathe ferfar karva jevu lage to change kari ne
backtest karo. NO live changes, backtest only."

This file imports the existing backtest machinery (backtest_orderflow_zones_tiers.py
and _tiers2.py) and DOES NOT modify any live or backtest file - it only reads
from them and post-filters/re-exits signals for the VA experiments.

CURRENT LIVE 9100 LOGIC (the baseline reproduced here exactly):
  Order-Block zones + structural SL (origin candle high/low) + min-1.5R gate
  + Stacked Footprint Imbalance required + CVD Structure Break required,
  exited with the $2-arm/$1-trail trailing stop (no fixed TP), $0.24 spread.

VA VARIANTS TESTED (each vs the baseline, each vs a random control):
  BASE          - current live logic, unchanged (reference)
  VA_INSIDE     - only take the trade if entry sits INSIDE the value area
                  (VAL..VAH) - "trade only in balance / accepted price"
  VA_EDGE       - only take the trade if it's a reversal off a value-area
                  EDGE: BUY only when entry <= VAL (+tol), SELL only when
                  entry >= VAH (-tol) - the classic Market-Profile
                  responsive-reversal idea
  VA_OUTSIDE    - only take the trade if entry is OUTSIDE the value area
                  (breakout / imbalance continuation)
  VA_TARGET     - keep every baseline entry, but CHANGE THE EXIT: instead of
                  pure trailing, also exit at the opposite value-area edge
                  (BUY targets VAH, SELL targets VAL) whichever comes first
                  with the trail - this is the "ferfar" the user allowed:
                  using VA as a profit target inside the exit

Value Area is the rolling 6-hour session POC/VAH/VAL (70% volume band),
computed causally from real tick volume - identical to the compute_session_va
already in backtest_orderflow_zones_tiers.py.

Data: real Binance PAXG ticks from QuestDB (same documented project-wide
caveat: historical iTick spot was never stored; PAXG is the closest real
proxy). FULL period, no in-sample/out-of-sample split (per user's standing
preference). Random-entry control on every variant.

Run: python backtest_9100_value_area.py
"""
from backtest_9080_all_strategies_oos import load_ticks
from backtest_orderflow_zones_tiers import (
    calc_atr_series, precompute_va_series, run_trailing_exit,
    stats, row, generate_random_signals, MIN_RR, SPREAD, TRAIL_ARM, TRAIL_DIST,
)
from backtest_orderflow_zones_tiers2 import (
    build_bars_with_footprint, build_ob_zones_causal_full,
    precompute_stacked_imbalance_series, precompute_cvd_structure_break_series,
    generate_signals_r2, execute,
)
from backtest_9080_all_strategies_oos import pnl_of
from backtest_vwap_first_close import market_closed

VA_TOL = 1.0  # $ tolerance for "at" a value-area edge / inside


def entry_price_of(bars, i, px):
    """Actual fill = first tick of the next bar, exactly like execute()."""
    nxt = bars[i]["next_tick"]
    return px[nxt] if nxt < len(px) else bars[i]["close"]


def va_filter(sigs, bars, px, va_series, mode):
    """Post-filter baseline signals by where the entry sits vs the value area.
    Keeps the SAME entries/SLs the live logic produced - only drops the ones
    that fail the VA condition, so this is a pure confluence test."""
    out = []
    for i, d, sl in sigs:
        va = va_series[i]
        if va is None:
            continue
        entry = entry_price_of(bars, i, px)
        val, vah = va["val"], va["vah"]
        if mode == "INSIDE":
            if val - VA_TOL <= entry <= vah + VA_TOL:
                out.append((i, d, sl))
        elif mode == "EDGE":
            if d == "BUY" and entry <= val + VA_TOL:
                out.append((i, d, sl))
            elif d == "SELL" and entry >= vah - VA_TOL:
                out.append((i, d, sl))
        elif mode == "OUTSIDE":
            if entry < val - VA_TOL or entry > vah + VA_TOL:
                out.append((i, d, sl))
    return out


def run_trailing_with_va_target(ts, px, start, d, entry, sl0, va_target):
    """Same $2-arm/$1-trail trailing exit, but also take profit if the
    opposite value-area edge is reached first."""
    long_ = d == "BUY"
    best = entry
    sl = sl0
    armed = False
    for k in range(start, len(ts)):
        p = px[k]
        if long_:
            if va_target is not None and p >= va_target:
                return va_target, k, "VA_TP"
            best = max(best, p)
            if not armed and (best - entry) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = max(sl, best - TRAIL_DIST)
            if p <= sl:
                return sl, k, ("TRAIL" if armed else "SL")
        else:
            if va_target is not None and p <= va_target:
                return va_target, k, "VA_TP"
            best = min(best, p)
            if not armed and (entry - best) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = min(sl, best + TRAIL_DIST)
            if p >= sl:
                return sl, k, ("TRAIL" if armed else "SL")
    return px[-1], len(ts) - 1, "OPEN"


def execute_va_target(bars, ts, px, sigs, va_series):
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
        va = va_series[i]
        target = None
        if va is not None:
            if d == "BUY" and va["vah"] > entry:
                target = va["vah"]
            elif d == "SELL" and va["val"] < entry:
                target = va["val"]
        xp, xi, why = run_trailing_with_va_target(ts, px, nxt, d, entry, sl, target)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "risk": abs(entry - sl), "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days real PAXG ticks | spread ${SPREAD} | "
          f"trail ${TRAIL_ARM}/${TRAIL_DIST} | min {MIN_RR}R | VA=rolling 6h POC/VAH/VAL\n")

    bars = build_bars_with_footprint(ts, px, vol, buy)
    atrs = calc_atr_series(bars)
    print(f"{len(bars)} bars built. Building OB zones / stacked-imb / CVD-break / Value Area (causal)...")
    ob = build_ob_zones_causal_full(bars, atrs)
    stacked = precompute_stacked_imbalance_series(bars)
    cvdbreak = precompute_cvd_structure_break_series(bars)
    va_series = precompute_va_series(bars, ts, px, vol)
    print("done.\n")

    # ---- BASELINE = the exact current live 9100 logic ----
    base_sigs = generate_signals_r2(bars, atrs, ob, structural_sl=True,
                                    stacked=stacked, require_stack=True,
                                    cvd_break=cvdbreak, require_cvdbreak=True)
    base_tr = execute(bars, ts, px, base_sigs)
    base_s = stats(base_tr)

    print("=" * 122)
    print("BASELINE = CURRENT LIVE 9100 LOGIC (for reference)")
    print("=" * 122)
    print(row("BASE (live 9100)", base_s))
    print()

    print("=" * 122)
    print("VALUE AREA as an ENTRY FILTER (same entries/SL/exit, just dropped by VA condition)")
    print("=" * 122)
    for seed_i, mode in enumerate(["INSIDE", "EDGE", "OUTSIDE"]):
        fsigs = va_filter(base_sigs, bars, px, va_series, mode)
        tr = execute(bars, ts, px, fsigs)
        s = stats(tr)
        kept = f"{len(fsigs)}/{len(base_sigs)} of baseline signals kept"
        print(row(f"VA_{mode} ({kept})", s))
        rnd = generate_random_signals(bars, atrs, len(fsigs), seed=5000 + seed_i)
        print(row(f"   -> random control ({len(fsigs)} sigs)", stats(execute(bars, ts, px, rnd))))
        print()

    print("=" * 122)
    print("VALUE AREA as an EXIT TARGET (all baseline entries; exit = trail OR opposite VA edge)")
    print("=" * 122)
    va_tr = execute_va_target(bars, ts, px, base_sigs, va_series)
    va_s = stats(va_tr)
    n_vatp = sum(1 for t in va_tr if t["why"] == "VA_TP")
    print(row(f"VA_TARGET ({n_vatp} exits at VA edge)", va_s))
    print(row("   (compare) BASE pure-trail", base_s))
    print()

    print("=" * 122)
    print("VERDICT GUIDE")
    print("=" * 122)
    print("A VA variant is worth adding to live ONLY if its net/trade beats BOTH")
    print("the BASE row AND its own random-control row. If it just matches or is")
    print("worse (like it was the first time VA was tested), it stays out - same")
    print("rule that already dropped Daily-Bias, Session-time and plain Value-Area.")


if __name__ == "__main__":
    main()
