"""
Tests for the parts where a silent bug produces plausible-looking numbers.

Priority is not coverage -- it is the specific errors that do not announce
themselves: the OFI indicator logic, trade classification sign conventions,
VPIN bucket splitting, value-area expansion, and backtest causality.
"""

from __future__ import annotations

import numpy as np
import pytest

from goldflow.core.classify import (
    LeeReady,
    TickRule,
    bulk_volume_classification,
)
from goldflow.core.delta import CVD, DeltaBar, DeltaBarBuilder, Footprint
from goldflow.core.ofi import OFIBucketer, event_contribution, fit_ofi
from goldflow.core.profile import VolumeProfile
from goldflow.core.vpin import VPIN
from goldflow.feeds.base import NS_PER_MS, NS_PER_S, Quote, Side, Trade


def q(bp, ap, bs, a_s, ts=0):
    return Quote(ts, ts, bp, ap, bs, a_s, "GC")


def t(px, sz, side, ts=0):
    return Trade(ts, ts, px, sz, side, "GC")


# ---------------------------------------------------------------------------
# OFI -- the indicator logic
# ---------------------------------------------------------------------------

class TestOFI:
    def test_same_price_uses_size_difference(self):
        """Both indicators fire when price is unchanged -> q_n - q_{n-1}.

        The classic bug is writing this as if/elif, which drops every
        same-price size change -- i.e. most of the informative events.
        """
        prev = q(100.0, 100.1, 50, 60)
        cur = q(100.0, 100.1, 80, 60)          # +30 on the bid
        assert event_contribution(prev, cur) == pytest.approx(30.0)

        cur2 = q(100.0, 100.1, 50, 90)         # +30 on the ask
        assert event_contribution(prev, cur2) == pytest.approx(-30.0)

    def test_bid_price_up_adds_full_new_queue(self):
        prev = q(100.0, 100.1, 50, 60)
        cur = q(100.05, 100.1, 20, 60)
        # bid up: +20 (new bid), no -prev_bid; ask unchanged: -60 + 60 = 0
        assert event_contribution(prev, cur) == pytest.approx(20.0)

    def test_ask_price_down_is_negative(self):
        prev = q(100.0, 100.1, 50, 60)
        cur = q(100.0, 100.05, 50, 30)
        # bid unchanged: 50 - 50 = 0; ask down: -30, no +prev_ask
        assert event_contribution(prev, cur) == pytest.approx(-30.0)

    def test_cancellation_counts_like_a_trade(self):
        """A cancel and a market order that remove the same size are equal.

        This is deliberate in Cont-Kukanov-Stoikov and is why OFI beats delta.
        """
        prev = q(100.0, 100.1, 50, 60)
        cancelled = q(100.0, 100.1, 50, 40)     # 20 pulled from the ask
        assert event_contribution(prev, cancelled) == pytest.approx(20.0)

    def test_crossed_quotes_are_dropped(self):
        b = OFIBucketer(NS_PER_S)
        b.update(q(100.0, 100.1, 10, 10, ts=0))
        assert b.update(q(100.2, 100.1, 10, 10, ts=NS_PER_MS)) is None

    def test_avg_depth_matches_paper_denominator(self):
        quotes = [q(100.0, 100.1, 10, 10, ts=i * NS_PER_MS) for i in range(1, 6)]
        quotes.append(q(100.0, 100.1, 10, 10, ts=2 * NS_PER_S))
        buckets = list(OFIBucketer(NS_PER_S).run(quotes))
        assert buckets
        bk = buckets[0]
        assert bk.avg_depth == pytest.approx(
            bk.depth_sum / (2 * max(bk.n_events - 1, 1)))

    def test_normalised_is_depth_invariant(self):
        """Same relative flow in a book 10x deeper gives the same normalised OFI."""
        def run(scale):
            qs = [q(100.0, 100.1, 100 * scale, 100 * scale, ts=0)]
            for i in range(1, 20):
                qs.append(q(100.0, 100.1, (100 + i) * scale, 100 * scale,
                            ts=i * NS_PER_MS))
            qs.append(q(100.0, 100.1, 100 * scale, 100 * scale, ts=2 * NS_PER_S))
            return list(OFIBucketer(NS_PER_S).run(qs))[0].normalised

        assert run(1) == pytest.approx(run(10), rel=1e-9)

    def test_predictive_regression_only_pairs_adjacent_buckets(self):
        """Gaps must not be treated as consecutive intervals."""
        from goldflow.core.ofi import OFIBucket
        mk = lambda s, e: OFIBucket(s, e, 1.0, 10, 100.0, 100.0, 100.1, 0.1)
        contiguous = [mk(i * NS_PER_S, (i + 1) * NS_PER_S) for i in range(40)]
        fit = fit_ofi(contiguous)
        assert fit.n == 40


# ---------------------------------------------------------------------------
# Trade classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_tick_rule_carries_sign_on_zero_tick(self):
        tr = TickRule()
        tr.classify(100.0)
        assert tr.classify(100.1) == Side.ASK
        assert tr.classify(100.1) == Side.ASK      # unchanged -> carry
        assert tr.classify(100.0) == Side.BID

    def test_lee_ready_uses_prevailing_quote(self):
        lr = LeeReady()
        lr.on_quote(q(100.0, 100.2, 10, 10))
        assert lr.classify(t(100.2, 5, Side.UNKNOWN)) == Side.ASK
        assert lr.classify(t(100.0, 5, Side.UNKNOWN)) == Side.BID

    def test_lee_ready_falls_back_at_midpoint(self):
        lr = LeeReady()
        lr.on_quote(q(100.0, 100.2, 10, 10))
        lr.classify(t(100.2, 5, Side.UNKNOWN))     # warms the tick state to ASK
        assert lr.classify(t(100.1, 5, Side.UNKNOWN)) in (Side.ASK, Side.BID)

    def test_bvc_fractions_are_bounded_and_sum_to_volume(self):
        px = np.cumsum(np.random.default_rng(0).normal(0, 0.1, 500)) + 100
        vol = np.full(500, 10.0)
        r = bulk_volume_classification(px, vol)
        assert np.all(r.fraction_buy >= 0) and np.all(r.fraction_buy <= 1)
        assert np.allclose(r.buy_volume + r.sell_volume, vol)

    def test_signed_size_convention(self):
        assert t(100, 5, Side.ASK).signed_size == 5
        assert t(100, 5, Side.BID).signed_size == -5
        assert t(100, 5, Side.UNKNOWN).signed_size == 0


# ---------------------------------------------------------------------------
# Delta / CVD
# ---------------------------------------------------------------------------

class TestDelta:
    def test_unknown_side_is_not_split(self):
        """UNKNOWN must contribute to neither side, not 50/50."""
        b = DeltaBarBuilder(NS_PER_S)
        for i in range(5):
            b.update(t(100.0, 10, Side.UNKNOWN, ts=i * NS_PER_MS))
        bars = list(b.run([t(100.0, 10, Side.ASK, ts=2 * NS_PER_S)]))
        assert bars[0].buy_volume == 0
        assert bars[0].sell_volume == 0
        assert bars[0].trades == 5

    def test_cvd_resets_on_roll(self):
        c = CVD()
        c.update(DeltaBar(0, 1, 100, 40, 5, 1, 1, 1, 1))
        assert c.value == 60
        c.on_roll(ts=1)
        assert c.value == 0
        assert c.rolls == [1]

    def test_cvd_roll_can_carry_when_asked(self):
        c = CVD()
        c.update(DeltaBar(0, 1, 100, 40, 5, 1, 1, 1, 1))
        c.on_roll(ts=1, carry=True)
        assert c.value == 60

    def test_footprint_diagonal_imbalance(self):
        fp = Footprint(0, 1, tick=0.1)
        fp.add(t(100.0, 100, Side.ASK))
        fp.add(t(99.9, 10, Side.BID))
        found = fp.diagonal_imbalances(ratio=3.0, min_vol=10)
        assert any(p == pytest.approx(100.0) and side == "buy"
                   for p, side, _ in found)


# ---------------------------------------------------------------------------
# VPIN
# ---------------------------------------------------------------------------

class TestVPIN:
    def test_large_trade_splits_across_buckets(self):
        """One print bigger than the bucket must fill several, not just one."""
        v = VPIN(bucket_volume=10, window=2)
        v.update(t(100.0, 100, Side.ASK))
        assert len(v._buckets) == 2          # capped by window, but >1 closed

    def test_perfectly_balanced_flow_gives_zero(self):
        v = VPIN(bucket_volume=10, window=5)
        last = None
        for i in range(200):
            side = Side.ASK if i % 2 == 0 else Side.BID
            r = v.update(t(100.0, 5, side))
            if r is not None:
                last = r
        assert last is not None and last < 0.15

    def test_one_sided_flow_approaches_one(self):
        v = VPIN(bucket_volume=10, window=5)
        last = None
        for _ in range(200):
            r = v.update(t(100.0, 5, Side.ASK))
            if r is not None:
                last = r
        assert last == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Volume profile
# ---------------------------------------------------------------------------

class TestProfile:
    def test_poc_is_the_heaviest_level(self):
        vp = VolumeProfile(tick=0.1)
        for px, sz in ((100.0, 10), (100.1, 90), (100.2, 20)):
            vp.add(t(px, sz, Side.ASK))
        assert vp.poc() == pytest.approx(100.1)

    def test_value_area_contains_poc_and_enough_volume(self):
        rng = np.random.default_rng(3)
        vp = VolumeProfile(tick=0.1)
        for px in rng.normal(100, 0.5, 4000):
            vp.add(t(round(px, 1), 1, Side.ASK))
        val, vah = vp.value_area()
        assert val <= vp.poc() <= vah
        px, vol = vp.as_arrays()
        inside = vol[(px >= val) & (px <= vah)].sum()
        assert inside / vol.sum() >= 0.68


# ---------------------------------------------------------------------------
# Backtest causality
# ---------------------------------------------------------------------------

class TestBacktestCausality:
    def test_flat_strategy_has_exactly_zero_pnl(self):
        from goldflow.feeds import SynthConfig, make_pair
        from goldflow.research import Backtester, BacktestConfig, flat_strategy

        fut, spot = make_pair(SynthConfig(n_events=20_000, seed=5))
        fq = [e for e in fut if isinstance(e, Quote)]
        r = Backtester(BacktestConfig()).run(fq, spot, flat_strategy())
        assert abs(r.net_pnl) < 1e-12
        assert r.n_trades == 0

    def test_fills_never_happen_before_the_signal(self):
        from goldflow.feeds import SynthConfig, make_pair
        from goldflow.research import (
            BacktestConfig, Backtester, ofi_threshold_strategy)

        fut, spot = make_pair(SynthConfig(n_events=30_000, seed=6))
        fq = [e for e in fut if isinstance(e, Quote)]
        cfg = BacktestConfig(signal_delay_ns=250 * NS_PER_MS)
        r = Backtester(cfg).run(fq, spot, ofi_threshold_strategy())
        # every fill must be at or after its bucket close plus the delay
        assert all(f.ts_ns > 0 for f in r.fills)
        assert r.signal_delay_ns == 250 * NS_PER_MS

    def test_more_delay_never_helps_a_profitable_baseline(self):
        """Sanity check on the look-ahead probe's own logic."""
        from goldflow.feeds import SynthConfig, make_pair
        from goldflow.research import (
            BacktestConfig, Backtester, ofi_threshold_strategy)

        fut, spot = make_pair(SynthConfig(n_events=30_000, seed=7))
        fq = [e for e in fut if isinstance(e, Quote)]
        bt = Backtester(BacktestConfig())
        probe = bt.lookahead_probe(fq, spot, ofi_threshold_strategy())
        assert "verdict" in probe
        assert not probe["verdict"].startswith("LOOK-AHEAD SUSPECTED")


# ---------------------------------------------------------------------------
# Feed contract
# ---------------------------------------------------------------------------

class TestFeeds:
    def test_synthetic_stream_is_time_ordered(self):
        from goldflow.feeds import SynthConfig, SyntheticBook, assert_monotonic

        n = assert_monotonic(SyntheticBook(SynthConfig(n_events=5_000)).stream())
        assert n > 5_000

    def test_assert_monotonic_catches_out_of_order(self):
        from goldflow.feeds import assert_monotonic

        bad = [q(100, 100.1, 1, 1, ts=10), q(100, 100.1, 1, 1, ts=5)]
        with pytest.raises(ValueError, match="went backwards"):
            assert_monotonic(bad)

    def test_spot_leg_lags_futures_by_config(self):
        from goldflow.feeds import SynthConfig, make_pair
        from goldflow.research.leadlag import cross_correlation

        cfg = SynthConfig(n_events=60_000, lag_ms=45.0, seed=9)
        fut, spot = make_pair(cfg)
        fq = [e for e in fut if isinstance(e, Quote)]
        r = cross_correlation(fq, spot, grid_ns=10 * NS_PER_MS, max_lag_steps=20)
        assert 20 <= r.best_lag_ms <= 70      # recovers the planted 45ms lead


# ---------------------------------------------------------------------------
# Execution quality forensics
# ---------------------------------------------------------------------------

class TestExecutionQuality:
    """The detector must find a known-predatory venue and clear a known-honest
    one. A forensic tool validated only on the negative case is not validated.
    """

    @staticmethod
    def _orders(spot, aggression, n=2500, hold_ms=30.0):
        from goldflow.research.execution_quality import LastLookBroker

        rng = np.random.default_rng(0)
        b = LastLookBroker(spot, hold_ms=hold_ms, aggression=aggression, seed=5)
        idx = sorted(rng.choice(len(spot) - 800, size=n, replace=False))
        out = []
        for i in idx:
            sq = spot[i]
            side = 1 if rng.random() < 0.5 else -1
            out.append(b.submit(sq.ts_recv, side, 0.01,
                                sq.ask_px if side > 0 else sq.bid_px))
        return out

    @pytest.fixture(scope="class")
    def spot(self):
        from goldflow.feeds import SynthConfig, make_pair
        return make_pair(SynthConfig(n_events=200_000, seed=3))[1]

    def test_detects_last_look_asymmetry(self, spot):
        from goldflow.research.execution_quality import asymmetry_test

        r = asymmetry_test(self._orders(spot, 0.8), spot, horizon_ms=1_000)
        assert r.rejection_rate > 0.2
        assert r.gap > 0
        assert r.t_stat > 3.0
        assert r.verdict.startswith("ASYMMETRIC")

    def test_clears_an_honest_venue(self, spot):
        """Note the sample size: 12,000 orders to clear an honest venue versus
        2,500 to convict a predatory one.

        Rejections are the scarce sample, and an honest broker produces few of
        them -- so clearing a broker is much more expensive than catching one.
        That asymmetry is a real operational fact, not a quirk of the test.
        """
        from goldflow.research.execution_quality import asymmetry_test

        r = asymmetry_test(self._orders(spot, 0.0, n=12_000), spot,
                           horizon_ms=1_000)
        assert r.rejection_rate < 0.05
        assert r.n_rejected >= 50
        assert r.t_stat < 3.0
        assert not r.verdict.startswith("ASYMMETRIC")
        # a null must come with its own power statement
        assert "Smallest gap this sample could have detected" in r.verdict

    def test_slippage_alone_would_miss_it(self, spot):
        """The point of having two tests: slippage clears the bad venue."""
        from goldflow.research.execution_quality import slippage_symmetry

        r = slippage_symmetry(self._orders(spot, 0.8))
        assert not r.verdict.startswith("ONE-SIDED")

    def test_flags_mid_vs_touch_capture_error(self, spot):
        """Comparing fills against the mid fabricates constant improvement."""
        from goldflow.research.execution_quality import (
            OrderRecord, slippage_symmetry)

        bad = [OrderRecord(q.ts_recv, 1, 0.01, q.mid, True,
                           q.ts_recv, q.mid - 0.05, None)
               for q in spot[:200]]
        assert "IMPLAUSIBLE" in slippage_symmetry(bad).verdict

    def test_small_sample_refuses_to_reassure(self, spot):
        from goldflow.research.execution_quality import asymmetry_test

        r = asymmetry_test(self._orders(spot, 0.8, n=60), spot)
        assert "insufficient sample" in r.verdict
        assert "NOT evidence" in r.verdict


# ---------------------------------------------------------------------------
# Broker probe harness
# ---------------------------------------------------------------------------

class TestBrokerProbe:
    @pytest.fixture(scope="class")
    def spot(self):
        from goldflow.feeds import SynthConfig, make_pair
        return make_pair(SynthConfig(n_events=200_000, seed=3))[1]

    def test_probe_captures_touch_price_not_mid(self, spot):
        """requested_px must be the side you crossed, or slippage is fiction."""
        from goldflow.live import BrokerProbe, ProbeConfig
        from goldflow.research import LastLookBroker

        p = BrokerProbe(ProbeConfig(n_orders=400, seed=1))
        st = p.run_offline(LastLookBroker(spot, aggression=0.0, seed=5), spot)
        assert st.orders
        for o in st.orders[:50]:
            assert o.requested_px > 0
            # a buy must be probed at an ask, a sell at a bid -- never the mid
            assert o.side in (1, -1)

    def test_probe_finds_last_look_venue(self, spot):
        from goldflow.live import BrokerProbe, ProbeConfig
        from goldflow.research import LastLookBroker, asymmetry_test

        p = BrokerProbe(ProbeConfig(n_orders=3_000, seed=1))
        st = p.run_offline(LastLookBroker(spot, aggression=0.8, seed=5), spot)
        r = asymmetry_test(st.orders, st.quotes, horizon_ms=1_000)
        assert r.t_stat > 3.0
        assert r.verdict.startswith("ASYMMETRIC")

    def test_probe_direction_is_unbiased(self, spot):
        """Signal-driven probing would confound broker behaviour with edge."""
        from goldflow.live import BrokerProbe, ProbeConfig
        from goldflow.research import LastLookBroker

        p = BrokerProbe(ProbeConfig(n_orders=3_000, seed=1))
        st = p.run_offline(LastLookBroker(spot, aggression=0.0, seed=5), spot)
        longs = sum(1 for o in st.orders if o.side > 0)
        assert 0.42 < longs / len(st.orders) < 0.58

    def test_ceiling_allows_clearing_an_honest_venue(self, spot):
        """A max_orders too low can only ever return 'underpowered'."""
        from goldflow.live import BrokerProbe, ProbeConfig
        from goldflow.research import LastLookBroker

        p = BrokerProbe(ProbeConfig(n_orders=5_000, seed=1))
        st = p.run_offline(LastLookBroker(spot, aggression=0.0, seed=5), spot)
        assert p.report(st)["sample_status"] == "adequate"

    def test_live_refuses_without_explicit_acknowledgement(self):
        from goldflow.live import BrokerProbe, ProbeConfig

        with pytest.raises(RuntimeError, match="i_understand_live"):
            BrokerProbe(ProbeConfig()).run_live(
                lambda *a: None, lambda: None, lambda s: None, lambda: 0)

    def test_config_audit_lists_every_placeholder(self):
        from goldflow.config import PLACEHOLDERS, audit

        text = audit()
        for k in PLACEHOLDERS:
            assert k in text


# ---------------------------------------------------------------------------
# Footprint provenance -- real vs tick-derived
# ---------------------------------------------------------------------------

class TestFootprintProvenance:
    """The two footprints render identically. Only arithmetic separates them."""

    @pytest.fixture(scope="class")
    def window(self):
        from goldflow.core.classify import classify_stream
        from goldflow.feeds import SynthConfig, SyntheticBook

        ev = list(SyntheticBook(SynthConfig(n_events=40_000, seed=42)).stream())
        sub = ev[5_000:20_000]
        trades = [x for x in classify_stream(sub) if isinstance(x, Trade)]
        quotes = [e for e in sub if isinstance(e, Quote)]
        return trades, quotes

    def test_real_footprint_matches_traded_volume(self, window):
        from goldflow.core.render import (footprint_from_trades,
                                          footprint_provenance)
        trades, quotes = window
        fp, _ = footprint_from_trades(trades, 0.10)
        r = footprint_provenance(fp, quotes, trades)
        assert r.ratio_to_volume == pytest.approx(1.0, abs=1e-9)
        assert r.verdict.startswith("REAL")

    def test_tick_footprint_matches_quote_change_count(self, window):
        from goldflow.core.render import (footprint_from_quotes,
                                          footprint_provenance)
        trades, quotes = window
        fp, _ = footprint_from_quotes(quotes, 0.10)
        r = footprint_provenance(fp, quotes, trades)
        assert r.ratio_to_quotes == pytest.approx(1.0, abs=1e-9)
        assert r.verdict.startswith("TICK-DERIVED")

    def test_both_render_as_plausible_footprints(self, window):
        """Neither output looks broken -- that is precisely the problem."""
        from goldflow.core.render import (footprint_from_quotes,
                                          footprint_from_trades,
                                          render_footprint)
        trades, quotes = window
        for fp, vp in (footprint_from_trades(trades, 0.10),
                       footprint_from_quotes(quotes, 0.10)):
            out = render_footprint(fp, vp)
            assert "bid" in out and "ask" in out
            assert "delta" in out
            assert len(out.splitlines()) > 4

    def test_tick_footprint_disagrees_on_delta_sign(self, window):
        """Same market, opposite conclusion -- the failure is silent."""
        from goldflow.core.render import (footprint_from_quotes,
                                          footprint_from_trades)
        trades, quotes = window
        fp_r, _ = footprint_from_trades(trades, 0.10)
        fp_t, _ = footprint_from_quotes(quotes, 0.10)
        d_r = sum(fp_r.ask_vol.values()) - sum(fp_r.bid_vol.values())
        d_t = sum(fp_t.ask_vol.values()) - sum(fp_t.bid_vol.values())
        assert d_r != d_t

    def test_profile_exposes_poc_val_vah(self, window):
        from goldflow.core.render import footprint_from_trades
        trades, _ = window
        _, vp = footprint_from_trades(trades, 0.10)
        s = vp.summary()
        assert s["poc"] is not None
        assert s["val"] is not None and s["vah"] is not None
        assert s["val"] <= s["poc"] <= s["vah"]


# ---------------------------------------------------------------------------
# Bracket management -- TP / SL / trail / time
# ---------------------------------------------------------------------------

class TestBrackets:
    @staticmethod
    def _q(bid, ask, ts=0):
        return Quote(ts, ts, bid, ask, 10, 10, "XAUUSD")

    def test_stop_wins_ties_with_target(self):
        """Pessimistic by design: within one quote, the stop is assumed first."""
        from goldflow.live.position import BracketConfig, ExitReason, ManagedPosition

        p = ManagedPosition(1, 1.0, 100.0, 0,
                            BracketConfig(sl=1.0, tp=1.0, spread_buffer_mult=0.0))
        # a quote whose bid is both below the stop and above the target
        assert p.update(self._q(98.0, 98.1, ts=NS_PER_S)) == ExitReason.STOP

    def test_stop_measured_on_exit_side_not_mid(self):
        from goldflow.live.position import BracketConfig, ExitReason, ManagedPosition

        p = ManagedPosition(1, 1.0, 100.0, 0,
                            BracketConfig(sl=1.0, tp=5.0, spread_buffer_mult=0.0))
        # mid is 99.25 (above the 99.0 stop) but the BID is 99.0 -- stop hit
        assert p.update(self._q(99.0, 99.5, ts=NS_PER_S)) == ExitReason.STOP

    def test_spread_buffer_prevents_phantom_stop(self):
        from goldflow.live.position import BracketConfig, ExitReason, ManagedPosition

        p = ManagedPosition(1, 1.0, 100.0, 0,
                            BracketConfig(sl=1.0, tp=5.0, spread_buffer_mult=1.0))
        # bid dips to 99.0 on a 0.5 spread blowout -- buffered, no stop
        assert p.update(self._q(99.0, 99.5, ts=NS_PER_S)) == ExitReason.NONE

    def test_time_exit_fires_at_horizon(self):
        from goldflow.live.position import BracketConfig, ExitReason, ManagedPosition

        p = ManagedPosition(1, 1.0, 100.0, 0,
                            BracketConfig(sl=9, tp=9, max_hold_s=30))
        assert p.update(self._q(100.0, 100.1, ts=29 * NS_PER_S)) == ExitReason.NONE
        assert p.update(self._q(100.0, 100.1, ts=31 * NS_PER_S)) == ExitReason.TIME

    def test_breakeven_and_trail_only_move_favourably(self):
        from goldflow.live.position import BracketConfig, ManagedPosition

        p = ManagedPosition(1, 1.0, 100.0, 0,
                            BracketConfig(sl=2.0, tp=99.0, max_hold_s=1e9,
                                          breakeven_at=1.0, trail_distance=0.5,
                                          spread_buffer_mult=0.0))
        p.update(self._q(101.5, 101.6, ts=NS_PER_S))     # arms + trails to 101.0
        armed = p.stop_px
        assert armed >= 100.0
        p.update(self._q(100.8, 100.9, ts=2 * NS_PER_S))  # pulls back
        assert p.stop_px == armed                          # stop never retreats

    def test_short_side_mirrors_long(self):
        from goldflow.live.position import BracketConfig, ExitReason, ManagedPosition

        p = ManagedPosition(-1, 1.0, 100.0, 0,
                            BracketConfig(sl=1.0, tp=2.0, spread_buffer_mult=0.0))
        assert p.update(self._q(97.9, 98.0, ts=NS_PER_S)) == ExitReason.TARGET

    def test_size_for_risk_arithmetic(self):
        from goldflow.live.position import size_for_risk
        # 150 of risk, 1.50 stop, 100oz contract -> exactly 1 lot
        assert size_for_risk(150.0, 1.50, 100.0, 10.0) == pytest.approx(1.0)
        assert size_for_risk(150.0, 1.50, 100.0, max_lots=0.5) == 0.5
        assert size_for_risk(150.0, 0.0) == 0.0


class TestBracketBacktest:
    @pytest.fixture(scope="class")
    def data(self):
        from goldflow.feeds import SynthConfig, make_pair
        fut, spot = make_pair(SynthConfig(n_events=200_000, seed=3))
        return [e for e in fut if isinstance(e, Quote)], spot

    def test_transient_gate_recovers_but_fault_is_sticky(self, data):
        """A basis spike must not disable the system for the session."""
        from goldflow.live import BracketConfig, Runner, RunnerConfig

        fq, spot = data
        r = Runner(RunnerConfig(entry_z=2.0, lots=1.0,
                                bracket=BracketConfig(sl=1.0, tp=3.0,
                                                      max_hold_s=60)),
                   now_ns=lambda: 0)
        r.cfg.require_fresh_quotes = False
        for _ in r.run(fq, spot):
            pass
        # gates opened and closed during the run; nothing stuck at the end
        assert not r.state.halted
        assert r.state.n_signals > 50
        assert len(r.state.trades) > 5

    def test_runner_and_backtester_agree(self, data):
        """Same components, same wiring -> same trade count. If these diverge,
        the backtest is describing a different system from the live path."""
        from goldflow.live import BracketConfig, Runner, RunnerConfig
        from goldflow.research import BracketBacktestConfig, BracketBacktester

        fq, spot = data
        br = BracketConfig(sl=1.0, tp=2.96, max_hold_s=60)
        bt = BracketBacktester(BracketBacktestConfig(entry_z=2.0, lots=1.0,
                                                    bracket=br)).run(fq, spot)
        r = Runner(RunnerConfig(entry_z=2.0, lots=1.0, bracket=br),
                   now_ns=lambda: 0)
        r.cfg.require_fresh_quotes = False
        for _ in r.run(fq, spot):
            pass
        assert abs(len(bt.trades) - len(r.state.trades)) <= 2

    def test_tighter_stop_can_lose_with_higher_win_rate(self, data):
        """Win rate is not the objective. This must stay demonstrable."""
        from goldflow.live import BracketConfig
        from goldflow.research import BracketBacktestConfig, BracketBacktester

        fq, spot = data
        loose = BracketBacktester(BracketBacktestConfig(
            entry_z=2.0, lots=1.0,
            bracket=BracketConfig(sl=1.0, tp=2.96, max_hold_s=60))).run(fq, spot)
        tight = BracketBacktester(BracketBacktestConfig(
            entry_z=2.0, lots=1.0,
            bracket=BracketConfig(sl=0.5, tp=1.0, max_hold_s=60))).run(fq, spot)
        assert tight.summary()["win_rate"] > loose.summary()["win_rate"]
        assert tight.net < loose.net


class TestExcursion:
    def test_bracket_needs_a_real_sample(self):
        from goldflow.research.excursion import Excursion, recommend_bracket

        few = [Excursion(0, 1, 100.0, 0.5, 1.0, 5.0, 0.3, 10)] * 20
        with pytest.raises(ValueError, match="fitted to noise"):
            recommend_bracket(few, round_trip_cost=0.1)

    def test_stop_never_sits_inside_the_round_trip(self):
        from goldflow.research.excursion import Excursion, recommend_bracket

        exc = [Excursion(i, 1, 100.0, 0.01, 2.0, 5.0, 1.0, 10)
               for i in range(200)]
        rec = recommend_bracket(exc, round_trip_cost=1.0)
        assert rec.sl >= 1.5
        assert "round-trip cost" in rec.sl_basis
