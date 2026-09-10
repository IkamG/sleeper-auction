"""Yahoo ranking-source adapter (public read-only fantasy API, no auth).

Endpoint (verified 2026-09-08, HTTP 200, application/json):
  https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/league/nfl.l.public/players;...?format=json_f

`format=json_f` returns "friendly" JSON (named keys instead of array-index maps).
Rows live at fantasy_content.league.players[].player and carry:
  name.full, editorial_team_abbr, primary_position / display_position,
  projected_auction_value, average_auction_cost,
  player_ranks[].player_rank {rank_type, rank_value, rank_season, rank_position?},
  draft_analysis {average_pick, average_round, average_cost, percent_drafted, preseason_*}

SCORING NOTE (read this before trusting the numbers):
  This public endpoint exposes exactly ONE scoring context -- Yahoo's default
  public-league settings (league 470.l.101). There is no scoring/PPR parameter,
  and no separate PPR vs standard variant to blend. Yahoo's default scoring sits
  closest to half-PPR of any free source, and its auction dollars are REAL market
  data (average_cost across actual Yahoo drafts), so we expose it as a native
  half-PPR signal (CAPS["half_ppr"] is True).
  Consequence: fetch() returns the SAME underlying values for half_ppr, ppr and
  std. The scoring argument is still validated and is baked into every cache key,
  so a future scoring-aware endpoint cannot be poisoned by cached rows from
  another format -- but today ppr/std callers are getting Yahoo's half-PPR-ish
  numbers, not true PPR or standard. Nothing is synthesized or rescaled.

DEPTH CEILING (measured, not assumed):
  Yahoo ranks ~376 players contiguously (season rank 1..~388), then dumps the
  rest of the player universe into an unranked bucket where rank jumps to 2000+
  with no ADP and no auction value. We paginate until that cliff and stop.
  Within the ranked set: every row has a season rank, ~225 have a real ADP
  (average_pick) and auction average_cost, and ~286 have a nonzero projected
  auction value. Rows past that carry rank only -- see notes emitted by __main__.
"""

try:  # package import (normal path: `from sources.yahoo import fetch`)
    from .base import http_get, get_json, record, player_key
except ImportError:  # standalone execution: `python3 sources/yahoo.py`
    from base import http_get, get_json, record, player_key  # noqa: F401

NAME = "yahoo"
LABEL = "Yahoo (public API)"
CAPS = {"adp": True, "auction": True, "proj": False, "half_ppr": True}

SCORINGS = ("half_ppr", "ppr", "std")

# Verified: `count` is NOT capped at 25 -- count=1000 returns 1000 rows. We still
# page in modest chunks so each cache entry is small and a partial outage only
# costs one page.
PAGE_SIZE = 100
MAX_PLAYERS = 500          # hard ceiling on how deep we walk
TTL = 1800                 # ADP-ish data: 30 minutes

# Cliff detection: Yahoo's unranked tail starts where the season rank leaps far
# ahead of the previous row while the player has no draft data at all.
_RANK_GAP = 100
_MIN_BEFORE_CLIFF = 150    # never honor a "cliff" before this many rows

_URL = (
    "https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/league/nfl.l.public/players;"
    "position=ALL;start={start};count={count};sort=rank_season;search=;"
    "out=auction_values,ranks;ranks=season;ranks_by_position=season/draft_analysis;"
    "cut_types=diamond;slices=last7days?format=json_f"
)


def _clean(v):
    """Yahoo writes missing numerics as the string '-'. Normalize those to None."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "N/A"):
        return None
    return s


def _num(v):
    s = _clean(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _players(payload):
    """Pull the player list out of the json_f envelope, tolerating shape drift."""
    league = (payload or {}).get("fantasy_content", {}).get("league", {})
    rows = league.get("players")
    if rows is None:
        return []
    if isinstance(rows, dict):  # some Yahoo shapes key by index instead of listing
        rows = [rows[k] for k in sorted(rows, key=lambda x: str(x)) if k != "count"]
    out = []
    for row in rows:
        if isinstance(row, dict):
            p = row.get("player", row)
            if isinstance(p, dict) and p.get("name"):
                out.append(p)
    return out


def _season_rank(p, season):
    """Overall season rank: rank_type 'S', matching season, and NOT a positional rank.

    player_ranks also carries prior seasons and per-position / W-R-T flex ranks,
    so both filters matter or you get last year's number or a position rank.
    """
    ranks = p.get("player_ranks") or []
    if isinstance(ranks, dict):
        ranks = list(ranks.values())
    fallback = None
    for item in ranks:
        pr = item.get("player_rank", item) if isinstance(item, dict) else None
        if not isinstance(pr, dict) or pr.get("rank_position"):
            continue
        if pr.get("rank_type") not in (None, "S"):
            continue
        val = _num(pr.get("rank_value"))
        if val is None:
            continue
        if str(pr.get("rank_season")) == str(season):
            return val, True
        if fallback is None:
            fallback = val
    return fallback, False


def _position(p):
    """primary_position is authoritative; display_position can be 'WR,TE'."""
    pos = _clean(p.get("primary_position")) or _clean(p.get("display_position")) or ""
    return pos.split(",")[0].strip()


def _name(p, pos):
    nm = p.get("name") or {}
    full = _clean(nm.get("full")) or ""
    if pos.upper() in ("DEF", "DST", "D/ST"):
        # Yahoo names defenses by nickname only ("Texans"); the full editorial
        # name reads better in the UI. The identity key comes from the team abbr
        # either way, so this is display-only.
        return _clean(p.get("editorial_team_full_name")) or full
    return full


def _to_record(p, season):
    pos = _position(p)
    team = _clean(p.get("editorial_team_abbr"))
    da = p.get("draft_analysis") or {}
    if not isinstance(da, dict):
        da = {}

    adp = _num(da.get("average_pick"))
    if adp is None:
        adp = _num(da.get("preseason_average_pick"))

    # Real market money first (average winning bid across actual Yahoo auctions),
    # then Yahoo's own projected value for players nobody has drafted yet.
    auction = _num(p.get("average_auction_cost"))
    if auction is None:
        auction = _num(da.get("average_cost"))
    if auction is None:
        auction = _num(da.get("preseason_average_cost"))
    market = auction is not None
    if auction is None:
        # Yahoo's projected value is an integer floor: 0 means "not valued", not
        # "worth $0". Emitting 0.0 would drag down any cross-source blend, so an
        # unvalued player abstains (None) instead.
        proj_val = _num(p.get("projected_auction_value"))
        auction = proj_val if proj_val else None

    rank, exact = _season_rank(p, season)

    rec = record(
        name=_name(p, pos),
        pos=pos,
        team=team,
        adp=adp,
        auction=auction,
        rank=rank,
        proj_pts=None,          # this endpoint carries no point projections
        sleeper_id=None,        # Yahoo ids are not Sleeper ids; leave for the merger
        tier=None,
    )
    return rec, market, exact


def fetch(season="2026", scoring="half_ppr"):
    """Return a list of base.record() dicts, in Yahoo's own season-rank order.

    scoring is one of half_ppr|ppr|std. Yahoo publishes a single scoring context
    (see the module docstring): the value is validated and included in the cache
    key, but the returned numbers are identical across the three formats.
    """
    if scoring not in SCORINGS:
        raise ValueError("yahoo: unknown scoring %r (expected one of %s)"
                         % (scoring, ", ".join(SCORINGS)))
    season = str(season)

    out = []
    seen = set()
    dupes = 0
    pages = 0
    stale_rank = 0
    market_rows = 0
    adp_rows = 0
    cliff_at = None
    prev_rank = None
    exhausted = False

    start = 0
    while start < MAX_PLAYERS and cliff_at is None:
        count = min(PAGE_SIZE, MAX_PLAYERS - start)
        url = _URL.format(start=start, count=count)
        # Cache key MUST carry the scoring format (and season/page geometry) so one
        # format can never be served another format's rows.
        ck = "yahoo-%s-%s-%d-%d" % (scoring, season, start, count)
        payload = get_json(url, cache_key=ck, ttl=TTL, timeout=30)
        rows = _players(payload)
        pages += 1
        if not rows:
            exhausted = True
            break

        for p in rows:
            rec, market, exact = _to_record(p, season)
            if not rec["name"] or rec["pos"] is None:
                continue

            rank = rec["rank"]
            # Unranked-tail detection: a big rank leap on a player with no draft
            # data at all means we've fallen off Yahoo's ranked universe.
            if (rank is not None and prev_rank is not None
                    and len(out) >= _MIN_BEFORE_CLIFF
                    and (rank - prev_rank) > _RANK_GAP
                    and rec["adp"] is None and not market):
                cliff_at = len(out)
                break
            if rank is not None:
                prev_rank = rank

            if rec["key"] in seen:
                dupes += 1
                continue
            seen.add(rec["key"])
            out.append(rec)
            if market:
                market_rows += 1
            if rec["adp"] is not None:
                adp_rows += 1
            if not exact:
                stale_rank += 1

        if cliff_at is None and len(rows) < count:
            exhausted = True
            break
        start += count

    if not out:
        raise RuntimeError("yahoo: fetched %d page(s) but parsed 0 players -- "
                           "response shape likely changed" % pages)

    fetch.stats = {
        "count": len(out),
        "pages": pages,
        "cliff_at": cliff_at,
        "exhausted": exhausted,
        "hit_max": cliff_at is None and not exhausted,
        "with_adp": adp_rows,
        "with_market_auction": market_rows,
        "rank_season_mismatch": stale_rank,
        "dupe_keys_dropped": dupes,
    }
    return out


if __name__ == "__main__":
    import collections
    import sys

    sc = sys.argv[1] if len(sys.argv) > 1 else "half_ppr"
    rows = fetch(scoring=sc)
    st = getattr(fetch, "stats", {})
    print("scoring=%s  count=%d" % (sc, len(rows)))
    print("stats: %s" % (st,))
    print("positions: %s" % dict(collections.Counter(r["pos"] for r in rows)))
    print("with rank=%d  with adp=%d  with auction=%d" % (
        sum(r["rank"] is not None for r in rows),
        sum(r["adp"] is not None for r in rows),
        sum(r["auction"] is not None for r in rows)))
    print("-" * 96)
    for r in rows[:15]:
        print("%-24s %-4s %-4s rank=%-6s adp=%-7s auction=%-7s key=%s" % (
            r["name"], r["pos"], r["team"], r["rank"], r["adp"], r["auction"], r["key"]))
