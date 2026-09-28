"""
Production presets -- every research finding encoded as a default.

The point of this file is that you should not have to re-derive any of these
numbers, and that each one carries the reason it is what it is. Where a value
is a guess, it says so. Where it came from a measurement, the measurement is
named.

Import the preset, do not copy the numbers:

    from goldflow.config import SPOT_XAUUSD
    bt = Backtester(SPOT_XAUUSD.backtest)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .feeds.base import NS_PER_MS, NS_PER_S
from .live.engine import EngineConfig
from .live.risk import RiskConfig
from .research.backtest import BacktestConfig
from .research.costs import CostConfig


@dataclass
class Preset:
    name: str
    rationale: str
    backtest: BacktestConfig
    engine: EngineConfig
    costs: CostConfig
    #: hold period in seconds for OFITimedStrategy
    hold_s: float
    entry_z: float


# ---------------------------------------------------------------------------
# GC signal -> spot XAUUSD execution
# ---------------------------------------------------------------------------

_SPOT_COSTS = CostConfig(
    # PLACEHOLDER. Replace via SpreadModel.from_ticks() on YOUR broker's tick
    # history before reading any backtest number. A cost model built from
    # someone else's data is a model of someone else's execution.
    base_half_spread=0.11,
    commission_per_lot=0.035,
    slippage_mult=0.5,
    # Spreads triple in event windows -- measured behaviour, not a safety
    # margin. spread_regime_profile() will give you your broker's real ratio.
    event_spread_mult=3.0,
    thin_hours_mult=1.8,
    max_half_spread=2.50,
)

SPOT_XAUUSD = Preset(
    name="GC signal -> spot XAUUSD",
    rationale=(
        "Signals from COMEX GC (real tape); execution on spot (where the "
        "account is). Every parameter below follows from one of three "
        "findings: (1) the OFI result is contemporaneous, so the tradeable "
        "horizon is long, not short; (2) last-look is a fixed tax, so the "
        "expected move must be large relative to it; (3) impact scales as "
        "1/depth, so thresholds must be depth-normalised."
    ),
    backtest=BacktestConfig(
        # 1s buckets: fine enough to resolve the GC lead, coarse enough that
        # the OFI z-score is not dominated by microstructure noise.
        bucket_ns=1 * NS_PER_S,
        # 250ms is optimistic for a retail stack. Measure yours and raise it.
        # The look-ahead probe re-runs with a full extra bucket on top.
        signal_delay_ns=250 * NS_PER_MS,
        zscore_window=600,
        zscore_min_obs=60,
        lots_per_unit=1.0,
        # Spreads triple in event windows. Standing aside costs a few trades
        # and removes the population where a constant-spread backtest invents
        # an edge.
        skip_event_windows=True,
        # The EFP kill switch. Gold's basis reached ~$60/oz in Jan 2025 --
        # "multiples above fair value" (MKS Pamp). 4 sigma is a guess; set it
        # from basis_stats() on your own data once you have a month.
        basis_z_halt=4.0,
        basis_window=3_600,
        costs=_SPOT_COSTS,
    ),
    engine=EngineConfig(
        bucket_ns=1 * NS_PER_S,
        zscore_window=600,
        zscore_min_obs=60,
        # 0 disables VPIN. Enable it only after measuring GC's daily volume:
        # the rule of thumb is bucket = ADV/50. Copying a number calibrated
        # for ES will produce buckets that never close.
        vpin_bucket_volume=0.0,
        vpin_window=50,
        basis_window=3_600,
        risk=RiskConfig(
            # Degrade to FLAT, never to "hold last". A frozen bridge repeating
            # its last instruction is the most dangerous failure mode here.
            max_signal_age_ms=2_000.0,
            basis_z_halt=4.0,
            halt_in_event_windows=True,
            halt_on_toxic_flow=True,
            # PLACEHOLDER -- set from spread_regime_profile()['overall_p99'].
            max_spot_spread=0.80,
            max_daily_loss=0.0,      # set this before going live
            max_position=3.0,
        ),
    ),
    costs=_SPOT_COSTS,
    # 30s+ is where the move clears the round trip. Measured on the simulator:
    # z-decay exit netted -29.15 over 131 trades; a 60s timed exit netted
    # +16.28 over 32 trades on IDENTICAL entries. Set this from YOUR
    # experiment_signal_returns output, not from these numbers.
    hold_s=60.0,
    # 2.0 sigma on the DEPTH-NORMALISED z. An unnormalised threshold fires
    # continuously overnight and never in New York, because impact scales as
    # 1/depth (lambda_hat ~ 0.88 measured, ~0.98 in the paper).
    entry_z=2.0,
)


#: Values that are guesses, not measurements. Replace each one before trusting
#: a number that depends on it. Printed by `goldflow.config.audit()`.
PLACEHOLDERS = {
    "costs.base_half_spread": "SpreadModel.from_ticks(your_broker_quotes)",
    "costs.commission_per_lot": "your broker's schedule",
    "risk.max_spot_spread": "spread_regime_profile(your_quotes)['overall_p99']",
    "risk.max_daily_loss": "your own risk budget -- 0 means DISABLED",
    "backtest.signal_delay_ns": "measure your own signal-to-order latency",
    "backtest.basis_z_halt": "basis_stats() on a month of your own data",
    "engine.vpin_bucket_volume": "GC average daily volume / 50",
    "hold_s": "experiment_signal_returns() best horizon on YOUR data",
}


def audit() -> str:
    """Print what is still a guess. Run before every backtest you believe."""
    lines = ["Unmeasured parameters in SPOT_XAUUSD:", ""]
    for k, how in PLACEHOLDERS.items():
        lines.append(f"  {k:<32} <- {how}")
    lines += [
        "",
        "Any result that depends on an unmeasured parameter is a result about",
        "the placeholder, not about gold.",
    ]
    return "\n".join(lines)
