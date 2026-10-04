"""
vwap_full_crossover.py - shared strategy module (same pattern as
vwap_first_close.py / portfolio_strategies.py).

RULE - confirmed with the user from two annotated chart screenshots
(100.png / 101.png) before writing this, then backtested in
backtest_vwap_full_crossover.py:
  - Track the SAME rolling chart VWAP already shown on the dashboard
    (TP-weighted, rolling ~100-bar window - the "_rolling_chart_vwap" each
    port already computes for its own chart, passed in here unchanged).
  - A bar is a valid BUY signal if its ENTIRE range (low AND close) clears
    the live VWAP value on the upside AND it closed green, AND the
    previous bar was NOT already fully above VWAP ("first crossover" - a
    bar whose wick pokes back below VWAP even though its close/body is
    above does NOT count and does not consume the signal - confirmed via
    the white-circled counter-example in the screenshots).
  - SELL is the exact mirror.
  - Entry: instant market order the moment the signal bar finalizes
    (equivalent to "the very next bar", since this fires right at the bar
    boundary).
  - SL: the VWAP price AT THE SIGNAL BAR, minus $1 (BUY) / plus $1 (SELL).
  - TP: 2x the resulting risk (2R cap).
  - Exit: SL trails once armed ($2 arm / $1 trail, same convention as every
    other live strategy here), OR the 2R TP is hit first - whichever comes
    first.

HONESTY NOTE (told to the user before deploying): the full-period backtest
on 13.48 real days showed this rule, exactly as specified, net NEGATIVE
overall (-$0.056/trade) - BUY side alone was negative (-$0.075/trade),
SELL side alone was positive (+$0.128/trade). The user explicitly asked to
deploy it exactly as specified anyway, to observe live rather than
backtest/tune further. This is, unlike every other strategy in this
project, going live WITHOUT having cleared the random-baseline bar - flagged
here in the source, not hidden.

MT5 execution defaults OFF regardless (mt5_open/mt5_modify passed as None
unless the caller explicitly wires them up and its own master switch is on)
- paper tracking/logging always runs independently of that.
"""
import sqlite3
from datetime import datetime, timezone

SL_BUFFER = 1.0
TP_R_MULT = 2.0
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
MAGIC = 90830
DB_FILE = "vwap_full_crossover_paper.db"


def _init_db(db_file):
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, port TEXT, side TEXT,
        entry REAL, sl REAL, tp REAL, exit_price REAL, outcome TEXT,
        pnl REAL, opened_at TEXT, closed_at TEXT
    )""")
    conn.commit()
    return conn


def _bar_state(bar, vwap):
    if bar["low"] > vwap:
        return "ABOVE"
    if bar["high"] < vwap:
        return "BELOW"
    return "STRADDLE"


class VwapFullCrossover:
    def __init__(self, port, logger, db_file=DB_FILE, mt5_open=None, mt5_modify=None, magic=MAGIC):
        self.port = port
        self.logger = logger
        self.db = _init_db(db_file)
        self.mt5_open = mt5_open
        self.mt5_modify = mt5_modify
        self.magic = magic          # per-port MT5 magic so each port's orders are distinct
        self.prev_state = None
        self.positions = []  # normally 0 or 1 - kept as a list for consistency
        # with this project's other multi-position-capable engines.

    def on_bar(self, bar, vwap, price_now, now_utc):
        """Call once per finalized bar with the SAME rolling chart VWAP the
        dashboard itself displays for that bar."""
        if vwap is None:
            return
        state = _bar_state(bar, vwap)
        green = bar["close"] > bar["open"]
        red = bar["close"] < bar["open"]

        # WARM-UP: on the first bar after (re)start prev_state is None, so an
        # already-established above/below position would look like a brand-new
        # crossover and fire an entry the instant the port connects (all ports at
        # once). Just record the state on that first bar - a real signal needs an
        # actual transition on a LATER bar.
        if self.prev_state is None:
            self.prev_state = state
            return

        signal = None
        if state == "ABOVE" and green and self.prev_state != "ABOVE":
            signal = ("BUY", round(vwap - SL_BUFFER, 2))
        elif state == "BELOW" and red and self.prev_state != "BELOW":
            signal = ("SELL", round(vwap + SL_BUFFER, 2))
        self.prev_state = state

        if signal and not self.positions:
            self._open(signal[0], signal[1], price_now, now_utc)

    def _open(self, side, sl, price_now, now_utc):
        entry = price_now
        if side == "BUY" and entry <= sl:
            return
        if side == "SELL" and entry >= sl:
            return
        risk = abs(entry - sl)
        tp = round(entry + TP_R_MULT * risk, 2) if side == "BUY" else round(entry - TP_R_MULT * risk, 2)
        pos = {"side": side, "entry": entry, "sl": sl, "tp": tp, "best": entry, "armed": False,
               "opened_at": now_utc.isoformat(), "ticket": None}
        if self.mt5_open:
            pos["ticket"] = self.mt5_open(side, sl, tp, self.magic, f"GF-VWAPX-{self.port}")
        self.positions.append(pos)
        self.logger.info(f"[VWAPCross-{self.port}] {side} @ {entry:.2f} SL={sl:.2f} TP={tp:.2f} "
                          f"(risk ${risk:.2f}) [{len(self.positions)} open]")

    def on_price(self, price_now, now_utc):
        for pos in list(self.positions):
            side = pos["side"]
            hit_tp = (price_now >= pos["tp"]) if side == "BUY" else (price_now <= pos["tp"])
            if hit_tp:
                self._close(pos, pos["tp"], "TP", now_utc)
                continue
            if side == "BUY":
                pos["best"] = max(pos["best"], price_now)
                if not pos["armed"] and (pos["best"] - pos["entry"]) >= TRAIL_ARM:
                    pos["armed"] = True
                if pos["armed"]:
                    new_sl = round(pos["best"] - TRAIL_DIST, 2)
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
                        if self.mt5_modify and pos["ticket"]:
                            self.mt5_modify(pos["ticket"], pos["sl"], pos["tp"])
                if price_now <= pos["sl"]:
                    self._close(pos, pos["sl"], "TRAIL" if pos["armed"] else "SL", now_utc)
            else:
                pos["best"] = min(pos["best"], price_now)
                if not pos["armed"] and (pos["entry"] - pos["best"]) >= TRAIL_ARM:
                    pos["armed"] = True
                if pos["armed"]:
                    new_sl = round(pos["best"] + TRAIL_DIST, 2)
                    if new_sl < pos["sl"]:
                        pos["sl"] = new_sl
                        if self.mt5_modify and pos["ticket"]:
                            self.mt5_modify(pos["ticket"], pos["sl"], pos["tp"])
                if price_now >= pos["sl"]:
                    self._close(pos, pos["sl"], "TRAIL" if pos["armed"] else "SL", now_utc)

    def _close(self, pos, exit_price, outcome, now_utc):
        pnl = (exit_price - pos["entry"]) if pos["side"] == "BUY" else (pos["entry"] - exit_price)
        try:
            self.db.execute(
                "INSERT INTO trades (port,side,entry,sl,tp,exit_price,outcome,pnl,opened_at,closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.port, pos["side"], pos["entry"], pos["sl"], pos["tp"], exit_price, outcome, pnl,
                 pos["opened_at"], now_utc.isoformat()))
            self.db.commit()
        except Exception:
            self.logger.exception("VwapFullCrossover DB write failed")
        self.logger.info(f"[VWAPCross-{self.port}] {outcome} {pos['side']} exit {exit_price:.2f} | "
                          f"pnl ${pnl:.2f} [{len(self.positions)-1} still open]")
        self.positions.remove(pos)
