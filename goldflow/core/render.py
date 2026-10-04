"""
Footprint and profile rendering -- the bid x ask ladder, as a terminal display.

Produces the same view the commercial platforms sell: volume-at-price split by
aggressor side, with imbalances highlighted and POC / VAH / VAL marked.

THE REASON THIS MODULE EXISTS is not to draw pretty ladders. It is
`compare_real_vs_tick()` at the bottom of this file.

A footprint built from real exchange data and a footprint built from a spot
CFD tick stream LOOK IDENTICAL. Same grid, same numbers, same colours, same
imbalance highlights. Nothing on the screen tells you which one you are
looking at. But one is a measurement of traded size and aggressor side, and
the other is a count of quote updates classified by the tick rule -- a
transformed price series wearing order-flow clothing.

`compare_real_vs_tick` renders both from the same underlying market so the
difference is visible as numbers rather than as an argument, and
`footprint_provenance` gives you the one arithmetic test that separates them
on data you did not generate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from ..feeds.base import Quote, Side, Trade
from .delta import Footprint
from .profile import VolumeProfile


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_footprint(fp: Footprint, profile: VolumeProfile | None = None,
                     imbalance_ratio: float = 3.0, min_vol: int = 5,
                     max_levels: int = 40, bar_width: int = 18) -> str:
    """Render one footprint bar as a bid x ask ladder.

    Columns: [imbalance flag] bid_vol x ask_vol [delta bar] [profile marker]

    The imbalance flag uses the DIAGONAL comparison every vendor implements
    slightly differently: ask volume at price p against bid volume at p-1 tick.
    The 3:1 default is convention, not a finding -- ATAS, Sierra and Quantower
    do not agree on the rule, which is why a pattern calibrated on one
    platform's rendering does not transfer to another's.
    """
    levels = fp.levels()
    if not levels:
        return "(empty footprint)"

    levels = sorted(levels, reverse=True)[:max_levels]
    imb = {p: (s, r) for p, s, r in
           fp.diagonal_imbalances(ratio=imbalance_ratio, min_vol=min_vol)}

    poc = fp.poc()
    val = vah = None
    if profile is not None:
        va = profile.value_area()
        if va:
            val, vah = va

    peak = max((fp.bid_vol.get(p, 0) + fp.ask_vol.get(p, 0)) for p in levels) or 1

    out = [f"{'flag':>5} {'bid':>7} x {'ask':<7} {'delta':<{bar_width}} level"]
    out.append("-" * (5 + 7 + 3 + 7 + 1 + bar_width + 8))

    for p in levels:
        b = fp.bid_vol.get(p, 0)
        a = fp.ask_vol.get(p, 0)
        d = a - b

        flag = ""
        if p in imb:
            side, ratio = imb[p]
            flag = "BUY" if side == "buy" else "SELL"

        # delta bar, centred
        half = bar_width // 2
        n = int(round(abs(d) / peak * half))
        if d >= 0:
            bar = " " * half + "+" * n
        else:
            bar = " " * (half - n) + "-" * n
        bar = bar[:bar_width].ljust(bar_width)

        mark = ""
        if poc is not None and abs(p - poc) < 1e-9:
            mark = "<-- POC"
        elif vah is not None and abs(p - vah) < 1e-9:
            mark = "<-- VAH"
        elif val is not None and abs(p - val) < 1e-9:
            mark = "<-- VAL"

        out.append(f"{flag:>5} {b:>7} x {a:<7} {bar} {p:>9.2f} {mark}")

    tot_b = sum(fp.bid_vol.values())
    tot_a = sum(fp.ask_vol.values())
    out.append("-" * (5 + 7 + 3 + 7 + 1 + bar_width + 8))
    out.append(f"{'':>5} {tot_b:>7} x {tot_a:<7} "
               f"delta {tot_a - tot_b:+d}   volume {tot_a + tot_b}")
    return "\n".join(out)


def render_profile(profile: VolumeProfile, width: int = 46,
                   max_levels: int = 40) -> str:
    """Horizontal volume-at-price histogram with POC / VAH / VAL marked."""
    px, vol = profile.as_arrays()
    if px.size == 0:
        return "(empty profile)"

    order = np.argsort(px)[::-1][:max_levels]
    px, vol = px[order], vol[order]
    peak = int(vol.max()) or 1

    poc = profile.poc()
    va = profile.value_area()
    val, vah = va if va else (None, None)

    out = []
    for p, v in zip(px, vol):
        n = int(round(v / peak * width))
        mark = ""
        if poc is not None and abs(p - poc) < 1e-9:
            mark = " POC"
        elif vah is not None and abs(p - vah) < 1e-9:
            mark = " VAH"
        elif val is not None and abs(p - val) < 1e-9:
            mark = " VAL"
        inside = (val is not None and val <= p <= vah)
        ch = "#" if inside else "."
        out.append(f"{p:>9.2f} |{ch * n:<{width}} {int(v):>7}{mark}")

    s = profile.summary()
    out.append("")
    out.append(f"POC {s['poc']}   value area [{s['val']}, {s['vah']}]   "
               f"volume {s['total_volume']}   levels {s['levels']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The part that matters
# ---------------------------------------------------------------------------

def footprint_from_trades(trades: Iterable[Trade], tick: float,
                          ts_start: int = 0, ts_end: int = 0
                          ) -> tuple[Footprint, VolumeProfile]:
    """Real footprint: traded SIZE, split by the exchange's aggressor flag."""
    fp = Footprint(ts_start, ts_end, tick=tick)
    vp = VolumeProfile(tick=tick)
    for t in trades:
        fp.add(t)
        vp.add(t)
    return fp, vp


def footprint_from_quotes(quotes: Sequence[Quote], tick: float,
                          ts_start: int = 0, ts_end: int = 0
                          ) -> tuple[Footprint, VolumeProfile]:
    """Tick-derived 'footprint': what a spot CFD platform actually draws.

    There is no trade size and no aggressor flag, so the platform:
      1. treats every QUOTE CHANGE as one unit of 'volume', and
      2. assigns it a side with the tick rule -- mid up = buy, mid down = sell.

    The output is structurally a footprint and semantically a signed count of
    upticks. Every cell in it is a tick count, not contracts. It will still
    render, still show imbalances, still produce a POC.
    """
    fp = Footprint(ts_start, ts_end, tick=tick)
    vp = VolumeProfile(tick=tick)
    prev: tuple[float, float] | None = None
    prev_mid: float | None = None
    last_side = Side.ASK

    for q in quotes:
        if q.is_crossed():
            continue
        cur = (q.bid_px, q.ask_px)
        # A tick feed publishes on a PRICE change. Counting every book event
        # instead -- including size-only updates -- would overstate the tick
        # count and break the provenance arithmetic below, which is the whole
        # reason this function exists.
        if prev is not None and cur != prev:
            mid = q.mid
            if prev_mid is not None:
                if mid > prev_mid:
                    last_side = Side.ASK
                elif mid < prev_mid:
                    last_side = Side.BID
                # unchanged -> carry the previous side, as the tick rule does
            synthetic = Trade(q.ts_event, q.ts_recv, mid, 1, last_side, q.symbol)
            fp.add(synthetic)
            vp.add(synthetic)
            prev_mid = mid
        elif prev is None:
            prev_mid = q.mid
        prev = cur
    return fp, vp


@dataclass
class ProvenanceResult:
    total_footprint_units: int
    total_real_volume: int | None
    n_quote_changes: int
    ratio_to_volume: float | None
    ratio_to_quotes: float
    verdict: str

    def __str__(self) -> str:
        rv = "n/a" if self.total_real_volume is None else str(self.total_real_volume)
        rr = "n/a" if self.ratio_to_volume is None else f"{self.ratio_to_volume:.3f}"
        return (
            f"Footprint provenance\n"
            f"  units in footprint        {self.total_footprint_units}\n"
            f"  exchange traded volume    {rv}      ratio {rr}\n"
            f"  quote changes in window   {self.n_quote_changes}      "
            f"ratio {self.ratio_to_quotes:.3f}\n"
            f"  -> {self.verdict}"
        )


def footprint_provenance(fp: Footprint, quotes: Sequence[Quote],
                         trades: Sequence[Trade] | None = None
                         ) -> ProvenanceResult:
    """The one arithmetic test that tells you what you are looking at.

    Sum every cell in the footprint. Then compare:

      * against the venue's reported traded volume for the same window --
        a REAL footprint matches it exactly;
      * against the number of quote CHANGES in the window -- a tick-derived
        footprint matches that instead, usually to within a percent.

    Run this before trusting any footprint on any platform, including one you
    are shown in a screenshot. If the platform cannot give you a traded-volume
    figure to compare against, that absence is itself the answer.
    """
    units = int(sum(fp.bid_vol.values()) + sum(fp.ask_vol.values()))

    changes = 0
    prev: tuple[float, float] | None = None
    for q in quotes:
        cur = (q.bid_px, q.ask_px)
        if prev is not None and cur != prev:
            changes += 1
        prev = cur

    real_vol = int(sum(t.size for t in trades)) if trades else None
    r_vol = (units / real_vol) if real_vol else None
    r_q = units / changes if changes else float("inf")

    if r_vol is not None and abs(r_vol - 1.0) < 0.02:
        verdict = ("REAL. Footprint units match exchange traded volume. "
                   "These are contracts.")
    elif abs(r_q - 1.0) < 0.05:
        verdict = ("TICK-DERIVED. Footprint units match the count of quote "
                   "changes, not traded size. These are ticks wearing a "
                   "footprint's clothes -- the 'delta' is a signed uptick "
                   "count and the imbalances are price-path artefacts.")
    else:
        verdict = ("inconclusive -- neither total matches. Check that the "
                   "window and symbol line up before concluding anything.")

    return ProvenanceResult(units, real_vol, changes, r_vol, r_q, verdict)


def compare_real_vs_tick(trades: Sequence[Trade], quotes: Sequence[Quote],
                         tick: float, max_levels: int = 14) -> str:
    """Render both footprints for the same window, side by side.

    This is the demonstration. Both ladders are legitimate output from working
    code. Only one of them measures order flow.
    """
    fp_r, vp_r = footprint_from_trades(trades, tick)
    fp_t, vp_t = footprint_from_quotes(quotes, tick)

    left = render_footprint(fp_r, vp_r, max_levels=max_levels).splitlines()
    right = render_footprint(fp_t, vp_t, max_levels=max_levels).splitlines()

    w = max(len(l) for l in left) + 4
    head = (f"{'REAL (exchange trades, aggressor flag)':<{w}}"
            f"{'TICK-DERIVED (quote changes, tick rule)'}")
    rows = [head, "=" * (w + 44)]
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        rows.append(f"{l:<{w}}{r}")
    return "\n".join(rows)
