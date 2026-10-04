"""
goldflow -- order flow research and execution toolkit for XAU/USD.

Architecture, in one line: READ COMEX GC, EXECUTE SPOT.

Spot gold is bilateral OTC. There is no consolidated tape, no exchange volume
and no public book, so order flow cannot be computed on it -- what a retail
platform calls "volume" on XAUUSD is a count of quote updates. Meanwhile
formal price-discovery work puts COMEX futures at 67%-94% of gold price
discovery (Hasbrouck information share, 1997-2014), with the noise-corrected
leadership share near 70%, despite trading a fraction of London's volume.

So the signal comes from a venue with a real tape, and the order goes to
whichever venue you can actually trade. This package implements that split and,
more importantly, implements the tests that decide whether it works for YOU --
at your latency, with your spread.

    from goldflow.feeds import make_pair
    from goldflow.research import run_all

    fut, spot = make_pair()
    run_all(fut, spot, your_latency_ms=50)

Run the validation plan before building anything on top. The experiments are
ordered so the cheapest one that could kill the project runs first.
"""

__version__ = "0.1.0"

from . import config, core, feeds, live, research

__all__ = ["config", "core", "feeds", "live", "research", "__version__"]
