"""
GOLDFLOW BACKTEST v6 - V3 base logic + Max-Consecutive-Losses Circuit Breaker
============================================================================
Same V3 redesigned base logic (ATR-normalized entry, entry-anchored SL/TP)
as v3/v4, now with a circuit breaker: after N consecutive losing trades,
new entries are paused for a cooldown period before resuming. This
directly targets drawdown (losing streaks) without touching entry quality
- unlike the TA-confluence filter tested in v5, which made things worse.

Tests N=3 and N=4 consecutive losses, each with a 60-minute cooldown,
against the RR_1.5 and RR_2.0 variants.

Run: python backtest_engines_v6_circuit_breaker.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"
STARTING_BALANCE = 100.0


def questdb_query(sql):
    url = QUESTDB_URL + "?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("dataset", [])


def fetch_ticks():
    print("Fetching tick history from QuestDB...")
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


def run_backtest(bars):
    variants = {
        "V3_RR_2.0 (no breaker)":        {"tp_mult": 2.0, "max_losses": None},
        "V3_RR_2.0 + breaker(N=3)":      {"tp_mult": 2.0, "max_losses": 3, "cooldown_min": 60},
        "V3_RR_2.0 + breaker(N=4)":      {"tp_mult": 2.0, "max_losses": 4, "cooldown_min": 60},
        "V3_RR_1.5 (no breaker)":        {"tp_mult": 1.5, "max_losses": None},
        "V3_RR_1.5 + breaker(N=3)":      {"tp_mult": 1.5, "max_losses": 3, "cooldown_min": 60},
        "V3_RR_1.5 + breaker(N=4)":      {"tp_mult": 1.5, "max_losses": 4, "cooldown_min": 60},
    }
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "equity": [STARTING_BALANCE],
                       "consec_losses": 0, "paused_until": None, "pauses_triggered": 0}
               for name in variants}
    open_pos = {name: None for name in variants}

    hist = []
    for bar in bars:
        hist.append(bar)
        now_t = bar["time"]

        for name, pos in list(open_pos.items()):
            if pos is None:
                continue
            is_long = pos["direction"] == "BUY"
            hit_tp = (bar["high"] >= pos["tp"]) if is_long else (bar["low"] <= pos["tp"])
            hit_sl = (bar["low"] <= pos["sl"]) if is_long else (bar["high"] >= pos["sl"])
            if hit_tp or hit_sl:
                exit_p = pos["tp"] if hit_tp else pos["sl"]
                pnl = (exit_p - pos["entry"]) if is_long else (pos["entry"] - exit_p)
                r = results[name]
                r["trades"] += 1; r["pnl"] += pnl
                cfg = variants[name]
                if pnl > 0:
                    r["wins"] += 1
                    r["consec_losses"] = 0
                else:
                    r["consec_losses"] += 1
                    if cfg["max_losses"] is not None and r["consec_losses"] >= cfg["max_losses"]:
                        r["paused_until"] = now_t + timedelta(minutes=cfg["cooldown_min"])
                        r["pauses_triggered"] += 1
                        r["consec_losses"] = 0
                r["equity"].append(r["equity"][-1] + pnl)
                open_pos[name] = None

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

        if direction is not None:
            for name, cfg in variants.items():
                if open_pos[name] is not None:
                    continue
                r = results[name]
                if r["paused_until"] is not None and now_t < r["paused_until"]:
                    continue
                sl_dist = 1.0 * atr
                tp_dist = cfg["tp_mult"] * atr
                sl = round(cp - sl_dist, 2) if direction == "BUY" else round(cp + sl_dist, 2)
                tp = round(cp + tp_dist, 2) if direction == "BUY" else round(cp - tp_dist, 2)
                open_pos[name] = {"direction": direction, "entry": cp, "sl": sl, "tp": tp}

    return results


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}\n")

    results = run_backtest(bars)

    print("=" * 110)
    print("BACKTEST v6 - V3 base logic WITH vs WITHOUT Max-Consecutive-Losses Circuit Breaker")
    print("=" * 110)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        dd, dd_pct = max_drawdown(r["equity"])
        print(f"{name:28s} | Trades: {r['trades']:4d} | WR: {wr:5.1f}% | PnL: ${r['pnl']:7.2f} | "
              f"Avg: ${avg:5.2f} | Final: ${r['equity'][-1]:7.2f} | MaxDD: ${dd:6.2f} ({dd_pct:.1f}%) | Breaker triggered: {r['pauses_triggered']}x")


if __name__ == "__main__":
    main()
