"""Sleeper ranking-source adapter.

Backbone source for the auction board: it is the only source that hands back a
native Sleeper ``player_id`` together with BOTH a real half-PPR ADP and real
half-PPR projected points, so every other adapter can bridge onto it.

Endpoints used (verified 2026-09-08):
  * projections  https://api.sleeper.com/projections/nfl/<season>?...   (note .COM)
  * player db    https://api.sleeper.app/v1/players/nfl                 (note .APP)
  * schedule     https://api.sleeper.app/schedule/nfl/regular/<season>  (bye weeks)

Reality checks against the live API (all three verified 2026-09-08):
  * ``player_id`` lives at the TOP level of a projection row, NOT under ``player``.
  * ``/v1/players/nfl`` has NO ``bye_week`` field at all; byes are derived from
    the schedule endpoint (the week in 1..18 in which a team has no game).
  * Sleeper's ADP is a continuous ordinal over its whole player database, so it
    degrades into filler ordering past roughly pick ~300. See ADP_TRUST_DEPTH.

Python 3.9, stdlib only.
"""

import collections
import json
import os
import re
import sys
import time
import urllib.error

try:  # normal package import
    from .base import http_get, get_json, record, player_key
    from . import base
except ImportError:  # running this file directly for testing
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sources.base import http_get, get_json, record, player_key
    from sleeper_auction.sources import base


NAME = "sleeper_src"
LABEL = "Sleeper (ADP + projections)"
CAPS = {"adp": True, "auction": False, "proj": True, "half_ppr": True}

PROJ_HOST = "https://api.sleeper.com"
APP_HOST = "https://api.sleeper.app"

# ADP is dense and believable to about here; past this Sleeper is just ordering
# the rest of its database. Consumers that want "real" ADP should clamp here.
ADP_TRUST_DEPTH = 300.0

# Sentinel: Sleeper writes 999.0 into every adp_* field for an unranked player.
_UNRANKED = 999.0

# Per-scoring wiring. First entry of each tuple is the native field; the rest are
# ordered fallbacks used only when the native field is the unranked sentinel.
_SCORING = {
    "half_ppr": {
        "order_by": "adp_half_ppr",
        "adp": ("adp_half_ppr", "adp_ppr", "adp_std"),
        "pts": "pts_half_ppr",
        "pts_synth": ("pts_ppr", "pts_std"),   # mean() only if native is absent
    },
    "ppr": {
        "order_by": "adp_ppr",
        "adp": ("adp_ppr", "adp_half_ppr", "adp_std"),
        "pts": "pts_ppr",
        "pts_synth": None,
    },
    "std": {
        "order_by": "adp_std",
        "adp": ("adp_std", "adp_half_ppr", "adp_ppr"),
        "pts": "pts_std",
        "pts_synth": None,
    },
}

# Populated by fetch(); handy for the UI / for debugging a degraded run.
LAST_FETCH = {}

_CACHE_SAFE = re.compile(r"[^A-Za-z0-9._-]")


# --------------------------------------------------------------------------- #
# http helpers
# --------------------------------------------------------------------------- #

def _warn(msg):
    sys.stderr.write("[%s] %s\n" % (NAME, msg))


def _have_cache(cache_key):
    if not cache_key:
        return False
    path = os.path.join(base.CACHE_DIR, _CACHE_SAFE.sub("_", cache_key))
    return os.path.exists(path)


def _get_json(url, cache_key, ttl, attempts=3, timeout=45):
    """get_json with retry/backoff, falling back to a stale cache entry."""
    last = None
    for i in range(attempts):
        try:
            return get_json(url, cache_key=cache_key, ttl=ttl, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(1.5 * (i + 1))
    if _have_cache(cache_key):
        _warn("live fetch failed (%s); serving STALE cache %s" % (last, cache_key))
        try:
            return get_json(url, cache_key=cache_key, ttl=10 ** 9, timeout=timeout)
        except Exception:
            pass
    raise last


# --------------------------------------------------------------------------- #
# small parsing helpers
# --------------------------------------------------------------------------- #

def _num(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _adp_val(stats, field):
    """Return a usable ADP or None (999.0 sentinel and junk become None)."""
    v = _num(stats.get(field))
    if v is None or v <= 0 or v >= _UNRANKED:
        return None
    return v


def _pts_val(stats, field):
    v = _num(stats.get(field))
    if v is None or v <= 0:
        return None
    return v


def _resolve_pos(pl):
    """Prefer fantasy_positions (already maps FB->RB, K/P->K, DB/WR->WR)."""
    for cand in (pl.get("fantasy_positions") or []):
        p = base.norm_pos(cand)
        if p in base.POSITIONS:
            return p
    p = base.norm_pos(pl.get("position"))
    return p if p in base.POSITIONS else None


def _full_name(pl):
    n = (pl.get("full_name") or "").strip()
    if n:
        return n
    return ("%s %s" % (pl.get("first_name") or "",
                       pl.get("last_name") or "")).strip()


# --------------------------------------------------------------------------- #
# projections
# --------------------------------------------------------------------------- #

def _proj_url(season, order_by, positions=base.POSITIONS):
    qs = "".join("&position[]=%s" % p for p in positions)
    return "%s/projections/nfl/%s?season_type=regular%s&order_by=%s" % (
        PROJ_HOST, season, qs, order_by)


def _load_projections(season, scoring, order_by):
    """One request for all positions; per-position retry if that request dies.

    Returns (rows, missing_positions).
    """
    key = "sleeper_proj_%s_%s.json" % (season, scoring)
    try:
        rows = _get_json(_proj_url(season, order_by), key, ttl=1800)
        if isinstance(rows, list) and rows:
            return rows, []
        _warn("combined projections request returned %d rows; splitting by position"
              % (len(rows) if isinstance(rows, list) else -1))
    except Exception as exc:
        _warn("combined projections request failed (%s); splitting by position" % exc)

    rows = []
    missing = []
    for pos in base.POSITIONS:
        pkey = "sleeper_proj_%s_%s_%s.json" % (season, scoring, pos)
        try:
            # attempts=2 here: the combined request has already burned its
            # retries, and draft day cannot afford a 30s stall on a real outage.
            part = _get_json(_proj_url(season, order_by, (pos,)), pkey,
                             ttl=1800, attempts=2)
            if isinstance(part, list) and part:
                rows.extend(part)
            else:
                missing.append(pos)
        except Exception as exc:
            _warn("projections for %s failed: %s" % (pos, exc))
            missing.append(pos)
    if not rows:
        raise RuntimeError(
            "Sleeper projections unavailable for season %s (%s): no rows from any "
            "position" % (season, scoring))
    return rows, missing


def fetch(season="2026", scoring="half_ppr"):
    """Return a list of base.record() dicts from Sleeper's season projections.

    scoring is one of half_ppr|ppr|std. Raises on hard failure.
    """
    season = str(season)
    scoring = (scoring or base.DEFAULT_SCORING).lower()
    if scoring not in _SCORING:
        raise ValueError("unknown scoring %r; expected one of %s"
                         % (scoring, ", ".join(sorted(_SCORING))))
    cfg = _SCORING[scoring]

    raw, missing_pos = _load_projections(season, scoring, cfg["order_by"])

    stats = {
        "raw_rows": len(raw),
        "missing_positions": missing_pos,
        "dropped_no_position": 0,
        "dropped_not_relevant": 0,
        "adp_fallback": 0,
        "adp_missing": 0,
        "pts_missing": 0,
        "pts_synthesized": 0,
        "duplicates": 0,
    }

    out = []
    seen = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        pl = row.get("player") or {}
        st = row.get("stats") or {}
        pos = _resolve_pos(pl)
        if pos is None:
            stats["dropped_no_position"] += 1
            continue

        # player_id is TOP level on a projection row (not inside "player").
        sid = row.get("player_id") or pl.get("player_id")
        team = row.get("team") or pl.get("team")
        name = _full_name(pl)
        if pos == "DEF":
            # For defenses the Sleeper id IS the team abbreviation.
            team = team or sid
            if not name:
                name = str(sid or "")
        if not name and not sid:
            stats["dropped_no_position"] += 1
            continue

        # --- points ---------------------------------------------------------
        pts = _pts_val(st, cfg["pts"])
        if pts is None and cfg["pts_synth"]:
            a = _pts_val(st, cfg["pts_synth"][0])
            b = _pts_val(st, cfg["pts_synth"][1])
            if a is not None and b is not None:
                pts = (a + b) / 2.0
                stats["pts_synthesized"] += 1
        if pts is None:
            stats["pts_missing"] += 1

        # --- relevance gate -------------------------------------------------
        # Sleeper projects its ENTIRE database (retired players, camp bodies).
        # Anyone with an NFL team, or with real projected points, stays.
        if not team and pts is None:
            stats["dropped_not_relevant"] += 1
            continue

        # --- adp ------------------------------------------------------------
        adp = None
        for i, field in enumerate(cfg["adp"]):
            adp = _adp_val(st, field)
            if adp is not None:
                if i:
                    stats["adp_fallback"] += 1
                break
        if adp is None:
            stats["adp_missing"] += 1

        rec = record(
            name=name,
            pos=pos,
            team=team,
            adp=adp,
            auction=None,          # Sleeper publishes no auction values
            rank=None,             # filled in below from the ADP ordering
            proj_pts=pts,
            sleeper_id=sid,
            tier=None,
        )

        prev = seen.get(rec["key"])
        if prev is not None:
            stats["duplicates"] += 1
            # keep the richer row (real ADP beats none, then more points)
            better = ((rec["adp"] is not None) - (prev["adp"] is not None)) or \
                     ((rec["proj_pts"] or 0) - (prev["proj_pts"] or 0))
            if better > 0:
                out[out.index(prev)] = rec
                seen[rec["key"]] = rec
            continue
        seen[rec["key"]] = rec
        out.append(rec)

    # Ranked players first (by ADP), then the unranked ordered by projection.
    out.sort(key=lambda r: (
        0 if r["adp"] is not None else 1,
        r["adp"] if r["adp"] is not None else 0.0,
        -(r["proj_pts"] or 0.0),
        r["name"],
    ))
    n = 0
    for r in out:
        if r["adp"] is not None:
            n += 1
            r["rank"] = float(n)

    stats["emitted"] = len(out)
    stats["with_adp"] = n
    stats["with_proj"] = sum(1 for r in out if r["proj_pts"] is not None)
    stats["by_pos"] = dict(collections.Counter(r["pos"] for r in out))
    stats["adp_within_trust_depth"] = sum(
        1 for r in out if r["adp"] is not None and r["adp"] <= ADP_TRUST_DEPTH)
    stats["scoring"] = scoring
    stats["season"] = season
    LAST_FETCH.clear()
    LAST_FETCH.update(stats)

    if missing_pos:
        _warn("PARTIAL FETCH: no rows for positions %s" % ", ".join(missing_pos))
    if stats["dropped_not_relevant"]:
        _warn("dropped %d teamless/unprojected database rows of %d"
              % (stats["dropped_not_relevant"], stats["raw_rows"]))
    if stats["adp_fallback"]:
        _warn("%d rows fell back off native adp_%s to another format's ADP"
              % (stats["adp_fallback"], scoring))
    if len(out) < 250:
        _warn("only %d players emitted (expected 250+)" % len(out))

    return out


# --------------------------------------------------------------------------- #
# player index (name/id bridge for every other adapter)
# --------------------------------------------------------------------------- #

def bye_weeks(season="2026"):
    """{TEAM: bye_week_int}. Derived from the schedule; /v1/players has no byes."""
    season = str(season)
    try:
        games = _get_json("%s/schedule/nfl/regular/%s" % (APP_HOST, season),
                          "sleeper_schedule_%s.json" % season, ttl=24 * 3600)
    except Exception as exc:
        _warn("schedule fetch failed (%s); bye weeks unavailable" % exc)
        return {}
    played = collections.defaultdict(set)
    weeks = set()
    for g in games or []:
        wk = g.get("week")
        if not isinstance(wk, int):
            continue
        weeks.add(wk)
        for side in ("home", "away"):
            t = base.norm_team(g.get(side))
            if t:
                played[t].add(wk)
    if not weeks:
        return {}
    allweeks = set(range(1, max(weeks) + 1))
    byes = {}
    for team, wks in played.items():
        off = sorted(allweeks - wks)
        if len(off) == 1:
            byes[team] = off[0]
    return byes


def _index_priority(p):
    """Higher is better, for resolving two players onto one player_key."""
    rank = p.get("search_rank")
    rank = rank if isinstance(rank, (int, float)) else 10 ** 7
    return (1 if p.get("team") else 0,
            1 if p.get("status") == "Active" else 0,
            -rank,
            p.get("years_exp") or 0)


def player_index(season="2026"):
    """{sleeper_id: entry} AND {base.player_key: entry} over active NFL players.

    Every entry is::

        {"id", "name", "pos", "team", "bye", "key",
         "active", "years_exp", "injury_status"}

    The same dict object is stored under both the Sleeper id and the cross-source
    player_key, so callers can go either direction (entry["id"] gives the Sleeper
    id back). Filtered to base.POSITIONS and active players.
    """
    season = str(season)
    players = _get_json("%s/v1/players/nfl" % APP_HOST,
                        "sleeper_players_nfl.json", ttl=24 * 3600, timeout=90)
    if not isinstance(players, dict) or not players:
        raise RuntimeError("Sleeper /v1/players/nfl returned no players")

    byes = bye_weeks(season)
    index = {}
    best = {}
    for sid, p in players.items():
        if not isinstance(p, dict):
            continue
        if not p.get("active"):
            continue
        pos = _resolve_pos(p)
        if pos is None:
            continue
        team = base.norm_team(p.get("team"))
        name = _full_name(p)
        if pos == "DEF":
            team = team or base.norm_team(sid)
            if not name:
                name = str(sid)
        if not name:
            continue
        entry = {
            "id": str(sid),
            "name": name,
            "pos": pos,
            "team": team,
            "bye": byes.get(team) if team else None,
            "key": player_key(name, pos, team),
            "active": bool(p.get("active")),
            "years_exp": p.get("years_exp"),
            "injury_status": p.get("injury_status"),
        }
        index[str(sid)] = entry
        k = entry["key"]
        prio = _index_priority(p)
        if k not in best or prio > best[k]:
            best[k] = prio
            index[k] = entry
    return index


# --------------------------------------------------------------------------- #
# standalone smoke test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else base.DEFAULT_SCORING
    rows = fetch(scoring=sc)
    print("%s / %s -> %d players" % (NAME, sc, len(rows)))
    print(json.dumps(LAST_FETCH, indent=1, sort_keys=True, default=str))
    print("-" * 96)
    for r in rows[:15]:
        print("%-4s %-24s %-4s adp=%-7s pts=%-7s rank=%-5s id=%-6s %s" % (
            r["pos"], r["name"], r["team"] or "--",
            r["adp"], r["proj_pts"], r["rank"], r["sleeper_id"], r["key"]))
