"""
GOLDFLOW BACKTEST - ALL blueprint strategies, standalone, individual test
============================================================================
Extends backtest_smc_strategies_v1.py: adds the remaining blueprint pieces
that were NOT yet tested (the 7 "Technical Strategies" and 3 more
standalone "Order Flow Strategies"), tested the SAME way - individually,
not combined, on the same ~8-day real tick history, with the same honest
WIN/LOSS/BREAK-EVEN accounting and ATR-normalized entry-anchored SL/TP.

Does NOT touch live PORT_9060/9080. Standalone only.

New strategies added here (blueprint's "Technical Strategies & Targets"
and remaining "Order Flow Strategies" panels):
  6. EMA Trend Crossover (20/50)
  7. RSI 14 Mean-Reversion (30/70)
  8. MACD Crossover (12/26/9)
  9. Bollinger Bands Mean-Reversion (20, 2-sigma)
  10. Fibonacci Retracement Bounce (38.2/50/61.8)
  11. Support/Resistance Bounce (swing pivots)
  12. VWAP Trend Position (session VWAP cross)
  13. Absorption Detection (standalone reversal)
  14. Volume Imbalance Spike (standalone continuation)
  15. Bar Delta Confirmation (standalone momentum)

Run: python backtest_smc_strategies_v2_full.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"
STARTING_BALANCE = 100.0
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0
BE_TRIGGER_R = 1.0
BE_BUFFER = 0.05


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
                    wins += 1
                elif be_armed:
                    be_count += 1
                else:
                    losses += 1
                break

    dd, dd_pct = max_drawdown(equity)
    total = wins + losses + be_count
    return {
        "trades": total, "wins": wins, "losses": losses, "be": be_count,
        "win_rate": (wins / total * 100) if total else 0,
        "win_be_rate": ((wins + be_count) / total * 100) if total else 0,
        "pnl": round(total_pnl, 2), "dd_pct": round(dd_pct, 1),
    }


# =============================================================================
# 6. EMA Trend Crossover (20/50)
# =============================================================================
def strategy_ema_crossover(bars):
    closes = [b["close"] for b in bars]
    k20, k50 = 2 / 21, 2 / 51
    ema20 = ema50 = None
    signals = []
    prev_diff = None
    for i, c in enumerate(closes):
        ema20 = c if ema20 is None else c * k20 + ema20 * (1 - k20)
        ema50 = c if ema50 is None else c * k50 + ema50 * (1 - k50)
        if i < 60:
            continue
        diff = ema20 - ema50
        if prev_diff is not None:
            if prev_diff <= 0 and diff > 0:
                signals.append((i, "BUY"))
            elif prev_diff >= 0 and diff < 0:
                signals.append((i, "SELL"))
        prev_diff = diff
    return signals


# =============================================================================
# 7. RSI 14 Mean-Reversion (30/70)
# =============================================================================
def strategy_rsi_reversion(bars, period=14):
    closes = [b["close"] for b in bars]
    signals = []
    for i in range(period + 1, len(closes)):
        window = closes[i - period:i + 1]
        deltas = np.diff(window)
        gains = np.where(deltas > 0, deltas, 0); losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains); avg_loss = np.mean(losses)
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        if i > period + 1:
            if rsi_prev < 30 and rsi >= 30:
                signals.append((i, "BUY"))
            elif rsi_prev > 70 and rsi <= 70:
                signals.append((i, "SELL"))
        rsi_prev = rsi
    return signals


# =============================================================================
# 8. MACD Crossover (12/26/9)
# =============================================================================
def strategy_macd_crossover(bars):
    closes = [b["close"] for b in bars]
    k12, k26, k9 = 2 / 13, 2 / 27, 2 / 10
    ema12 = ema26 = macd_signal = None
    signals = []
    prev_hist = None
    for i, c in enumerate(closes):
        ema12 = c if ema12 is None else c * k12 + ema12 * (1 - k12)
        ema26 = c if ema26 is None else c * k26 + ema26 * (1 - k26)
        macd = ema12 - ema26
        macd_signal = macd if macd_signal is None else macd * k9 + macd_signal * (1 - k9)
        hist = macd - macd_signal
        if i < 40:
            prev_hist = hist
            continue
        if prev_hist is not None:
            if prev_hist <= 0 and hist > 0:
                signals.append((i, "BUY"))
            elif prev_hist >= 0 and hist < 0:
                signals.append((i, "SELL"))
        prev_hist = hist
    return signals


# =============================================================================
# 9. Bollinger Bands Mean-Reversion (20, 2-sigma)
# =============================================================================
def strategy_bollinger_reversion(bars, period=20, k=2.0):
    closes = [b["close"] for b in bars]
    signals = []
    for i in range(period, len(bars)):
        window = closes[i - period:i]
        mean = np.mean(window); std = np.std(window)
        upper = mean + k * std; lower = mean - k * std
        b = bars[i]
        if b["low"] <= lower and b["close"] > lower:
            signals.append((i, "BUY"))
        elif b["high"] >= upper and b["close"] < upper:
            signals.append((i, "SELL"))
    return signals


# =============================================================================
# 10. Fibonacci Retracement Bounce (38.2/50/61.8), trend-following bounce
# =============================================================================
def strategy_fib_bounce(bars, lookback=50, tolerance=0.3):
    signals = []
    for i in range(lookback, len(bars)):
        window = bars[i - lookback:i]
        swing_high = max(b["high"] for b in window)
        swing_low = min(b["low"] for b in window)
        rng = swing_high - swing_low
        if rng <= 0:
            continue
        trend_up = window[-1]["close"] > window[0]["close"]
        b = bars[i]
        fib_levels = [swing_high - rng * f for f in (0.382, 0.5, 0.618)] if trend_up else \
                     [swing_low + rng * f for f in (0.382, 0.5, 0.618)]
        for lvl in fib_levels:
            if abs(b["close"] - lvl) <= tolerance:
                if trend_up and b["close"] > b["open"]:
                    signals.append((i, "BUY")); break
                elif not trend_up and b["close"] < b["open"]:
                    signals.append((i, "SELL")); break
    return signals


# =============================================================================
# 11. Support/Resistance Bounce (swing pivots)
# =============================================================================
def strategy_sr_bounce(bars, lookback=30, tolerance=0.3):
    signals = []
    for i in range(lookback, len(bars) - 1):
        window = bars[i - lookback:i]
        support = min(b["low"] for b in window)
        resistance = max(b["high"] for b in window)
        b = bars[i]
        if b["low"] <= support + tolerance and b["close"] > support + tolerance:
            signals.append((i, "BUY"))
        elif b["high"] >= resistance - tolerance and b["close"] < resistance - tolerance:
            signals.append((i, "SELL"))
    return signals


# =============================================================================
# 12. VWAP Trend Position (session VWAP cross with momentum)
# =============================================================================
def strategy_vwap_trend(bars):
    signals = []
    session_pv = session_vol = 0.0
    cur_day = None
    prev_side = None
    for i, b in enumerate(bars):
        day = b["time"].date()
        if day != cur_day:
            cur_day = day; session_pv = session_vol = 0.0; prev_side = None
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        session_pv += typical * b["volume"]; session_vol += b["volume"]
        vwap = (session_pv / session_vol) if session_vol > 0 else b["close"]
        side = "ABOVE" if b["close"] > vwap else "BELOW"
        if prev_side is not None and side != prev_side and i > 20:
            if side == "ABOVE" and b["close"] > b["open"]:
                signals.append((i, "BUY"))
            elif side == "BELOW" and b["close"] < b["open"]:
                signals.append((i, "SELL"))
        prev_side = side
    return signals


# =============================================================================
# 13. Absorption Detection (standalone reversal)
# =============================================================================
def strategy_absorption(bars, avg_window=20):
    signals = []
    for i in range(avg_window, len(bars)):
        window = bars[i - avg_window:i]
        avg_vol = np.mean([b["volume"] for b in window])
        b = bars[i]
        candle_range = b["high"] - b["low"]
        body_ratio = (abs(b["close"] - b["open"]) / candle_range) if candle_range > 0 else 0
        buy_ratio = (b["buy_vol"] / b["volume"]) if b["volume"] > 0 else 0.5
        is_absorption = b["volume"] > avg_vol * 2.0 and body_ratio < 0.35
        if is_absorption and buy_ratio >= 0.60:
            signals.append((i, "SELL"))   # heavy buying absorbed -> expect reversal down
        elif is_absorption and buy_ratio <= 0.40:
            signals.append((i, "BUY"))    # heavy selling absorbed -> expect reversal up
    return signals


# =============================================================================
# 14. Volume Imbalance Spike (standalone continuation)
# =============================================================================
def strategy_volume_imbalance(bars, avg_window=20):
    signals = []
    for i in range(avg_window, len(bars)):
        window = bars[i - avg_window:i]
        avg_vol = np.mean([b["volume"] for b in window])
        b = bars[i]
        candle_range = b["high"] - b["low"]
        body_ratio = (abs(b["close"] - b["open"]) / candle_range) if candle_range > 0 else 0
        if b["volume"] > avg_vol * 2.5 and body_ratio >= 0.65:
            if b["close"] > b["open"]:
                signals.append((i, "BUY"))
            else:
                signals.append((i, "SELL"))
    return signals


# =============================================================================
# 15. Bar Delta Confirmation (standalone momentum)
# =============================================================================
def strategy_bar_delta(bars, avg_window=20):
    signals = []
    deltas = [(b["buy_vol"] - b["sell_vol"]) for b in bars]
    for i in range(avg_window, len(bars)):
        avg_abs_delta = np.mean([abs(deltas[j]) for j in range(i - avg_window, i)])
        if avg_abs_delta <= 0:
            continue
        b = bars[i]
        if deltas[i] > avg_abs_delta * 2.0 and b["close"] > b["open"]:
            signals.append((i, "BUY"))
        elif deltas[i] < -avg_abs_delta * 2.0 and b["close"] < b["open"]:
            signals.append((i, "SELL"))
    return signals


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    span_days = (bars[-1]["time"] - bars[0]["time"]).total_seconds() / 86400
    print(f"Reconstructed {len(bars)} 1-minute bars, {bars[0]['time']} to {bars[-1]['time']} ({span_days:.2f} days)\n")

    strategies = {
        "6. EMA Crossover (20/50)":   strategy_ema_crossover(bars),
        "7. RSI 14 Reversion":        strategy_rsi_reversion(bars),
        "8. MACD Crossover":          strategy_macd_crossover(bars),
        "9. Bollinger Reversion":     strategy_bollinger_reversion(bars),
        "10. Fibonacci Bounce":       strategy_fib_bounce(bars),
        "11. Support/Resistance":     strategy_sr_bounce(bars),
        "12. VWAP Trend Position":    strategy_vwap_trend(bars),
        "13. Absorption Detection":   strategy_absorption(bars),
        "14. Volume Imbalance Spike": strategy_volume_imbalance(bars),
        "15. Bar Delta Confirmation": strategy_bar_delta(bars),
    }

    print("=" * 105)
    print("REMAINING BLUEPRINT STRATEGIES - INDIVIDUAL BACKTEST (honest WIN/LOSS/BE split)")
    print("=" * 105)
    for name, signals in strategies.items():
        r = simulate_trades(bars, signals)
        print(f"{name:28s} | Signals: {len(signals):4d} | Trades: {r['trades']:4d} | "
              f"W:{r['wins']:3d} L:{r['losses']:3d} BE:{r['be']:3d} | "
              f"WinRate: {r['win_rate']:5.1f}% | Win+BE: {r['win_be_rate']:5.1f}% | "
              f"PnL: ${r['pnl']:8.2f} | MaxDD: {r['dd_pct']:.1f}%")


if __name__ == "__main__":
    main()
