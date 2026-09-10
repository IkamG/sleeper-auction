"""FantasyPros ranking-source adapter (ECR + ADP + auction values).

Python 3.9, stdlib only.

WHY THIS SHAPE (verified 2026-09-08, do not "simplify" without re-probing):

  * https://www.fantasypros.com/nfl/adp/half-point-ppr-overall.php returns HTTP 200
    with a `window.FP.reportConfig = {...}` JSON blob, but for anonymous clients that
    blob carries exactly FIVE table rows -- the page ships a registration fence
    (`window.registrationFence`) and the Vue app renders straight from that blob with
    no follow-up XHR. So the pretty ADP page is useless to us: 5 rows, not 300.
  * https://www.fantasypros.com/nfl/projections/<pos>.php is fenced the same way
    (10 rows per position).
  * The RANKINGS cheat-sheet pages are NOT fenced. They embed two JS payloads:
        var ecrData = {...}   -> 957 half-PPR players w/ ECR rank, tier, team, bye, pos
        var adpData = [...]   -> [{player_id, rank_ecr}] == the consensus ADP order,
                                 byte-for-byte the same ordering the fenced ADP page
                                 shows for its 5 visible rows.
    Both are scoring-specific (separate URL per STD / PPR / HALF).
  * The auction calculator page (calculator.php) is a shell; the real numbers come from
    the Draft Wizard backend it embeds:
        https://draftwizard.fantasypros.com/auction/fp_nfl.jsp?teams=12&tb=200&scoring=HALF
    That page emits <tr pid='22968' v='62' pts='335' class=' PlayerRB'> rows -- real
    dollar values AND real projected points, and `scoring=` natively changes both
    (Ja'Marr Chase: $52/216 STD, $58/277 HALF, $60/338 PPR). `tb=` is the budget knob
    (tb=300 scales Josh Allen $29 -> $44); the `budget=` name used in the public
    calculator URL is ignored by the JSP but 200 is its default anyway.

  `pid` in the auction table IS the FantasyPros `player_id` from ecrData, so the join
  is exact -- no name matching, no fuzzy merge.

Everything is native half-PPR. Nothing here is synthesized from PPR/STD.
"""

import json
import re
import sys

try:
    from .base import http_get, get_json, record, player_key  # noqa: F401
except ImportError:  # standalone: python3 sources/fantasypros.py
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from base import http_get, get_json, record, player_key  # noqa: F401

NAME = "fantasypros"
LABEL = "FantasyPros (ECR + ADP + Auction $)"
CAPS = {"adp": True, "auction": True, "proj": True, "half_ppr": True}

# Auction calculator defaults -- the league we are drafting for.
AUCTION_TEAMS = 12
AUCTION_BUDGET = 200

# scoring -> (cheatsheet page slug, Draft Wizard scoring code)
_SCORING = {
    "half_ppr": ("half-point-ppr-cheatsheets.php", "HALF"),
    "ppr": ("ppr-cheatsheets.php", "PPR"),
    "std": ("consensus-cheatsheets.php", "STD"),
}

_RANKS_URL = "https://www.fantasypros.com/nfl/rankings/%s"
_AUCTION_URL = ("https://draftwizard.fantasypros.com/auction/fp_nfl.jsp"
                "?teams=%d&tb=%d&scoring=%s")

# <tr pid='22968' v='62' pts='335' class=' PlayerRB'>  -- quotes are sometimes
# backslash-escaped because the table is emitted inside a JS string literal.
_AUCTION_ROW = re.compile(
    r"<tr\s+pid=\\?'(\d+)\\?'\s+v=\\?'(-?[\d.]+)\\?'\s+pts=\\?'(-?[\d.]+)\\?'"
    r"\s+class=\\?'\s*Player(\w+)\\?'\\?'?>(.*?)</tr>",
    re.S,
)
# "Ja'Marr Chase (CIN - WR)" inside the row's name cell (fallback identity only).
_AUCTION_NAME = re.compile(r"<td>\s*([^<(]+?)\s*\((\w+)\s*-\s*(\w+)\)")
_TAGS = re.compile(r"<[^>]*>")

# Notes from the most recent fetch(): coverage counts, degraded sub-fetches, etc.
# Populated on every call so a partial fetch is never silently swallowed.
LAST_NOTES = []


def _extract_js_literal(text, marker, opener):
    """Pull the JS object/array literal that follows `marker` out of `text`.

    Brace-matches while respecting string literals and escapes, so it survives the
    apostrophes and braces inside player names.
    """
    i = text.find(marker)
    if i < 0:
        return None
    try:
        start = text.index(opener, i)
    except ValueError:
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                if c != closer:
                    return None
                return text[start:j + 1]
    return None


def _fetch_rankings(season, scoring, slug):
    """ecrData (ranks/tiers/identity) + adpData (consensus ADP order) for one scoring."""
    url = _RANKS_URL % slug
    html = http_get(url, cache_key="fp_ranks_%s_%s" % (season, scoring), ttl=1800)

    raw = _extract_js_literal(html, "var ecrData", "{")
    if not raw:
        raise RuntimeError(
            "FantasyPros: no 'var ecrData' payload in %s (%d bytes) -- page layout "
            "changed or the request was fenced/blocked." % (url, len(html)))
    ecr = json.loads(raw)
    players = ecr.get("players") or []
    if len(players) < 100:
        raise RuntimeError(
            "FantasyPros: ecrData held only %d players at %s -- expected 500+. "
            "Refusing to publish a truncated board." % (len(players), url))

    adp_by_id = {}
    raw_adp = _extract_js_literal(html, "var adpData", "[")
    if raw_adp:
        for row in json.loads(raw_adp):
            pid = row.get("player_id")
            pos = row.get("rank_ecr")
            if pid is not None and pos is not None:
                adp_by_id[int(pid)] = pos
    else:
        LAST_NOTES.append("no 'var adpData' payload at %s -- ADP column is empty" % url)

    return ecr, players, adp_by_id


def _fetch_auction(season, scoring, dw_code, teams, budget):
    """pid -> (dollars, projected points, pos, name, team) from the Draft Wizard JSP."""
    url = _AUCTION_URL % (teams, budget, dw_code)
    html = http_get(
        url,
        cache_key="fp_auction_%s_%s_t%d_b%d" % (season, scoring, teams, budget),
        ttl=1800,
    )
    out = {}
    for pid, dollars, pts, pos, body in _AUCTION_ROW.findall(html):
        # The JSP prints each player twice (values table + sortable clone); dedupe.
        pid = int(pid)
        if pid in out:
            continue
        name = team = None
        m = _AUCTION_NAME.search(body)
        if m:
            name, team, pos_cell = m.group(1).strip(), m.group(2), m.group(3)
            pos = pos_cell or pos
        else:
            # Defenses render as "Houston Texans" with no (TEAM - POS) suffix.
            txt = _TAGS.sub(" ", body).split("$")[0].strip()
            name = txt or None
        out[pid] = {
            "auction": float(dollars),
            "proj_pts": float(pts),
            "pos": pos,
            "name": name,
            "team": team,
        }
    if not out:
        raise RuntimeError(
            "FantasyPros: auction JSP returned 0 parseable rows at %s (%d bytes)"
            % (url, len(html)))
    return out, url


def fetch(season="2026", scoring="half_ppr", teams=AUCTION_TEAMS, budget=AUCTION_BUDGET):
    """Return a list of base.record() dicts for FantasyPros.

    scoring is one of half_ppr|ppr|std -- all three are NATIVE on FantasyPros
    (separate cheat-sheet URL + separate Draft Wizard scoring code); nothing is
    synthesized. Raises on hard failure.

    Each record carries the standard base.record() keys plus two extras:
      'bye'   -- bye week (int) or None
      'fp_id' -- FantasyPros player_id, the join key used internally
    """
    scoring = (scoring or "half_ppr").lower()
    if scoring not in _SCORING:
        raise ValueError("FantasyPros: unknown scoring %r (want one of %s)"
                         % (scoring, "/".join(_SCORING)))
    slug, dw_code = _SCORING[scoring]

    del LAST_NOTES[:]

    ecr, players, adp_by_id = _fetch_rankings(season, scoring, slug)

    data_year = str(ecr.get("year") or "")
    if data_year and data_year != str(season):
        LAST_NOTES.append(
            "FantasyPros is publishing %s rankings; caller asked for season %s "
            "(the cheat-sheet URLs always serve the live season)." % (data_year, season))
    if (ecr.get("scoring") or "").upper() != dw_code:
        LAST_NOTES.append("cheat-sheet page reported scoring=%r, expected %r"
                          % (ecr.get("scoring"), dw_code))

    # Auction dollars are a bonus layer: if Draft Wizard is down we still want the
    # ADP/ECR board rather than nothing. Never silent -- it lands in LAST_NOTES.
    auction = {}
    auction_url = None
    try:
        auction, auction_url = _fetch_auction(season, scoring, dw_code, teams, budget)
    except Exception as exc:  # noqa: BLE001 -- degrade, but loudly
        LAST_NOTES.append("AUCTION FETCH FAILED (%s: %s) -- rows have adp/rank but "
                          "no auction $ and no proj_pts" % (type(exc).__name__, exc))

    out = []
    seen = set()
    for p in players:
        pid = p.get("player_id")
        pid = int(pid) if pid is not None else None
        pos = p.get("player_position_id") or p.get("player_positions")
        name = p.get("player_name") or ""
        if not name or not pos:
            continue
        team = p.get("player_team_id") or None
        a = auction.get(pid) or {}
        try:
            bye = int(p.get("player_bye_week"))
        except (TypeError, ValueError):
            bye = None
        r = record(
            name=name,
            pos=pos,
            team=team,
            adp=adp_by_id.get(pid),
            auction=a.get("auction"),
            rank=p.get("rank_ecr"),
            proj_pts=a.get("proj_pts"),
            tier=p.get("tier"),
        )
        r["bye"] = bye
        r["fp_id"] = pid
        out.append(r)
        if pid is not None:
            seen.add(pid)

    # Anyone the auction calculator priced but the ECR board omitted still belongs
    # on an auction board -- carry them with the identity the JSP gave us.
    extras = 0
    for pid, a in auction.items():
        if pid in seen or not a.get("name"):
            continue
        r = record(
            name=a["name"],
            pos=a.get("pos"),
            team=a.get("team"),
            adp=adp_by_id.get(pid),
            auction=a.get("auction"),
            rank=None,
            proj_pts=a.get("proj_pts"),
        )
        r["bye"] = None
        r["fp_id"] = pid
        out.append(r)
        extras += 1

    out.sort(key=lambda r: (r["rank"] is None, r["rank"] if r["rank"] is not None else 0))

    n_adp = sum(1 for r in out if r["adp"] is not None)
    n_auc = sum(1 for r in out if r["auction"] is not None)
    n_proj = sum(1 for r in out if r["proj_pts"] is not None)
    LAST_NOTES.append(
        "%d rows | rank+tier %d | adp %d (adpData had %d ids, %d matched the ECR board)"
        " | auction $ %d | proj %d | auction from %s"
        % (len(out), len(out), n_adp, len(adp_by_id), n_adp, n_auc, n_proj,
           auction_url or "N/A"))
    if extras:
        LAST_NOTES.append("%d auction-only players appended with rank=None" % extras)
    if auction and n_auc < 120:
        LAST_NOTES.append("auction coverage unexpectedly thin: %d priced players "
                          "(12-team/$200 normally prices ~178)" % n_auc)
    return out


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "half_ppr"
    rows = fetch(scoring=sc)
    print("scoring=%s  rows=%d" % (sc, len(rows)))
    for note in LAST_NOTES:
        print("  note: %s" % note)
    hdr = "%-4s %-24s %-4s %-4s %6s %6s %6s %5s %4s"
    print(hdr % ("RK", "NAME", "POS", "TM", "ADP", "AUC$", "PROJ", "TIER", "BYE"))
    for r in rows[:15]:
        print(hdr % (
            "" if r["rank"] is None else int(r["rank"]),
            r["name"][:24], r["pos"] or "", r["team"] or "",
            "" if r["adp"] is None else r["adp"],
            "" if r["auction"] is None else r["auction"],
            "" if r["proj_pts"] is None else r["proj_pts"],
            "" if r["tier"] is None else int(r["tier"]),
            "" if r.get("bye") is None else r["bye"],
        ))
