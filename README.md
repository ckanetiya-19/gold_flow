# goldflow

Order flow research and execution toolkit for XAU/USD.

**Architecture: read COMEX GC, execute spot.**

Spot gold is bilateral OTC — no consolidated tape, no exchange volume, no
public book. What a retail platform calls "volume" on XAUUSD is a count of
quote updates, so delta, CVD and footprints cannot be computed on it at all.
Meanwhile formal price-discovery work puts COMEX futures at 67%–94% of gold
price discovery (Hasbrouck information share, 1997–2014), with the
noise-corrected leadership share near 70%, despite trading a fraction of
London's volume.

So the signal comes from a venue with a real tape, and the order goes to
whichever venue you can actually trade.

---

## Install and verify

```bash
pip install -r requirements.txt
python -m goldflow selftest
```

`selftest` runs the whole stack against a built-in limit-order-book simulator
with known parameters. It should end with `all checks passed`. Nothing here
needs an API key.

---

## The point of the package

The code is secondary. **The validation plan is the deliverable**, and it is
ordered so the cheapest experiment that could kill the project runs first:

```bash
python -m goldflow validate --latency-ms 50                  # synthetic
python -m goldflow validate --fut GC.csv --spot XAU.csv --colmap databento
```

| # | Experiment | Kills the project if… |
|---|---|---|
| 1 | `lead_lag` | GC doesn't lead your XAUUSD by more than your latency |
| 2 | `ofi_regression` | The Cont-Kukanov-Stoikov relation doesn't reproduce on gold |
| 3 | `tick_volume_proxy` | (informational — quantifies how bad broker tick volume is) |
| 4 | `signal_returns` | The gross move is smaller than your round-trip cost |
| 5 | `walk_forward` | Results concentrate in one regime |

`run_all` **stops at the first hard failure**. That is deliberate. Skipping
ahead is how people spend a year building on a premise a two-hour test would
have refuted.

---

## Three findings from building this

These came out of running the code, not from the literature, and each one
changes how you should read a result.

### 1. The 65% R² is contemporaneous, not predictive

Cont-Kukanov-Stoikov regress OFI over an interval against the price change
over **the same interval**. That is a price-*impact* result — close to a
description of what a price move is. It does not say you could have known
beforehand.

`fit_ofi` therefore runs a third regression the paper does not:
`dP_{k+1} = β · OFI_k`. On the simulator, contemporaneous R² is 0.51–0.87
while predictive R² at a 100ms grid is **0.012**. Same data, two orders of
magnitude apart. Read `r2_predictive`, not `r2`, when deciding whether there
is a trade.

### 2. The sampling grid decides the price-discovery answer

Sampling coarser than the actual lead destroys the decomposition. On a
controlled series with a planted 45ms lead:

| grid | IS_fut | band | CS_fut | ILS_fut |
|---|---|---|---|---|
| 20ms | 0.981 | [0.98, 0.98] | 0.894 | 0.859 |
| 50ms | 0.958 | [0.94, 0.98] | 0.773 | 0.870 |
| 100ms | 0.913 | [0.88, 0.95] | 0.812 | 0.709 |
| 250ms | 0.745 | [0.50, 0.99] | 0.907 | 0.230 |
| 1000ms | 0.616 | **[0.23, 1.00]** | 0.964 | **0.056** |

At 1 second the ordering is unidentified and ILS **names the wrong venue as
leader**. The published gold study used a 1-second grid in a slower era;
replicating that grid on modern data will understate futures leadership.
`experiment_lead_lag` measures the lead first, then sweeps grids and selects
on band width. Always read the band, never the midpoint alone.

### 3. The exit rule, not the signal, is usually what fails

Experiment 4 measures the move at fixed horizons. The baseline strategy exits
when the z-score decays — typically within seconds. On the simulator that gap
is the whole result:

| exit rule | gross | costs | net | trades |
|---|---|---|---|---|
| z-decay | +14.67 | 43.82 | **−29.15** | 131 |
| timed 10s | +17.47 | 27.89 | −10.42 | 84 |
| timed 30s | +20.22 | 14.61 | **+5.61** | 44 |
| timed 60s | +26.91 | 10.62 | **+16.28** | 32 |

Identical entries. The z-decay rule pays the spread 131 times to capture the
part of the move that hasn't happened yet. Fix the holding period before
touching entry logic — `OFITimedStrategy` separates the two so they can be
tested independently.

> These numbers are from the **simulator**, whose drift process is mine. They
> demonstrate that the harness distinguishes these cases. They say nothing
> about gold. Run experiments 1–5 on real data before believing any of it.

---

## Layout

```
goldflow/
  feeds/          API-agnostic event layer — plug your vendor in here
    base.py         Quote / Trade / Depth, Feed protocol, ts_recv merge
    synthetic.py    LOB simulator with known parameters (no key needed)
    tabular.py      CSV/Parquet with a configurable column map
    databento_feed.py  historical + live MDP 3.0 (lazy import)
  core/           the math, shared by research AND live
    ofi.py          Cont-Kukanov-Stoikov OFI, depth normalisation, causal z
    classify.py     tick rule, Lee-Ready, BVC + accuracy measurement
    delta.py        delta, CVD with roll stitching, footprint matrix
    vpin.py         VPIN as a regime filter
    microprice.py   Stoikov micro-price (Markov chain estimator)
    profile.py      volume profile, POC, value area
    kyle.py         lambda — impact per contract, and as a liquidity state
    sessions.py     CME session clock, event windows, volume-based roll
  research/
    leadlag.py      cross-correlation, Hasbrouck IS, GG CS, ILS, basis
    costs.py        spread model calibrated from YOUR tick history
    backtest.py     causal event-driven engine + look-ahead probe
    experiments.py  the five experiments above
  live/
    engine.py       signal engine — same objects the backtester uses
    risk.py         staleness, basis kill switch, event windows, toxic flow
    execution.py    Broker protocol + paper broker + reconciler
```

**`Backtester.run` and `SignalEngine` drive the same `OFIBucketer`, the same
`RollingOFIZScore` and the same `SessionClock`.** Research and production
computing the signal two different ways — as they do in nearly every retail
setup — is the reason most backtests describe a system that doesn't exist.

---

## Plugging in your data API

Write one adapter that yields `Quote` / `Trade` and everything else works:

```python
from goldflow.feeds.base import Quote, Trade, Side

class MyFeed:
    symbol = "GC"
    def stream(self):
        for row in my_vendor_client.stream():
            yield Quote(row.ts_ns, row.recv_ns, row.bid, row.ask,
                        row.bid_sz, row.ask_sz, "GC")
```

Then validate it before trusting it:

```python
from goldflow.feeds import assert_monotonic
assert_monotonic(MyFeed().stream())     # raises on out-of-order rows
```

Vendors ship out-of-order rows around session boundaries and snapshot
refreshes. That silently corrupts every cumulative statistic, and no
downstream check will catch it.

For flat files, skip the adapter and use `TabularFeed` with a `ColumnMap`.

### Non-negotiables for a data source

1. **Trade size** — without it there is no delta, no CVD, no footprint, no VPIN.
2. **Aggressor side** — reconstructing it costs accuracy you should measure
   (`experiment_classification_accuracy`) rather than assume.
3. **L1 book events, unthrottled** — OFI is computed from *changes* in queue
   size. A throttled or sampled feed destroys `e_n`; you get a plausible
   number from corrupted input.
4. **Exchange and receipt timestamps, both** — using `ts_event` for causality
   in live trading is cheating; using `ts_recv` in a backtest is optimistic.
   Keeping both is the only way to be explicit about which you used.

Order-by-order (MBO) data is required **only** for queue position, iceberg
detection and true absorption. MBP-1 is one to two orders of magnitude smaller
and is enough for everything in experiments 1–5. Do not pull a month of MBO
before MBP-1 has proven the premise.

### Non-negotiables for an execution API

1. Streaming quotes for the exact symbol you trade, with their own timestamps
   — otherwise you cannot measure your basis or your slippage.
2. Measurable order-acknowledgement latency.
3. Fill reports with the **actual** fill price, not the requested price.
4. Authoritative position and balance queries, so the local view can be
   reconciled rather than trusted (`PositionReconciler`).
5. Rejection reasons. "Order failed" with no reason makes live debugging
   guesswork.

`PaperBroker` implements all five against a live quote stream. Run there
first — it exercises the staleness and reconciliation paths without capital.

---

## Risk gates

These run **before** the strategy is consulted, not after — a strategy that is
asked for a target and then overridden has already updated its internal state
as if it had traded.

| Gate | Why |
|---|---|
| `stale_data` | A frozen bridge repeating its last instruction is the most dangerous failure mode in a bridged system. Degrade to **flat**, never to "hold last". |
| `basis_blowout` | The EFP kill switch. Gold's basis blew out to ~$50/oz in early 2025 on tariff concerns; this architecture is short that dislocation. |
| `event_window` | Spreads triple exactly when flow signals look strongest — the London fixes, 08:30 ET macro. |
| `toxic_flow` | VPIN in the top decile of its own history. Not directional; stand aside. |
| `spread_guard` | Direct check on the spot spread right now. |
| `daily_loss` | Hard stop, not overridable by the strategy. |

---

## What this package will not do

- **Compute order flow from spot data.** It cannot be done. See the constraint
  at the top.
- **Backtest inside MetaTrader.** The MT5 Strategy Tester replays one symbol's
  ticks and has no mechanism to replay a second instrument's book events at
  nanosecond resolution alongside it. Anything it says about a two-leg system
  is fiction. That is why this exists in Python.
- **Endorse footprint pattern trading.** `Footprint.diagonal_imbalances` is
  implemented so you can *falsify* it. The 3:1 threshold is arbitrary and
  vendors disagree on the rule; the parameter space is large and the
  independent-event count is small. Expect a multiple-testing problem.

---

## References

- Cont, Kukanov & Stoikov, *The Price Impact of Order Book Events*, J. Financial Econometrics 12(1), 2014 — OFI, λ ≈ 0.98, R² ≈ 65%
- Putnis et al., *Who sets the price of gold? London or New York* — Hasbrouck IS / GG CS / ILS, 1997–2014
- Evans & Lyons, *Order Flow and Exchange Rate Dynamics*, JPE 110(1), 2002
- Easley, López de Prado & O'Hara, *The Microstructure of the Flash Crash* — VPIN
- Andersen & Bondarenko, *VPIN and the Flash Crash* — the rebuttal
- Chakrabarty, Pascual & Shkilko, *Evaluating trade classification algorithms*, J. Financial Markets 25, 2015
- Stoikov, *The Micro-Price: A High Frequency Estimator of Future Prices*
