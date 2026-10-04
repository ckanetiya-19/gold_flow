"""
GOLDFLOW BACKTEST - SMC Strategies (from user-shared blueprint), standalone test
============================================================================
Does NOT touch live PORT_9060/9080. Pure standalone backtest against real
historical ticks already captured in QuestDB (ticks_v2 - currently ~8 days).

Tests the 5 SMC concepts from the blueprint INDIVIDUALLY (not combined, to
avoid the over-fitting risk of a 17-factor confluence system):
  1. Asian Session Judas Swing Sweep
  2. London Session Sweep (PDH/PDL)
  3. NY Session Reversal (London H/L sweep + structure shift)
  4. Volume Profile POC Rejection (with CVD divergence)
  5. Fair Value Gap (FVG) retest continuation

Design choices (kept consistent with our own v3-v8 backtest methodology,
NOT copying the video system's approach, because of 2 issues found there):
  - SL/TP: ATR-normalized, anchored to ENTRY (not bar extremes) - same fix
    as our own Quant Sniper redesign.
  - Auto Break-Even (1:1, per blueprint Pillar 4) IS implemented, but BE
    exits are counted as their OWN bucket - NOT folded into "wins" like the
    video's backtest lab did (that inflated its 59.9% "win rate").
  - Position sizing: $100 start, 0.01 lot equivalent ($1 price move = $1
    PnL) - same convention as our v4-v8 scripts, NOT the video's $1000/0.1
    lot (which risks ~50% of capital per stop-loss - too aggressive to be
    a fair comparison).

CAVEAT (important): only ~8 days of tick history exist in QuestDB right
now, so session-based strategies (Asian/London/NY) only get ~8 instances
each - too small a sample for a real verdict. This is a first-pass sanity
check only. If any strategy looks promising here, the next step is pulling
months of real MT5 historical bars (mt5.copy_rates_range) for a proper test.

Run: python backtest_smc_strategies_v1.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"
STARTING_BALANCE = 100.0
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0          # RRR = 2.0, matches blueprint's ">=2.0" minimum
BE_TRIGGER_R = 1.0         # move SL to entry once price moves 1R in favor
BE_BUFFER = 0.05           # small buffer above/below entry so BE isn't a dead heat


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
    cum_pv = 0.0; cum_vol = 0.0; cum_delta = 0.0
    for side, price, volume, ts in ticks:
        price = float(price); volume = float(volume)
        is_buy = (side == "BUY")
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        minute = ts_dt.replace(second=0, microsecond=0)
        cum_pv += price * volume; cum_vol += volume
        cum_delta += volume if is_buy else -volume
        vwap = (cum_pv / cum_vol) if cum_vol > 0 else price
        if current is None or current["time"] < minute:
            if current is not None:
                bars.append({**current})
            current = {"time": minute, "open": price, "high": price, "low": price, "close": price,
                       "volume": volume, "buy_vol": volume if is_buy else 0.0, "sell_vol": 0.0 if is_buy else volume,
                       "vwap": round(vwap, 2), "cvd": cum_delta}
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += volume
            if is_buy: current["buy_vol"] += volume
            else: current["sell_vol"] += volume
            current["vwap"] = round(vwap, 2)
            current["cvd"] = cum_delta
    if current is not None:
        bars.append({**current})
    return bars


def calc_atr(bars, idx, period=14):
    if idx < period:
        return 2.0
    trs = []
    for i in range(idx - period + 1, idx + 1):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 2.0


def max_drawdown(equity_curve):
    peak = equity_curve[0]; max_dd = 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd
    return max_dd, (max_dd / peak * 100 if peak > 0 else 0)


# =============================================================================
# Shared trade simulator: given a list of (signal_idx, direction) pairs and
# the full bars array, replays each trade bar-by-bar with ATR SL/TP and the
# 1:1 auto-breakeven rule, counting WIN / LOSS / BREAK-EVEN as separate,
# honest buckets (this is the key fix vs. the video's backtest lab).
# =============================================================================
def simulate_trades(bars, signals):
    trades = []
    equity = [STARTING_BALANCE]
    wins = losses = be_count = 0
    total_pnl = 0.0

    for sig_idx, direction in signals:
        if sig_idx + 1 >= len(bars):
            continue
        entry_bar = bars[sig_idx]
        entry = entry_bar["close"]
        atr = calc_atr(bars, sig_idx)
        if atr <= 0:
            continue
        is_long = direction == "BUY"
        sl = entry - SL_ATR_MULT * atr if is_long else entry + SL_ATR_MULT * atr
        tp = entry + TP_ATR_MULT * atr if is_long else entry - TP_ATR_MULT * atr
        risk = abs(entry - sl)
        be_trigger_price = entry + BE_TRIGGER_R * risk if is_long else entry - BE_TRIGGER_R * risk
        be_armed = False
        cur_sl = sl
        outcome = None
        exit_p = None

        for j in range(sig_idx + 1, len(bars)):
            b = bars[j]
            if is_long:
                if not be_armed and b["high"] >= be_trigger_price:
                    be_armed = True
                    cur_sl = entry + BE_BUFFER
                hit_tp = b["high"] >= tp
                hit_sl = b["low"] <= cur_sl
            else:
                if not be_armed and b["low"] <= be_trigger_price:
                    be_armed = True
                    cur_sl = entry - BE_BUFFER
                hit_tp = b["low"] <= tp
                hit_sl = b["high"] >= cur_sl

            if hit_tp or hit_sl:
                exit_p = tp if hit_tp else cur_sl
                pnl = (exit_p - entry) if is_long else (entry - exit_p)
                total_pnl += pnl
                equity.append(equity[-1] + pnl)
                if hit_tp:
                    outcome = "WIN"; wins += 1
                elif be_armed:
                    outcome = "BREAK-EVEN"; be_count += 1
                else:
                    outcome = "LOSS"; losses += 1
                trades.append({"time": entry_bar["time"], "dir": direction, "entry": round(entry, 2),
                               "exit": round(exit_p, 2), "pnl": round(pnl, 2), "outcome": outcome})
                break

    dd, dd_pct = max_drawdown(equity)
    total = wins + losses + be_count
    return {
        "trades": total, "wins": wins, "losses": losses, "be": be_count,
        "win_rate": (wins / total * 100) if total else 0,
        "win_be_rate": ((wins + be_count) / total * 100) if total else 0,
        "pnl": round(total_pnl, 2), "final": round(equity[-1], 2),
        "dd": round(dd, 2), "dd_pct": round(dd_pct, 1), "trade_log": trades,
    }


# =============================================================================
# Strategy 1: Asian Session Judas Swing Sweep
# =============================================================================
def strategy_asian_judas(bars):
    signals = []
    by_day = {}
    for i, b in enumerate(bars):
        day = b["time"].date()
        by_day.setdefault(day, []).append(i)

    for day, idxs in by_day.items():
        asian_idxs = [i for i in idxs if 0 <= bars[i]["time"].hour < 8]
        early_london_idxs = [i for i in idxs if 8 <= bars[i]["time"].hour < 11]
        if not asian_idxs or not early_london_idxs:
            continue
        asian_high = max(bars[i]["high"] for i in asian_idxs)
        asian_low = min(bars[i]["low"] for i in asian_idxs)

        fired = False
        for i in early_london_idxs:
            if fired:
                break
            b = bars[i]
            buy_ratio = (b["buy_vol"] / b["volume"]) if b["volume"] > 0 else 0.5
            if b["high"] > asian_high and b["close"] < asian_high and buy_ratio <= 0.42:
                signals.append((i, "SELL")); fired = True
            elif b["low"] < asian_low and b["close"] > asian_low and buy_ratio >= 0.58:
                signals.append((i, "BUY")); fired = True
    return signals


# =============================================================================
# Strategy 2: London Session Sweep (PDH/PDL) with absorption
# =============================================================================
def strategy_london_sweep(bars):
    signals = []
    by_day = {}
    for i, b in enumerate(bars):
        day = b["time"].date()
        by_day.setdefault(day, []).append(i)
    days_sorted = sorted(by_day.keys())

    avg_vol = np.mean([b["volume"] for b in bars]) if bars else 1.0

    for d_idx in range(1, len(days_sorted)):
        prev_day = days_sorted[d_idx - 1]
        cur_day = days_sorted[d_idx]
        prev_idxs = by_day[prev_day]
        pdh = max(bars[i]["high"] for i in prev_idxs)
        pdl = min(bars[i]["low"] for i in prev_idxs)

        london_idxs = [i for i in by_day[cur_day] if 8 <= bars[i]["time"].hour < 13]
        fired = False
        for i in london_idxs:
            if fired:
                break
            b = bars[i]
            candle_range = b["high"] - b["low"]
            body_ratio = (abs(b["close"] - b["open"]) / candle_range) if candle_range > 0 else 0
            absorption = b["volume"] > avg_vol * 1.5 and body_ratio < 0.4
            if b["high"] > pdh and b["close"] < pdh and absorption:
                signals.append((i, "SELL")); fired = True
            elif b["low"] < pdl and b["close"] > pdl and absorption:
                signals.append((i, "BUY")); fired = True
    return signals


# =============================================================================
# Strategy 3: NY Session Reversal (London H/L sweep + structure shift)
# =============================================================================
def strategy_ny_reversal(bars):
    signals = []
    by_day = {}
    for i, b in enumerate(bars):
        day = b["time"].date()
        by_day.setdefault(day, []).append(i)

    for day, idxs in by_day.items():
        london_idxs = [i for i in idxs if 8 <= bars[i]["time"].hour < 13]
        ny_idxs = [i for i in idxs if 13 <= bars[i]["time"].hour < 20]
        if not london_idxs or len(ny_idxs) < 6:
            continue
        london_high = max(bars[i]["high"] for i in london_idxs)
        london_low = min(bars[i]["low"] for i in london_idxs)

        swept_high = swept_low = False
        fired = False
        for k, i in enumerate(ny_idxs):
            if fired:
                break
            b = bars[i]
            if b["high"] > london_high:
                swept_high = True
            if b["low"] < london_low:
                swept_low = True
            if k < 3:
                continue
            recent_low = min(bars[j]["low"] for j in ny_idxs[max(0, k - 3):k])
            recent_high = max(bars[j]["high"] for j in ny_idxs[max(0, k - 3):k])
            if swept_high and b["close"] < recent_low:
                signals.append((i, "SELL")); fired = True
            elif swept_low and b["close"] > recent_high:
                signals.append((i, "BUY")); fired = True
    return signals


# =============================================================================
# Strategy 4: Volume Profile POC Rejection (+ CVD divergence)
# =============================================================================
def strategy_poc_rejection(bars, lookback=240):
    signals = []
    for i in range(lookback, len(bars) - 1):
        window = bars[i - lookback:i]
        levels = {}
        for b in window:
            lvl = round(b["close"], 0)
            levels[lvl] = levels.get(lvl, 0) + b["volume"]
        if not levels:
            continue
        poc = max(levels, key=levels.get)

        b = bars[i]
        near_poc = abs(b["close"] - poc) <= 0.5
        if not near_poc or i < 5:
            continue
        cvd_now = b["cvd"]; cvd_prev = bars[i - 5]["cvd"]
        price_now = b["close"]; price_prev = bars[i - 5]["close"]
        bearish_div = price_now >= price_prev and cvd_now < cvd_prev
        bullish_div = price_now <= price_prev and cvd_now > cvd_prev
        if bearish_div:
            signals.append((i, "SELL"))
        elif bullish_div:
            signals.append((i, "BUY"))
    # de-dup: keep only 1 signal per 30-bar window to avoid overlapping trades
    filtered = []
    last_i = -999
    for i, d in signals:
        if i - last_i >= 30:
            filtered.append((i, d)); last_i = i
    return filtered


# =============================================================================
# Strategy 5: Fair Value Gap (FVG) retest continuation
# =============================================================================
def strategy_fvg_retest(bars, max_wait=60):
    signals = []
    pending_fvgs = []  # list of dicts: {type, top, bottom, created_idx}

    for i in range(2, len(bars)):
        b0, b2 = bars[i - 2], bars[i]
        if b0["high"] < b2["low"]:
            pending_fvgs.append({"type": "BULL", "top": b2["low"], "bottom": b0["high"], "created": i})
        elif b0["low"] > b2["high"]:
            pending_fvgs.append({"type": "BEAR", "top": b0["low"], "bottom": b2["high"], "created": i})

        pending_fvgs = [f for f in pending_fvgs if i - f["created"] <= max_wait]

        b = bars[i]
        for f in list(pending_fvgs):
            if f["created"] >= i:
                continue
            mid = (f["top"] + f["bottom"]) / 2.0
            if f["type"] == "BULL" and b["low"] <= mid and b["close"] > mid:
                signals.append((i, "BUY"))
                pending_fvgs.remove(f)
            elif f["type"] == "BEAR" and b["high"] >= mid and b["close"] < mid:
                signals.append((i, "SELL"))
                pending_fvgs.remove(f)
    return signals


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    span_days = (bars[-1]["time"] - bars[0]["time"]).total_seconds() / 86400
    print(f"Reconstructed {len(bars)} 1-minute bars, {bars[0]['time']} to {bars[-1]['time']} ({span_days:.2f} days)\n")
    print("CAVEAT: only ~8 days of history -> ~8 instances per session-based strategy.")
    print("This is a first-pass sanity check, NOT a statistically reliable verdict.\n")

    strategies = {
        "1. Asian Judas Swing":     strategy_asian_judas(bars),
        "2. London Sweep (PDH/PDL)": strategy_london_sweep(bars),
        "3. NY Reversal":           strategy_ny_reversal(bars),
        "4. POC Rejection":         strategy_poc_rejection(bars),
        "5. FVG Retest":            strategy_fvg_retest(bars),
    }

    print("=" * 100)
    print("SMC STRATEGIES - INDIVIDUAL BACKTEST RESULTS (honest WIN/LOSS/BE split)")
    print("=" * 100)
    for name, signals in strategies.items():
        r = simulate_trades(bars, signals)
        print(f"{name:28s} | Signals: {len(signals):3d} | Trades: {r['trades']:3d} | "
              f"W:{r['wins']:2d} L:{r['losses']:2d} BE:{r['be']:2d} | "
              f"WinRate: {r['win_rate']:5.1f}% | Win+BE: {r['win_be_rate']:5.1f}% | "
              f"PnL: ${r['pnl']:7.2f} | MaxDD: {r['dd_pct']:.1f}%")


if __name__ == "__main__":
    main()
