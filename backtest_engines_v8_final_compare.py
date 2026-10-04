"""
GOLDFLOW BACKTEST v8 - Final 3-way comparison on FULL current dataset (~7.7 days)
============================================================================
Base entry logic used by ALL variants below (unchanged since v3):
  "V3 redesigned entry logic" = ATR-normalized VWAP-momentum entry
  (price vs VWAP measured in ATR units, not a fixed $ threshold) +
  clean SL/TP anchored to ENTRY price at a fixed ATR multiple (not to
  bar extremes) - this guarantees the designed Risk:Reward is the REAL
  Risk:Reward. Only the TP multiple (RR_1.5 vs RR_2.0) and the optional
  Max-Consecutive-Losses circuit breaker (N=4, 60-min cooldown) differ
  between the 3 variants tested here.

This script re-runs the 3 shortlisted configs on the FULL current tick
history (re-fetched fresh - now ~7.7 days, more than the ~7.5 days used
in v6/v7) for a clean, up-to-date, apples-to-apples comparison:
  1. V3_RR_2.0 (no breaker)
  2. V3_RR_2.0 + breaker(N=4)
  3. V3_RR_1.5 + breaker(N=4)

For EACH, reports:
  (a) Full-dataset result (trades/WR/PnL/MaxDD)
  (b) In-Sample vs Out-of-Sample split result (~67%/33% by time), to check
      whether performance is a real generalizable edge or an overfit to
      one week's noise - including for RR_2.0 (no breaker), which had NOT
      been out-of-sample tested before.

Run: python backtest_engines_v8_final_compare.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"
STARTING_BALANCE = 100.0


def questdb_query(sql):
    url = QUESTDB_URL + "?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("dataset", [])


def fetch_ticks():
    print("Fetching FULL current tick history from QuestDB...")
    rows = questdb_query("SELECT side, price, volume, timestamp FROM ticks_v2 WHERE symbol='XAUUSD' ORDER BY timestamp")
    print(f"{len(rows)} ticks fetched.")
    return rows


def build_bars(ticks):
    bars = []
    current = None
    cum_pv = 0.0; cum_vol = 0.0
    for side, price, volume, ts in ticks:
        price = float(price); volume = float(volume)
        is_buy = (side == "BUY")
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        minute = ts_dt.replace(second=0, microsecond=0)
        cum_pv += price * volume; cum_vol += volume
        vwap = (cum_pv / cum_vol) if cum_vol > 0 else price
        if current is None or current["time"] < minute:
            if current is not None:
                bars.append({**current})
            current = {"time": minute, "open": price, "high": price, "low": price, "close": price,
                       "volume": volume, "buy_vol": volume if is_buy else 0.0, "sell_vol": 0.0 if is_buy else volume,
                       "vwap": round(vwap, 2)}
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += volume
            if is_buy: current["buy_vol"] += volume
            else: current["sell_vol"] += volume
            current["vwap"] = round(vwap, 2)
    if current is not None:
        bars.append({**current})
    return bars


def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return 2.0
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_chop_ker(bars):
    if len(bars) < 16:
        return 50.0, 0.5
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    tr_list = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(bars))]
    if len(tr_list) < 14:
        return 50.0, 0.5
    sum_tr = sum(tr_list[-14:])
    range_14 = max(highs[-14:]) - min(lows[-14:])
    chop = max(0.0, min(100.0, 100.0 * (np.log10(sum_tr/range_14) / np.log10(14.0)))) if range_14 > 0 and sum_tr > 0 else 50.0
    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[i]-closes[i-1]) for i in range(len(closes)-9, len(closes)))
    ker = round(max(0.0, min(1.0, change/path if path > 0 else 0.5)), 3)
    return round(chop, 1), ker


def max_drawdown(equity_curve):
    peak = equity_curve[0]; max_dd = 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd
    return max_dd, (max_dd / peak * 100 if peak > 0 else 0)


def simulate(bars, tp_mult, max_losses, cooldown_min=60, balance_start=STARTING_BALANCE):
    """Base V3 entry logic (ATR-normalized VWAP momentum, entry-anchored SL/TP),
    with an optional Max-Consecutive-Losses circuit breaker."""
    trades = 0; wins = 0; pnl = 0.0
    equity = [balance_start]
    consec_losses = 0
    paused_until = None
    open_pos = None

    hist = []
    for bar in bars:
        hist.append(bar)
        now_t = bar["time"]

        if open_pos is not None:
            is_long = open_pos["direction"] == "BUY"
            hit_tp = (bar["high"] >= open_pos["tp"]) if is_long else (bar["low"] <= open_pos["tp"])
            hit_sl = (bar["low"] <= open_pos["sl"]) if is_long else (bar["high"] >= open_pos["sl"])
            if hit_tp or hit_sl:
                exit_p = open_pos["tp"] if hit_tp else open_pos["sl"]
                trade_pnl = (exit_p - open_pos["entry"]) if is_long else (open_pos["entry"] - exit_p)
                trades += 1; pnl += trade_pnl
                if trade_pnl > 0:
                    wins += 1; consec_losses = 0
                else:
                    consec_losses += 1
                    if max_losses is not None and consec_losses >= max_losses:
                        paused_until = now_t + timedelta(minutes=cooldown_min)
                        consec_losses = 0
                equity.append(equity[-1] + trade_pnl)
                open_pos = None

        if len(hist) < 20:
            continue

        atr = calc_atr(hist)
        chop, ker = calc_chop_ker(hist)
        is_trending = not ((chop >= 60.0) or (ker < 0.25))
        cp = bar["close"]; vwap = bar["vwap"]
        candle_range = bar["high"] - bar["low"]
        body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
        buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5

        direction = None
        if is_trending and atr > 0:
            if (bar["close"] > bar["open"]) and (cp >= vwap + 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio >= 0.58):
                direction = "BUY"
            elif (bar["close"] < bar["open"]) and (cp <= vwap - 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio <= 0.42):
                direction = "SELL"

        if direction is not None and open_pos is None:
            if paused_until is None or now_t >= paused_until:
                sl_dist = 1.0 * atr
                tp_dist = tp_mult * atr
                sl = round(cp - sl_dist, 2) if direction == "BUY" else round(cp + sl_dist, 2)
                tp = round(cp + tp_dist, 2) if direction == "BUY" else round(cp - tp_dist, 2)
                open_pos = {"direction": direction, "entry": cp, "sl": sl, "tp": tp}

    dd, dd_pct = max_drawdown(equity)
    wr = (wins / trades * 100) if trades else 0
    return {"trades": trades, "wins": wins, "pnl": pnl, "wr": wr, "final": equity[-1], "dd": dd, "dd_pct": dd_pct}


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    span_days = (bars[-1]["time"] - bars[0]["time"]).total_seconds() / 86400
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}")
    print(f"Total span: {span_days:.2f} days\n")

    print("=" * 100)
    print("BASE ENTRY LOGIC (same for all 3 configs below) = 'V3 redesigned entry logic':")
    print("  ATR-normalized VWAP-momentum entry (price vs VWAP in ATR units, not fixed $)")
    print("  + SL/TP as clean ATR-multiples anchored to ENTRY price (real, predictable R:R)")
    print("  RR_1.5 / RR_2.0 = just the TP-multiple. N=4 breaker = optional add-on layer.")
    print("=" * 100)

    configs = [
        ("V3_RR_2.0 (no breaker)",   2.0, None),
        ("V3_RR_2.0 + breaker(N=4)", 2.0, 4),
        ("V3_RR_1.5 + breaker(N=4)", 1.5, 4),
    ]

    print("\n" + "=" * 100)
    print(f"PART 1: FULL-DATASET result (all {span_days:.1f} days)")
    print("=" * 100)
    for label, tp_mult, n in configs:
        r = simulate(bars, tp_mult, n)
        print(f"{label:28s} | Trades: {r['trades']:4d} | WR: {r['wr']:5.1f}% | PnL: ${r['pnl']:7.2f} | "
              f"Final: ${r['final']:7.2f} | MaxDD: ${r['dd']:6.2f} ({r['dd_pct']:.1f}%)")

    # ~67/33 split of the CURRENT full range
    total_seconds = (bars[-1]["time"] - bars[0]["time"]).total_seconds()
    split_time = bars[0]["time"] + timedelta(seconds=total_seconds * 0.67)
    in_sample = [b for b in bars if b["time"] < split_time]
    out_sample = [b for b in bars if b["time"] >= split_time]

    print("\n" + "=" * 100)
    print(f"PART 2: IN-SAMPLE vs OUT-OF-SAMPLE (split at {split_time} UTC, ~67%/33%)")
    print(f"In-sample: {len(in_sample)} bars ({(split_time - bars[0]['time']).total_seconds()/86400:.2f} days)")
    print(f"Out-of-sample: {len(out_sample)} bars ({(bars[-1]['time'] - split_time).total_seconds()/86400:.2f} days)")
    print("=" * 100)
    for label, tp_mult, n in configs:
        r_in = simulate(in_sample, tp_mult, n)
        r_out = simulate(out_sample, tp_mult, n)
        drop_pct = ((r_in['pnl'] - r_out['pnl']) / r_in['pnl'] * 100) if r_in['pnl'] > 0 else float('nan')
        print(f"{label}")
        print(f"  IN-SAMPLE : Trades {r_in['trades']:4d} | WR {r_in['wr']:5.1f}% | PnL ${r_in['pnl']:7.2f} | MaxDD {r_in['dd_pct']:.1f}%")
        print(f"  OUT-SAMPLE: Trades {r_out['trades']:4d} | WR {r_out['wr']:5.1f}% | PnL ${r_out['pnl']:7.2f} | MaxDD {r_out['dd_pct']:.1f}%")
        print()


if __name__ == "__main__":
    main()
