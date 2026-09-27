"""ID crosswalk: any external player id (or, last resort, a name) -> Sleeper id.

Python 3.9, stdlib only.

Sources, in priority order:
  1. Sleeper's player DB (/v1/players/nfl, cache key `slp-players`, 24 h).
  2. DynastyProcess db_playerids.csv, which fills the gaps and is the only
     source of pfr_id (snap counts), cbs_id, nfl_id, fantasypros_id and ktc_id.
On conflict Sleeper wins.

Verified 2026-09-27 (probe on this machine):
  * Sleeper DB: 12,229 records. It carries gsis_id, espn_id, yahoo_id,
    rotowire_id, sportradar_id, stats_id, fantasy_data_id, swish_id, opta_id,
    pandascore_id, oddsjam_id, kalshi_id, plus practice_participation,
    practice_description, injury_status, news_updated (epoch ms).
  * TRAP: Sleeper's own gsis_id/espn_id are often null for current starters
    (Trey McBride: both null). DynastyProcess has them (gsis 00-0037744,
    espn 4361307), so the DP fill is not optional.
  * TRAP: 866 Sleeper gsis_id values carry a leading space. Every id is
    normalised to a stripped str.
  * DP: 12,508 rows; missing values are the literal string "NA".
  (run `python3 -m sleeper_auction.feeds.ids --probe` for current counts)

Name fallback uses board.pkey (so NAME_FIXES apply) and drops ambiguous keys
(two active players with one key) instead of guessing. Every lookup counts
hit / name_fallback / miss per kind for /api/feeds.
"""
import os
import sys
import threading

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

from sleeper_auction.feeds import common

SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
DP_URL = ("https://raw.githubusercontent.com/dynastyprocess/data/master/files/"
          "db_playerids.csv")
FEED = "ids"

# kind -> (sleeper field, dynastyprocess column)
KINDS = {
    "gsis": ("gsis_id", "gsis_id"),
    "espn": ("espn_id", "espn_id"),
    "yahoo": ("yahoo_id", "yahoo_id"),
    "pfr": (None, "pfr_id"),
    "fantasypros": (None, "fantasypros_id"),
    "cbs": (None, "cbs_id"),
    "nfl": (None, "nfl_id"),
    "rotowire": ("rotowire_id", "rotowire_id"),
    "sportradar": ("sportradar_id", "sportradar_id"),
    "kalshi": ("kalshi_id", None),
    "ktc": (None, "ktc_id"),
    "fantasy_data": ("fantasy_data_id", "fantasy_data_id"),
}
SKILL = ("QB", "RB", "WR", "TE", "K", "DEF")

_MEMO = {"players": None, "players_mtime": None, "cw": None, "cw_at": 0}
_LOCK = threading.Lock()
COUNTS = {}


def players():
    """Parsed Sleeper players DB, memoised on the cache file's mtime (15 MB)."""
    with _LOCK:
        try:
            txt = common.fetch_text(SLEEPER_PLAYERS, "slp-players", 86400, feed="sleeper.players")
        except Exception as e:
            common.fail("sleeper.players", e)
            return _MEMO["players"] or {}
        m = os.path.getmtime(common.cache_path("slp-players"))
        if _MEMO["players"] is None or m != _MEMO["players_mtime"]:
            import json
            _MEMO["players"] = json.loads(txt)
            _MEMO["players_mtime"] = m
            common.ok("sleeper.players", rows=len(_MEMO["players"]))
        return _MEMO["players"]


def dp_rows():
    try:
        rows = common.fetch_csv([DP_URL], "dp-playerids.csv", 86400, feed="dynastyprocess")
        common.ok("dynastyprocess", rows=len(rows))
        return rows
    except Exception as e:
        common.fail("dynastyprocess", e)
        return []


def _pkey(name, pos, team=None):
    from sleeper_auction import board
    return board.pkey(name, pos, team)


def build(slp=None, dp=None):
    """Pure builder (tests pass fixtures). Returns the crosswalk dict."""
    slp = slp if slp is not None else players()
    dp = dp if dp is not None else dp_rows()
    by = {k: {} for k in KINDS}
    info = {}
    names = {}
    for sid, p in slp.items():
        sid = common.sid_str(sid)
        if not sid:
            continue
        pos = p.get("position") or ""
        info[sid] = {"name": p.get("full_name") or ("%s %s" % (p.get("first_name") or "",
                                                               p.get("last_name") or "")).strip(),
                     "pos": pos, "team": p.get("team"), "active": bool(p.get("active"))}
        for kind, (sf, _) in KINDS.items():
            v = common.sid_str(p.get(sf)) if sf else None
            if v:
                by[kind].setdefault(v, sid)
        if pos in SKILL and info[sid]["name"]:
            k = _pkey(info[sid]["name"], pos, p.get("team"))
            names.setdefault(k, []).append((sid, info[sid]["active"]))
    for r in dp:
        sid = common.sid_str(r.get("sleeper_id"))
        if not sid:
            continue
        for kind, (_, dc) in KINDS.items():
            v = common.sid_str(r.get(dc)) if dc else None
            if v and v not in by[kind]:
                by[kind][v] = sid
        if sid not in info and r.get("name"):
            info[sid] = {"name": r["name"], "pos": r.get("position"), "team": r.get("team"),
                         "active": False}
    by_name = {}
    for k, lst in names.items():
        act = [s for s, a in lst if a]
        pick = act if act else lst and [s for s, _ in lst]
        if len(pick) == 1:
            by_name[k] = pick[0]
    return {"by": by, "by_name": by_name, "info": info}


def crosswalk(max_age=3600):
    import time
    with _LOCK:
        if _MEMO["cw"] is not None and time.time() - _MEMO["cw_at"] < max_age:
            return _MEMO["cw"]
    cw = build()
    with _LOCK:
        _MEMO["cw"], _MEMO["cw_at"] = cw, time.time()
    common.ok(FEED, rows=len(cw["info"]),
              sizes={k: len(v) for k, v in cw["by"].items()})
    return cw


def _count(kind, what):
    c = COUNTS.setdefault(kind, {"hit": 0, "name_fallback": 0, "miss": 0})
    c[what] += 1


def to_sleeper(kind, value, name=None, pos=None, team=None, cw=None, count=True):
    """External id -> Sleeper id, falling back to a unique name key."""
    cw = cw or crosswalk()
    v = common.sid_str(value)
    if kind == "sleeper" and v:
        if count:
            _count(kind, "hit")
        return v
    if v and v in cw["by"].get(kind, {}):
        if count:
            _count(kind, "hit")
        return cw["by"][kind][v]
    if name and pos:
        sid = cw["by_name"].get(_pkey(name, pos, team))
        if sid:
            if count:
                _count(kind, "name_fallback")
            return sid
    if count:
        _count(kind, "miss")
    return None


def join_status():
    """{kind: {hit, name_fallback, miss, miss_rate}} for /api/feeds."""
    out = {}
    for k, c in COUNTS.items():
        n = sum(c.values())
        out[k] = dict(c, miss_rate=round(c["miss"] / n, 3) if n else None)
    return out


def _probe():
    import collections
    cw = crosswalk(0)
    print("sleeper records:", len(players()), "| crosswalk players:", len(cw["info"]))
    for k, m in cw["by"].items():
        sample = list(m.items())[:5]
        print("  %-13s %6d  e.g. %s" % (k, len(m), ", ".join(
            "%s->%s(%s)" % (a, b, cw["info"].get(b, {}).get("name")) for a, b in sample)))
    print("unique name keys:", len(cw["by_name"]))
    rk = collections.Counter()
    for r in dp_rows():
        if r.get("draft_year") == "2026" and r.get("position") in ("QB", "RB", "WR", "TE"):
            rk["rookies"] += 1
            rk["with_sleeper"] += bool(common.sid_str(r.get("sleeper_id")))
            rk["with_gsis"] += bool(common.sid_str(r.get("gsis_id")))
    print("2026 skill rookies:", dict(rk))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        _probe()
    else:
        print(__doc__)
