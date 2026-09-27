"""Put the repo root on sys.path so `sleeper_auction` imports from any cwd."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def text(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def rows(name):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(text(name))))


def jload(name):
    import json
    return json.loads(text(name))
