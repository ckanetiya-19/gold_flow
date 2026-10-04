"""
Portfolio strategies: RSI-14 Reversion, Volume Imbalance Spike, ADX-Fade, ADX-Trend.
All independent ("Portfolio" mode - no confluence needed between them), each with its own
Trailing exit ($2 arm / $1 trail, SL = 1x ATR). Validated by backtest_iteration_round1.py,
backtest_adx_type1_vs_type2.py, backtest_combined_verify.py (FULL period, vs random-entry
baseline: combined portfolio-of-4 beat random in 26/30 seeds, NET $435 vs random mean $321,
MaxDD 11.1%. ADX-Trend alone was the strongest single leg: 30/30, MaxDD 8.3%).

Paper trading by default. Optionally places/modifies REAL demo-account MT5 orders when the
caller wires in mt5_open/mt5_modify callbacks (same pattern as vwap_first_close.py) - only
ONE port should do this to avoid duplicate live orders for the same signal.

Shares NOTHING with vwap_first_close.py's DB (separate file: portfolio_paper.db) so the two
systems' trade logs never mix.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

import numpy as np

SPREAD = 0.24
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
SL_ATR_MULT = 1.0
ATR_PERIOD = 14
RSI_PERIOD = 14
ADX_PERIOD = 14
VOL_AVG_WINDOW = 20
COOLDOWN_BARS = 5           # matches backtest's cooldown_until = i+5 for ADX strategies

MAGIC = {"RSI14": 90820, "VOLIMB": 90821, "ADXFADE": 90822, "ADXTREND": 90823}
COMMENT = {k: f"GF-P-{k}" for k in MAGIC}
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_paper.db")
_db_lock = threading.Lock()


def market_closed(dt_utc):
    wd, hr = dt_utc.weekday(), dt_utc.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


def _db(db_file):
    conn = sqlite3.connect(db_file, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, port TEXT, strategy TEXT, dir TEXT,
        open_utc TEXT, close_utc TEXT, entry REAL, sl_initial REAL, risk REAL,
        exit REAL, outcome TEXT, gross REAL, spread REAL, net REAL, hold_min REAL)""")
    return conn


# ------------------------------------------------------------------ indicators
def calc_atr(bars, period=ATR_PERIOD):
    if len(bars) < period + 1:
        return 2.0
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_rsi(bars, period=RSI_PERIOD):
    if len(bars) < period + 2:
        return None, None
    closes = np.array([b["close"] for b in bars[-(period + 2):]])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    def rsi_of(g, l):
        ag, al = np.mean(g), np.mean(l)
        return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

    rsi_now = rsi_of(gains[-period:], losses[-period:])
    rsi_prev = rsi_of(gains[-period - 1:-1], losses[-period - 1:-1])
    return rsi_now, rsi_prev


def calc_adx_vwap(bars, period=ADX_PERIOD):
    """ADX (Wilder) + cumulative VWAP, computed fresh over the available bars window
    (bounded to ~200-300 live bars - not a true infinite-history cumulative like the
    backtest had, but converges within a few x period; a documented approximation)."""
    n = len(bars)
    if n < period * 2 + 2:
        return None, None
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    def smoothed(series):
        out = [0.0] * n
        out[period] = sum(series[1:period + 1])
        for i in range(period + 1, n):
            out[i] = out[i - 1] - out[i - 1] / period + series[i]
        return out

    atr_sm, pdm_sm, mdm_sm = smoothed(tr), smoothed(plus_dm), smoothed(minus_dm)
    dx_hist = []
    for i in range(period, n):
        if atr_sm[i] <= 0:
            continue
        pdi = 100 * pdm_sm[i] / atr_sm[i]
        mdi = 100 * mdm_sm[i] / atr_sm[i]
        dx_hist.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)
    if len(dx_hist) < period:
        return None, None
    adx = sum(dx_hist[-period:]) / period

    cum_pv = sum(((b["high"] + b["low"] + b["close"]) / 3.0) * b["volume"] for b in bars)
    cum_vol = sum(b["volume"] for b in bars)
    vwap = cum_pv / cum_vol if cum_vol > 0 else closes[-1]
    return adx, vwap


# ------------------------------------------------------------------ signal checks (on the LATEST bar)
def check_rsi14(bars):
    rsi_now, rsi_prev = calc_rsi(bars)
    if rsi_now is None:
        return None
    if rsi_prev < 30 <= rsi_now:
        return "BUY"
    if rsi_prev > 70 >= rsi_now:
        return "SELL"
    return None


def check_volume_imbalance(bars, avg_window=VOL_AVG_WINDOW):
    if len(bars) < avg_window + 1:
        return None
    window = bars[-avg_window - 1:-1]
    avg_vol = sum(b["volume"] for b in window) / len(window)
    b = bars[-1]
    rng = b["high"] - b["low"]
    body_ratio = (abs(b["close"] - b["open"]) / rng) if rng > 0 else 0.0
    if avg_vol > 0 and b["volume"] > avg_vol * 2.5 and body_ratio >= 0.65:
        return "BUY" if b["close"] > b["open"] else "SELL"
    return None


def check_adx_fade(bars, adx_low=20.0, dev_atr_mult=1.5):
    adx, vwap = calc_adx_vwap(bars)
    if adx is None:
        return None
    atr = calc_atr(bars)
    b = bars[-1]
    dev = b["close"] - vwap
    if 0 < adx < adx_low and atr > 0 and abs(dev) > dev_atr_mult * atr:
        return "SELL" if dev > 0 else "BUY"
    return None


def check_adx_trend(bars, adx_high=25.0):
    adx, vwap = calc_adx_vwap(bars)
    if adx is None or adx < adx_high:
        return None
    b = bars[-1]
    rng = b["high"] - b["low"]
    body_ratio = (abs(b["close"] - b["open"]) / rng) if rng > 0 else 0.0
    buy_ratio = (b.get("buy_vol", b.get("volume", 0) / 2.0) / b["volume"]) if b.get("volume", 0) > 0 else 0.5
    if b["close"] > vwap and b["close"] > b["open"] and body_ratio >= 0.55 and buy_ratio >= 0.58:
        return "BUY"
    if b["close"] < vwap and b["close"] < b["open"] and body_ratio >= 0.55 and buy_ratio <= 0.42:
        return "SELL"
    return None


# ADXFADE (Type 2) dropped - showed negative edge vs random baseline (13/30, -$7.88) in the
# windowed (live-matching) verification. Only the 3 proven components go live.
CHECKS = {"RSI14": check_rsi14, "VOLIMB": check_volume_imbalance, "ADXTREND": check_adx_trend}


# ------------------------------------------------------------------ live engine
class PortfolioEngine:
    def __init__(self, port, logger, on_trade_closed=None, db_file=DB_FILE, mt5_open=None, mt5_modify=None):
        self.port, self.log, self.cb, self.db_file = port, logger, on_trade_closed, db_file
        self.mt5_open, self.mt5_modify = mt5_open, mt5_modify
        self.positions = []                       # list of open trade dicts (any # concurrently)
        self.cooldown_until = {k: -1 for k in CHECKS}
        self._bar_count = 0
        try:
            with _db_lock:
                conn = _db(db_file)
                n = conn.execute("UPDATE trades SET outcome='ABORTED_RESTART', close_utc=? WHERE port=? AND outcome='OPEN'",
                                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), port)).rowcount
                conn.commit(); conn.close()
            if n:
                self.log.warning(f"[Portfolio] {n} open paper trade(s) from previous run marked ABORTED_RESTART")
        except Exception as e:
            self.log.error(f"[Portfolio] db init error: {e}")

    def on_bar(self, bars, price_now, now_utc):
        """bars = historical_bars (list, oldest->newest, already includes the just-closed bar)."""
        self._bar_count += 1
        if market_closed(now_utc):
            return
        for name, check_fn in CHECKS.items():
            if self._bar_count <= self.cooldown_until[name]:
                continue
            try:
                direction = check_fn(bars)
            except Exception:
                self.log.exception(f"[Portfolio:{name}] signal check error")
                continue
            if direction is None:
                continue
            self.cooldown_until[name] = self._bar_count + COOLDOWN_BARS
            self._open(name, direction, price_now, bars, now_utc)

    def on_price(self, price, now_utc):
        for p in list(self.positions):
            long_ = p["dir"] == "BUY"
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

    # -------------------------------------------------------------- internals
    def _open(self, name, d, price_now, bars, now_utc):
        atr = calc_atr(bars)
        if atr <= 0:
            return
        entry = float(price_now)
        sl = entry - SL_ATR_MULT * atr if d == "BUY" else entry + SL_ATR_MULT * atr
        p = {"dir": d, "entry": entry, "sl": sl, "sl_initial": sl, "risk": abs(entry - sl),
             "armed": False, "open": now_utc, "id": None, "best": entry,
             "mt5_ticket": None, "mt5_sl_sent": sl, "strategy": name}
        self.positions.append(p)
        if self.mt5_open:
            try:
                ticket = self.mt5_open(d, sl, 0.0, MAGIC[name], f"{COMMENT[name]}-{self.port}")
                p["mt5_ticket"] = ticket
                if ticket:
                    self.log.info(f"[Portfolio:{name}] MT5 demo order placed, ticket=#{ticket}")
            except Exception:
                self.log.exception(f"[Portfolio:{name}] mt5_open error")
        try:
            with _db_lock:
                conn = _db(self.db_file)
                cur = conn.execute("INSERT INTO trades (port, strategy, dir, open_utc, entry, sl_initial, risk, outcome) "
                                   "VALUES (?,?,?,?,?,?,?,?)",
                                   (self.port, name, d, now_utc.isoformat(timespec="seconds"), entry, sl, p["risk"], "OPEN"))
                p["id"] = cur.lastrowid
                conn.commit(); conn.close()
        except Exception as e:
            self.log.error(f"[Portfolio:{name}] db insert error: {e}")
        self.log.info(f"[Portfolio:{name}] {d} @ {entry:.2f} SL={sl:.2f} (risk ${p['risk']:.2f}) TRAILING "
                       f"[{len(self.positions)} open]")

    def _sync_mt5_sl(self, p):
        if not self.mt5_modify or not p.get("mt5_ticket"):
            return
        if abs(p["sl"] - p.get("mt5_sl_sent", p["sl_initial"])) < 0.05:
            return
        try:
            if self.mt5_modify(p["mt5_ticket"], p["sl"], 0.0):
                p["mt5_sl_sent"] = p["sl"]
        except Exception:
            self.log.exception(f"[Portfolio:{p['strategy']}] mt5_modify error")

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
            self.log.error(f"[Portfolio:{p['strategy']}] db update error: {e}")
        self.log.info(f"[Portfolio:{p['strategy']}] {outcome} {p['dir']} exit {exit_p:.2f} | gross ${gross:.2f} "
                       f"net ${net:.2f} | held {hold:.0f}m [{len(self.positions)} still open]")
        if self.cb:
            try:
                self.cb({"strategy": p["strategy"], "dir": p["dir"], "entry": p["entry"], "sl": p["sl_initial"],
                         "exit": exit_p, "outcome": outcome, "gross": gross, "net": net,
                         "open_utc": p["open"], "close_utc": now_utc})
            except Exception as e:
                self.log.error(f"[Portfolio:{p['strategy']}] callback error: {e}")
