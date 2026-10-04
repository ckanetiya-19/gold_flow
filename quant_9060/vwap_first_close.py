"""
VWAP paper strategies - FINAL SET (1-minute only), shared by PORT_9060 and PORT_9080.
Paper trading by default. Optionally places/modifies REAL demo-account MT5 orders when the caller
wires in mt5_open/mt5_modify callbacks - ONLY ONE port should do this (currently PORT_9080) to avoid
placing duplicate trades for the same signal from two independent processes.

Common rules (validated by the backtest_vwap_*.py scripts):
  * VWAP = daily-reset (00:00 UTC) volume-weighted average of the spot price.
  * A "stretch" = consecutive candle closes on the same side of VWAP.
  * FIRST entry : first GREEN candle closing above VWAP (BUY) / first RED candle closing below VWAP
                  (SELL) in a stretch. One per stretch (even if skipped).
  * RETEST-B entry: inside a stretch (after the first-close candle) a GREEN (BUY) / RED (SELL) candle
                  that closes on the stretch side of VWAP AND whose low/high comes within $0.50 of VWAP,
                  while the PREVIOUS candle did not (= fresh pullback, not price hugging VWAP).
  * Entry at once = first price of the next candle. One open position per variant.
  * Stop: CANDLE = signal candle low/high -/+ $1 ; VWAP = VWAP -/+ $0.50.
  * Exit mode TP    : target = entry +/- 2 x risk ; 1:1 break-even (SL -> entry +/- $0.05 once +1R reached).
  * Exit mode TRAIL : no fixed target - once floating profit reaches $2, SL trails $1 behind the best
                      price reached since entry (only tightens, never loosens).
  * On (re)start the current stretch is treated as already used (history unknown).
  * Signals are skipped while spot gold is closed (Fri 21:00 - Sun 22:00 UTC).
Every trade is written to vwap_fc_paper.db (clean $ per 0.01 lot, spread $0.24 subtracted, column `variant`).
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

SPREAD = 0.24
SL_BUFFER = 1.0
TP_MULT = 2.0
BE_TRIGGER_R = 1.0
BE_BUFFER = 0.05
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vwap_fc_paper.db")
_db_lock = threading.Lock()

# FINAL SET - 1-minute only, 3 variants (validated by backtest_vwap_*.py + live forward-test).
# T1/T2/T3 (exact duplicates of V1/V4/V10) removed - user confirmed only V1/V4/V10 should trade.
VARIANTS = {
    "V1": dict(desc="First-close, Candle-SL, Trailing $2/$1",      kind="FIRST",  tol=0.0, sl_mode="CANDLE", sl_buf=1.0, exit_mode="TRAIL"),
    "V4": dict(desc="First-close, VWAP-SL $0.50, Trailing $2/$1",  kind="FIRST",  tol=0.0, sl_mode="VWAP",   sl_buf=0.5, exit_mode="TRAIL"),
    "V10": dict(desc="Retest-B, VWAP-SL $0.50, Trailing $2/$1",    kind="RETEST", tol=0.5, sl_mode="VWAP",   sl_buf=0.5, exit_mode="TRAIL"),
}

# Distinct MT5 magic number + comment tag per variant, so each shows up separately
# in the MT5 terminal (positions/history). Only used when mt5_open/mt5_modify are wired in.
MT5_MAGIC = {"V1": 90810, "V4": 90811, "T1": 90812, "T2": 90813, "V10": 90814, "T3": 90815}
MT5_COMMENT = {v: f"GF-{v}" for v in VARIANTS}
MT5_SL_MODIFY_MIN_DELTA = 0.05   # skip a broker modify-SL call for sub-nickel trail moves


def market_closed(dt_utc):
    wd, hr = dt_utc.weekday(), dt_utc.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


def _db(db_file):
    conn = sqlite3.connect(db_file, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, port TEXT, tf INTEGER, dir TEXT,
        open_utc TEXT, close_utc TEXT, entry REAL, sl_initial REAL, tp REAL, risk REAL,
        exit REAL, outcome TEXT, gross REAL, spread REAL, net REAL, hold_min REAL, vwap REAL)""")
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN variant TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


class DailyVwap:
    """Daily-reset VWAP of the spot price, weighted by real trade volume."""

    def __init__(self):
        self.day = None
        self.pv = 0.0
        self.vol = 0.0

    def update(self, price, volume, now_utc):
        d = now_utc.date()
        if d != self.day:
            self.day, self.pv, self.vol = d, 0.0, 0.0
        self.pv += price * volume
        self.vol += volume

    def value(self, now_utc):
        if now_utc.date() != self.day or self.vol <= 0:
            return None
        return self.pv / self.vol


class VwapFirstClose:
    def __init__(self, port, tf_min, daily, logger, on_trade_closed=None, db_file=DB_FILE,
                 variant="V1", kind="FIRST", tol=0.0, sl_mode="CANDLE", sl_buf=1.0, exit_mode="TP", desc="",
                 mt5_open=None, mt5_modify=None):
        self.port, self.tf, self.daily, self.log = port, tf_min, daily, logger
        self.cb, self.db_file = on_trade_closed, db_file
        self.variant, self.kind, self.tol = variant, kind, tol
        self.sl_mode, self.sl_buf, self.exit_mode = sl_mode, sl_buf, exit_mode
        self.mt5_open, self.mt5_modify = mt5_open, mt5_modify
        self.name = f"{variant} {tf_min}m"
        self.side = None
        self.used = True
        self.positions = []       # multiple concurrent open positions allowed per variant
        self.prev = None
        self.bucket = None
        self.clean = False
        self.agg = None
        self.first_seen = None
        self._last_vwap = None
        try:
            with _db_lock:
                conn = _db(db_file)
                n = conn.execute("UPDATE trades SET outcome='ABORTED_RESTART', close_utc=? WHERE port=? AND tf=? "
                                 "AND COALESCE(variant,'V1')=? AND outcome='OPEN'",
                                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), port, tf_min, variant)).rowcount
                conn.commit(); conn.close()
            if n:
                self.log.warning(f"[{self.name}] {n} open paper trade(s) from previous run marked ABORTED_RESTART")
        except Exception as e:
            self.log.error(f"[{self.name}] db init error: {e}")

    # ---- candle close (called with the just-finalized 1-min bar) -----------------
    def on_bar(self, o, h, l, c, vwap, bar_start_utc, price_now, now_utc):
        """`vwap` is the LIVE chart VWAP for this bar (same value the dashboard's yellow
        VWAP line plots) - passed in by the caller, not computed separately here."""
        self._last_vwap = vwap
        if self.first_seen is None:
            self.first_seen = int(bar_start_utc.timestamp())
        if self.tf > 1:
            span = self.tf * 60
            start_ts = int(bar_start_utc.timestamp())
            b = start_ts // span
            if b != self.bucket:
                self.bucket = b
                self.clean = b * span >= self.first_seen      # bucket observed from its start
                self.agg = [o, h, l, c]
            elif self.agg is not None:
                self.agg[1] = max(self.agg[1], h)
                self.agg[2] = min(self.agg[2], l)
                self.agg[3] = c
            if (start_ts + 60) % span != 0 or self.agg is None:
                return                                        # bucket may still get bars (or is closed by on_price)
            agg, self.agg = self.agg, None
            if not self.clean:
                return
            o, h, l, c = agg
        self._evaluate(o, h, l, c, vwap, price_now, now_utc)

    def _evaluate(self, o, h, l, c, vwap, price_now, now_utc):
        if vwap is None or vwap <= 0:
            return
        prev, self.prev = self.prev, (o, h, l, c, vwap)
        s = "A" if c > vwap else ("B" if c < vwap else self.side)
        if self.side is None:
            self.side = s
            self.used = True          # unknown history -> don't fire on the first bar after (re)start
            return
        if s != self.side:
            self.side, self.used = s, False
        green, red = c > o, c < o
        direction = None
        if not self.used and ((self.side == "A" and green) or (self.side == "B" and red)):
            self.used = True          # first-close candle of this stretch (consumed even if skipped)
            if self.kind == "FIRST":
                direction = "BUY" if self.side == "A" else "SELL"
        elif self.kind == "RETEST" and prev is not None:
            po, ph, pl, pc, pv = prev
            if self.side == "A" and green and l <= vwap + self.tol and not (pl <= pv + self.tol):
                direction = "BUY"
            elif self.side == "B" and red and h >= vwap - self.tol and not (ph >= pv - self.tol):
                direction = "SELL"
        if direction is None:
            return
        if market_closed(now_utc):
            return
        entry = float(price_now)
        if direction == "BUY":
            sl = (vwap - self.sl_buf) if self.sl_mode == "VWAP" else (l - self.sl_buf)
            if entry <= sl:
                return
            risk = entry - sl
        else:
            sl = (vwap + self.sl_buf) if self.sl_mode == "VWAP" else (h + self.sl_buf)
            if entry >= sl:
                return
            risk = sl - entry
        self._open(direction, entry, sl, risk, vwap, now_utc)

    # ---- every price update ---------------------------------------------------------
    def on_price(self, price, now_utc):
        if self.tf > 1 and self.agg is not None and self.clean and \
                int(now_utc.timestamp() // (self.tf * 60)) > self.bucket:
            o, h, l, c = self.agg
            self.agg = None
            self._evaluate(o, h, l, c, self._last_vwap, price, now_utc)
        for p in list(self.positions):
            long_ = p["dir"] == "BUY"

            if self.exit_mode == "TRAIL":
                if long_:
                    p["best"] = max(p["best"], price)
                    if not p["armed"] and (p["best"] - p["entry"]) >= TRAIL_ARM:
                        p["armed"] = True
                    if p["armed"]:
                        p["sl"] = max(p["sl"], p["best"] - TRAIL_DIST)
                    hit = price <= p["sl"]
                else:
                    p["best"] = min(p["best"], price)
                    if not p["armed"] and (p["entry"] - p["best"]) >= TRAIL_ARM:
                        p["armed"] = True
                    if p["armed"]:
                        p["sl"] = min(p["sl"], p["best"] + TRAIL_DIST)
                    hit = price >= p["sl"]
                self._sync_mt5_sl(p)
                if hit:
                    self._close(p, p["sl"], "TRAIL" if p["armed"] else "LOSS", now_utc)
                continue

            # exit_mode == "TP"
            if not p["armed"] and (price >= p["be_price"] if long_ else price <= p["be_price"]):
                p["armed"] = True
                p["sl"] = p["entry"] + BE_BUFFER if long_ else p["entry"] - BE_BUFFER
                self._sync_mt5_sl(p)
            hit_sl = price <= p["sl"] if long_ else price >= p["sl"]
            hit_tp = price >= p["tp"] if long_ else price <= p["tp"]
            if hit_sl:
                self._close(p, p["sl"], "BE" if p["armed"] else "LOSS", now_utc)
            elif hit_tp:
                self._close(p, p["tp"], "WIN", now_utc)

    # ---- internals ------------------------------------------------------------------
    def _open(self, d, entry, sl, risk, vwap, now_utc):
        p = {"dir": d, "entry": entry, "sl": sl, "sl_initial": sl, "risk": risk,
             "armed": False, "open": now_utc, "vwap": vwap, "id": None, "best": entry,
             "mt5_ticket": None, "mt5_sl_sent": sl}
        if self.exit_mode == "TP":
            p["tp"] = entry + TP_MULT * risk if d == "BUY" else entry - TP_MULT * risk
            p["be_price"] = entry + BE_TRIGGER_R * risk if d == "BUY" else entry - BE_TRIGGER_R * risk
        else:
            p["tp"] = None
        self.positions.append(p)
        if self.mt5_open:
            try:
                comment = f"{MT5_COMMENT[self.variant]}-{self.port}"   # distinguish which port placed it (9060 vs 9080)
                ticket = self.mt5_open(d, sl, p["tp"] or 0.0, MT5_MAGIC[self.variant], comment)
                p["mt5_ticket"] = ticket
                if ticket:
                    self.log.info(f"[{self.name}] MT5 demo order placed, ticket=#{ticket}")
            except Exception:
                self.log.exception(f"[{self.name}] mt5_open error")
        try:
            with _db_lock:
                conn = _db(self.db_file)
                cur = conn.execute("INSERT INTO trades (port, tf, dir, open_utc, entry, sl_initial, tp, risk, outcome, vwap, variant) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (self.port, self.tf, d, now_utc.isoformat(timespec="seconds"), entry, sl,
                                    p["tp"], risk, "OPEN", vwap, self.variant))
                p["id"] = cur.lastrowid
                conn.commit(); conn.close()
        except Exception as e:
            self.log.error(f"[{self.name}] db insert error: {e}")
        tp_txt = f"TP={p['tp']:.2f}" if p["tp"] is not None else "TRAILING"
        self.log.info(f"[{self.name}] {d} @ {entry:.2f} SL={sl:.2f} (risk ${risk:.2f}) {tp_txt} VWAP={vwap:.2f} "
                       f"[{len(self.positions)} open]")

    def _sync_mt5_sl(self, p):
        if not self.mt5_modify or not p.get("mt5_ticket"):
            return
        if abs(p["sl"] - p.get("mt5_sl_sent", p["sl_initial"])) < MT5_SL_MODIFY_MIN_DELTA:
            return
        try:
            if self.mt5_modify(p["mt5_ticket"], p["sl"], p["tp"] or 0.0):
                p["mt5_sl_sent"] = p["sl"]
        except Exception:
            self.log.exception(f"[{self.name}] mt5_modify error")

    def _close(self, p, exit_p, outcome, now_utc):
        if p in self.positions:
            self.positions.remove(p)
        gross = (exit_p - p["entry"]) if p["dir"] == "BUY" else (p["entry"] - exit_p)
        net = gross - SPREAD
        hold = (now_utc - p["open"]).total_seconds() / 60.0
        try:
            with _db_lock:
                conn = _db(self.db_file)
                conn.execute("UPDATE trades SET close_utc=?, exit=?, outcome=?, gross=?, spread=?, net=?, hold_min=? WHERE id=?",
                             (now_utc.isoformat(timespec="seconds"), exit_p, outcome, gross, SPREAD, net, hold, p["id"]))
                conn.commit(); conn.close()
        except Exception as e:
            self.log.error(f"[{self.name}] db update error: {e}")
        self.log.info(f"[{self.name}] {outcome} {p['dir']} exit {exit_p:.2f} | gross ${gross:.2f} net ${net:.2f} | held {hold:.0f}m "
                       f"[{len(self.positions)} still open]")
        if self.cb:
            try:
                self.cb({"tf": self.tf, "dir": p["dir"], "entry": p["entry"], "sl": p["sl_initial"],
                         "tp": p["tp"] if p["tp"] is not None else exit_p,
                         "exit": exit_p, "outcome": outcome, "gross": gross, "net": net,
                         "open_utc": p["open"], "close_utc": now_utc, "variant": self.variant})
            except Exception as e:
                self.log.error(f"[{self.name}] callback error: {e}")


def make_variant(variant_id, port, tf, daily, logger, on_trade_closed=None, db_file=DB_FILE,
                  mt5_open=None, mt5_modify=None):
    cfg = dict(VARIANTS[variant_id])
    return VwapFirstClose(port, tf, daily, logger, on_trade_closed, db_file, variant=variant_id,
                           mt5_open=mt5_open, mt5_modify=mt5_modify, **cfg)
