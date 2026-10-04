"""
GOLDFLOW - Backtest of ALL strategies running in PORT_9080 (standalone, read-only)
============================================================================
Replays the EXACT logic found in PORT_9080_AI_QUANT_TERMINAL_V2.py against the
real history in QuestDB (ticks_v2 = Binance PAXG trades, depth_v2 = 100ms
book snapshots), with TICK-LEVEL exit checking (SL/TP/time-stop evaluated on
the real tick path, like live), in-sample (first 67%) / out-of-sample (last
33%) split, and PnL shown gross AND net of the live XAUUSD.sd spread.

Strategies replicated:
  CONFLUENCE  (shares one position slot with Sniper, exactly like live)
  S1 VWAP Momentum | S2 Band Absorption | S3 Imbalance | S4 Tape Momentum
  S5 Micro Breakout | HFT-Engine (OFI / microprice / Kyle-lambda, 9s stop)
  (Quant Sniper V3_RR_2.0+N4 included as a reference row)

Known simplifications (honest list):
  - Price = PAXG path (live bars are iTick-driven; historical iTick was not
    stored for this period).  Relative moves are similar, levels are not.
  - S1/S2/S5/Confluence entry = first tick after bar close (live uses
    last_known_price at bar finalisation).  Sniper/Confluence entry = bar close.
  - VWAP is cumulative from start of data (live: cumulative since process start).
  - S3 book snapshot = sum of top-5 levels during the last second of the minute.
  - HFT: entry/exit on PAXG mid; the live EV gate uses PAXG's own tiny spread.
  - Position PnL is $ per 0.01 lot ($1 per $1 move).

Run: python backtest_9080_all_strategies_oos.py
"""

import bisect
import csv
import io
import json
import math
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

import numpy as np

Q = "http://127.0.0.1:9010"
SPREAD_COST = 0.24          # live XAUUSD.sd spread (read from MT5 earlier)
SCALP_MAX_CANDLE_RANGE = 5.0


# ----------------------------------------------------------------------------- data
def q_json(sql):
    url = Q + "/exec?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=600) as r:
        return json.loads(r.read().decode())["dataset"]


def q_csv(sql):
    url = Q + "/exp?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=1800) as r:
        text = r.read().decode()
    rd = csv.reader(io.StringIO(text))
    next(rd)
    return rd


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_ticks():
    print("Loading ticks...")
    rows = q_json("SELECT side, price, volume, timestamp FROM ticks_v2 WHERE symbol='XAUUSD' ORDER BY timestamp")
    ts = [parse_ts(r[3]) for r in rows]
    px = [float(r[1]) for r in rows]
    vol = [float(r[2]) for r in rows]
    buy = [r[0] == "BUY" for r in rows]
    print(f"  {len(ts)} ticks")
    return ts, px, vol, buy


def build_bars(ts, px, vol, buy):
    """1-min bars + per-bar metadata: index of first tick of the NEXT minute
    (entry tick), and whether the last 3 ticks were same-side block trades (S4)."""
    bars = []
    cur = None
    cum_pv = cum_vol = cum_delta = 0.0
    for i in range(len(ts)):
        minute = int(ts[i] // 60) * 60
        p, v, b = px[i], vol[i], buy[i]
        cum_pv += p * v
        cum_vol += v
        cum_delta += v if b else -v
        vwap = cum_pv / cum_vol if cum_vol > 0 else p
        if cur is None or cur["t"] < minute:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": minute, "open": p, "high": p, "low": p, "close": p, "volume": v,
                   "buy_vol": v if b else 0.0, "sell_vol": 0.0 if b else v,
                   "vwap": vwap, "cvd": cum_delta, "first_tick": i, "last_tick": i}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
            if b:
                cur["buy_vol"] += v
            else:
                cur["sell_vol"] += v
            cur["vwap"] = vwap
            cur["cvd"] = cum_delta
            cur["last_tick"] = i
    # drop last (no next tick)
    for bar in bars:
        li = bar["last_tick"]
        s = None
        cnt = 0
        for k in range(li, max(-1, li - 3), -1):
            if vol[k] <= 3.0:
                break
            side = buy[k]
            if s is None:
                s = side; cnt = 1
            elif side == s:
                cnt += 1
            else:
                break
        bar["tape_streak"] = cnt if cnt >= 3 else 0
        bar["tape_side"] = "BUY" if s else "SELL"
    return bars


def finalize_features(bars):
    """Same per-bar features as finalize_bar() in PORT_9080."""
    for i, b in enumerate(bars):
        delta = b["buy_vol"] - b["sell_vol"]
        b["delta"] = delta
        b["absorption"] = None
        if abs(delta) > 50 and abs(b["close"] - b["open"]) < 0.25:
            b["absorption"] = "Bullish" if delta > 0 else "Bearish"
        b["div"] = None
        if i >= 4:
            cs = b["cvd"] - bars[i - 4]["cvd"]
            ps = b["close"] - bars[i - 4]["close"]
            if cs > 0 and ps < 0:
                b["div"] = "Bullish"
            elif cs < 0 and ps > 0:
                b["div"] = "Bearish"


def quant_metrics(bars, i):
    """calculate_quant_metrics(historical_bars) with historical_bars = bars[..i] (max 300)."""
    lo = max(0, i - 299)
    bs = bars[lo:i + 1]
    if len(bs) < 16:
        return 50.0, 0.5, 2.0
    closes = [x["close"] for x in bs]
    highs = [x["high"] for x in bs]
    lows = [x["low"] for x in bs]
    tr = [max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]), abs(lows[k] - closes[k - 1])) for k in range(1, len(bs))]
    if len(tr) < 14:
        return 50.0, 0.5, 2.0
    s = sum(tr[-14:])
    atr = s / 14.0
    rng = max(highs[-14:]) - min(lows[-14:])
    chop = 50.0
    if rng > 0 and s > 0:
        chop = max(0.0, min(100.0, 100.0 * (math.log10(s / rng) / math.log10(14.0))))
    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(len(closes) - 9, len(closes)))
    ker = round(max(0.0, min(1.0, change / path if path > 0 else 0.5)), 3)
    return round(chop, 1), ker, round(atr, 2)


def vwap_bands(bars, i):
    bs = bars[max(0, i - 99):i + 1]
    cum_pv = cum_vol = cum_d2 = 0.0
    vw = bs[-1]["close"]
    for b in bs:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += tp * b["volume"]
        cum_vol += b["volume"]
        vw = cum_pv / cum_vol if cum_vol > 0 else tp
        cum_d2 += ((tp - vw) ** 2) * b["volume"]
    sd = (cum_d2 / max(cum_vol, 1.0)) ** 0.5
    return round(vw, 2), round(vw + 1.28 * sd, 2), round(vw - 1.28 * sd, 2)


def vwap120(bars, i):
    rb = bars[max(0, i - 119):i + 1]
    tps = sum(((b["high"] + b["low"] + b["close"]) / 3.0) * b["volume"] for b in rb)
    vs = sum(b["volume"] for b in rb)
    return round(tps / vs, 2) if vs > 0 else bars[i]["vwap"]


def load_book_imbalance():
    """minute -> (bids_top5, asks_top5) from the last second of that minute."""
    print("Loading depth (S3 book imbalance)...")
    rows = q_json("SELECT timestamp, side, sum(qty) FROM depth_v2 WHERE level < 5 AND second(timestamp) = 59 SAMPLE BY 1m")
    d = {}
    for t, side, s in rows:
        m = int(parse_ts(t) // 60) * 60
        d.setdefault(m, {})[side] = float(s)
    return d


# ----------------------------------------------------------------------------- exits
def run_exit(ts, px, start_idx, direction, sl, tp, max_seconds=None):
    """Walk the real tick path from start_idx. Returns (exit_price, exit_idx, reason)."""
    is_long = direction == "BUY"
    t0 = ts[start_idx]
    n = len(ts)
    for k in range(start_idx, n):
        p = px[k]
        hit_tp = p >= tp if is_long else p <= tp
        hit_sl = p <= sl if is_long else p >= sl
        if hit_sl:                       # conservative if both on same tick
            return sl, k, "SL"
        if hit_tp:
            return tp, k, "TP"
        if max_seconds is not None and ts[k] - t0 >= max_seconds:
            return p, k, "TIME"
    return px[-1], n - 1, "OPEN"


def pnl_of(direction, entry, exit_p):
    return (exit_p - entry) if direction == "BUY" else (entry - exit_p)


# ----------------------------------------------------------------------------- bar strategies
def run_bar_strategies(bars, ts, px, book):
    trades = {k: [] for k in ("Sniper V3_RR2+N4", "Confluence", "S1 VWAP Momentum", "S2 Band Absorption",
                              "S3 Imbalance", "S4 Tape Momentum", "S5 Micro Breakout")}
    # --- shared Sniper/Confluence slot (one trade_state, like live) ---
    slot_free_idx = -1          # tick index when the slot frees
    consec = 0
    paused_until = None
    # --- independent scalp slots ---
    free = {k: -1 for k in ("S1 VWAP Momentum", "S2 Band Absorption", "S3 Imbalance", "S4 Tape Momentum", "S5 Micro Breakout")}

    for i in range(len(bars) - 1):
        b = bars[i]
        nxt = b["next_tick"]
        entry_px = px[nxt]
        chop, ker, atr = quant_metrics(bars, i)
        is_sideways = (chop >= 60.0) or (ker < 0.25)
        candle_range = b["high"] - b["low"]
        body_ratio = (abs(b["close"] - b["open"]) / candle_range) if candle_range > 0 else 0.0
        buy_ratio = (b["buy_vol"] / b["volume"]) if b["volume"] > 0 else 0.5
        cp = b["close"]

        # ---------------- Sniper / Confluence (single shared slot) ----------------
        if nxt > slot_free_idx and i >= 9:
            v120 = vwap120(bars, i)
            fired = None
            sniper_paused = paused_until is not None and ts[nxt] < paused_until
            if not is_sideways and not sniper_paused and atr > 0:
                if b["close"] > b["open"] and cp >= v120 + 0.3 * atr and body_ratio >= 0.60 and buy_ratio >= 0.58:
                    fired = ("Sniper V3_RR2+N4", "BUY", cp - atr, cp + 2 * atr, cp)
                elif b["close"] < b["open"] and cp <= v120 - 0.3 * atr and body_ratio >= 0.60 and buy_ratio <= 0.42:
                    fired = ("Sniper V3_RR2+N4", "SELL", cp + atr, cp - 2 * atr, cp)
            if fired is None and not is_sideways:
                sig = None
                if (b["div"] == "Bullish" or b["absorption"] == "Bullish") and cp > v120:
                    sig = "BUY"
                elif (b["div"] == "Bearish" or b["absorption"] == "Bearish") and cp < v120:
                    sig = "SELL"
                if sig:
                    fired = ("Confluence", sig, cp - 2.0 if sig == "BUY" else cp + 2.0,
                             cp + 4.0 if sig == "BUY" else cp - 4.0, cp)
            if fired:
                name, d, sl, tp, ent = fired
                xp, xi, why = run_exit(ts, px, nxt, d, sl, tp)
                pnl = pnl_of(d, ent, xp)
                trades[name].append((ts[nxt], pnl))
                slot_free_idx = xi
                if name.startswith("Sniper"):
                    if pnl > 0:
                        consec = 0
                    else:
                        consec += 1
                        if consec >= 4:
                            paused_until = ts[xi] + 3600
                            consec = 0

        # ---------------- scalps (need >= 6 bars, skip spike bars) ----------------
        if i < 6 or candle_range > SCALP_MAX_CANDLE_RANGE:
            continue
        price = entry_px
        vwap = b["vwap"]

        def open_scalp(name, d, sl, tp):
            if nxt <= free[name]:
                return
            if (d == "BUY" and not (sl < price < tp)) or (d == "SELL" and not (tp < price < sl)):
                return
            xp, xi, why = run_exit(ts, px, nxt, d, sl, tp)
            trades[name].append((ts[nxt], pnl_of(d, price, xp)))
            free[name] = xi

        # S1
        if not is_sideways:
            if b["low"] > vwap and b["close"] > b["open"] and body_ratio >= 0.65 and buy_ratio >= 0.60:
                sl = round(b["low"], 2); open_scalp("S1 VWAP Momentum", "BUY", sl, round(price + (price - sl) * 1.5, 2))
            elif b["high"] < vwap and b["close"] < b["open"] and body_ratio >= 0.65 and buy_ratio <= 0.40:
                sl = round(b["high"], 2); open_scalp("S1 VWAP Momentum", "SELL", sl, round(price - (sl - price) * 1.5, 2))
        # S2
        vw_now, up, lo = vwap_bands(bars, i)
        if b["low"] <= lo and b["absorption"] == "Bullish":
            open_scalp("S2 Band Absorption", "BUY", round(b["low"], 2), vw_now)
        elif b["high"] >= up and b["absorption"] == "Bearish":
            open_scalp("S2 Band Absorption", "SELL", round(b["high"], 2), vw_now)
        # S3
        bk = book.get(b["t"])
        if bk and bk.get("BID", 0) > 0 and bk.get("ASK", 0) > 0:
            if bk["BID"] >= 2 * bk["ASK"]:
                sl = round(b["low"], 2); open_scalp("S3 Imbalance", "BUY", sl, round(price + (price - sl) * 1.5, 2))
            elif bk["ASK"] >= 2 * bk["BID"]:
                sl = round(b["high"], 2); open_scalp("S3 Imbalance", "SELL", sl, round(price - (sl - price) * 1.5, 2))
        # S4
        if b["tape_streak"] >= 3:
            if b["tape_side"] == "BUY":
                sl = round(b["low"], 2); open_scalp("S4 Tape Momentum", "BUY", sl, round(price + (price - sl) * 1.5, 2))
            else:
                sl = round(b["high"], 2); open_scalp("S4 Tape Momentum", "SELL", sl, round(price - (sl - price) * 1.5, 2))
        # S5
        lb = bars[i - 5:i]
        rh = max(x["high"] for x in lb); rl = min(x["low"] for x in lb)
        av = sum(x["volume"] for x in lb) / len(lb)
        spike = av > 0 and b["volume"] > av * 1.5
        if spike and b["close"] > rh:
            open_scalp("S5 Micro Breakout", "BUY", round(rl, 2), round(price + (price - rl) * 1.5, 2))
        elif spike and b["close"] < rl:
            open_scalp("S5 Micro Breakout", "SELL", round(rh, 2), round(price - (rh - price) * 1.5, 2))
    return trades


# ----------------------------------------------------------------------------- HFT engine
def run_hft(ts, px, vol, buy):
    print("Loading level-0 book for HFT replay (1.2M snapshots)...")
    rows = q_csv("SELECT timestamp, side, price, qty FROM depth_v2 WHERE level = 0 ORDER BY timestamp")
    snaps = []                       # (t, bid_p, bid_q, ask_p, ask_q)
    cur_t = None
    cb = ca = None
    for t, side, p, qv in rows:
        if t != cur_t:
            if cb is not None and ca is not None:
                snaps.append((parse_ts(cur_t), cb[0], cb[1], ca[0], ca[1]))
            cur_t = t; cb = ca = None
        if side == "BID":
            cb = (float(p), float(qv))
        else:
            ca = (float(p), float(qv))
    if cb is not None and ca is not None:
        snaps.append((parse_ts(cur_t), cb[0], cb[1], ca[0], ca[1]))
    print(f"  {len(snaps)} snapshots; replaying with {len(ts)} trades")

    buy_times, sell_times = deque(), deque()
    impact = deque(maxlen=300)
    S = {"o": 0.0, "m": 0.0, "oo": 0.0, "om": 0.0}
    prob = deque(maxlen=500)
    pc_tot, pc_up = {}, {}
    pending = deque(maxlen=100)
    prev = None
    pos = None
    trades = []
    ti = 0
    n_t = len(ts)

    def add_impact(o, m):
        if len(impact) == impact.maxlen:
            o2, m2 = impact[0]
            S["o"] -= o2; S["m"] -= m2; S["oo"] -= o2 * o2; S["om"] -= o2 * m2
        impact.append((o, m))
        S["o"] += o; S["m"] += m; S["oo"] += o * o; S["om"] += o * m

    def add_prob(k, up):
        if len(prob) == prob.maxlen:
            k2, u2 = prob[0]
            pc_tot[k2] -= 1; pc_up[k2] -= u2
        prob.append((k, up))
        pc_tot[k] = pc_tot.get(k, 0) + 1
        pc_up[k] = pc_up.get(k, 0) + up

    for (t, pb, qb, pa, qa) in snaps:
        while ti < n_t and ts[ti] <= t:
            (buy_times if buy[ti] else sell_times).append(ts[ti])
            ti += 1
        while buy_times and t - buy_times[0] > 3.0:
            buy_times.popleft()
        while sell_times and t - sell_times[0] > 3.0:
            sell_times.popleft()
        mid = (pb + pa) / 2.0
        if prev is not None:
            pb0, qb0, pa0, qa0 = prev
            ofi = (qb if pb >= pb0 else 0.0) - (qb0 if pb <= pb0 else 0.0) - (qa if pa <= pa0 else 0.0) + (qa0 if pa >= pa0 else 0.0)
            dsum = qb + qa
            ofi_norm = ofi / dsum if dsum > 0 else 0.0
            spread = pa - pb
            if dsum > 0:
                micro = (pa * qb + pb * qa) / dsum
                mps = (micro - mid) / spread if spread > 0 else 0.0
                qi = (qb - qa) / dsum
            else:
                mps = qi = 0.0
            ii = (len(buy_times) - len(sell_times)) / 3.0
            score = 2.0 * ofi_norm + 1.0 * qi + 1.5 * mps + 0.5 * math.tanh(ii)

            while pending and t - pending[0][0] >= 2.0:
                t0, p0, o0, s0 = pending.popleft()
                mv = mid - p0
                add_impact(o0, mv)
                add_prob(int(round(round(s0, 1) * 10)), 1 if mv > 0 else 0)
            pending.append((t, mid, ofi_norm, score))

            if pos is not None:
                is_long = pos["d"] == "BUY"
                hit_tp = mid >= pos["tp"] if is_long else mid <= pos["tp"]
                hit_sl = mid <= pos["sl"] if is_long else mid >= pos["sl"]
                if hit_tp or hit_sl or (t - pos["t"] >= 9.0):
                    xp = pos["sl"] if hit_sl else (pos["tp"] if hit_tp else mid)
                    trades.append((pos["t"], pnl_of(pos["d"], pos["e"], xp)))
                    pos = None
            if pos is None and len(impact) >= 30 and abs(score) > 0.15:
                n = len(impact)
                mo = S["o"] / n; mm = S["m"] / n
                cov = S["om"] / n - mo * mm
                var = S["oo"] / n - mo * mo
                lam = (cov / var) if var > 1e-12 else None
                if lam is not None:
                    em = lam * ofi_norm
                    d = None
                    if score > 0.15 and em > 0:
                        d = "BUY"
                    elif score < -0.15 and em < 0:
                        d = "SELL"
                    if d:
                        k = int(round(round(score, 1) * 10))
                        tot = sum(pc_tot.get(x, 0) for x in (k - 1, k, k + 1))
                        up = sum(pc_up.get(x, 0) for x in (k - 1, k, k + 1))
                        p_up = (up / tot) if tot >= 20 else 0.5
                        p_dir = p_up if d == "BUY" else 1.0 - p_up
                        ev_gross = p_dir * abs(em) - (1 - p_dir) * abs(em) * 0.5
                        ev_net = ev_gross - spread - 0.05
                        if ev_net > 0.05:
                            sl_d = max(0.4, spread * 3)
                            tp_d = max(0.6, sl_d * 1.3)
                            pos = {"d": d, "e": mid, "t": t,
                                   "sl": mid - sl_d if d == "BUY" else mid + sl_d,
                                   "tp": mid + tp_d if d == "BUY" else mid - tp_d}
        prev = (pb, qb, pa, qa)
    return {"HFT-Engine": trades}


# ----------------------------------------------------------------------------- report
def stats(tr):
    if not tr:
        return dict(n=0, wr=0.0, pnl=0.0, net=0.0, dd=0.0, avg=0.0)
    tr = sorted(tr)
    pnls = [p for _, p in tr]
    eq = 100.0; peak = eq; dd = 0.0
    for p in pnls:
        eq += p - SPREAD_COST
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    n = len(pnls)
    return dict(n=n, wr=sum(1 for p in pnls if p > 0) / n * 100, pnl=sum(pnls),
                net=sum(pnls) - SPREAD_COST * n, dd=dd, avg=sum(pnls) / n)


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars(ts, px, vol, buy)
    finalize_features(bars)
    span = (ts[-1] - ts[0]) / 86400
    split_t = ts[0] + (ts[-1] - ts[0]) * 0.67
    print(f"{len(bars)} bars over {span:.2f} days; split at {datetime.fromtimestamp(split_t, timezone.utc)}\n")

    book = load_book_imbalance()
    allt = run_bar_strategies(bars, ts, px, book)
    allt.update(run_hft(ts, px, vol, buy))

    order = ["Sniper V3_RR2+N4", "Confluence", "S1 VWAP Momentum", "S2 Band Absorption", "S3 Imbalance",
             "S4 Tape Momentum", "S5 Micro Breakout", "HFT-Engine"]
    print("\n" + "=" * 128)
    print(f"{'Strategy':20s} | {'Period':10s} | {'Trades':>6s} | {'WR%':>5s} | {'AvgPnL/tr':>9s} | {'Gross PnL':>10s} | "
          f"{'Net (-$%.2f/tr)' % SPREAD_COST:>16s} | {'MaxDD%(net)':>11s}")
    print("=" * 128)
    for name in order:
        tr = allt.get(name, [])
        for label, sub in (("FULL", tr), ("IN-SAMPLE", [x for x in tr if x[0] < split_t]),
                           ("OUT-SAMPLE", [x for x in tr if x[0] >= split_t])):
            s = stats(sub)
            print(f"{name if label == 'FULL' else '':20s} | {label:10s} | {s['n']:6d} | {s['wr']:5.1f} | "
                  f"{s['avg']:9.3f} | {s['pnl']:10.2f} | {s['net']:16.2f} | {s['dd']:11.1f}")
        print("-" * 128)


if __name__ == "__main__":
    main()
