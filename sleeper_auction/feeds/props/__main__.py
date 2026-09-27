"""python3 -m sleeper_auction.feeds.props --probe --week N"""
import sys

from sleeper_auction.feeds.props import __doc__ as _doc, _probe

if "--probe" in sys.argv:
    _probe(int(sys.argv[sys.argv.index("--week") + 1]) if "--week" in sys.argv else 1)
else:
    print(_doc)
