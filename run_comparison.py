"""
DEPRECATED - kept as a thin wrapper.

The old implementation read a non-existent `orderbook_fill_cache.json` (silently falling back to
100%-fill mode) and would have crashed on the `min_up_bid` key even with a cache. All order-book
replay logic now lives in lib/orderbook_replay.py and run_simulation.py.

    uv run python run_comparison.py [any run_simulation.py args]
==  uv run python run_simulation.py --compare [args]
"""
import sys

import run_simulation

if __name__ == "__main__":
    if "--compare" not in sys.argv:
        sys.argv.append("--compare")
    run_simulation.main()
