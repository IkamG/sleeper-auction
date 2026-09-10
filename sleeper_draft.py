"""Sleeper live-draft client for the auction-draft value app.

Python 3.9, stdlib only. Talks to the public Sleeper REST API.

IMPORTANT design constraint (verified, do not re-litigate): the public REST API
exposes COMPLETED picks only. There is NO public endpoint for the player
currently on the block or the live high bid -- that flows over Sleeper's private
app websocket, which we do not use. So "available" is always computed as
(player universe minus drafted_ids), refreshed by polling this module.

Poll endpoints (draft object, picks) are fetched with ttl=0 so live polling is
never served stale from the on-disk cache. Slow-moving lookups (nfl state,
league users/rosters, league+draft listings) get a short TTL.

Every player-shaped row is built through sources.base.record() so the "key"
field joins cleanly against the ranking sources.
"""

import json
import os
import sys
import urllib.error
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sources import base  # noqa: E402

API = "https://api.sleeper.app/v1"

# Cache TTLs (seconds). Poll endpoints are deliberately 0.
TTL_POLL = 0          # draft object, picks
TTL_STATE = 300       # nfl state
TTL_LEAGUE = 300      # league users / rosters (stable during a draft)
TTL_LIST = 60         # user -> leagues -> drafts discovery

DEFAULT_LEAGUE = {"teams": 12, "budget": 200, "scoring": "half_ppr",
                  "slots": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1,
                            "K": 1, "DEF": 1, "BN": 6},
                  "flex_pos": ["RB", "WR", "TE"]}

# settings['slots_*'] suffix -> canonical slot bucket.
_SLOT_MAP = {
    "qb": "QB", "rb": "RB", "wr": "WR", "te": "TE", "k": "K",
    "def": "DEF", "dst": "DEF", "bn": "BN", "bench": "BN",
    "flex": "FLEX", "wrrb_flex": "FLEX", "rec_flex": "FLEX",
    "wrrbte_flex": "FLEX", "wrte_flex": "FLEX", "rbwr_flex": "FLEX",
    "super_flex": "SUPER_FLEX", "qb_flex": "SUPER_FLEX", "superflex": "SUPER_FLEX",
    "idp_flex": "FLEX",
}
# Slots that exist on a roster but are never filled by the draft.
_SLOT_IGNORE = ("taxi", "ir", "reserve")

_SCORING_MAP = {
    "half_ppr": "half_ppr", "half": "half_ppr", "halfppr": "half_ppr",
    "0.5ppr": "half_ppr", "ppr_0.5": "half_ppr",
    "ppr": "ppr", "full_ppr": "ppr", "fullppr": "ppr",
    "std": "std", "standard": "std", "non_ppr": "std", "nonppr": "std",
    "2qb": "half_ppr",  # 2qb describes lineup, not scoring; fall back to league default
}


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _fetch(path, ttl, default=None):
    """GET {API}/{path}. Never raises -- returns `default` on any failure."""
    url = "%s/%s" % (API, path.lstrip("/"))
    cache_key = "sleeper_" + path.strip("/").replace("/", "_")
    try:
        data = base.get_json(url, cache_key=(cache_key if ttl else None), ttl=ttl)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
        sys.stderr.write("[sleeper_draft] GET %s failed: %s\n" % (url, exc))
        return default
    return default if data is None else data


def _norm_scoring(value):
    if not value:
        return base.DEFAULT_SCORING
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return _SCORING_MAP.get(key, base.DEFAULT_SCORING)


# --------------------------------------------------------------------------
# contract: discovery
# --------------------------------------------------------------------------

def nfl_state():
    """Current NFL season/week per Sleeper. {} if unreachable."""
    return _fetch("state/nfl", TTL_STATE, default={}) or {}


def resolve_user(username):
    """Username or user_id -> {'user_id','username','display_name'}; None if unknown."""
    if not username:
        return None
    handle = str(username).strip().lstrip("@")
    if not handle:
        return None
    data = _fetch("user/%s" % urllib.parse.quote(handle, safe=""), TTL_LIST)
    if not isinstance(data, dict) or not data.get("user_id"):
        return None
    return {
        "user_id": str(data.get("user_id")),
        "username": data.get("username") or handle,
        "display_name": data.get("display_name") or data.get("username") or handle,
    }


def user_drafts(user_id, season="2026"):
    """Walk the user's leagues, then each league's drafts. Newest first."""
    if not user_id:
        return []
    leagues = _fetch("user/%s/leagues/nfl/%s" % (user_id, season), TTL_LIST, default=[])
    if not isinstance(leagues, list):
        return []
    out = []
    for lg in leagues:
        if not isinstance(lg, dict):
            continue
        league_id = lg.get("league_id")
        if not league_id:
            continue
        drafts = _fetch("league/%s/drafts" % league_id, TTL_LIST, default=[])
        if not isinstance(drafts, list):
            continue
        for d in drafts:
            if not isinstance(d, dict):
                continue
            settings = d.get("settings") or {}
            meta = d.get("metadata") or {}
            dtype = (d.get("type") or "").lower()
            out.append({
                "draft_id": str(d.get("draft_id") or ""),
                "name": (meta.get("name") or lg.get("name")
                         or d.get("season") or "Draft"),
                "type": dtype,
                "status": d.get("status") or "",
                "season": str(d.get("season") or season),
                "league_id": str(league_id),
                "teams": _num(settings.get("teams"), lg.get("total_rosters")) or DEFAULT_LEAGUE["teams"],
                "budget": _num(settings.get("budget"), DEFAULT_LEAGUE["budget"]),
                "scoring": _norm_scoring(meta.get("scoring_type")),
                "is_auction": dtype == "auction",
                "_sort": _num(d.get("start_time"), 0) or _num(d.get("created"), 0) or 0,
            })
    out.sort(key=lambda r: r["_sort"], reverse=True)
    for row in out:
        row.pop("_sort", None)
    return out


# --------------------------------------------------------------------------
# contract: raw fetches (poll endpoints, ttl=0)
# --------------------------------------------------------------------------

def get_draft(draft_id):
    """Raw draft dict, or None."""
    if not draft_id:
        return None
    data = _fetch("draft/%s" % draft_id, TTL_POLL)
    return data if isinstance(data, dict) else None


def get_picks(draft_id):
    """Raw picks list (completed picks only). [] on failure."""
    if not draft_id:
        return []
    data = _fetch("draft/%s/picks" % draft_id, TTL_POLL, default=[])
    return data if isinstance(data, list) else []


def league_users(league_id):
    if not league_id:
        return []
    data = _fetch("league/%s/users" % league_id, TTL_LEAGUE, default=[])
    return data if isinstance(data, list) else []


def league_rosters(league_id):
    if not league_id:
        return []
    data = _fetch("league/%s/rosters" % league_id, TTL_LEAGUE, default=[])
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# contract: league config
# --------------------------------------------------------------------------

def league_config(draft):
    """DEFAULT_LEAGUE-shaped dict derived from a real draft object."""
    cfg = {
        "teams": DEFAULT_LEAGUE["teams"],
        "budget": DEFAULT_LEAGUE["budget"],
        "scoring": DEFAULT_LEAGUE["scoring"],
        "slots": dict(DEFAULT_LEAGUE["slots"]),
        "flex_pos": list(DEFAULT_LEAGUE["flex_pos"]),
    }
    if not isinstance(draft, dict):
        return cfg

    settings = draft.get("settings") or {}
    meta = draft.get("metadata") or {}

    cfg["teams"] = _num(settings.get("teams"), DEFAULT_LEAGUE["teams"]) or DEFAULT_LEAGUE["teams"]
    cfg["budget"] = _num(settings.get("budget"), DEFAULT_LEAGUE["budget"])
    if cfg["budget"] is None or cfg["budget"] <= 0:
        cfg["budget"] = DEFAULT_LEAGUE["budget"]
    cfg["scoring"] = _norm_scoring(meta.get("scoring_type"))

    slots = {}
    saw_flex_kinds = set()
    for raw_key, raw_val in settings.items():
        if not str(raw_key).startswith("slots_"):
            continue
        suffix = str(raw_key)[len("slots_"):].lower()
        if suffix in _SLOT_IGNORE:
            continue
        bucket = _SLOT_MAP.get(suffix)
        if bucket is None:
            continue
        count = _num(raw_val, 0) or 0
        if count <= 0:
            continue
        slots[bucket] = slots.get(bucket, 0) + count
        if bucket == "FLEX":
            saw_flex_kinds.add(suffix)
    if slots:
        cfg["slots"] = slots
        if saw_flex_kinds == {"rec_flex"}:
            cfg["flex_pos"] = ["WR", "TE"]
        elif saw_flex_kinds and saw_flex_kinds <= {"wrrb_flex", "rbwr_flex"}:
            cfg["flex_pos"] = ["RB", "WR"]
        else:
            cfg["flex_pos"] = list(DEFAULT_LEAGUE["flex_pos"])
    return cfg


def roster_size(league):
    """Total draftable roster spots = sum of all slot counts (bench included)."""
    slots = (league or {}).get("slots") or DEFAULT_LEAGUE["slots"]
    total = 0
    for v in slots.values():
        total += _num(v, 0) or 0
    return total


def max_bid(left, slots_left):
    """Always keep $1 for every other remaining slot. 0 means they are out."""
    left = _num(left, 0) or 0
    slots_left = _num(slots_left, 0) or 0
    if slots_left <= 0 or left <= 0:
        return 0
    return max(1, left - (slots_left - 1))


# --------------------------------------------------------------------------
# contract: pick + team parsing (pure functions -- unit-testable)
# --------------------------------------------------------------------------

def parse_picks(picks):
    """Raw Sleeper picks -> normalized rows. Auction amount is metadata['amount']
    (a STRING of dollars); non-auction drafts have none, so amount is None."""
    out = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        meta = p.get("metadata") or {}
        player_id = p.get("player_id")
        player_id = str(player_id) if player_id is not None else None
        pos = meta.get("position") or meta.get("pos")
        team = meta.get("team")
        first = (meta.get("first_name") or "").strip()
        last = (meta.get("last_name") or "").strip()
        name = (first + " " + last).strip()
        if not name:
            name = (meta.get("name") or "").strip()
        if not name and base.norm_pos(pos) == "DEF" and player_id:
            # Sleeper keys team defenses by the team abbreviation itself.
            name = player_id
            team = team or player_id
        rec = base.record(name or (player_id or ""), pos, team, sleeper_id=player_id)
        out.append({
            "player_id": player_id,
            "roster_id": _num(p.get("roster_id")),
            "picked_by": (str(p.get("picked_by")) if p.get("picked_by") else None),
            "draft_slot": _num(p.get("draft_slot")),
            "pick_no": _num(p.get("pick_no")),
            "amount": _num(meta.get("amount")),
            "name": rec["name"],
            "pos": rec["pos"],
            "team": rec["team"],
            "key": rec["key"],
        })
    out.sort(key=lambda r: (r["pick_no"] is None, r["pick_no"] or 0))
    return out


def _owner_labels(users):
    """user_id -> (team_name_or_display, display_name)."""
    labels = {}
    for u in users or []:
        if not isinstance(u, dict):
            continue
        uid = u.get("user_id")
        if not uid:
            continue
        meta = u.get("metadata") or {}
        display = u.get("display_name") or u.get("username") or ""
        team_name = (meta.get("team_name") or "").strip()
        labels[str(uid)] = (team_name or display or str(uid), display)
    return labels


def team_rows(draft, picks, league, users=None, rosters=None):
    """Per-team auction budget grid. Pure function of its inputs.

        left       = budget - spent
        slots_left = roster_size - filled
        max_bid    = max(1, left - (slots_left - 1))   # $1 held per open slot
                     (0 when they have no money or no slots left -> out)
    """
    draft = draft if isinstance(draft, dict) else {}
    league = league if isinstance(league, dict) else dict(DEFAULT_LEAGUE)
    rows_picks = picks or []
    if rows_picks and isinstance(rows_picks[0], dict) and "key" not in rows_picks[0]:
        rows_picks = parse_picks(rows_picks)

    budget = _num(league.get("budget"), DEFAULT_LEAGUE["budget"]) or DEFAULT_LEAGUE["budget"]
    size = roster_size(league)
    n_teams = _num(league.get("teams"), DEFAULT_LEAGUE["teams"]) or DEFAULT_LEAGUE["teams"]

    # slot <-> roster_id
    slot_to_roster = {}
    for k, v in (draft.get("slot_to_roster_id") or {}).items():
        slot = _num(k)
        rid = _num(v)
        if slot is not None and rid is not None:
            slot_to_roster[slot] = rid
    roster_to_slot = dict((rid, slot) for slot, rid in slot_to_roster.items())

    # roster_id -> owner user_id
    roster_to_user = {}
    for r in rosters or []:
        if not isinstance(r, dict):
            continue
        rid = _num(r.get("roster_id"))
        if rid is not None and r.get("owner_id"):
            roster_to_user[rid] = str(r.get("owner_id"))
    for uid, slot in (draft.get("draft_order") or {}).items():
        slot = _num(slot)
        rid = slot_to_roster.get(slot, slot)
        if rid is not None and not roster_to_user.get(rid):
            roster_to_user[rid] = str(uid)
    for p in rows_picks:
        rid, uid = p.get("roster_id"), p.get("picked_by")
        if rid is not None and uid and not roster_to_user.get(rid):
            roster_to_user[rid] = str(uid)
        if rid is not None and p.get("draft_slot") is not None:
            roster_to_slot.setdefault(rid, p.get("draft_slot"))

    labels = _owner_labels(users)

    # Which roster_ids exist: the declared team count, plus anything we saw.
    rids = set(range(1, n_teams + 1))
    rids.update(r for r in slot_to_roster.values() if r is not None)
    rids.update(r for r in roster_to_user if r is not None)
    rids.update(p["roster_id"] for p in rows_picks if p.get("roster_id") is not None)

    by_roster = {}
    for p in rows_picks:
        by_roster.setdefault(p.get("roster_id"), []).append(p)

    out = []
    for rid in sorted(rids, key=lambda r: (roster_to_slot.get(r, 10 ** 6), r)):
        mine = by_roster.get(rid, [])
        spent = 0
        for p in mine:
            spent += _num(p.get("amount"), 0) or 0
        filled = len(mine)
        left = budget - spent
        slots_left = size - filled
        uid = roster_to_user.get(rid)
        owner, display = labels.get(uid or "", (None, ""))
        out.append({
            "roster_id": rid,
            "slot": roster_to_slot.get(rid),
            "user_id": uid,
            "owner": owner or ("Team %s" % rid),
            "display_name": display or "",
            "spent": spent,
            "left": left,
            "filled": filled,
            "slots_left": slots_left,
            "max_bid": max_bid(left, slots_left),
            "roster": sorted(mine, key=lambda p: (p["pick_no"] is None, p["pick_no"] or 0)),
        })
    return out


# --------------------------------------------------------------------------
# contract: the one call the UI polls
# --------------------------------------------------------------------------

def draft_state(draft_id):
    """Everything the board needs for one poll tick.

    Degrades clearly: on a bad/unreachable draft_id you still get the full shape
    back with an "error" string set, so callers never KeyError mid-draft.
    """
    draft = get_draft(draft_id)
    if draft is None:
        league = dict(DEFAULT_LEAGUE)
        return {
            "draft": None, "league": league, "status": "unknown", "is_auction": False,
            "picks": [], "drafted_ids": [], "drafted_keys": [],
            "teams": [], "totals": _totals([], league, []),
            "error": "draft %s not found or Sleeper unreachable" % draft_id,
        }

    league = league_config(draft)
    raw_picks = get_picks(draft_id)
    picks = parse_picks(raw_picks)

    league_id = draft.get("league_id")
    users = league_users(league_id) if league_id else []
    rosters = league_rosters(league_id) if league_id else []

    teams = team_rows(draft, picks, league, users=users, rosters=rosters)
    drafted_ids = [p["player_id"] for p in picks if p.get("player_id")]
    drafted_keys = sorted(set(p["key"] for p in picks if p.get("key")))

    return {
        "draft": draft,
        "league": league,
        "status": draft.get("status") or "unknown",
        "is_auction": (draft.get("type") or "").lower() == "auction",
        "picks": picks,
        "drafted_ids": drafted_ids,
        "drafted_keys": drafted_keys,
        "teams": teams,
        "totals": _totals(teams, league, picks),
        "error": None,
    }


def _totals(teams, league, picks):
    n_teams = _num((league or {}).get("teams"), DEFAULT_LEAGUE["teams"]) or DEFAULT_LEAGUE["teams"]
    budget = _num((league or {}).get("budget"), DEFAULT_LEAGUE["budget"]) or DEFAULT_LEAGUE["budget"]
    size = roster_size(league)
    if teams:
        budget_total = budget * len(teams)
        spent_total = sum(t.get("spent", 0) for t in teams)
        left_total = sum(t.get("left", 0) for t in teams)
        slots_total = size * len(teams)
        slots_left = sum(t.get("slots_left", 0) for t in teams)
    else:
        budget_total = budget * n_teams
        spent_total = 0
        left_total = budget_total
        slots_total = size * n_teams
        slots_left = slots_total
    return {
        "budget_total": budget_total,
        "spent_total": spent_total,
        "left_total": left_total,
        "picks_made": len(picks or []),
        "slots_total": slots_total,
        "slots_left": slots_left,
    }


# --------------------------------------------------------------------------
# smoke test / CLI
# --------------------------------------------------------------------------

def _fixture():
    """Synthetic 4-team, $200, auction draft used by --selftest."""
    draft = {
        "draft_id": "FIXTURE", "league_id": None, "type": "auction",
        "status": "drafting", "season": "2026",
        "settings": {"teams": 4, "budget": 200, "rounds": 9,
                     "slots_qb": 1, "slots_rb": 2, "slots_wr": 3, "slots_te": 1,
                     "slots_flex": 1, "slots_k": 1, "slots_def": 1, "slots_bn": 6,
                     "slots_ir": 2},
        "metadata": {"scoring_type": "half_ppr", "name": "Fixture League"},
        "draft_order": {"u1": 1, "u2": 2, "u3": 3, "u4": 4},
        "slot_to_roster_id": {"1": 1, "2": 2, "3": 3, "4": 4},
    }
    users = [
        {"user_id": "u1", "display_name": "ikam", "metadata": {"team_name": "Chase Money"}},
        {"user_id": "u2", "display_name": "Dana", "metadata": {}},
        {"user_id": "u3", "display_name": "Roy", "metadata": {"team_name": "Punt QB"}},
        {"user_id": "u4", "display_name": "Sam", "metadata": {}},
    ]
    rosters = [{"roster_id": i, "owner_id": "u%d" % i} for i in (1, 2, 3, 4)]
    picks = [
        {"player_id": "7564", "roster_id": 1, "picked_by": "u1", "draft_slot": 1, "pick_no": 1,
         "metadata": {"first_name": "Ja'Marr", "last_name": "Chase", "position": "WR",
                      "team": "CIN", "amount": "72"}},
        {"player_id": "4034", "roster_id": 2, "picked_by": "u2", "draft_slot": 2, "pick_no": 2,
         "metadata": {"first_name": "Christian", "last_name": "McCaffrey", "position": "RB",
                      "team": "SF", "amount": "54"}},
        {"player_id": "BAL", "roster_id": 3, "picked_by": "u3", "draft_slot": 3, "pick_no": 3,
         "metadata": {"first_name": "Baltimore", "last_name": "Ravens", "position": "DEF",
                      "team": "BAL", "amount": "3"}},
        {"player_id": "6797", "roster_id": 1, "picked_by": "u1", "draft_slot": 1, "pick_no": 4,
         "metadata": {"first_name": "Justin", "last_name": "Jefferson", "position": "WR",
                      "team": "MIN", "amount": "113"}},
    ]
    return draft, picks, users, rosters


def _selftest():
    ok = True

    def check(label, got, want):
        nonlocal_ok = got == want
        print("  %s %-38s got=%r want=%r" % ("PASS" if nonlocal_ok else "FAIL", label, got, want))
        return nonlocal_ok

    print("league_config:")
    draft, raw_picks, users, rosters = _fixture()
    lg = league_config(draft)
    ok &= check("teams", lg["teams"], 4)
    ok &= check("budget", lg["budget"], 200)
    ok &= check("scoring", lg["scoring"], "half_ppr")
    ok &= check("slots", lg["slots"], {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1,
                                       "K": 1, "DEF": 1, "BN": 6})
    ok &= check("roster_size (IR excluded)", roster_size(lg), 16)
    ok &= check("empty draft -> defaults", league_config(None), DEFAULT_LEAGUE)
    ok &= check("scoring fallback (unknown)",
                league_config({"metadata": {"scoring_type": "bananas"}})["scoring"], "half_ppr")
    ok &= check("scoring ppr",
                league_config({"metadata": {"scoring_type": "ppr"}})["scoring"], "ppr")

    print("parse_picks:")
    picks = parse_picks(raw_picks)
    ok &= check("count", len(picks), 4)
    ok &= check("amount is int", picks[0]["amount"], 72)
    ok &= check("key joins to sources", picks[0]["key"], base.player_key("Ja'Marr Chase", "WR", "CIN"))
    ok &= check("DEF key", picks[2]["key"], "DEF|BAL")
    ok &= check("DEF name", picks[2]["name"], "Baltimore Ravens")
    ok &= check("no-amount pick -> None",
                parse_picks([{"player_id": "1", "roster_id": 1, "pick_no": 1,
                              "metadata": {"first_name": "A", "last_name": "B",
                                           "position": "RB", "team": "SF"}}])[0]["amount"], None)

    print("team_rows / budget math:")
    teams = team_rows(draft, picks, lg, users=users, rosters=rosters)
    ok &= check("team count", len(teams), 4)
    t1, t2, t3, t4 = teams
    ok &= check("t1 owner (team_name wins)", t1["owner"], "Chase Money")
    ok &= check("t2 owner (display fallback)", t2["owner"], "Dana")
    ok &= check("t1 spent", t1["spent"], 185)
    ok &= check("t1 left", t1["left"], 15)
    ok &= check("t1 filled", t1["filled"], 2)
    ok &= check("t1 slots_left", t1["slots_left"], 14)
    ok &= check("t1 max_bid (15-13)", t1["max_bid"], 2)
    ok &= check("t2 max_bid (146-14)", t2["max_bid"], 132)
    ok &= check("t4 untouched left", t4["left"], 200)
    ok &= check("t4 max_bid (200-15)", t4["max_bid"], 185)
    ok &= check("t3 roster len", len(t3["roster"]), 1)
    ok &= check("slot preserved", t3["slot"], 3)
    ok &= check("raw picks accepted too",
                team_rows(draft, raw_picks, lg, users, rosters)[0]["spent"], 185)

    print("non-auction (snake) draft:")
    snake = {"type": "snake", "status": "complete",
             "settings": {"teams": 2, "slots_qb": 1, "slots_rb": 2, "slots_bn": 1},
             "slot_to_roster_id": {"1": 1, "2": 2}}
    slg = league_config(snake)
    ok &= check("snake budget default", slg["budget"], 200)
    ok &= check("snake slots", slg["slots"], {"QB": 1, "RB": 2, "BN": 1})
    snake_picks = [
        {"player_id": "1", "roster_id": 1, "pick_no": 1, "draft_slot": 1,
         "metadata": {"first_name": "Josh", "last_name": "Allen", "position": "QB", "team": "BUF"}},
        {"player_id": "2", "roster_id": 2, "pick_no": 2, "draft_slot": 2,
         "metadata": {"first_name": "Bijan", "last_name": "Robinson", "position": "RB", "team": "ATL"}},
    ]
    srows = team_rows(snake, snake_picks, slg)
    ok &= check("snake spent stays 0", [r["spent"] for r in srows], [0, 0])
    ok &= check("snake filled counted", [r["filled"] for r in srows], [1, 1])
    ok &= check("snake slots_left", [r["slots_left"] for r in srows], [3, 3])

    print("superflex / rec_flex configs:")
    sf = league_config({"settings": {"teams": 12, "budget": 300, "slots_qb": 1, "slots_rb": 2,
                                     "slots_wr": 2, "slots_te": 1, "slots_super_flex": 1,
                                     "slots_bn": 5},
                        "metadata": {"scoring_type": "ppr"}})
    ok &= check("superflex bucket", sf["slots"].get("SUPER_FLEX"), 1)
    ok &= check("budget 300", sf["budget"], 300)
    ok &= check("flex_pos default", sf["flex_pos"], ["RB", "WR", "TE"])
    rf = league_config({"settings": {"slots_wr": 2, "slots_rec_flex": 1, "slots_bn": 4}})
    ok &= check("rec_flex -> FLEX", rf["slots"].get("FLEX"), 1)
    ok &= check("rec_flex flex_pos", rf["flex_pos"], ["WR", "TE"])
    mf = league_config({"settings": {"slots_flex": 1, "slots_wrrb_flex": 1, "slots_bn": 4}})
    ok &= check("flex buckets sum", mf["slots"].get("FLEX"), 2)

    print("max_bid edges:")
    ok &= check("broke", max_bid(0, 5), 0)
    ok &= check("negative left", max_bid(-3, 5), 0)
    ok &= check("roster full", max_bid(50, 0), 0)
    ok &= check("last slot takes it all", max_bid(50, 1), 50)
    ok &= check("floor is $1", max_bid(3, 9), 1)

    print("totals:")
    tot = _totals(teams, lg, picks)
    ok &= check("budget_total", tot["budget_total"], 800)
    ok &= check("spent_total", tot["spent_total"], 242)
    ok &= check("left_total", tot["left_total"], 558)
    ok &= check("picks_made", tot["picks_made"], 4)
    ok &= check("slots_total", tot["slots_total"], 64)
    ok &= check("slots_left", tot["slots_left"], 60)

    print("degradation:")
    st = draft_state("definitely-not-a-real-draft-id")
    ok &= check("shape intact", sorted(st.keys()),
                ["draft", "drafted_ids", "drafted_keys", "error", "is_auction", "league",
                 "picks", "status", "teams", "totals"])
    ok &= check("error set", bool(st["error"]), True)
    ok &= check("resolve_user(None)", resolve_user(None), None)
    ok &= check("user_drafts(None)", user_drafts(None), [])
    ok &= check("get_picks(None)", get_picks(None), [])
    ok &= check("get_draft(None)", get_draft(None), None)

    print("\n%s" % ("ALL SELFTESTS PASSED" if ok else "SELFTEST FAILURES"))
    return 0 if ok else 1


def main(argv):
    if len(argv) > 1 and argv[1] in ("--selftest", "-t"):
        return _selftest()
    if len(argv) < 2:
        print("usage: python3 sleeper_draft.py <draft_id>")
        print("       python3 sleeper_draft.py --selftest")
        print("\nnfl_state(): %s" % json.dumps(nfl_state(), sort_keys=True))
        return 2
    state = draft_state(argv[1])
    slim = dict(state)
    slim["draft"] = {k: state["draft"].get(k) for k in
                     ("draft_id", "league_id", "type", "status", "season",
                      "settings", "metadata")} if state.get("draft") else None
    print(json.dumps(slim, indent=2, sort_keys=True, default=str))
    return 0 if not state.get("error") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
