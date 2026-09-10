"""FantasyFootballCalculator ADP adapter.

Real-human-draft ADP from https://fantasyfootballcalculator.com. This is the
purest *market* signal we have: mock+real drafts run by actual people over the
last week, not an editorial ranking. No auction values and no projections are
exposed by the public API (see NOTES at the bottom), so this adapter is ADP-only.

Endpoint (verified 200):
    https://fantasyfootballcalculator.com/api/v1/adp/<fmt>?teams=12&year=2026&position=all
    fmt in {half-ppr, ppr, standard}
Response shape:
    {"status":"Success",
     "meta":{"type","teams","rounds","total_drafts","start_date","end_date"},
     "players":[{"player_id","name","position","team","adp","adp_formatted",
                 "times_drafted","high","low","stdev","bye"}, ...]}

Depth: the half-ppr pool is shallower than the ppr pool (209 vs 255 as of
2026-09-08), so we fetch every format and backfill players missing from the
requested one with their ADP from the deepest available format, flagged via
r["adp_source"] / r["backfilled"] so downstream can discount them.

Python 3.9, stdlib only.
"""

import sys

try:                                    # package import (normal path)
    from .base import http_get, get_json, record, player_key
except ImportError:                     # standalone: python3 sources/ffc.py
    from base import http_get, get_json, record, player_key

NAME = "ffc"
LABEL = "FantasyFootballCalculator ADP"
# No auction $ and no projections on the public API; half-PPR is native (not synthesized).
CAPS = {"adp": True, "auction": False, "proj": False, "half_ppr": True}

API = "https://fantasyfootballcalculator.com/api/v1/adp/%s?teams=%d&year=%s&position=all"

# scoring -> FFC url path segment
SCORING_PATH = {"half_ppr": "half-ppr", "ppr": "ppr", "std": "standard"}

# Backfill preference, deepest pool first. ppr carries the most drafts (~5k vs
# ~1.8k for half-ppr), so it is both the deepest and the most stable fallback.
# NOTE: the '2qb' format has a comparable pool size but its ADP is structurally
# distorted (QBs pulled up 3+ rounds), so it is deliberately NOT used to backfill.
BACKFILL_ORDER = ("ppr", "half_ppr", "std")

TEAMS = 12          # the API ignores this (identical total_drafts for 8/10/12/14)
ADP_TTL = 1800      # 30 min: ADP moves all day on draft day
TIMEOUT = 30

# Populated by the most recent fetch(); diagnostics for the caller / UI.
LAST_META = {}


def _url(season, fmt_path):
    return API % (fmt_path, TEAMS, season)


def _cache_key(season, fmt_path):
    # MUST include the scoring format, or one format silently serves another's data.
    return "ffc_adp_%s_%s_t%d" % (season, fmt_path, TEAMS)


def _fetch_format(season, scoring):
    """Fetch one scoring format. Returns (meta, players). Raises on failure."""
    fmt_path = SCORING_PATH[scoring]
    data = get_json(
        _url(season, fmt_path),
        cache_key=_cache_key(season, fmt_path),
        ttl=ADP_TTL,
        timeout=TIMEOUT,
    )
    if not isinstance(data, dict):
        raise ValueError("%s: unexpected payload type %s for %s" % (NAME, type(data).__name__, fmt_path))
    status = str(data.get("status", "")).lower()
    if status != "success":
        raise ValueError("%s: API status=%r for %s" % (NAME, data.get("status"), fmt_path))
    players = data.get("players")
    if not isinstance(players, list) or not players:
        raise ValueError("%s: no players in %s response" % (NAME, fmt_path))
    meta = data.get("meta") or {}
    return meta, players


def _num(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return None if f is None else int(f)


def _team(raw):
    """FFC uses 'FA' for free agents; that is not a team."""
    t = (raw or "").strip().upper()
    return None if t in ("", "FA", "NONE", "N/A") else t


def _index(players):
    """player_key -> raw row, keeping the best (lowest) ADP on the rare dup."""
    out = {}
    for p in players:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        pos = p.get("position")
        if not name or not pos:
            continue
        key = player_key(name, pos, _team(p.get("team")))
        adp = _num(p.get("adp"))
        if adp is None:
            continue
        prev = out.get(key)
        if prev is None or adp < _num(prev.get("adp")):
            out[key] = p
    return out


def fetch(season="2026", scoring="half_ppr"):
    """Return a list of base.record() dicts of FFC ADP for `season`.

    scoring is one of half_ppr|ppr|std. The requested format is primary; players
    absent from it are backfilled from the other formats (deepest pool first) and
    flagged with r["backfilled"] = True. Raises on hard failure.
    """
    if scoring not in SCORING_PATH:
        raise ValueError("%s: unknown scoring %r (expected one of %s)"
                         % (NAME, scoring, "|".join(sorted(SCORING_PATH))))
    season = str(season)

    warnings = []

    # --- primary format: a failure here is a hard failure -------------------
    primary_meta, primary_players = _fetch_format(season, scoring)
    pools = {scoring: _index(primary_players)}
    metas = {scoring: primary_meta}

    # --- other formats: best effort, used only to deepen the pool -----------
    for other in BACKFILL_ORDER:
        if other == scoring:
            continue
        try:
            m, ps = _fetch_format(season, other)
            pools[other] = _index(ps)
            metas[other] = m
        except Exception as exc:                      # noqa: BLE001 - degrade, don't die
            warnings.append("backfill fetch failed for %s: %s" % (other, exc))

    primary_pool = pools[scoring]
    order = [scoring] + [f for f in BACKFILL_ORDER if f != scoring and f in pools]

    # --- merge --------------------------------------------------------------
    merged = {}          # key -> (row, source_scoring)
    for fmt in order:
        for key, row in pools[fmt].items():
            if key not in merged:
                merged[key] = (row, fmt)

    rows = []
    for key, pair in merged.items():
        row, src = pair
        adp = _num(row.get("adp"))
        rows.append((adp, key, row, src))
    rows.sort(key=lambda t: (t[0] if t[0] is not None else 9e9, t[1]))

    out = []
    pos_counts = {}
    backfilled = 0
    for i, item in enumerate(rows):
        adp, key, row, src = item
        name = (row.get("name") or "").strip()
        pos = row.get("position")           # base.norm_pos maps PK->K, DEF->DEF
        team = _team(row.get("team"))

        r = record(
            name, pos, team,
            adp=adp,
            auction=None,                   # FFC public API exposes no auction values
            rank=i + 1,                     # overall rank by merged ADP
            proj_pts=None,                  # ...and no projections
        )
        p = r["pos"]
        pos_counts[p] = pos_counts.get(p, 0) + 1
        r["pos_rank"] = pos_counts[p]

        # Extra signal FFC gives us for free. Uncertainty is valuable in auctions:
        # a high stdev / wide high-low band means the room disagrees about price.
        r["stdev"] = _num(row.get("stdev"))
        r["times_drafted"] = _int(row.get("times_drafted"))
        r["high"] = _int(row.get("high"))
        r["low"] = _int(row.get("low"))
        bye = _int(row.get("bye"))
        r["bye"] = bye if bye else None     # FFC uses 0 for "unknown/free agent"
        r["adp_formatted"] = row.get("adp_formatted")
        r["ffc_id"] = row.get("player_id")

        # Provenance: which scoring format actually supplied this ADP.
        r["source"] = NAME
        r["scoring"] = scoring
        r["adp_source"] = src
        r["backfilled"] = (src != scoring)
        if r["backfilled"]:
            backfilled += 1
        # Cross-format ADP is a cheap sanity/leverage check (we fetched all three anyway).
        by_fmt = {}
        for fmt in pools:
            hit = pools[fmt].get(key)
            if hit is not None:
                by_fmt[fmt] = _num(hit.get("adp"))
        r["adp_by_format"] = by_fmt

        out.append(r)

    if not out:
        raise ValueError("%s: merged 0 players for %s %s" % (NAME, season, scoring))

    total_drafts = _int(primary_meta.get("total_drafts"))
    if total_drafts is not None and total_drafts < 100:
        warnings.append("thin sample: only %d drafts in the %s pool" % (total_drafts, scoring))
    missing_pos = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if p not in pos_counts]
    if missing_pos:
        warnings.append("no players at position(s): %s" % ",".join(missing_pos))
    if len(out) < 250:
        warnings.append("pool is %d players; FFC's full union across formats is the ceiling "
                        "(no pagination available - rounds=/position= params are ignored)" % len(out))

    global LAST_META
    LAST_META = {
        "source": NAME,
        "season": season,
        "scoring": scoring,
        "primary_count": len(primary_pool),
        "merged_count": len(out),
        "backfilled": backfilled,
        "pool_sizes": dict((f, len(pools[f])) for f in pools),
        "total_drafts": total_drafts,
        "sample_start": primary_meta.get("start_date"),
        "sample_end": primary_meta.get("end_date"),
        "meta_type": primary_meta.get("type"),
        "warnings": warnings,
    }
    return out


if __name__ == "__main__":
    fmt = sys.argv[1] if len(sys.argv) > 1 else "half_ppr"
    yr = sys.argv[2] if len(sys.argv) > 2 else "2026"
    recs = fetch(yr, fmt)
    print("%s [%s] %s %s -> %d players" % (NAME, LABEL, yr, fmt, len(recs)))
    print("meta: %s" % (LAST_META,))
    for r in recs[:15]:
        print("%3d %-24s %-3s %-4s adp=%-6s stdev=%-5s n=%-5s bye=%-4s src=%s" % (
            r["rank"], r["name"], r["pos"], r["team"] or "-", r["adp"],
            r["stdev"], r["times_drafted"], r["bye"], r["adp_source"]))
