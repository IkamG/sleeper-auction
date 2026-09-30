"""Clear the on-disk and in-memory caches without restarting the server.

What is KEPT by default, and why:
  * cache/ai/               AI analyses are cached permanently on purpose; each
                            one is a paid model call. Cleared only with ai=True.
  * cache/history/, calibration.json
                            the calibration record. It cannot be re-fetched:
                            it is built week by week on this machine.
  * oddsapi-quota.json, oddsapi-ev-*
                            The Odds API responses cost credits (~90 for a
                            full slate of a 500-a-month plan). Cleared only
                            with odds=True; the quota file is always kept so
                            the reserve guard keeps working.
Everything else (projections, stats, news, lines, weather, rosters, the draft
pool's sources) is re-fetched on demand, and the prefetcher is kicked so the
slow scrapers refill in the background.
"""
import os
import shutil
import threading

from sleeper_auction.feeds import common

KEEP_DIRS = ("history",)
KEEP_FILES = ("calibration.json", "oddsapi-quota.json")
_LOCK = threading.Lock()


def _dirs():
    root = common.CACHE
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    return [d for d in (root, pkg) if os.path.isdir(d)]


def clear(ai=False, odds=False):
    """Delete cached data. Returns {"files": n, "bytes": b, "kept": [...]}."""
    n = size = 0
    kept = set()
    with _LOCK:
        for d in _dirs():
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if name in KEEP_DIRS or name in KEEP_FILES \
                        or (name == "ai" and not ai) \
                        or (name.startswith("oddsapi-ev-") and not odds):
                    kept.add(name if not name.startswith("oddsapi-ev-") else "oddsapi-ev-*")
                    continue
                try:
                    if os.path.isdir(p):
                        for root, _, files in os.walk(p):
                            for f in files:
                                size += os.path.getsize(os.path.join(root, f))
                                n += 1
                        shutil.rmtree(p)
                    else:
                        size += os.path.getsize(p)
                        os.remove(p)
                        n += 1
                except OSError:
                    continue
        _reset_memory(ai)
    return {"files": n, "bytes": size, "kept": sorted(kept)}


def _reset_memory(ai):
    """Drop in-process copies so the next request re-reads fresh data."""
    from sleeper_auction import board
    with board.POOL["lock"]:
        board.POOL["players"] = None
        board.POOL["meta"] = {}
    with board.JOBS["lock"]:
        board.JOBS["items"].clear()
    from sleeper_auction.feeds import ids, nflverse
    ids._MEMO.update(players=None, players_mtime=None, cw=None, cw_at=0)
    with nflverse._MLOCK:
        nflverse._MEMO.clear()
    try:
        from sleeper_auction.feeds import props
        props._MEMO.update(at=0, key=None, val=None)
    except Exception:
        pass
    with common._SLOCK:
        common.STATUS.clear()


def refill():
    """Kick the background refresh of the slow feeds for the current week."""
    from sleeper_auction.feeds import prefetch
    season, week = prefetch.current_week()
    prefetch._KICKED.clear()
    prefetch.kick(season, week)
    prefetch.kick_ros(season, week + 1)
