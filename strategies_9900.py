"""
strategies_9900.py - PAPER-ONLY strategy layer for Port 9900 (iTick real-spot
order-flow terminal). Two INDEPENDENT strategies, each with its own position
and its own paper DB, both driven by 9900's REAL iTick spot bars:

  1. VWAP Full-Crossover  - the exact same rule 9060/9080 run
     (reuses vwap_full_crossover.VwapFullCrossover unchanged; mt5 hooks = None).
  2. Order-Flow Zones     - the exact same logic 9100 runs
     (Order Block + structural SL + min-1.5R + stacked footprint imbalance +
      CVD structure break + $2-arm/$1-trail), ported here as a paper class.

NO MT5. NO real orders. This is signal + paper-PnL tracking only, so the
real-spot behaviour of both strategies can be OBSERVED and later backtested
before any execution decision (which stays entirely the user's own action).

Open positions are kept in memory (a restart starts flat); every CLOSED trade
is persisted, so the trade history and stats survive restarts.
"""
import os
import sqlite3
from datetime import datetime, timezone

from vwap_full_crossover import VwapFullCrossover

# --- Zones constants (identical to PORT_9100) ------------------------------
ZONE_CALC_WINDOW = 300
ZONE_ATR_BUFFER = 0.3
IMPULSE_MULT = 1.2
OB_LOOKAHEAD = 3
SL_TICK_BUFFER = 0.05
MIN_RR = 1.5
STACK_MIN_RUN = 3
STACK_RATIO = 3.0
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0


# ===========================================================================
# Zones pure functions - copied verbatim from PORT_9100_ORDERFLOW_ZONES_CHART
# (they operate only on the bar list, which 9900 produces in the same shape).
# ===========================================================================
def calc_atr(bars, period=14):
    if len(bars) < 2:
        return 2.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return (sum(recent) / len(recent)) if recent else 2.0


def build_zones(bars, tf_label, atr, max_zones=4, impulse_mult=IMPULSE_MULT, lookahead=OB_LOOKAHEAD):
    if len(bars) < lookahead + 2:
        return []
    zones = []
    for m in range(1, len(bars) - lookahead):
        b = bars[m]
        a = atr if atr > 0 else 1.0
        if b["close"] < b["open"]:
            fwd_high = max(bars[j]["high"] for j in range(m + 1, m + 1 + lookahead))
            if fwd_high - b["close"] >= impulse_mult * a:
                top = max(b["open"], b["close"])
                bottom = b["low"] - ZONE_ATR_BUFFER * a
                mitigated = any(bars[j]["close"] < bottom for j in range(m + 1, len(bars)))
                zones.append({"kind": "DEMAND", "top": round(top, 2), "bottom": round(bottom, 2),
                              "start_time": int(b["time"].timestamp()), "tf": tf_label, "mitigated": mitigated,
                              "origin_low": round(b["low"], 2), "origin_high": round(b["high"], 2)})
        if b["close"] > b["open"]:
            fwd_low = min(bars[j]["low"] for j in range(m + 1, m + 1 + lookahead))
            if b["close"] - fwd_low >= impulse_mult * a:
                top = b["high"] + ZONE_ATR_BUFFER * a
                bottom = min(b["open"], b["close"])
                mitigated = any(bars[j]["close"] > top for j in range(m + 1, len(bars)))
                zones.append({"kind": "SUPPLY", "top": round(top, 2), "bottom": round(bottom, 2),
                              "start_time": int(b["time"].timestamp()), "tf": tf_label, "mitigated": mitigated,
                              "origin_low": round(b["low"], 2), "origin_high": round(b["high"], 2)})
    active = [z for z in zones if not z["mitigated"]]
    active.sort(key=lambda z: z["start_time"])
    supply = [z for z in active if z["kind"] == "SUPPLY"][-max_zones:]
    demand = [z for z in active if z["kind"] == "DEMAND"][-max_zones:]
    return supply + demand


def has_stacked_imbalance(bar, direction, min_run=STACK_MIN_RUN, ratio=STACK_RATIO):
    prices = sorted(set(bar.get("levels_buy", {})) | set(bar.get("levels_sell", {})))
    if len(prices) < min_run:
        return False
    run = best = 0
    for p in prices:
        bv = bar["levels_buy"].get(p, 0.0)
        sv = bar["levels_sell"].get(p, 0.0)
        if direction == "Bullish" and bv >= ratio * max(sv, 0.01):
            run += 1
        elif direction == "Bearish" and sv >= ratio * max(bv, 0.01):
            run += 1
        else:
            run = 0
        best = max(best, run)
    return best >= min_run


def find_cvd_break(bars, direction, left=2, right=2, lookback=300):
    window_bars = bars[-lookback:] if len(bars) > lookback else bars
    n = len(window_bars)
    if n < left + right + 1:
        return False
    last_high_val = last_low_val = None
    for i in range(left, n - right):
        cvd_window = [window_bars[j]["cvd"] for j in range(i - left, i + right + 1)]
        if window_bars[i]["cvd"] == max(cvd_window) and cvd_window.count(window_bars[i]["cvd"]) == 1:
            last_high_val = window_bars[i]["cvd"]
        if window_bars[i]["cvd"] == min(cvd_window) and cvd_window.count(window_bars[i]["cvd"]) == 1:
            last_low_val = window_bars[i]["cvd"]
    last_cvd = window_bars[-1]["cvd"]
    if direction == "Bullish":
        return last_high_val is not None and last_cvd > last_high_val
    return last_low_val is not None and last_cvd < last_low_val


def check_zone_reaction(bars, zones, atr):
    if len(bars) < 2:
        return None
    last = bars[-1]
    for z in zones:
        if z["kind"] == "DEMAND" and last["low"] <= z["top"] and last["close"] > z["top"] and last["close"] > last["open"]:
            entry = z["top"]
            sl = round(z["origin_low"] - SL_TICK_BUFFER, 2)
            risk = entry - sl
            if risk <= 0:
                continue
            opp = [zz for zz in zones if zz["kind"] == "SUPPLY" and zz["bottom"] > entry]
            tp = min(zz["bottom"] for zz in opp) if opp else round(entry + 2 * risk, 2)
            if (tp - entry) / risk < MIN_RR:
                continue
            if not has_stacked_imbalance(last, "Bullish"):
                continue
            if not find_cvd_break(bars, "Bullish"):
                continue
            return {"side": "BUY", "entry": round(entry, 2), "sl": sl, "tp": round(tp, 2),
                    "opened_at": int(last["time"].timestamp())}
        if z["kind"] == "SUPPLY" and last["high"] >= z["bottom"] and last["close"] < z["bottom"] and last["close"] < last["open"]:
            entry = z["bottom"]
            sl = round(z["origin_high"] + SL_TICK_BUFFER, 2)
            risk = sl - entry
            if risk <= 0:
                continue
            opp = [zz for zz in zones if zz["kind"] == "DEMAND" and zz["top"] < entry]
            tp = max(zz["top"] for zz in opp) if opp else round(entry - 2 * risk, 2)
            if (entry - tp) / risk < MIN_RR:
                continue
            if not has_stacked_imbalance(last, "Bearish"):
                continue
            if not find_cvd_break(bars, "Bearish"):
                continue
            return {"side": "SELL", "entry": round(entry, 2), "sl": sl, "tp": round(tp, 2),
                    "opened_at": int(last["time"].timestamp())}
    return None


# ===========================================================================
# Zones PAPER strategy - mirrors PORT_9100's _update_zones_and_setup_locked
# exit/trail logic, but paper-only (no MT5) and self-contained.
# ===========================================================================
class ZonesStrategy:
    HCOLS = ["side", "entry", "sl", "tp", "outcome", "exit_price", "pnl", "opened_at", "closed_at"]

    def __init__(self, logger, db_file, mt5_open=None, mt5_modify=None, mt5_close=None,
                 magic=90902, comment="GF-ZONES-9900"):
        self.logger = logger
        self.mt5_open = mt5_open
        self.mt5_modify = mt5_modify
        self.mt5_close = mt5_close
        self.magic = magic
        self.comment = comment
        self.db = sqlite3.connect(db_file, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS setup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, side TEXT, entry REAL, sl REAL, tp REAL,
            outcome TEXT, exit_price REAL, pnl REAL, opened_at INTEGER, closed_at INTEGER)""")
        self.db.commit()
        self.current_setup = None
        self.history = []
        rows = self.db.execute(
            "SELECT side,entry,sl,tp,outcome,exit_price,pnl,opened_at,closed_at "
            "FROM setup_history ORDER BY id DESC LIMIT 200").fetchall()
        self.history = [dict(zip(self.HCOLS, r)) for r in reversed(rows)]

    def on_bar(self, bars):
        """Call once per finalized bar (data already locked by caller)."""
        if len(bars) < 6:
            return
        last = bars[-1]
        closed_at = int(last["time"].timestamp())

        if self.current_setup is not None:
            s = self.current_setup
            if s["side"] == "BUY":
                s["best"] = max(s.get("best", s["entry"]), last["high"])
                if not s.get("armed") and (s["best"] - s["entry"]) >= TRAIL_ARM:
                    s["armed"] = True
                if s.get("armed"):
                    new_sl = round(max(s["sl"], s["best"] - TRAIL_DIST), 2)
                    if new_sl != s["sl"]:
                        s["sl"] = new_sl
                        if self.mt5_modify and s.get("ticket"):
                            self.mt5_modify(s["ticket"], new_sl, 0)
                if last["low"] <= s["sl"]:
                    self._close("TP" if s.get("armed") else "SL", s["sl"], closed_at)
            else:
                s["best"] = min(s.get("best", s["entry"]), last["low"])
                if not s.get("armed") and (s["entry"] - s["best"]) >= TRAIL_ARM:
                    s["armed"] = True
                if s.get("armed"):
                    new_sl = round(min(s["sl"], s["best"] + TRAIL_DIST), 2)
                    if new_sl != s["sl"]:
                        s["sl"] = new_sl
                        if self.mt5_modify and s.get("ticket"):
                            self.mt5_modify(s["ticket"], new_sl, 0)
                if last["high"] >= s["sl"]:
                    self._close("TP" if s.get("armed") else "SL", s["sl"], closed_at)

        if self.current_setup is None:
            calc_bars = bars[-ZONE_CALC_WINDOW:]
            atr = calc_atr(calc_bars)
            zones = build_zones(calc_bars, "LTF", atr)
            new_setup = check_zone_reaction(bars, zones, atr)
            if new_setup:
                new_setup["best"] = new_setup["entry"]
                new_setup["armed"] = False
                new_setup["ticket"] = None
                if self.mt5_open:   # SL only (tp=0): exit is the trailing stop, not a fixed TP
                    new_setup["ticket"] = self.mt5_open(new_setup["side"], new_setup["sl"], 0,
                                                        self.magic, self.comment)
                self.current_setup = new_setup
                self.logger.info(f"[Zones-9900] {new_setup['side']} @ {new_setup['entry']:.2f} "
                                 f"SL={new_setup['sl']:.2f} TP={new_setup['tp']:.2f}")

    def _close(self, outcome, exit_price, closed_at):
        s = self.current_setup
        if self.mt5_close and s.get("ticket"):
            self.mt5_close(s["ticket"], s["side"])   # instant MT5 exit mirrors the paper exit
        pnl = (exit_price - s["entry"]) if s["side"] == "BUY" else (s["entry"] - exit_price)
        rec = {"side": s["side"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
               "outcome": outcome, "exit_price": round(exit_price, 2), "pnl": round(pnl, 2),
               "opened_at": s["opened_at"], "closed_at": closed_at}
        self.history.append(rec)
        try:
            self.db.execute(
                "INSERT INTO setup_history (side,entry,sl,tp,outcome,exit_price,pnl,opened_at,closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["side"], rec["entry"], rec["sl"], rec["tp"], rec["outcome"],
                 rec["exit_price"], rec["pnl"], rec["opened_at"], rec["closed_at"]))
            self.db.commit()
        except Exception:
            self.logger.exception("[Zones-9900] DB write failed")
        self.logger.info(f"[Zones-9900] {outcome} {s['side']} exit {exit_price:.2f} | pnl ${pnl:+.2f}")
        self.current_setup = None

    def open_snapshot(self):
        s = self.current_setup
        if not s:
            return None
        return {"side": s["side"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
                "armed": bool(s.get("armed")), "opened_at": s["opened_at"]}


# ===========================================================================
# Manager - one place the terminal hooks into.
# ===========================================================================
def _today_local():
    return datetime.now().astimezone().date()


def _iso_local_date(iso_str):
    try:
        return datetime.fromisoformat(iso_str).astimezone().date()
    except Exception:
        return None


def _epoch_local_date(sec):
    try:
        return datetime.fromtimestamp(sec).astimezone().date()
    except Exception:
        return None


def _stats(trades):
    n = len(trades)
    net = round(sum(t["pnl"] for t in trades), 2)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {"n": n, "wins": wins, "losses": n - wins, "net": net}


class Strategies9900:
    def __init__(self, logger, script_dir, mt5exec=None):
        self.logger = logger
        _open = mt5exec.place if mt5exec else None
        _modify = mt5exec.modify if mt5exec else None
        _close = mt5exec.close if mt5exec else None
        # Distinct magics so 9900's orders are separable from 9060/9080 on the demo.
        self.vwapx = VwapFullCrossover(
            "9900", logger,
            db_file=os.path.join(script_dir, "vwapx_9900.db"),
            mt5_open=_open, mt5_modify=_modify, magic=90901)
        self.zones = ZonesStrategy(
            logger, os.path.join(script_dir, "zones_9900.db"),
            mt5_open=_open, mt5_modify=_modify, mt5_close=_close, magic=90902, comment="GF-ZONES-9900")
        mode = "MT5 DEMO" if (mt5exec and getattr(mt5exec, "ready", False)) else "PAPER"
        logger.info(f"Strategies9900 ready ({mode}): VWAP-Crossover(magic 90901) + Order-Flow Zones(magic 90902).")

    # --- hooks -------------------------------------------------------------
    def on_bar_finalized(self, bars, vwap_last, now_utc):
        fb = bars[-1]
        try:
            self.vwapx.on_bar(fb, vwap_last, fb["close"], now_utc)
        except Exception:
            self.logger.exception("[VWAPCross-9900] on_bar failed")
        try:
            self.zones.on_bar(bars)
        except Exception:
            self.logger.exception("[Zones-9900] on_bar failed")

    def on_price(self, price, now_utc):
        try:
            self.vwapx.on_price(price, now_utc)     # per-tick trailing/exit
        except Exception:
            self.logger.exception("[VWAPCross-9900] on_price failed")

    # --- read for the dashboard -------------------------------------------
    def snapshot(self):
        today = _today_local()

        # VWAP-Crossover open positions (in memory)
        vx_open = [{"side": p["side"], "entry": round(p["entry"], 2), "sl": round(p["sl"], 2),
                    "tp": round(p["tp"], 2), "armed": bool(p.get("armed"))}
                   for p in self.vwapx.positions]
        # VWAP-Crossover closed today (from its DB)
        vx_rows = self.vwapx.db.execute(
            "SELECT side,entry,exit_price,pnl,opened_at,closed_at,outcome FROM trades "
            "WHERE port='9900' ORDER BY id DESC LIMIT 200").fetchall()
        vx_today = []
        for side, en, ex, pnl, oa, ca, oc in vx_rows:
            if _iso_local_date(ca or oa) == today:
                vx_today.append({"side": side, "entry": en, "exit": ex, "pnl": round(pnl or 0, 2),
                                 "opened_at": oa, "closed_at": ca, "outcome": oc})
        vx_today.reverse()

        # Zones open + closed today
        zo_open = self.zones.open_snapshot()
        zo_today = [{"side": r["side"], "entry": r["entry"], "exit": r["exit_price"],
                     "pnl": round(r["pnl"], 2), "opened_at": r["opened_at"],
                     "closed_at": r["closed_at"], "outcome": r["outcome"]}
                    for r in self.zones.history if _epoch_local_date(r["closed_at"]) == today]

        return {
            "vwapx": {"name": "VWAP Full-Crossover", "open": vx_open,
                      "today": vx_today, "stats": _stats(vx_today)},
            "zones": {"name": "Order-Flow Zones", "open": [zo_open] if zo_open else [],
                      "today": zo_today, "stats": _stats(zo_today)},
            "mode": "PAPER (no MT5) - real-spot signals only",
        }
