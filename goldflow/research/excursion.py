"""
MAE / MFE excursion analysis -- how to set TP and SL from data.

This is the tool that answers "where do I put the stop?" without guessing.

  MAE  Maximum Adverse Excursion: the worst the trade went against you before
       the horizon ended.
  MFE  Maximum Favourable Excursion: the best it went for you.

Setting TP and SL by any other method is a decision made without evidence:

  * A fixed R:R (1:2, 1:3) asserts a relationship between the two
    distributions rather than measuring it. On most real signals the MFE and
    MAE distributions are not related by a round number, and forcing one
    throws away trades that were working.
  * A round number in dollars ignores volatility regime entirely -- the same
    $3 stop is loose at 02:00 and tight at 08:30.
  * "Below the last swing low" is a price-structure rule, which is a different
    claim from "this signal's losers rarely go more than X against me".

The correct procedure, and what this module implements:

  1. Take every signal your rule would have fired.
  2. Record the full MAE/MFE path over your intended holding horizon.
  3. Put the STOP beyond the MAE that most WINNERS survive -- a stop tighter
     than that converts winners into losers, which is the most expensive
     mistake available here.
  4. Put the TARGET where the MFE distribution flattens -- beyond that point
     you are holding for moves that mostly do not arrive.
  5. Subtract costs and check the expectancy STILL clears. A TP/SL pair that
     is profitable gross and negative net is the normal outcome, not an
     unlucky one.

Everything here is measured on the SPOT leg -- the venue you actually trade --
because MAE on GC is not the MAE your stop will experience.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..feeds.base import NS_PER_S, Quote


@dataclass(slots=True)
class Excursion:
    """One signal's realised path."""

    ts_signal: int
    side: int                # +1 long, -1 short
    entry_px: float
    mae: float               # >= 0, in price units, adverse
    mfe: float               # >= 0, in price units, favourable
    time_to_mfe_s: float
    final_move: float        # signed, at the horizon
    n_quotes: int


@dataclass
class BracketRecommendation:
    horizon_s: float
    n_signals: int
    sl: float
    tp: float
    sl_basis: str
    tp_basis: str
    winners_surviving_sl: float
    tp_hit_rate: float
    expectancy_gross: float
    round_trip_cost: float
    expectancy_net: float
    mae_quantiles: dict
    mfe_quantiles: dict
    verdict: str

    def __str__(self) -> str:
        return (
            f"Bracket recommendation  horizon={self.horizon_s:g}s  "
            f"n={self.n_signals}\n"
            f"  SL {self.sl:.3f}   ({self.sl_basis})\n"
            f"  TP {self.tp:.3f}   ({self.tp_basis})\n"
            f"  winners surviving the SL   {self.winners_surviving_sl:.1%}\n"
            f"  TP hit rate                {self.tp_hit_rate:.1%}\n"
            f"  expectancy gross {self.expectancy_gross:+.4f}   "
            f"cost {self.round_trip_cost:.4f}   "
            f"NET {self.expectancy_net:+.4f}\n"
            f"  MAE q50/q75/q90  {self.mae_quantiles['q50']:.3f} / "
            f"{self.mae_quantiles['q75']:.3f} / {self.mae_quantiles['q90']:.3f}\n"
            f"  MFE q50/q75/q90  {self.mfe_quantiles['q50']:.3f} / "
            f"{self.mfe_quantiles['q75']:.3f} / {self.mfe_quantiles['q90']:.3f}\n"
            f"  -> {self.verdict}"
        )


def measure_excursions(signals: Sequence[tuple[int, int]],
                       spot: Sequence[Quote],
                       horizon_s: float = 60.0) -> list[Excursion]:
    """Walk every signal forward through the real spot quotes.

    `signals` is a sequence of (ts_ns, side). Entry is at the tradeable price
    on your side of the book -- ask for a long, bid for a short -- because that
    is where you would actually get in, and measuring MAE from the mid
    understates the stop distance by half a spread on every trade.

    MAE/MFE are then measured against the EXIT side (bid for a long), for the
    same reason: your stop is hit on the bid, not the mid.
    """
    if not signals or not spot:
        return []

    ts = np.fromiter((q.ts_recv for q in spot), dtype="int64", count=len(spot))
    bid = np.fromiter((q.bid_px for q in spot), dtype=float, count=len(spot))
    ask = np.fromiter((q.ask_px for q in spot), dtype=float, count=len(spot))

    h = int(horizon_s * NS_PER_S)
    out: list[Excursion] = []

    for ts_sig, side in signals:
        i0 = int(np.searchsorted(ts, ts_sig, side="right"))
        if i0 >= len(spot):
            continue
        i1 = int(np.searchsorted(ts, ts_sig + h, side="right"))
        if i1 <= i0 + 1:
            continue

        entry = ask[i0] if side > 0 else bid[i0]
        exit_path = bid[i0:i1] if side > 0 else ask[i0:i1]
        move = (exit_path - entry) * side

        mfe_i = int(np.argmax(move))
        out.append(Excursion(
            ts_signal=ts_sig,
            side=side,
            entry_px=float(entry),
            mae=float(max(-move.min(), 0.0)),
            mfe=float(max(move.max(), 0.0)),
            time_to_mfe_s=float((ts[i0 + mfe_i] - ts[i0]) / NS_PER_S),
            final_move=float(move[-1]),
            n_quotes=int(i1 - i0),
        ))
    return out


def recommend_bracket(exc: Sequence[Excursion],
                      round_trip_cost: float,
                      winner_survival: float = 0.85,
                      tp_quantile: float = 0.65,
                      min_signals: int = 100) -> BracketRecommendation:
    """Derive TP and SL from the measured distributions.

    `winner_survival`: the fraction of eventual winners whose MAE the stop must
    sit beyond. 0.85 means "do not stop out 85% of the trades that would have
    worked". Tightening this is the fastest way to destroy a real edge, and it
    is what most people do when a strategy feels uncomfortable.

    `tp_quantile`: where on the MFE distribution to take profit. Higher is
    greedier and hits less often; the product of hit rate and size is what
    matters, and the function reports both so the trade-off is visible.

    Two things in the output that are NOT findings, and must not be read as
    such:

      * `tp_hit_rate` is mechanically 1 - tp_quantile on the sample the TP was
        fitted to. Seeing 35% for tp_quantile=0.65 confirms the arithmetic, not
        the strategy. It only becomes evidence out of sample.
      * `sl_basis` will say the stop was raised to the cost floor whenever the
        winners' MAE sits below 1.5x the round trip. When that happens the stop
        is set by your spread, not by the signal -- which is itself the finding:
        the trade is too small relative to its costs, and the fix is a longer
        horizon, not a cleverer stop.
    """
    if len(exc) < min_signals:
        raise ValueError(
            f"need >={min_signals} signals to fit a bracket, got {len(exc)}. "
            "A bracket fitted on fewer is fitted to noise."
        )

    mae = np.array([e.mae for e in exc])
    mfe = np.array([e.mfe for e in exc])
    final = np.array([e.final_move for e in exc])

    winners = final > 0
    # Stop beyond the MAE that most winners survived. If there are too few
    # winners to form a distribution, fall back to the overall MAE -- and say
    # so, rather than silently using a different basis.
    if winners.sum() >= 30:
        sl = float(np.quantile(mae[winners], winner_survival))
        sl_basis = (f"q{winner_survival:.0%} of MAE among the {int(winners.sum())} "
                    f"eventual winners")
    else:
        sl = float(np.quantile(mae, winner_survival))
        sl_basis = (f"q{winner_survival:.0%} of ALL MAE -- only "
                    f"{int(winners.sum())} winners, too few to isolate")

    tp = float(np.quantile(mfe, tp_quantile))
    tp_basis = f"q{tp_quantile:.0%} of the MFE distribution"

    # Never let the stop sit inside the cost of the round trip: it would be
    # hit by the spread itself before the market moved at all.
    floor = round_trip_cost * 1.5
    if sl < floor:
        sl = floor
        sl_basis += f" (raised to 1.5x round-trip cost = {floor:.3f})"

    # Simulate the bracket over the measured paths. Approximate: if MAE >= SL
    # the stop is assumed hit first when MAE occurred before MFE is unknown --
    # so this is the PESSIMISTIC assignment, stop wins ties.
    pnl = np.empty(len(exc))
    tp_hits = 0
    for i, e in enumerate(exc):
        hit_sl = e.mae >= sl
        hit_tp = e.mfe >= tp
        if hit_sl and hit_tp:
            pnl[i] = -sl          # pessimistic: assume the stop got there first
        elif hit_tp:
            pnl[i] = tp
            tp_hits += 1
        elif hit_sl:
            pnl[i] = -sl
        else:
            pnl[i] = e.final_move
    if not (tp <= 0):
        tp_hits += 0

    gross = float(pnl.mean())
    net = gross - round_trip_cost
    surv = float((mae[winners] < sl).mean()) if winners.sum() else 0.0

    if net > 0 and len(exc) >= min_signals:
        verdict = ("Positive net expectancy on the measured paths. Validate "
                   "out of sample before sizing up.")
    elif gross > 0:
        verdict = ("Gross positive, NET NEGATIVE -- the bracket works and the "
                   "costs eat it. Widening TP or lengthening the horizon are "
                   "the only real levers; tightening the SL will not help.")
    else:
        verdict = ("Negative even before costs. The signal, not the bracket, "
                   "is the problem. Do not search the TP/SL grid -- that is "
                   "how a noise fit gets found.")

    q = lambda a, p: float(np.quantile(a, p))
    return BracketRecommendation(
        horizon_s=0.0,
        n_signals=len(exc),
        sl=sl, tp=tp, sl_basis=sl_basis, tp_basis=tp_basis,
        winners_surviving_sl=surv,
        tp_hit_rate=float((mfe >= tp).mean()),
        expectancy_gross=gross,
        round_trip_cost=round_trip_cost,
        expectancy_net=net,
        mae_quantiles={"q50": q(mae, .5), "q75": q(mae, .75), "q90": q(mae, .9)},
        mfe_quantiles={"q50": q(mfe, .5), "q75": q(mfe, .75), "q90": q(mfe, .9)},
        verdict=verdict,
    )


def sweep_horizons(signals: Sequence[tuple[int, int]], spot: Sequence[Quote],
                   horizons_s: Sequence[float], round_trip_cost: float
                   ) -> list[BracketRecommendation]:
    """Fit a bracket at each horizon. Pick on NET expectancy, not gross.

    Expect net expectancy to be hump-shaped: too short and costs dominate, too
    long and the signal has decayed while the variance has grown.
    """
    out = []
    for h in horizons_s:
        exc = measure_excursions(signals, spot, horizon_s=h)
        try:
            rec = recommend_bracket(exc, round_trip_cost)
        except ValueError:
            continue
        rec.horizon_s = h
        out.append(rec)
    return out
