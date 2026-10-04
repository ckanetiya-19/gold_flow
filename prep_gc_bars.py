"""
LOCAL ONLY (no API, no cost): build 1-minute bars with REAL order flow from
gc_trades_3mo.dbn ONCE and cache them, so strategy iteration is fast (no
re-processing 5.6M trades each run).

Caches:
  gc_bars.pkl  - list of 1-min bars (o/h/l/c, real buy_vol/sell_vol, delta,
                 cvd, footprint levels, next_tick index)
  gc_ticks.npz - ts[] and px[] full tick path (for tick-level exit sim)

Aggressor mapping validated earlier: side 'A' = BUY, 'B' = SELL.
Run: python prep_gc_bars.py
"""
import pickle
import numpy as np
import databento as db

DBN_FILE = "gc_trades_3mo.dbn"


def main():
    print("Loading local GC trades (no API, no cost)...")
    store = db.DBNStore.from_file(DBN_FILE)
    df = store.to_df()
    df = df[df["side"].isin(["A", "B"])]
    ts = (df.index.astype("int64") // 10**9).to_numpy().astype(np.float64)
    px = df["price"].to_numpy().astype(np.float64)
    vol = df["size"].to_numpy().astype(np.float64)
    buy = (df["side"].to_numpy() == "A")
    print(f"  {len(ts):,} trades | {(ts[-1]-ts[0])/86400:.1f} days")

    # build 1-min bars with real order flow (same structure the backtest engine expects)
    bars = []
    cur = None
    cum_pv = cum_vol = cum_delta = 0.0
    for i in range(len(ts)):
        minute = int(ts[i] // 60) * 60
        p, v, b = px[i], vol[i], buy[i]
        cum_pv += p * v
        cum_vol += v
        cum_delta += v if b else -v
        vwap = cum_pv / cum_vol if cum_vol > 0 else p
        lvl = round(p / 0.1) * 0.1
        if cur is None or cur["t"] < minute:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": minute, "open": p, "high": p, "low": p, "close": p, "volume": v,
                   "buy_vol": v if b else 0.0, "sell_vol": 0.0 if b else v,
                   "vwap": vwap, "cvd": cum_delta, "last_tick": i,
                   "levels_buy": {}, "levels_sell": {}}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
            if b:
                cur["buy_vol"] += v
            else:
                cur["sell_vol"] += v
            cur["vwap"] = vwap
            cur["cvd"] = cum_delta
            cur["last_tick"] = i
        bucket = cur["levels_buy"] if b else cur["levels_sell"]
        bucket[lvl] = bucket.get(lvl, 0.0) + v
    for b_ in bars:
        b_["delta"] = b_["buy_vol"] - b_["sell_vol"]

    print(f"  {len(bars):,} one-minute bars built.")
    with open("gc_bars.pkl", "wb") as f:
        pickle.dump(bars, f)
    np.savez_compressed("gc_ticks.npz", ts=ts, px=px)
    print("Cached: gc_bars.pkl + gc_ticks.npz")


if __name__ == "__main__":
    main()
