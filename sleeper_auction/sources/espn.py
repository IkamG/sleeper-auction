"""ESPN ranking-source adapter (kona_player_info).

One of the few sources with REAL auction dollars: ``ownership.auctionValueAverage``
is ESPN's rolling average winning bid across live ESPN auction drafts.

Scoring notes (all verified against the live 2026 payload, see module notes):
  * ESPN publishes draft ranks for PPR and STANDARD only -- there is no half-PPR
    rank type, so CAPS["half_ppr"] is False and half-PPR rank/auction are
    synthesized as the mean of the PPR and STANDARD values.
  * Season projections DO come in a scoring-specific flavour: the same player
    endpoint under ``leaguedefaults/3`` returns PPR-scored ``appliedTotal`` and
    under ``leaguedefaults/1`` returns STANDARD-scored ``appliedTotal``. The
    delta between them is exactly the projected reception count (verified for
    354/354 players that have one), so half-PPR points are the exact midpoint,
    not an approximation.
  * ADP, ownership auction values and draft ranks are byte-identical across both
    leaguedefaults -- they are global, scoring-agnostic market numbers.
"""

import json

try:
    from .base import http_get, get_json, record, player_key
except ImportError:  # standalone execution: python3 sources/espn.py
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from base import http_get, get_json, record, player_key  # noqa: F401

NAME = "espn"
LABEL = "ESPN"
CAPS = {"adp": True, "auction": True, "proj": True, "half_ppr": False}

BASE_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            "%s/segments/0/leaguedefaults/%d?view=kona_player_info")

# ADP-ish + live auction averages -> short cache.
ADP_TTL = 1800
# The whole 2026 pool is 1036 rows; one page covers it, the loop is insurance.
PAGE_SIZE = 2000
MAX_PLAYERS = 8000

# defaultPositionId -> position.
POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# proTeamId -> abbreviation. Verified against the live payload: every one of the
# 32 "<Nickname> D/ST" rows resolves to the team this map names, and no
# unmapped proTeamId appears in the response.
TEAM_BY_ID = {
    0: None,  # free agent / no pro team
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
    22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS",
    29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# scoring -> (draft rank types to blend, leaguedefaults ids whose projections to blend)
_SCORING_PLAN = {
    "ppr": (("PPR",), (3,)),
    "std": (("STANDARD",), (1,)),
    "half_ppr": (("PPR", "STANDARD"), (3, 1)),
}

# Populated by fetch() so callers/tests can see coverage without re-parsing.
LAST_FETCH_STATS = {}


def _mean(values):
    """Mean of the non-None values, or None if there are none."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / float(len(vals))


def _positive(value):
    """ESPN uses 0 for 'no market data'; keep that distinct from a real $0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _filter_header(limit, offset, rank_type):
    """The x-fantasy-filter header ESPN requires for this view."""
    flt = {"players": {
        "limit": limit,
        "offset": offset,
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": rank_type},
    }}
    return {"x-fantasy-filter": json.dumps(flt, separators=(",", ":"))}


def _fetch_pool(season, scoring, league_default, rank_type):
    """Page the player universe for one leaguedefaults id -> {espn_id: player}."""
    url = BASE_URL % (season, league_default)
    pool = {}
    offset = 0
    pages = 0
    while offset < MAX_PLAYERS:
        # cache_key carries the scoring format (mandatory: otherwise one format
        # silently serves another's rows) plus everything else that changes the
        # bytes -- season, leaguedefaults id, sort rank type, and the page.
        cache_key = "espn_%s_%s_ld%d_%s_off%d_lim%d" % (
            season, scoring, league_default, rank_type, offset, PAGE_SIZE)
        data = get_json(
            url,
            headers=_filter_header(PAGE_SIZE, offset, rank_type),
            cache_key=cache_key,
            ttl=ADP_TTL,
        )
        players = (data or {}).get("players") or []
        pages += 1
        for entry in players:
            info = entry.get("player") or {}
            pid = info.get("id")
            if pid is not None and pid not in pool:
                pool[pid] = info
        if len(players) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    if not pool:
        raise RuntimeError(
            "ESPN returned no players for season=%s leaguedefaults=%d (%s)"
            % (season, league_default, rank_type))
    return pool


def _season_projection(info, season_int):
    """Season-total projection: statSourceId 1 (projected), statSplitTypeId 0 (season)."""
    for stat in info.get("stats") or []:
        if (stat.get("statSourceId") == 1
                and stat.get("statSplitTypeId") == 0
                and stat.get("seasonId") == season_int
                and stat.get("scoringPeriodId") == 0):
            total = stat.get("appliedTotal")
            if total is not None and float(total) > 0:
                return float(total)
            return None
    return None


def fetch(season="2026", scoring="half_ppr"):
    """Return a list of base.record() dicts for the ESPN player universe."""
    scoring = (scoring or "half_ppr").lower()
    if scoring not in _SCORING_PLAN:
        raise ValueError("espn: unsupported scoring %r (expected half_ppr|ppr|std)" % scoring)
    rank_types, league_defaults = _SCORING_PLAN[scoring]
    try:
        season_int = int(season)
    except (TypeError, ValueError):
        raise ValueError("espn: season must be a year, got %r" % (season,))

    # Draft ranks for every rank type ship in one response, so the first
    # leaguedefaults pool drives identity/rank/auction; the extra pool(s) exist
    # only to supply the other scoring flavour of the projections.
    pools = []
    for idx, league_default in enumerate(league_defaults):
        pools.append(_fetch_pool(season, scoring, league_default, rank_types[idx if idx < len(rank_types) else 0]))
    primary = pools[0]

    rows = {}
    skipped_pos = 0
    no_rank = 0
    proj_partial = 0
    for pid, info in primary.items():
        pos = POSITION_BY_ID.get(info.get("defaultPositionId"))
        if pos is None:
            skipped_pos += 1
            continue
        name = (info.get("fullName") or "").strip()
        if not name:
            skipped_pos += 1
            continue
        team = TEAM_BY_ID.get(info.get("proTeamId"))

        draft_ranks = info.get("draftRanksByRankType") or {}
        ranks, draft_aucs = [], []
        for rank_type in rank_types:
            block = draft_ranks.get(rank_type) or {}
            r = block.get("rank")
            ranks.append(float(r) if isinstance(r, (int, float)) and r > 0 else None)
            draft_aucs.append(_positive(block.get("auctionValue")))
        rank = _mean(ranks)
        if rank is None:
            no_rank += 1

        # Blend the season projection across the scoring flavours. Both halves
        # must be present for half-PPR, otherwise the number would be a
        # PPR (or STANDARD) total wearing a half-PPR label.
        projections = [_season_projection(pool.get(pid) or {}, season_int) for pool in pools]
        if any(p is None for p in projections):
            if any(p is not None for p in projections):
                proj_partial += 1
            proj_pts = None
        else:
            proj_pts = _mean(projections)

        ownership = info.get("ownership") or {}
        row = record(
            name=name,
            pos=pos,
            team=team,
            adp=_positive(ownership.get("averageDraftPosition")),
            # Real market dollars: rolling average winning bid in ESPN auctions.
            auction=_positive(ownership.get("auctionValueAverage")),
            rank=rank,
            proj_pts=proj_pts,
        )
        # ESPN extras (namespaced; downstream reads the contract keys above).
        row["espn_id"] = pid
        row["espn_draft_auction"] = _mean(draft_aucs)
        for rank_type in ("PPR", "STANDARD"):
            block = draft_ranks.get(rank_type) or {}
            suffix = "ppr" if rank_type == "PPR" else "std"
            r = block.get("rank")
            row["espn_rank_" + suffix] = float(r) if isinstance(r, (int, float)) and r > 0 else None
            row["espn_auction_" + suffix] = _positive(block.get("auctionValue"))
        row["pct_owned"] = _positive(ownership.get("percentOwned"))

        key = row["key"] or player_key(name, pos, team)
        prior = rows.get(key)
        # Duplicate identity (same normalized name+pos): keep the better rank.
        if prior is None or (row["rank"] is not None
                             and (prior["rank"] is None or row["rank"] < prior["rank"])):
            rows[key] = row

    out = sorted(rows.values(), key=lambda r: (r["rank"] is None, r["rank"] or 0.0, r["name"]))

    by_pos = {}
    for r in out:
        by_pos[r["pos"]] = by_pos.get(r["pos"], 0) + 1
    LAST_FETCH_STATS.clear()
    LAST_FETCH_STATS.update({
        "season": season,
        "scoring": scoring,
        "rank_types": list(rank_types),
        "league_defaults": list(league_defaults),
        "raw_players": len(primary),
        "returned": len(out),
        "by_pos": by_pos,
        "with_auction": sum(1 for r in out if r["auction"] is not None),
        "with_adp": sum(1 for r in out if r["adp"] is not None),
        "with_proj": sum(1 for r in out if r["proj_pts"] is not None),
        "skipped_unknown_pos": skipped_pos,
        "without_rank": no_rank,
        "proj_dropped_one_sided": proj_partial,
        "duplicate_keys_merged": len(primary) - skipped_pos - len(out),
    })
    return out


if __name__ == "__main__":
    import sys

    which = sys.argv[1] if len(sys.argv) > 1 else "half_ppr"
    rows = fetch(scoring=which)
    print("%s (%s) scoring=%s -> %d players" % (NAME, LABEL, which, len(rows)))
    print("stats: %s" % json.dumps(LAST_FETCH_STATS, sort_keys=True))
    print("-" * 100)
    print("%-4s %-24s %-4s %-5s %8s %8s %9s %9s" % (
        "#", "name", "pos", "team", "rank", "adp", "auction", "proj"))
    for i, r in enumerate(rows[:15], 1):
        print("%-4d %-24s %-4s %-5s %8s %8s %9s %9s" % (
            i, r["name"], r["pos"], r["team"] or "-",
            r["rank"], r["adp"],
            "$%.2f" % r["auction"] if r["auction"] is not None else "-",
            "%.1f" % r["proj_pts"] if r["proj_pts"] is not None else "-"))
