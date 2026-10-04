"""
Standalone backtest (read-only): ROUND 2 of order-flow zone improvements -
the 7 ideas discussed (stacked imbalances, anchored VWAP, delta-weighted POC,
order-book imbalance, relative volume, liquidity pools, CVD trendline break),
each tested on top of the already-validated Round-1 winner (Order-Block
zones + min-1.5R filter), PLUS a from-scratch redesign of SL/TP placement
(the user asked explicitly: which candle should the SL sit behind, and
where should the reward target actually be, not just "zone edge -/+ ATR").
Same trailing exit as Round 1 throughout (no fixed TP).

SL/TP REDESIGN (this is the "use your own judgement" part of the ask):
  Old SL: zone edge -/+ 0.3xATR (an arbitrary buffer, same for every zone).
  New SL ("structural"): the ORIGIN CANDLE's own high/low +/- a small fixed
    tick buffer. Reasoning: that candle IS the order block / swing point the
    whole setup is built on - if price actually trades through ITS extreme,
    the premise is objectively wrong, not just "a bit unlucky". This is also
    tighter than a 0.3xATR buffer on average, which should improve R:R for
    free if it doesn't increase stop-outs too much - tested below, not
    assumed.
  Old reward estimate: distance to the nearest opposite zone.
  New reward estimate ("liquidity-aware"): distance to the nearest LIQUIDITY
    POOL beyond the nearest opposite zone - i.e. a cluster of 2+ swing
    points within a small tolerance, which is where stop-loss/breakout
    orders actually concentrate and where price is statistically more
    likely to actually travel to, not just the first opposing zone.

ROUND-2 IDEAS, each layered onto the OB+minRR base and checked individually:
  I1_STACKIMB - require 3+ consecutive footprint price-levels imbalanced
                the same direction (buy/sell ratio >= 3) inside the signal
                candle - "stacked imbalance", a real ATAS/Bookmap footprint
                concept, different from plain FVG.
  I2_AVWAP    - require price to be reacting FROM the correct side of an
                Anchored VWAP (anchored to the most recent opposite-extreme
                swing point, re-anchoring as new swings form) - not the
                rolling 90-bar VWAP already on the chart.
  I3_DPOC     - require the zone to sit near a Delta-weighted POC (price
                level with the largest |buy-sell| imbalance in the trailing
                window), not just the plain volume POC.
  I4_OBI      - require real Binance order-book imbalance (top-3 levels,
                from depth_v2) favoring the trade direction at signal time.
  I5_RVOL     - require the signal candle's volume to be elevated versus
                the SAME time-of-day on prior days (relative volume), not
                just an absolute number.
  I6_LIQPOOL  - reward-target upgrade only (see SL/TP redesign above) -
                tested as its own row since it changes the min-RR filter's
                pass/fail set even with nothing else changed.
  I7_CVDTL    - require the CVD series' own short-term structure (mini
                swing highs/lows on cumulative delta) to have already
                broken in the trade's direction ("delta leads price").

Data: real Binance PAXG ticks + real order-book depth from QuestDB, same
documented caveat as every other backtest here (historical iTick spot was
never stored; PAXG is the closest real proxy).

Run: python backtest_orderflow_zones_tiers2.py
"""
import urllib.parse
import urllib.request
import json
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_orderflow_zones_tiers import (
    calc_atr_series, precompute_daily_bias_series, run_trailing_exit,
    stats, row, generate_random_signals, ZONE_ATR_BUFFER, MIN_RR, MAX_ZONES_KEPT,
    IMPULSE_MULT, OB_LOOKAHEAD, _mitigate,
)

Q = "http://127.0.0.1:9010"
SPREAD = 0.24
PRICE_BIN = 0.1
STACK_MIN_RUN = 3
STACK_RATIO = 3.0
SL_TICK_BUFFER = 0.05          # structural SL: origin candle extreme +/- this
LIQPOOL_TOLERANCE = 1.0        # $ tolerance to cluster swing points into a pool
LIQPOOL_MIN_TOUCHES = 2
RVOL_LOOKBACK_DAYS = 5
RVOL_MIN_RATIO = 1.3
DPOC_WINDOW_BARS = 30
DPOC_TOLERANCE = 1.0
OBI_MIN_RATIO = 1.3            # bid/ask (or ask/bid) ratio required


def q_json(sql):
    url = Q + "/exec?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=600) as r:
        return json.loads(r.read().decode())["dataset"]


# ============================================================================
# Bars WITH footprint (per-price buy/sell volume), needed for I1/I3
# ============================================================================
def build_bars_with_footprint(ts, px, vol, buy):
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
        lvl = round(p / PRICE_BIN) * PRICE_BIN
        if cur is None or cur["t"] < minute:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": minute, "open": p, "high": p, "low": p, "close": p, "volume": v,
                   "buy_vol": v if b else 0.0, "sell_vol": 0.0 if b else v,
                   "vwap": vwap, "cvd": cum_delta, "last_tick": i,
                   "levels_buy": {}, "levels_sell": {}}
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
        bucket = cur["levels_buy"] if b else cur["levels_sell"]
        bucket[lvl] = bucket.get(lvl, 0.0) + v
    for i, b in enumerate(bars):
        delta = b["buy_vol"] - b["sell_vol"]
        b["delta"] = delta
        b["div"] = None
        if i >= 4:
            cs = b["cvd"] - bars[i - 4]["cvd"]
            ps = b["close"] - bars[i - 4]["close"]
            if cs > 0 and ps < 0:
                b["div"] = "Bullish"
            elif cs < 0 and ps > 0:
                b["div"] = "Bearish"
    return bars


def build_ob_zones_causal_full(bars, atrs, impulse_mult=IMPULSE_MULT, lookahead=OB_LOOKAHEAD, max_zones=MAX_ZONES_KEPT):
    """Same as Round 1's build_ob_zones_causal, but ALSO records the origin
    candle's own high/low on each zone (origin_high/origin_low) - needed for
    the structural SL redesign."""
    zones = []
    zones_by_bar = [[] for _ in range(len(bars))]
    for i in range(len(bars)):
        m = i - lookahead
        if m >= 1:
            b = bars[m]
            atr = atrs[m] if atrs[m] > 0 else 1.0
            if b["close"] < b["open"]:
                fwd_high = max(bars[j]["high"] for j in range(m + 1, i + 1))
                if fwd_high - b["close"] >= impulse_mult * atr:
                    zones.append({"kind": "DEMAND", "top": max(b["open"], b["close"]),
                                  "bottom": b["low"] - ZONE_ATR_BUFFER * atr, "start_idx": m, "mitigated": False,
                                  "origin_low": b["low"], "origin_high": b["high"]})
            if b["close"] > b["open"]:
                fwd_low = min(bars[j]["low"] for j in range(m + 1, i + 1))
                if b["close"] - fwd_low >= impulse_mult * atr:
                    zones.append({"kind": "SUPPLY", "bottom": min(b["open"], b["close"]),
                                  "top": b["high"] + ZONE_ATR_BUFFER * atr, "start_idx": m, "mitigated": False,
                                  "origin_low": b["low"], "origin_high": b["high"]})
        _mitigate(zones, bars[i]["close"])
        zones_by_bar[i] = [z for z in zones if not z["mitigated"]][-max_zones:]
    return zones_by_bar


# ============================================================================
# I1 - Stacked Imbalances (footprint-based, causal per-bar)
# ============================================================================
def precompute_stacked_imbalance_series(bars):
    out = [None] * len(bars)
    for i, b in enumerate(bars):
        prices = sorted(set(b["levels_buy"]) | set(b["levels_sell"]))
        if len(prices) < STACK_MIN_RUN:
            continue
        run_up = run_down = 0
        best_up = best_down = 0
        for p in prices:
            buy_v = b["levels_buy"].get(p, 0.0)
            sell_v = b["levels_sell"].get(p, 0.0)
            if buy_v >= STACK_RATIO * max(sell_v, 0.01):
                run_up += 1; run_down = 0
            elif sell_v >= STACK_RATIO * max(buy_v, 0.01):
                run_down += 1; run_up = 0
            else:
                run_up = run_down = 0
            best_up = max(best_up, run_up)
            best_down = max(best_down, run_down)
        if best_up >= STACK_MIN_RUN:
            out[i] = "Bullish"
        elif best_down >= STACK_MIN_RUN:
            out[i] = "Bearish"
    return out


# ============================================================================
# I2 - Anchored VWAP (from the most recent opposite-extreme swing, causal)
# ============================================================================
def precompute_anchored_vwap_series(bars):
    """Two lines: AVWAP-from-last-swing-low (support context) and
    AVWAP-from-last-swing-high (resistance context), each re-anchoring the
    moment a new confirmed swing extreme forms - never using future bars."""
    avwap_lo = [None] * len(bars)  # anchored from last swing LOW
    avwap_hi = [None] * len(bars)  # anchored from last swing HIGH
    anchor_lo_idx = anchor_hi_idx = None
    cum_pv_lo = cum_vol_lo = 0.0
    cum_pv_hi = cum_vol_hi = 0.0
    left = right = 2
    for i in range(len(bars)):
        m = i - right
        if m - left >= 0:
            window = bars[m - left:i + 1]
            highs = [x["high"] for x in window]
            lows = [x["low"] for x in window]
            if bars[m]["low"] == min(lows) and lows.count(bars[m]["low"]) == 1:
                anchor_lo_idx = m
                cum_pv_lo = cum_vol_lo = 0.0
            if bars[m]["high"] == max(highs) and highs.count(bars[m]["high"]) == 1:
                anchor_hi_idx = m
                cum_pv_hi = cum_vol_hi = 0.0
        tp = (bars[i]["high"] + bars[i]["low"] + bars[i]["close"]) / 3.0
        vol = bars[i]["volume"]
        if anchor_lo_idx is not None:
            cum_pv_lo += tp * vol
            cum_vol_lo += vol
            avwap_lo[i] = cum_pv_lo / cum_vol_lo if cum_vol_lo > 0 else bars[i]["close"]
        if anchor_hi_idx is not None:
            cum_pv_hi += tp * vol
            cum_vol_hi += vol
            avwap_hi[i] = cum_pv_hi / cum_vol_hi if cum_vol_hi > 0 else bars[i]["close"]
    return avwap_lo, avwap_hi


# ============================================================================
# I3 - Delta-weighted POC (trailing window, causal)
# ============================================================================
def precompute_delta_poc_series(bars, window=DPOC_WINDOW_BARS):
    out = [None] * len(bars)
    for i in range(len(bars)):
        lo = max(0, i - window + 1)
        agg = {}
        for b in bars[lo:i + 1]:
            for p, v in b["levels_buy"].items():
                agg[p] = agg.get(p, 0.0) + v
            for p, v in b["levels_sell"].items():
                agg[p] = agg.get(p, 0.0) - v
        if not agg:
            continue
        out[i] = max(agg, key=lambda p: abs(agg[p]))
    return out


# ============================================================================
# I4 - Order-book imbalance (real Binance top-3 depth, per-minute, causal)
# ============================================================================
def load_book_imbalance_by_minute():
    rows = q_json(f"SELECT timestamp, side, sum(qty) FROM depth_v2 WHERE symbol='XAUUSD' AND level<3 SAMPLE BY 1m")
    from datetime import datetime as dt
    per_min = {}
    for t_str, side, qty in rows:
        t = dt.fromisoformat(t_str.replace("Z", "+00:00"))
        minute = int(t.timestamp() // 60) * 60
        per_min.setdefault(minute, {"BID": 0.0, "ASK": 0.0})[side] = qty
    return per_min


def precompute_obi_series(bars, per_min):
    out = [None] * len(bars)
    for i, b in enumerate(bars):
        # use the PRIOR minute's snapshot (causal - this bar's own minute may
        # still be forming when the signal fires on its close)
        d = per_min.get(b["t"] - 60)
        if not d or d["ASK"] <= 0 or d["BID"] <= 0:
            continue
        ratio = d["BID"] / d["ASK"]
        if ratio >= OBI_MIN_RATIO:
            out[i] = "Bullish"
        elif (1.0 / ratio) >= OBI_MIN_RATIO:
            out[i] = "Bearish"
    return out


# ============================================================================
# I5 - Relative Volume vs same time-of-day on prior days (causal)
# ============================================================================
def precompute_rvol_series(bars, lookback_days=RVOL_LOOKBACK_DAYS):
    out = [1.0] * len(bars)
    by_slot = {}  # (weekday-agnostic) minute-of-day -> list of past volumes (deque-like, capped)
    for i, b in enumerate(bars):
        slot = b["t"] % 86400
        hist = by_slot.get(slot, [])
        if hist:
            avg = sum(hist[-lookback_days:]) / len(hist[-lookback_days:])
            out[i] = b["volume"] / avg if avg > 0 else 1.0
        hist.append(b["volume"])
        by_slot[slot] = hist
    return out


# ============================================================================
# I6 - Liquidity pools (clustered swing points) for the reward estimate
# ============================================================================
def precompute_liquidity_pools_causal(bars, left=2, right=2):
    """Returns pools_by_bar[i] = (list of high-pools, list of low-pools)
    known as of bar i - a pool is 2+ swing points within LIQPOOL_TOLERANCE."""
    highs_seen, lows_seen = [], []
    pools_by_bar = [([], []) for _ in range(len(bars))]
    for i in range(len(bars)):
        m = i - right
        if m - left >= 0:
            window = bars[m - left:i + 1]
            hh = [x["high"] for x in window]
            ll = [x["low"] for x in window]
            if bars[m]["high"] == max(hh) and hh.count(bars[m]["high"]) == 1:
                highs_seen.append(bars[m]["high"])
            if bars[m]["low"] == min(ll) and ll.count(bars[m]["low"]) == 1:
                lows_seen.append(bars[m]["low"])
        high_pools = _cluster(highs_seen)
        low_pools = _cluster(lows_seen)
        pools_by_bar[i] = (high_pools, low_pools)
    return pools_by_bar


def _cluster(points, tol=LIQPOOL_TOLERANCE, min_touches=LIQPOOL_MIN_TOUCHES):
    if not points:
        return []
    pts = sorted(points)
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        if p - cur[-1] <= tol:
            cur.append(p)
        else:
            if len(cur) >= min_touches:
                clusters.append(sum(cur) / len(cur))
            cur = [p]
    if len(cur) >= min_touches:
        clusters.append(sum(cur) / len(cur))
    return clusters


# ============================================================================
# I7 - CVD's own short-term structure break (mini swings on cumulative delta)
# ============================================================================
def precompute_cvd_structure_break_series(bars, left=2, right=2):
    out = [None] * len(bars)
    cvd_highs, cvd_lows = [], []
    last_cvd_high_val = last_cvd_low_val = None
    for i in range(len(bars)):
        m = i - right
        if m - left >= 0:
            window = bars[m - left:i + 1]
            hh = [x["cvd"] for x in window]
            ll = [x["cvd"] for x in window]
            if bars[m]["cvd"] == max(hh) and hh.count(bars[m]["cvd"]) == 1:
                last_cvd_high_val = bars[m]["cvd"]
            if bars[m]["cvd"] == min(ll) and ll.count(bars[m]["cvd"]) == 1:
                last_cvd_low_val = bars[m]["cvd"]
        if last_cvd_high_val is not None and bars[i]["cvd"] > last_cvd_high_val:
            out[i] = "Bullish"
        elif last_cvd_low_val is not None and bars[i]["cvd"] < last_cvd_low_val:
            out[i] = "Bearish"
    return out


# ============================================================================
# Signal generation for this round - OB zones + min-RR always on (the
# validated Round-1 base), each I-flag adds one more required confirmation.
# SL/TP redesign is its own toggle (structural_sl, liqpool_reward).
# ============================================================================
def generate_signals_r2(bars, atrs, zones_by_bar, structural_sl=False, liqpool_reward=False,
                         pools_by_bar=None, stacked=None, avwap_lo=None, avwap_hi=None,
                         dpoc=None, obi=None, rvol=None, cvd_break=None,
                         require_stack=False, require_avwap=False, require_dpoc=False,
                         require_obi=False, require_rvol=False, require_cvdbreak=False):
    sigs = []
    for i in range(6, len(bars)):
        b = bars[i]
        zones = zones_by_bar[i - 1]
        atr = atrs[i] if atrs[i] > 0 else 1.0

        for z in zones:
            d = None
            if z["kind"] == "DEMAND" and b["low"] <= z["top"] and b["close"] > z["top"] and b["close"] > b["open"]:
                d = "BUY"
            elif z["kind"] == "SUPPLY" and b["high"] >= z["bottom"] and b["close"] < z["bottom"] and b["close"] < b["open"]:
                d = "SELL"
            if d is None:
                continue

            if structural_sl:
                sl = (z["origin_low"] - SL_TICK_BUFFER) if d == "BUY" else (z["origin_high"] + SL_TICK_BUFFER)
            else:
                sl = z["bottom"] - ZONE_ATR_BUFFER * atr if d == "BUY" else z["top"] + ZONE_ATR_BUFFER * atr
            entry_est = z["top"] if d == "BUY" else z["bottom"]
            risk = entry_est - sl if d == "BUY" else sl - entry_est
            if risk <= 0:
                continue

            if liqpool_reward and pools_by_bar is not None:
                high_pools, low_pools = pools_by_bar[i - 1]
                if d == "BUY":
                    opp = [zz for zz in zones if zz["kind"] == "SUPPLY" and zz["bottom"] > entry_est]
                    base_target = min((zz["bottom"] for zz in opp), default=entry_est + 2 * risk)
                    beyond = [p for p in high_pools if p > base_target]
                    reward = (min(beyond) if beyond else base_target) - entry_est
                else:
                    opp = [zz for zz in zones if zz["kind"] == "DEMAND" and zz["top"] < entry_est]
                    base_target = max((zz["top"] for zz in opp), default=entry_est - 2 * risk)
                    beyond = [p for p in low_pools if p < base_target]
                    reward = entry_est - (max(beyond) if beyond else base_target)
            else:
                if d == "BUY":
                    opp = [zz for zz in zones if zz["kind"] == "SUPPLY" and zz["bottom"] > entry_est]
                    reward = (min(zz["bottom"] for zz in opp) - entry_est) if opp else 2 * risk
                else:
                    opp = [zz for zz in zones if zz["kind"] == "DEMAND" and zz["top"] < entry_est]
                    reward = (entry_est - max(zz["top"] for zz in opp)) if opp else 2 * risk
            if reward / risk < MIN_RR:
                continue

            want = "Bullish" if d == "BUY" else "Bearish"
            if require_stack and (stacked is None or stacked[i] != want):
                continue
            if require_avwap:
                ref = avwap_lo[i] if d == "BUY" else avwap_hi[i]
                if ref is None or (d == "BUY" and b["close"] < ref) or (d == "SELL" and b["close"] > ref):
                    continue
            if require_dpoc:
                if dpoc is None or dpoc[i] is None or abs(entry_est - dpoc[i]) > DPOC_TOLERANCE:
                    continue
            if require_obi and (obi is None or obi[i] != want):
                continue
            if require_rvol and (rvol is None or rvol[i] < RVOL_MIN_RATIO):
                continue
            if require_cvdbreak and (cvd_break is None or cvd_break[i] != want):
                continue

            sigs.append((i, d, sl))
            break
    return sigs


def execute(bars, ts, px, sigs):
    trades = []
    free_idx = -1
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
        trades.append({"t": ts[nxt], "d": d, "risk": abs(entry - sl), "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days of real PAXG ticks | spread ${SPREAD}\n")

    bars = build_bars_with_footprint(ts, px, vol, buy)
    atrs = calc_atr_series(bars)
    print(f"{len(bars)} one-minute bars built (with footprint levels).\n")

    print("Building Order-Block zones (Round-1 winner, causal)...")
    ob_zones = build_ob_zones_causal_full(bars, atrs)
    print("Building I1 stacked-imbalance series...")
    stacked = precompute_stacked_imbalance_series(bars)
    print("Building I2 anchored-VWAP series...")
    avwap_lo, avwap_hi = precompute_anchored_vwap_series(bars)
    print("Building I3 delta-weighted-POC series...")
    dpoc = precompute_delta_poc_series(bars)
    print("Loading I4 real order-book depth + building imbalance series...")
    per_min = load_book_imbalance_by_minute()
    obi = precompute_obi_series(bars, per_min)
    print("Building I5 relative-volume series...")
    rvol = precompute_rvol_series(bars)
    print("Building I6 liquidity-pool series...")
    pools_by_bar = precompute_liquidity_pools_causal(bars)
    print("Building I7 CVD-structure-break series...\n")
    cvd_break = precompute_cvd_structure_break_series(bars)

    base_sigs = generate_signals_r2(bars, atrs, ob_zones)
    base_tr = execute(bars, ts, px, base_sigs)
    base_s = stats(base_tr)
    print("=" * 122)
    print("BASE (Round-1 winner: OB zones + min-1.5R, old ATR-buffer SL) - for reference")
    print("=" * 122)
    print(row("BASE", base_s))
    print()

    print("=" * 122)
    print("SL/TP REDESIGN - tested alone first")
    print("=" * 122)
    struct_sigs = generate_signals_r2(bars, atrs, ob_zones, structural_sl=True)
    print(row("Structural SL (origin candle extreme)", stats(execute(bars, ts, px, struct_sigs))))
    liq_sigs = generate_signals_r2(bars, atrs, ob_zones, liqpool_reward=True, pools_by_bar=pools_by_bar)
    print(row("Liquidity-pool reward target", stats(execute(bars, ts, px, liq_sigs))))
    both_sigs = generate_signals_r2(bars, atrs, ob_zones, structural_sl=True, liqpool_reward=True, pools_by_bar=pools_by_bar)
    print(row("Structural SL + liquidity-pool reward", stats(execute(bars, ts, px, both_sigs))))
    print()

    print("=" * 122)
    print("ROUND-2 IDEAS (each layered on the BASE, with random control)")
    print("=" * 122)
    ideas = {
        "I1_STACKIMB (+ stacked footprint imbalance)": dict(stacked=stacked, require_stack=True),
        "I2_AVWAP (+ anchored VWAP side)": dict(avwap_lo=avwap_lo, avwap_hi=avwap_hi, require_avwap=True),
        "I3_DPOC (+ delta-weighted POC nearby)": dict(dpoc=dpoc, require_dpoc=True),
        "I4_OBI (+ real order-book imbalance)": dict(obi=obi, require_obi=True),
        "I5_RVOL (+ relative volume >=1.3x)": dict(rvol=rvol, require_rvol=True),
        "I7_CVDTL (+ CVD structure break)": dict(cvd_break=cvd_break, require_cvdbreak=True),
    }
    idea_results = {}
    for seed_i, (name, kwargs) in enumerate(ideas.items()):
        sigs = generate_signals_r2(bars, atrs, ob_zones, **kwargs)
        tr = execute(bars, ts, px, sigs)
        s = stats(tr)
        idea_results[name] = (s, sigs)
        print(row(name, s))
        rnd_sigs = generate_random_signals(bars, atrs, len(sigs), seed=3000 + seed_i)
        rnd_tr = execute(bars, ts, px, rnd_sigs)
        print(row(f"   -> random control ({len(sigs)} sigs)", stats(rnd_tr)))
        print()

    print("=" * 122)
    print("FINAL COMBINED - structural SL (the SL/TP win) + the Round-2 ideas that")
    print("individually beat both BASE and random: I1 stacked-imbalance,")
    print("I4 order-book-imbalance, I7 CVD-structure-break")
    print("=" * 122)
    final_variants = {
        "FINAL: structuralSL only": dict(structural_sl=True),
        "FINAL: structuralSL + I1": dict(structural_sl=True, stacked=stacked, require_stack=True),
        "FINAL: structuralSL + I4": dict(structural_sl=True, obi=obi, require_obi=True),
        "FINAL: structuralSL + I7": dict(structural_sl=True, cvd_break=cvd_break, require_cvdbreak=True),
        "FINAL: structuralSL + I1+I7": dict(structural_sl=True, stacked=stacked, require_stack=True,
                                             cvd_break=cvd_break, require_cvdbreak=True),
        "FINAL: structuralSL + I1+I4+I7": dict(structural_sl=True, stacked=stacked, require_stack=True,
                                                obi=obi, require_obi=True, cvd_break=cvd_break, require_cvdbreak=True),
    }
    final_sigs_map = {}
    for seed_i, (name, kwargs) in enumerate(final_variants.items()):
        sigs = generate_signals_r2(bars, atrs, ob_zones, **kwargs)
        final_sigs_map[name] = sigs
        tr = execute(bars, ts, px, sigs)
        s = stats(tr)
        print(row(name, s))
        rnd_sigs = generate_random_signals(bars, atrs, len(sigs), seed=4000 + seed_i)
        rnd_tr = execute(bars, ts, px, rnd_sigs)
        print(row(f"   -> random control ({len(sigs)} sigs)", stats(rnd_tr)))
        print()

    print("=" * 122)
    print("IN-SAMPLE / OUT-OF-SAMPLE CHECK - the finalists")
    print("=" * 122)
    split_i = int(len(bars) * 0.67)
    split_t = bars[split_i]["t"]
    for name in ["FINAL: structuralSL only", "FINAL: structuralSL + I1", "FINAL: structuralSL + I7",
                 "FINAL: structuralSL + I1+I7", "FINAL: structuralSL + I1+I4+I7"]:
        sigs = final_sigs_map[name]
        tr = execute(bars, ts, px, sigs)
        is_tr = [x for x in tr if x["t"] < split_t]
        oos_tr = [x for x in tr if x["t"] >= split_t]
        print(f"{name}:")
        print(row("  in-sample (first 67%)", stats(is_tr)))
        print(row("  out-of-sample (last 33%)", stats(oos_tr)))
        print()


if __name__ == "__main__":
    main()
