"""Background refresh for the slow feeds. Never runs on the request path.

A daemon thread started by board.main wakes every 10 minutes and refreshes
the projection scrapers and FantasyPros weekly ECR for the current NFL week
(and the next one early in the week) when they are due:

    Sunday before 1 pm ET      hourly      (inactives, late news)
    Tuesday noon -> kickoff    every 3 h   (sites publish and revise)
    otherwise                  every 12 h

Everything goes through feeds.common, so the cache is the contract: pages
read it with common.cache_only() and show what is there. A lock stops a
manual kick() from double-fetching. The last-run times persist in
cache/prefetch-state.json so a restart does not re-scrape everything.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

from sleeper_auction.feeds import common

TICK = 600
_LOCK = threading.Lock()
_KICKED = {}
KICK_EVERY = 600
_STARTED = {"thread": None}
STATE = "prefetch-state.json"


def _et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


def interval(now=None):
    """Seconds between refreshes at this moment (ET)."""
    now = now or _et_now()
    wd, h = now.weekday(), now.hour            # Mon 0 .. Sun 6
    if wd == 6 and h < 13:
        return 3600
    if (wd == 1 and h >= 12) or wd in (2, 3, 4, 5, 6) or (wd == 0 and h < 23):
        return 3 * 3600
    return 12 * 3600


def _load():
    try:
        with open(common.cache_path(STATE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    os.makedirs(common.CACHE, exist_ok=True)
    with open(common.cache_path(STATE), "w", encoding="utf-8") as f:
        json.dump(st, f)


def current_week():
    try:
        from sleeper_auction import sleeper
        st = sleeper.nfl_state() or {}
        return str(st.get("season") or "2026"), int(st.get("week") or 1)
    except Exception:
        return "2026", 1


def refresh(season, week, force=False):
    """Fetch every slow source for one week. Returns False if already running."""
    if not _LOCK.acquire(blocking=False):
        return False
    try:
        from sleeper_auction.feeds import projections
        ttl = 0 if force else interval()
        # Adapters take their own TTL, so run them with the refresh interval:
        # a page fetched inside it is reused, anything older is re-scraped.
        for ad in projections.ADAPTERS:
            if ad.NAME == "sleeper" or not common.enabled("projections.%s" % ad.NAME):
                continue
            t0 = time.time()
            try:
                rows = ad.fetch_week(season, week, ttl=ttl)
                common.ok("projections.%s" % ad.NAME, rows=len(rows), week=int(week),
                          fetch_s=round(time.time() - t0, 1))
            except Exception as e:
                common.fail("projections.%s" % ad.NAME, e)
        if common.enabled("fantasypros.week"):
            from sleeper_auction.sources import fantasypros
            for i, pos in enumerate(fantasypros.WEEK_PAGES):
                try:
                    age = common.cache_age("fp-week-ecr-%s" % pos)
                    if i and (age is None or age >= ttl):
                        time.sleep(5)            # FantasyPros robots.txt crawl delay
                    fantasypros.fetch_week_ecr(pos, ttl=ttl)
                    common.ok("fantasypros.week", week=int(week))
                except Exception as e:
                    common.fail("fantasypros.week", e)
        st = _load()
        st["%s-%s" % (season, week)] = int(time.time())
        _save(st)
        return True
    finally:
        _LOCK.release()


ROS_EVERY = 12 * 3600


def refresh_ros(season, from_week):
    """Rest-of-season inputs: Sleeper future weeks, FanDuel REMAINING,
    FantasyPros ROS ECR. Twice a day is plenty; they move slowly."""
    from sleeper_auction.feeds import values
    from sleeper_auction.feeds.projections import fanduel
    for name, fn in (("values.ros", lambda: values.ros_points(season, from_week,
                                                              cache_only=False)),
                     ("projections.fanduel", lambda: fanduel.fetch_remaining()),
                     ("fantasypros.ros", lambda: values.fp_ros(cache_only=False))):
        try:
            fn()
        except Exception as e:
            common.fail(name, e)
    st = _load()
    st["ros-%s" % season] = int(time.time())
    _save(st)


def kick_ros(season, from_week):
    k = "ros-%s-%s" % (season, from_week)
    if time.time() - _KICKED.get(k, 0) < KICK_EVERY:
        return
    _KICKED[k] = time.time()
    threading.Thread(target=refresh_ros, args=(season, from_week), daemon=True).start()


def due(season, week, now_ts=None):
    last = _load().get("%s-%s" % (season, week))
    return not last or (now_ts or time.time()) - last >= interval()


def tick():
    season, week = current_week()
    weeks = [week]
    if _et_now().weekday() in (0, 1) and week < 18:
        weeks.append(week + 1)
    for w in weeks:
        if due(season, w):
            refresh(season, w)
    if time.time() - _load().get("ros-%s" % season, 0) >= ROS_EVERY:
        refresh_ros(season, week + 1)




def kick(season, week):
    """Non-blocking refresh of one week because a page found a source missing
    from the cache. Runs even when the schedule says it is not due (the cache
    may have been cleared, or this is a week the schedule does not cover),
    but at most once per KICK_EVERY per week."""
    k = "%s-%s" % (season, week)
    if time.time() - _KICKED.get(k, 0) < KICK_EVERY:
        return
    _KICKED[k] = time.time()
    threading.Thread(target=refresh, args=(season, week), daemon=True).start()


def _loop():
    time.sleep(5)
    while True:
        try:
            tick()
        except Exception as e:
            common.fail("prefetch", e)
        time.sleep(TICK)


def start():
    """Start the daemon once per process (board.main). PREFETCH=0 disables it."""
    if os.environ.get("PREFETCH", "1") == "0" or _STARTED["thread"]:
        return
    t = threading.Thread(target=_loop, name="feeds-prefetch", daemon=True)
    _STARTED["thread"] = t
    t.start()
