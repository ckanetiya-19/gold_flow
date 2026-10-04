"""
GOLDFLOW BACKTEST v5 - V3 (fixed base logic) + Traditional-TA Confluence Filter
============================================================================
Tests whether the TA confluence filter (EMA/RSI/Swing+Fib, 2-of-3 must
agree) reduces drawdown when applied on TOP of the v3 redesigned base
logic (ATR-normalized entry + clean entry-anchored SL/TP) - previously the
same filter was tested against the OLD, structurally-flawed base logic
and didn't help. This checks if it helps now that the base logic itself
is sound.

Run: python backtest_engines_v5_v3_plus_ta.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime
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
    cum_delta = 0.0; cum_pv = 0.0; cum_vol = 0.0
    for side, price, volume, ts in ticks:
        price = float(price); volume = float(volume)
        is_buy = (side == "BUY")
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        minute = ts_dt.replace(second=0, microsecond=0)
        cum_delta += volume if is_buy else -volume
        cum_pv += price * volume
        cum_vol += volume
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


class TraditionalTA:
    def __init__(self):
        self._current_5m = None
        self.ema9 = None
        self.ema21 = None
        self.closes_5m = []

    def update(self, bar_1m):
        bucket = bar_1m["time"].replace(minute=(bar_1m["time"].minute // 5) * 5, second=0, microsecond=0)
        if self._current_5m is None or self._current_5m["time"] < bucket:
            if self._current_5m is not None:
                self._on_5m_close(self._current_5m["close"])
            self._current_5m = {"time": bucket, "close": bar_1m["close"]}
        else:
            self._current_5m["close"] = bar_1m["close"]

    def _on_5m_close(self, close_price):
        self.closes_5m.append(close_price)
        k9 = 2 / 10; k21 = 2 / 22
        self.ema9 = close_price if self.ema9 is None else close_price * k9 + self.ema9 * (1 - k9)
        self.ema21 = close_price if self.ema21 is None else close_price * k21 + self.ema21 * (1 - k21)

    def ema_bias(self):
        if self.ema9 is None or len(self.closes_5m) < 21:
            return None
        return "UP" if self.ema9 > self.ema21 else "DOWN"

    def rsi_bias(self, period=14):
        if len(self.closes_5m) < period + 1:
            return None
        deltas = np.diff(self.closes_5m[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0); losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains); avg_loss = np.mean(losses)
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        return "BULL" if rsi > 50 else "BEAR"


def swing_and_fib(bars_1m, lookback=30):
    if len(bars_1m) < lookback:
        return None
    recent = bars_1m[-lookback:]
    high = max(b["high"] for b in recent)
    low = min(b["low"] for b in recent)
    return high, low


def check_ta_confluence(direction, ta, bars_1m, price):
    checks_passed = 0; checks_total = 0
    ema_bias = ta.ema_bias()
    if ema_bias is not None:
        checks_total += 1
        if (direction == "BUY" and ema_bias == "UP") or (direction == "SELL" and ema_bias == "DOWN"):
            checks_passed += 1
    rsi_bias = ta.rsi_bias()
    if rsi_bias is not None:
        checks_total += 1
        if (direction == "BUY" and rsi_bias == "BULL") or (direction == "SELL" and rsi_bias == "BEAR"):
            checks_passed += 1
    sr = swing_and_fib(bars_1m)
    if sr is not None:
        high, low = sr
        rng = high - low
        checks_total += 1
        near_resistance = rng > 0 and abs(price - high) < rng * 0.1
        near_support = rng > 0 and abs(price - low) < rng * 0.1
        sr_ok = not ((direction == "BUY" and near_resistance) or (direction == "SELL" and near_support))
        if sr_ok:
            checks_passed += 1
    if checks_total < 3:
        return False
    return checks_passed >= 2


def max_drawdown(equity_curve):
    peak = equity_curve[0]; max_dd = 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd
    return max_dd, (max_dd / peak * 100 if peak > 0 else 0)


def run_backtest(bars):
    variants = {
        "V3_RR_2.0 (no filter)":     {"tp_mult": 2.0, "use_ta": False},
        "V3_RR_2.0 + TA filter":     {"tp_mult": 2.0, "use_ta": True},
        "V3_RR_1.5 (no filter)":     {"tp_mult": 1.5, "use_ta": False},
        "V3_RR_1.5 + TA filter":     {"tp_mult": 1.5, "use_ta": True},
    }
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "equity": [STARTING_BALANCE], "rejected": 0} for name in variants}
    open_pos = {name: None for name in variants}
    ta = TraditionalTA()

    hist = []
    for bar in bars:
        hist.append(bar)
        ta.update(bar)

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
                if pnl > 0: r["wins"] += 1
                r["equity"].append(r["equity"][-1] + pnl)
                open_pos[name] = None

        if len(hist) < 30:
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
                if cfg["use_ta"] and not check_ta_confluence(direction, ta, hist, cp):
                    results[name]["rejected"] += 1
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

    print("=" * 105)
    print("BACKTEST v5 - V3 base logic WITH vs WITHOUT TA-Confluence filter (drawdown focus)")
    print("=" * 105)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        dd, dd_pct = max_drawdown(r["equity"])
        print(f"{name:26s} | Trades: {r['trades']:4d} | WR: {wr:5.1f}% | PnL: ${r['pnl']:7.2f} | "
              f"Avg: ${avg:5.2f} | Final: ${r['equity'][-1]:7.2f} | MaxDD: ${dd:6.2f} ({dd_pct:.1f}%) | Rejected: {r['rejected']}")


if __name__ == "__main__":
    main()
