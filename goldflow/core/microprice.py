"""
Micro-price -- Stoikov's high-frequency estimator of future prices.

    I     = Qb / (Qb + Qa)                  inside-book imbalance
    W     = I*Pa + (1-I)*Pb                 weighted mid (naive)
    Pmicro = M + g(I, S)                    mid plus an adjustment in (I, S)

The micro-price is the limit of expected future mid-prices given the current
imbalance and spread. It beats the weighted mid, whose problem is heavy
autocorrelation -- W is not a martingale and mean-reverts against you.

The estimator here is Stoikov's Markov-chain construction:

  1. Discretise the state as (imbalance bucket, spread in ticks).
  2. Split one-step transitions into those where the mid did NOT move (T) and
     those where it did (Q), and record the expected mid move on the latter (R).
  3. G1     = (I - T)^-1 R            expected move up to the next mid change
     B      = (I - T)^-1 Q
     G_k    = G1 + B G_{k-1}          add the k-th subsequent mid change
     G_inf  = lim_k G_k               the micro-price adjustment

Practical uses, in order of how much they are worth:
  * entry/exit timing -- do not cross the spread when the micro-price says the
    mid is about to come to you;
  * a fair-value reference for the spot-vs-futures comparison, so "spot is
    rich" is measured against something better than a mid;
  * a feature. It decays in seconds; it is not a swing signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..feeds.base import Quote


def imbalance(q: Quote) -> float:
    tot = q.bid_sz + q.ask_sz
    return 0.5 if tot <= 0 else q.bid_sz / tot


def weighted_mid(q: Quote) -> float:
    """W = I*Pa + (1-I)*Pb. Note the crossing: heavy bid pulls toward the ask."""
    i = imbalance(q)
    return i * q.ask_px + (1.0 - i) * q.bid_px


@dataclass
class MicroPriceModel:
    """Fitted Stoikov adjustment table g(imbalance_bucket, spread)."""

    tick: float
    n_imb: int
    spreads: np.ndarray            # spread values in ticks, ascending
    g: np.ndarray                  # shape (n_states,), state = imb*len(spreads)+s
    imb_edges: np.ndarray
    n_iter: int
    converged: bool

    def _state(self, imb: float, spread_ticks: int) -> int | None:
        s_idx = np.searchsorted(self.spreads, spread_ticks)
        if s_idx >= len(self.spreads) or self.spreads[s_idx] != spread_ticks:
            return None
        i_idx = int(np.clip(np.digitize(imb, self.imb_edges) - 1,
                            0, self.n_imb - 1))
        return i_idx * len(self.spreads) + s_idx

    def adjustment(self, q: Quote) -> float:
        """g(I, S) in price units. 0.0 when the state was never observed."""
        st = self._state(imbalance(q), int(round(q.spread / self.tick)))
        return 0.0 if st is None else float(self.g[st])

    def micro_price(self, q: Quote) -> float:
        return q.mid + self.adjustment(q)


def fit_microprice(quotes: Sequence[Quote], tick: float, n_imb: int = 10,
                   dt: int = 1, n_spread: int = 2, max_iter: int = 6,
                   tol: float = 1e-6) -> MicroPriceModel:
    """Estimate the micro-price adjustment table from a quote sequence.

    Parameters
    ----------
    dt : events to look ahead for the "next" state. 1 is the literal one-step
         chain; larger values smooth at the cost of resolution.
    n_spread : how many of the tightest spread values to model. Gold futures
         sit at 1 tick most of the time; 2 covers the widening.

    Needs a lot of data. Below ~50k quotes the transition matrices are too
    sparse and the inverse is unstable; the function will tell you rather than
    return a confident-looking table built on nothing.

    On `converged`: the G_k series is NOT guaranteed to converge -- B has
    spectral radius near 1 by construction, so successive terms shrink slowly.
    Stoikov uses a fixed small number of iterations (6 here) rather than
    iterating to a tolerance, and `converged=False` is the normal outcome, not
    a failure. Judge the fit by `evaluate_predictors`, which measures whether
    the table actually forecasts the future mid better than the mid does --
    that is the only test that matters.
    """
    n = len(quotes)
    if n < 5_000:
        raise ValueError(f"need >=5000 quotes to fit, got {n}")

    mid = np.array([q.mid for q in quotes])
    imb = np.array([imbalance(q) for q in quotes])
    spr = np.rint(np.array([q.spread for q in quotes]) / tick).astype(int)

    spreads = np.arange(1, n_spread + 1)
    keep = np.isin(spr, spreads)

    # forward state
    fwd = np.arange(n) + dt
    valid = keep & (fwd < n)
    valid &= keep[np.clip(fwd, 0, n - 1)]

    idx = np.flatnonzero(valid)
    if idx.size < 2_000:
        raise ValueError("too few usable (spread in range) observations")

    nxt = idx + dt
    # round the mid change to a half tick; larger jumps are clipped, since
    # multi-tick jumps are rare and blow up the R vector if left unbounded
    dM = np.rint((mid[nxt] - mid[idx]) / (tick / 2.0)) * (tick / 2.0)
    dM = np.clip(dM, -2 * tick, 2 * tick)

    imb_edges = np.linspace(0.0, 1.0, n_imb + 1)
    imb_edges[-1] += 1e-9
    i_bucket = np.clip(np.digitize(imb[idx], imb_edges) - 1, 0, n_imb - 1)
    i_bucket_n = np.clip(np.digitize(imb[nxt], imb_edges) - 1, 0, n_imb - 1)
    s_bucket = np.searchsorted(spreads, spr[idx])
    s_bucket_n = np.searchsorted(spreads, spr[nxt])

    K = n_imb * len(spreads)
    state = i_bucket * len(spreads) + s_bucket
    state_n = i_bucket_n * len(spreads) + s_bucket_n

    counts = np.bincount(state, minlength=K).astype(float)
    counts[counts == 0] = 1.0

    no_move = dM == 0.0
    move = ~no_move

    # T: state -> next state, conditional on no mid change (unnormalised by
    # total, so rows sum to P(no move and land in j) -- this is what makes
    # (I - T) invertible)
    T = np.zeros((K, K))
    np.add.at(T, (state[no_move], state_n[no_move]), 1.0)
    T /= counts[:, None]

    # Q: state -> next state given a mid change
    Q = np.zeros((K, K))
    np.add.at(Q, (state[move], state_n[move]), 1.0)
    Q /= counts[:, None]

    # R: E[dM * 1{move} | state]
    R = np.zeros(K)
    np.add.at(R, state[move], dM[move])
    R /= counts

    I = np.eye(K)
    try:
        inv = np.linalg.inv(I - T)
    except np.linalg.LinAlgError:
        inv = np.linalg.pinv(I - T)

    G1 = inv @ R
    B = inv @ Q

    G = G1.copy()
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        G_new = G1 + B @ G
        if np.max(np.abs(G_new - G)) < tol:
            G = G_new
            converged = True
            break
        G = G_new

    return MicroPriceModel(tick=tick, n_imb=n_imb, spreads=spreads, g=G,
                           imb_edges=imb_edges, n_iter=it, converged=converged)


def evaluate_predictors(quotes: Sequence[Quote], model: MicroPriceModel,
                        horizon: int = 50) -> dict:
    """Compare mid, weighted mid and micro-price as forecasts of the future mid.

    Lower mean squared error is better. If the micro-price does not beat the
    plain mid on your data, do not use it -- and check the fit diagnostics
    before concluding anything about the market.
    """
    n = len(quotes)
    if n <= horizon + 10:
        raise ValueError("not enough quotes for the requested horizon")

    mid = np.array([q.mid for q in quotes])
    wmid = np.array([weighted_mid(q) for q in quotes])
    mp = np.array([model.micro_price(q) for q in quotes])
    target = mid[horizon:]

    def mse(pred):
        return float(np.mean((pred[:-horizon] - target) ** 2))

    m_mid, m_w, m_mp = mse(mid), mse(wmid), mse(mp)
    return {
        "mse_mid": m_mid,
        "mse_weighted_mid": m_w,
        "mse_microprice": m_mp,
        "improvement_vs_mid_pct": 100.0 * (m_mid - m_mp) / m_mid if m_mid else 0.0,
        "improvement_vs_wmid_pct": 100.0 * (m_w - m_mp) / m_w if m_w else 0.0,
        "horizon_events": horizon,
        "converged": model.converged,
        "iterations": model.n_iter,
    }
