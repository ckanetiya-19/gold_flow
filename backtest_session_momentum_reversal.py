"""
Standalone backtest (read-only): SSRN-inspired "Morning Momentum, Afternoon Reversal" (Xu & Zhu)
applied to our own V1 signal (First-close, Candle-SL, Trailing $2/$1 - exactly the live logic).

Idea: session EARLY hours = information-driven -> momentum (take the signal as-is, i.e. our
current live behavior). Session LATE hours = liquidity-driven -> reversal -> FADE the signal
(trade the OPPOSITE direction) instead of following it. Test whether this session-aware
treatment beats just always following the signal (our current baseline).

Gold has two liquidity events/day (London, New York), so both get an early/late split:
  London EARLY  = 08:00-10:00 UTC   London LATE = 14:00-16:00 UTC
  NY EARLY      = 13:00-15:00 UTC   NY LATE     = 19:00-21:00 UTC
  Everything else (Asian session, off-hours) = OTHER.

Variants compared:
  A) BASELINE   - every signal traded as-is (momentum), regardless of time. = our current live V1.
  B) EARLY-ONLY - only trade signals in EARLY windows, as-is (momentum). Drop LATE and OTHER.
  C) SESSION-AWARE - EARLY: momentum (as-is). LATE: FADE (reverse direction). OTHER: dropped.
  D) SESSION-AWARE-KEEP-OTHER - same as C but OTHER hours kept as momentum (not dropped).

Same cost/mechanics as V1: candle-SL, Trailing ($2 arm / $1 trail), spread $0.24, tick-level exits,
market-hours only, FULL period.

Run: python backtest_session_momentum_reversal.py
"""
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars, market_closed
from backtest_vwap_retest import make_signals
from backtest_vwap_trailing import execute_trailing, TRAIL_ARM, TRAIL_DIST
from backtest_vwap_first_close_be import run_exit_be

SPREAD = 0.24
SL_BUFFER = 1.0


def zone_of(t_epoch):
    h = datetime.fromtimestamp(t_epoch, timezone.utc).hour
    if 8 <= h < 10:
        return "LONDON_EARLY"
    if 14 <= h < 16:
        return "LONDON_LATE"
    if 13 <= h < 15:
        return "NY_EARLY"
    if 19 <= h < 21:
        return "NY_LATE"
    return "OTHER"


def flip(d):
    return "SELL" if d == "BUY" else "BUY"


def build_variant_signals(sigs, ts, mode):
    """mode: 'baseline' | 'early_only' | 'session_aware_drop' | 'session_aware_keep'"""
    out = []
    for i, d, kind in sigs:
        z = zone_of(ts[i])
        is_early = z in ("LONDON_EARLY", "NY_EARLY")
        is_late = z in ("LONDON_LATE", "NY_LATE")
        if mode == "baseline":
            out.append((i, d))
        elif mode == "early_only":
            if is_early:
                out.append((i, d))
        elif mode == "session_aware_drop":
            if is_early:
                out.append((i, d))
            elif is_late:
                out.append((i, flip(d)))
        elif mode == "session_aware_keep":
            if is_late:
                out.append((i, flip(d)))
            else:
                out.append((i, d))
    return out


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars(ts, px, vol, 60)
    first_sigs = make_signals(bars, 0.0, True, False)  # V1's exact signal generator (First-close)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, {len(first_sigs)} first-close signals, spread ${SPREAD}, FULL period\n")

    # tag zone distribution
    from collections import Counter
    zc = Counter(zone_of(ts[bars[i]['next_tick']]) for i, d, k in first_sigs)
    print("Signal distribution by zone:", dict(zc), "\n")

    print("=" * 100)
    for label, mode in (("A) BASELINE (momentum always, = live V1)", "baseline"),
                        ("B) EARLY-ONLY (momentum, early hours only)", "early_only"),
                        ("C) SESSION-AWARE, drop OTHER", "session_aware_drop"),
                        ("D) SESSION-AWARE, keep OTHER as momentum", "session_aware_keep")):
        sigs2 = build_variant_signals(first_sigs, ts, mode)
        tr = execute_trailing(bars, ts, px, [(i, d, "FIRST") for i, d in sigs2])
        n = len(tr)
        if n == 0:
            print(f"{label}: no trades")
            continue
        wins = sum(1 for x in tr if x["why"] == "TRAIL")
        gross = sum(x["pnl"] for x in tr)
        net = gross - SPREAD * n
        eq, peak, dd = 100.0, 100.0, 0.0
        for x in sorted(tr, key=lambda z: z["t"]):
            eq += x["pnl"] - SPREAD
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        print(f"{label}")
        print(f"   trades {n:4d} | trail-exit {wins:4d} | gross ${gross:8.2f} | NET ${net:8.2f} | "
              f"net/trade ${net/n:6.3f} | MaxDD {dd:5.1f}%\n")


if __name__ == "__main__":
    main()
