"""CBS Sports consensus ranking adapter. Python 3.9, stdlib only.

Endpoint (public, no key):
    https://api.cbssports.com/fantasy/players/rankings
      ?version=3.0&SPORT=football&response_format=JSON&max=500&order=DESC
      &type=<by_pos|overall>&source=<cbs_avg_ppr|cbs_avg|cbs_ppr|cbs>

Two request knobs are genuinely honored by the server; everything else is
silently ignored (an unknown value falls back to the cbs_avg_ppr default, which
is why _rankings() hard-fails when the echoed `source` is not the one we asked
for -- a silent fallback would mislabel Standard data as PPR and poison the
board).

    type=by_pos    248 rows, all six position groups (QB 32 / RB 60 / WR 60 /
                   TE 32 / K 32 / DST 32) with a PER-POSITION rank.
    type=overall   200 rows, one true cross-position rank, but QB/RB/WR/TE ONLY
                   (no K, no DST). Includes RB/WR ranked deeper than by_pos
                   truncates at, so neither response is a superset.

We fetch both and union them: ~282 players, all six positions, with a real
overall rank for the top 200 and a synthesized tail (see _board).

SCORING: CBS exposes PPR (cbs_avg_ppr) and Standard (cbs_avg) consensus boards.
There is NO half-PPR variant -- see the module notes at the bottom for the full
list of parameter names and values probed. half_ppr is therefore SYNTHESIZED as
the mean of the PPR and Standard ranks, so CAPS["half_ppr"] is False.

CBS publishes ranks only: no ADP, no auction dollars, no projected points.
"""

try:
    from .base import http_get, get_json, record, player_key
    from .base import norm_pos
except ImportError:  # standalone: python3 sources/cbs.py
    from base import http_get, get_json, record, player_key
    from base import norm_pos

NAME = "cbs"
LABEL = "CBS Sports"
CAPS = {"adp": False, "auction": False, "proj": False, "half_ppr": False}

_API = "https://api.cbssports.com/fantasy/players/rankings"
_MAX = 500          # server caps well below this; harmless to ask
_TTL = 1800         # ranking-ish data, per the 30-minute rule

# Requested scoring -> CBS consensus board id. No half-PPR board exists.
_CBS_SOURCE = {"ppr": "cbs_avg_ppr", "std": "cbs_avg"}

# Where un-ranked (tail) players land, after the real overall board. Kickers and
# defenses come off the auction block last, so they sort last.
_TAIL_GROUP = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "DEF": 1, "K": 2}

# Populated by fetch() so callers/tests can see what actually came back.
LAST_FETCH = {}


def _endpoint(cbs_source, rtype):
    return ("%s?version=3.0&SPORT=football&response_format=JSON"
            "&max=%d&order=DESC&type=%s&source=%s"
            % (_API, _MAX, rtype, cbs_source))


def _rankings(cbs_source, rtype, season):
    """GET one rankings document, verifying the server honored our scoring."""
    # cache_key carries the CBS board id, so PPR and Standard can never share a
    # cache entry (that would silently serve one format's data as another's).
    cache_key = "cbs_%s_%s_%s.json" % (season, rtype, cbs_source)
    doc = get_json(_endpoint(cbs_source, rtype), cache_key=cache_key, ttl=_TTL)
    body = doc.get("body") or {}
    rk = body.get("rankings") or {}
    served = rk.get("source")
    if served != cbs_source:
        raise RuntimeError(
            "CBS ignored source=%s and served %r (type=%s); refusing to "
            "mislabel scoring." % (cbs_source, served, rtype))
    return rk


def _row(p, abbr):
    pos = norm_pos(abbr or p.get("position"))
    name = (p.get("fullname") or "").strip()
    team = p.get("pro_team") or None
    # CBS names defenses by nickname only ("Rams"); make it readable. base's
    # defense_key resolves either spelling to DEF|<TEAM>.
    if pos == "DEF" and name and not name.upper().endswith("D/ST"):
        name = "%s D/ST" % name
    return {"name": name, "pos": pos, "team": team,
            "pos_rank": None, "overall": None, "synth": False}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _board(cbs_source, season):
    """Union by_pos + overall into {cbs_player_id: row}, every row ranked.

    Returns (players, n_overall, n_by_pos) where n_overall is how many rows
    carry a REAL cross-position rank; the rest were synthesized.
    """
    players = {}

    # 1. by_pos -- the only response that carries K and DST.
    bp = _rankings(cbs_source, "by_pos", season)
    groups = bp.get("positions") or []
    if not groups:
        raise RuntimeError("CBS by_pos returned no position groups (source=%s)"
                           % cbs_source)
    for grp in groups:
        abbr = grp.get("abbr")
        for p in grp.get("players") or []:
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            row = players.setdefault(pid, _row(p, abbr))
            row["pos_rank"] = _int(p.get("rank"))
    n_by_pos = len(players)

    # 2. overall -- the real cross-position ordering for the top 200.
    ov = _rankings(cbs_source, "overall", season)
    olist = ov.get("players") or []
    if not olist:
        raise RuntimeError("CBS overall returned no players (source=%s)"
                           % cbs_source)
    for p in olist:
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        row = players.get(pid)
        if row is None:
            # Ranked deeper than by_pos truncates (RB 61+, WR 61+).
            row = players.setdefault(pid, _row(p, p.get("position")))
        rank = _int(p.get("rank"))
        if rank is not None:
            row["overall"] = rank
    n_overall = sum(1 for r in players.values() if r["overall"] is not None)

    # 3. Everyone the overall board omits (all K, all DST, deep QB/TE) gets a
    #    synthesized rank appended after it, ordered by position group then by
    #    their own positional rank. Marked so callers can tell them apart.
    tail = [pid for pid, r in players.items() if r["overall"] is None]
    tail.sort(key=lambda pid: (
        _TAIL_GROUP.get(players[pid]["pos"], 3),
        players[pid]["pos_rank"] if players[pid]["pos_rank"] is not None else 9999,
        players[pid]["name"],
    ))
    nxt = n_overall + 1
    for pid in tail:
        players[pid]["overall"] = nxt
        players[pid]["synth"] = True
        nxt += 1

    return players, n_overall, n_by_pos


def _emit(row, rank):
    rec = record(name=row["name"], pos=row["pos"], team=row["team"], rank=rank)
    # Additive extra: positional rank ("WR12"), the natural companion to rank on
    # a draft board. All base.record() keys are present and unmodified.
    rec["pos_rank"] = row["pos_rank"]
    return rec


def fetch(season="2026", scoring="half_ppr"):
    """Return a list of base.record() dicts, best rank first.

    scoring is one of half_ppr|ppr|std. half_ppr is synthesized (CBS has no
    half-PPR board) as the mean of the PPR and Standard overall ranks.
    """
    scoring = (scoring or "half_ppr").strip().lower()
    if scoring not in ("half_ppr", "ppr", "std"):
        raise ValueError("unsupported scoring %r (want half_ppr|ppr|std)" % scoring)

    diag = {"scoring": scoring, "season": str(season), "synthesized_half_ppr": False}

    if scoring in ("ppr", "std"):
        cbs_source = _CBS_SOURCE[scoring]
        board, n_overall, n_by_pos = _board(cbs_source, season)
        ordered = sorted(board.values(),
                         key=lambda r: (r["overall"], r["name"]))
        rows = [_emit(r, i) for i, r in enumerate(ordered, 1)]
        diag.update({"cbs_sources": [cbs_source], "real_overall_ranks": n_overall,
                     "by_pos_rows": n_by_pos,
                     "synthesized_tail_ranks": sum(1 for r in ordered if r["synth"])})
    else:
        # Blend. Rank boards are ordinal, so we average the two ordinals and
        # re-rank -- the standard way to merge rank lists.
        ppr, n_ov_p, n_bp_p = _board(_CBS_SOURCE["ppr"], season)
        std, n_ov_s, n_bp_s = _board(_CBS_SOURCE["std"], season)
        miss_p = max(r["overall"] for r in ppr.values()) + 1
        miss_s = max(r["overall"] for r in std.values()) + 1
        only_one = 0
        scored = []
        for pid in set(ppr) | set(std):
            pr = ppr.get(pid)
            sr = std.get(pid)
            if pr is None or sr is None:
                only_one += 1
            a = pr["overall"] if pr is not None else miss_p
            b = sr["overall"] if sr is not None else miss_s
            base_row = pr if pr is not None else sr
            scored.append(((a + b) / 2.0, a, base_row["name"], base_row))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        rows = [_emit(t[3], i) for i, t in enumerate(scored, 1)]
        diag.update({
            "synthesized_tail_ranks": sum(1 for t in scored if t[3]["synth"]),
            "cbs_sources": [_CBS_SOURCE["ppr"], _CBS_SOURCE["std"]],
            "synthesized_half_ppr": True,
            "real_overall_ranks": {"ppr": n_ov_p, "std": n_ov_s},
            "by_pos_rows": {"ppr": n_bp_p, "std": n_bp_s},
            "in_only_one_board": only_one,
        })

    if not rows:
        raise RuntimeError("CBS produced no rows for scoring=%s" % scoring)

    by_pos = {}
    for r in rows:
        by_pos[r["pos"]] = by_pos.get(r["pos"], 0) + 1
    diag["count"] = len(rows)
    diag["by_position"] = by_pos
    missing = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if p not in by_pos]
    if missing:
        raise RuntimeError("CBS response missing position groups: %s" % missing)
    LAST_FETCH.clear()
    LAST_FETCH.update(diag)
    return rows


if __name__ == "__main__":
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "half_ppr"
    rs = fetch(scoring=which)
    print("scoring=%s  count=%d" % (which, len(rs)))
    print("diagnostics: %s" % (LAST_FETCH,))
    print("-" * 78)
    for r in rs[:15]:
        print("%3s %-26s %-4s %-4s posrank=%-4s key=%s"
              % (int(r["rank"]), r["name"], r["pos"], r["team"] or "-",
                 r["pos_rank"], r["key"]))
