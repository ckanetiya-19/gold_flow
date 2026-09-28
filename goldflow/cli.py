"""
Command line entry point.

    python -m goldflow selftest              # verify the install end to end
    python -m goldflow validate              # run the Part 10 plan on synthetic data
    python -m goldflow validate --fut a.csv --spot b.csv
    python -m goldflow backtest --entry-z 2.0
    python -m goldflow live --dry-run        # engine loop against synthetic feeds
"""

from __future__ import annotations

import argparse
import json
import sys

from .feeds.base import NS_PER_MS, NS_PER_S, Quote


def _load(path: str, symbol: str, colmap_name: str):
    from .feeds import tabular

    colmap = {
        "databento": tabular.DATABENTO_MBP1,
        "polygon": tabular.POLYGON_QUOTES,
        "generic": tabular.GENERIC_TICK,
    }[colmap_name]
    feed = tabular.TabularFeed(path, colmap, symbol=symbol)
    return list(feed.stream())


def cmd_selftest(args) -> int:
    from .feeds import make_pair, SynthConfig
    from .core import (
        OFIBucketer, fit_ofi, classify_stream, DeltaBarBuilder, CVD,
        VolumeProfile, fit_kyle, fit_microprice, evaluate_predictors, VPIN,
    )
    from .research import Backtester, BacktestConfig, flat_strategy, ofi_threshold_strategy

    print("goldflow selftest\n" + "=" * 60)
    cfg = SynthConfig(n_events=120_000, seed=11)
    fut, spot = make_pair(cfg)
    quotes = [e for e in fut if isinstance(e, Quote)]
    print(f"  synthetic: {len(fut)} futures events "
          f"({len(quotes)} quotes), {len(spot)} spot quotes")

    fails: list[str] = []

    # OFI
    buckets = list(OFIBucketer(NS_PER_S).run(quotes))
    fit = fit_ofi(buckets)
    lam = "n/a" if fit.lambda_hat is None else f"{fit.lambda_hat:.3f}"
    print(f"\n  OFI      buckets={fit.n}  contemporaneous R2={fit.r2:.4f}  "
          f"t={fit.t_stat:+.0f}  lambda={lam}")
    print(f"           predictive R2={fit.r2_predictive:.5f}  "
          f"t={fit.t_predictive:+.1f}   <- the one that can be traded")
    if fit.r2 <= 0.01 or fit.beta <= 0:
        fails.append("OFI regression found no relationship on synthetic data")

    # classification + delta
    trades = list(classify_stream(fut))
    bars = list(DeltaBarBuilder(NS_PER_S).run(trades))
    cvd = CVD()
    for b in bars:
        cvd.update(b)
    print(f"  delta    {len(bars)} bars  final CVD={cvd.value:+d}")
    if not bars:
        fails.append("no delta bars produced")

    # volume profile
    vp = VolumeProfile(tick=0.10).extend(trades)
    s = vp.summary()
    print(f"  profile  POC={s['poc']}  VA=[{s['val']}, {s['vah']}]  "
          f"vol={s['total_volume']}")
    if s["poc"] is None:
        fails.append("volume profile empty")

    # VPIN
    total_vol = sum(t.size for t in trades)
    v = VPIN(bucket_volume=max(total_vol / 400, 50), window=50)
    last = None
    for t in trades:
        r = v.update(t)
        if r is not None:
            last = r
    print(f"  VPIN     last={last if last is None else round(last, 4)}  "
          f"pct={v.percentile()}")

    # Kyle
    k = fit_kyle(trades, window_ns=NS_PER_S)
    print(f"  Kyle     {k}")

    # micro-price
    try:
        mp = fit_microprice(quotes, tick=0.10, n_imb=8, n_spread=2)
        ev = evaluate_predictors(quotes, mp, horizon=50)
        print(f"  micro    converged={ev['converged']} iters={ev['iterations']}  "
              f"vs mid {ev['improvement_vs_mid_pct']:+.1f}%  "
              f"vs wmid {ev['improvement_vs_wmid_pct']:+.1f}%")
    except Exception as exc:                            # noqa: BLE001
        print(f"  micro    SKIPPED: {exc}")

    # backtest accounting control
    bt = Backtester(BacktestConfig())
    ctrl = bt.run(quotes, spot, flat_strategy())
    print(f"\n  control  net={ctrl.net_pnl:.10f} (must be exactly 0)")
    if abs(ctrl.net_pnl) > 1e-9:
        fails.append(f"flat strategy has non-zero PnL: {ctrl.net_pnl}")

    res = bt.run(quotes, spot, ofi_threshold_strategy(entry_z=2.0))
    print(f"\n{res}")
    probe = bt.lookahead_probe(quotes, spot, ofi_threshold_strategy(entry_z=2.0))
    print(f"\n  look-ahead probe: {probe['verdict']}")
    print(f"    base={probe['base_net']}  delayed={probe['delayed_net']}  "
          f"delta={probe['delta']}")

    print("\n" + "=" * 60)
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        return 1
    print("  all checks passed")
    return 0


def cmd_validate(args) -> int:
    from .research import run_all

    if args.fut and args.spot:
        fut = _load(args.fut, args.fut_symbol, args.colmap)
        spot = [e for e in _load(args.spot, args.spot_symbol, args.colmap)
                if isinstance(e, Quote)]
        print(f"loaded {len(fut)} futures events, {len(spot)} spot quotes")
    else:
        from .feeds import make_pair, SynthConfig
        print("no files given -- running on SYNTHETIC data. "
              "Results say the code works, not that the market does.\n")
        fut, spot = make_pair(SynthConfig(n_events=args.n_events))

    res = run_all(fut, spot, your_latency_ms=args.latency_ms)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(res, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0 if res.get("stopped_at") is None else 2


def cmd_backtest(args) -> int:
    from .feeds import make_pair, SynthConfig
    from .research import Backtester, BacktestConfig, ofi_threshold_strategy

    if args.fut and args.spot:
        fut = [e for e in _load(args.fut, args.fut_symbol, args.colmap)
               if isinstance(e, Quote)]
        spot = [e for e in _load(args.spot, args.spot_symbol, args.colmap)
                if isinstance(e, Quote)]
    else:
        f, spot = make_pair(SynthConfig(n_events=args.n_events))
        fut = [e for e in f if isinstance(e, Quote)]

    cfg = BacktestConfig(
        bucket_ns=int(args.bucket_ms * NS_PER_MS),
        signal_delay_ns=int(args.delay_ms * NS_PER_MS),
    )
    bt = Backtester(cfg)
    res = bt.run(fut, spot, ofi_threshold_strategy(entry_z=args.entry_z))
    print(res)
    print("\nlook-ahead probe:",
          bt.lookahead_probe(fut, spot, ofi_threshold_strategy(entry_z=args.entry_z)))
    return 0


def cmd_live(args) -> int:
    from .feeds import SynthConfig, SyntheticBook, SyntheticSpot
    from .live import EngineConfig, SignalEngine, PaperBroker, Order
    from .research import ofi_threshold_strategy

    if not args.dry_run:
        print("live mode without --dry-run needs a configured feed adapter; "
              "see goldflow/feeds/databento_feed.py", file=sys.stderr)
        return 1

    cfg = SynthConfig(n_events=args.n_events)
    book = SyntheticBook(cfg)
    fut_events = list(book.stream())
    fut_quotes = [e for e in fut_events if isinstance(e, Quote)]
    spot = SyntheticSpot(fut_quotes, cfg)

    eng = SignalEngine(EngineConfig(), strategy=ofi_threshold_strategy())
    broker = PaperBroker()

    spot_iter = iter(spot.stream())
    n_pkt = n_halt = n_ord = 0
    for ev in fut_events:
        if not isinstance(ev, Quote):
            eng.on_trade(ev)
            continue
        try:
            sq = next(spot_iter)
            eng.on_spot_quote(sq)
            broker.on_quote(sq)
        except StopIteration:
            break
        pkt = eng.on_fut_quote(ev)
        if pkt is None:
            continue
        n_pkt += 1
        if pkt.halt:
            n_halt += 1
            if abs(eng.position) > 1e-9:
                broker.flatten("XAUUSD")
                eng.position = 0.0
            continue
        if abs(pkt.target - eng.position) > 1e-9:
            broker.submit(Order(pkt.ts_ns, pkt.target - eng.position, "XAUUSD"))
            eng.position = pkt.target
            n_ord += 1
        if args.verbose and n_pkt % 500 == 0:
            print(pkt.to_json())

    broker.flatten("XAUUSD")
    ok = sum(1 for e in broker.executions if e.ok)
    print(f"\npackets={n_pkt}  halted={n_halt}  orders={n_ord}  "
          f"filled={ok}  rejected={len(broker.executions) - ok}")
    print(f"paper realised PnL: {broker.realised_pnl:+.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("goldflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_data_args(sp):
        sp.add_argument("--fut", help="futures CSV/parquet")
        sp.add_argument("--spot", help="spot CSV/parquet")
        sp.add_argument("--fut-symbol", default="GC")
        sp.add_argument("--spot-symbol", default="XAUUSD")
        sp.add_argument("--colmap", default="databento",
                        choices=["databento", "polygon", "generic"])
        sp.add_argument("--n-events", type=int, default=150_000)

    s = sub.add_parser("selftest", help="verify the install end to end")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("validate", help="run the validation plan")
    add_data_args(s)
    s.add_argument("--latency-ms", type=float, default=50.0)
    s.add_argument("--json-out")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("backtest", help="run the OFI baseline strategy")
    add_data_args(s)
    s.add_argument("--entry-z", type=float, default=2.0)
    s.add_argument("--bucket-ms", type=float, default=1000.0)
    s.add_argument("--delay-ms", type=float, default=250.0)
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("live", help="run the engine loop")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--n-events", type=int, default=150_000)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_live)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
