#!/usr/bin/env python3
"""Waiver wire: who to add, why, and how much FAAB to bid.

Two things decide a pickup, and they pull in different directions:

  need    -- how much better this player is than what he would replace in
             YOUR lineup, which is a property of your roster, not of him
  quality -- how good he is in absolute terms, which matters even without a
             need because a genuinely valuable player is worth rostering
             and the roster sorts itself out later

A pure-need model misses league-winners at positions you happen to be set at.
A pure-quality model tells a team with two elite QBs to bid on a third. This
scores both and requires a bigger edge before recommending a position you are
already fine at -- most sharply at single-slot positions, where the second one
never plays.

FAAB pricing is anchored on real demand: Sleeper publishes how many leagues
added each player in the last 24 hours, across millions of leagues. That is
crowd-sourced waiver demand measured rather than opined.

    python3 -m sleeper_auction.waivers --draft <id> --me <n> --week <n>
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleeper_auction import board as db          # noqa: E402
from sleeper_auction import sitstart             # noqa: E402

# How much of a weekly-points edge each position needs before a pickup is worth
# a roster spot. A single-slot position needs a far bigger gap: the backup
# never plays, so a marginal upgrade there is worth close to nothing, while an
# extra RB or WR slots straight into the flex.
POS_HURDLE = {"QB": 5.0, "TE": 3.0, "K": 3.0, "DEF": 3.0, "RB": 1.0, "WR": 1.0}
FLEX_POS = ("RB", "WR", "TE")

# FAAB bands, as a share of the season budget. These are the shapes the
# fantasy community converges on; demand and need move a player between bands.
FAAB_BANDS = [
    (28.0, 60.0, "league-winner"),      # immediate every-week starter, big upside
    (12.0, 27.0, "clear starter"),      # steps into your lineup now
    (5.0, 11.0, "flex / high-upside"),  # plays some weeks, real ceiling
    (2.0, 4.0, "stash"),                # worth a bench spot
    (0.0, 1.0, "streamer"),             # this week only
]


# ------------------------------------------------------------------ inputs

def rostered_ids(league_id):
    """Every player id on a roster in this league."""
    try:
        rosters = db.gj("https://api.sleeper.app/v1/league/%s/rosters" % league_id,
                        ttl=120)
    except Exception:
        return set(), {}
    taken, by_roster = set(), {}
    for r in rosters:
        ids = [str(x) for x in (r.get("players") or [])]
        by_roster[r.get("roster_id")] = ids
        taken.update(ids)
    return taken, by_roster


def trending(kind="add", hours=24, limit=200):
    """How many leagues added (or dropped) each player in the window.

    Real measured demand across Sleeper's whole user base, not an expert
    opinion, and the single best free signal for what a player will cost.
    """
    try:
        rows = db.gj("https://api.sleeper.app/v1/players/nfl/trending/%s"
                     "?lookback_hours=%d&limit=%d" % (kind, hours, limit),
                     key="trend-%s-%d" % (kind, hours), ttl=1800)
    except Exception:
        return {}
    out, top = {}, max((r.get("count") or 0) for r in rows) if rows else 1
    for i, r in enumerate(rows):
        pid = str(r.get("player_id"))
        cnt = r.get("count") or 0
        out[pid] = {"count": cnt, "rank": i + 1,
                    "share": round(cnt / top, 3) if top else 0.0}
    return out


def reddit_posts(limit=40):
    """Recent r/fantasyfootball posts, for injury and role news.

    Reddit's JSON endpoints now return HTML to unauthenticated clients; the
    Atom feed still works and carries the same titles and links, which is all
    that is wanted here.
    """
    try:
        xml = db.get("https://www.reddit.com/r/fantasyfootball/hot.rss?limit=%d" % limit,
                     headers={"User-Agent": db.UA}, key="reddit-ff", ttl=1800)
    except Exception:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        t = e.find("a:title", ns)
        link = e.find("a:link", ns)
        upd = e.find("a:updated", ns)
        if t is not None and t.text:
            out.append({"title": t.text.strip(),
                        "url": (link.get("href") if link is not None else None),
                        "updated": (upd.text if upd is not None else None)})
    return out


# ------------------------------------------------------------------ scoring

def _starters(my_players, league):
    """My current best starter at each position, by weekly projection."""
    slots = league.get("slots", {})
    by_pos = {}
    for p in my_players:
        by_pos.setdefault(p["pos"], []).append(p)
    for lst in by_pos.values():
        lst.sort(key=lambda x: -(x.get("wk_proj") or 0))
    out = {}
    for pos, lst in by_pos.items():
        n = slots.get(pos, 0)
        out[pos] = {"starters": lst[:n] if n else [],
                    "worst_starter": lst[n - 1] if n and len(lst) >= n else None,
                    "depth": len(lst), "all": lst}
    return out


def _flex_bar(mine):
    """The weekly projection a player must beat to crack the flex."""
    pool = []
    for pos in FLEX_POS:
        info = mine.get(pos)
        if not info:
            continue
        pool.extend(info["all"][len(info["starters"]):])
    pool.sort(key=lambda x: -(x.get("wk_proj") or 0))
    return (pool[0].get("wk_proj") or 0) if pool else 0.0


def score_candidate(fa, mine, flex_bar, league, trend):
    """Rate one free agent on need, quality, and what he would displace."""
    pos = fa["pos"]
    info = mine.get(pos) or {"worst_starter": None, "starters": [], "all": []}
    worst = info.get("worst_starter")
    wk = fa.get("wk_proj") or 0.0

    # Need: how much better than the man he would actually replace. For a
    # flex-eligible player that is whichever is easier to beat -- his own
    # position's weakest starter, or the current flex bar.
    bar = (worst.get("wk_proj") or 0.0) if worst else 0.0
    if pos in FLEX_POS and flex_bar and flex_bar < bar:
        bar = flex_bar
    if not info.get("starters"):
        bar = 0.0                      # nothing rostered here at all
    need_delta = round(wk - bar, 2)

    # Quality: season-long value, independent of my roster. This is what stops
    # the model from ignoring a genuinely elite player at a set position.
    quality = fa.get("season_value") or 0.0

    hurdle = POS_HURDLE.get(pos, 1.5)
    clears = need_delta >= hurdle

    tr = trend.get(fa.get("sleeper_id")) or {}
    demand = tr.get("share", 0.0)

    # Need is the spine of the ranking. A player who would make the lineup
    # worse must not out-rank a real upgrade just because the whole internet
    # is adding him -- demand sets his PRICE, not his value to this roster.
    if need_delta <= 0:
        core = need_delta                      # full penalty, no discount
    elif clears:
        core = need_delta
    else:
        core = need_delta * 0.4                # right player, wrong roster

    # Only genuine season-long assets earn a quality bonus; a $1 replacement
    # -level player gets nothing for existing.
    quality_term = max(0.0, quality - 5.0) * 0.15

    # Demand is a tiebreaker among players who actually help, and barely
    # registers for one who does not.
    demand_term = demand * (2.0 if need_delta > 0 else 0.5)

    score = core + quality_term + demand_term
    return {"need_delta": need_delta, "bar": round(bar, 2), "clears_hurdle": clears,
            "hurdle": hurdle, "quality": quality, "demand": demand,
            "trend_rank": tr.get("rank"), "trend_count": tr.get("count"),
            "replaces": (worst or {}).get("name"), "score": round(score, 2)}


def faab_bid(cand, budget_left, league):
    """Recommended FAAB as a percentage of the season budget, and in dollars.

    Anchored on measured demand (how many leagues actually added him in the
    last 24 hours) and on how much he improves this specific lineup, then
    capped by what is actually left to spend.
    """
    need = cand["need_delta"]
    demand = cand["demand"]
    q = cand["quality"]

    # Need is the dominant term: a player who does not improve the lineup is
    # not worth real money regardless of how many people are chasing him.
    pct = 0.0
    if need >= 6:
        pct = 30.0
    elif need >= 4:
        pct = 18.0
    elif need >= 2:
        pct = 9.0
    elif need >= 0.5:
        pct = 3.5
    else:
        pct = 1.0
    pct *= 1.0 + 0.8 * demand              # crowd demand raises the clearing price
    if q >= 25:
        pct *= 1.25                        # genuine season-long asset
    if not cand["clears_hurdle"]:
        # A player who does not clear his position's hurdle buys a bench spot,
        # not a lineup upgrade. Cap him at token money however good the raw
        # projection looks -- the model flagged the previous 0.5x discount as
        # still far too rich, and it was right.
        pct = min(pct * 0.25, 2.0)
    pct = max(0.0, min(55.0, pct))

    band = next((lbl for lo, hi, lbl in FAAB_BANDS if lo <= pct <= hi), "streamer")
    dollars = None
    if budget_left is not None:
        dollars = int(round(budget_left * pct / 100.0))
    return {"faab_pct": round(pct, 1), "faab_dollars": dollars, "band": band}


# ------------------------------------------------------------------ board

def build_board(draft_id, roster_id, week, limit=25):
    """Assemble free agents, my needs, demand and suggested bids. No LLM."""
    pool = db.value_pool(db.build_pool()[0])
    byid = {p["sleeper_id"]: p for p in pool if p.get("sleeper_id")}
    state = db.draft_state(draft_id)
    league_id = (state["draft"] or {}).get("league_id")
    if not league_id:
        raise SystemExit("draft %s has no league_id" % draft_id)

    taken, by_roster = rostered_ids(league_id)
    if not taken:
        raise SystemExit("no rosters returned for league %s -- is the season live?"
                         % league_id)
    wk = sitstart.week_projections(week)
    tr = trending("add")
    dropped = trending("drop")
    depth = sitstart.depth_charts()
    inj = sitstart.injuries()
    lines = sitstart.vegas(week)

    def enrich(pid, p):
        team = p.get("team")
        ln = lines.get(team) or {}
        return {"sleeper_id": pid, "name": p["name"], "pos": p["pos"], "team": team,
                "season_value": p.get("base"), "season_tier": p.get("tier"),
                "value_conf": p.get("value_conf"),
                "wk_proj": (wk.get(pid) or {}).get("proj"),
                "opponent": ln.get("opp"), "implied_total": ln.get("implied"),
                "depth": depth.get(pid), "injury": inj.get(db.nname(p["name"])),
                "rostered": pid in taken}

    mine_ids = by_roster.get(roster_id) or []
    my_players = [enrich(pid, byid[pid]) for pid in mine_ids if pid in byid]
    mine = _starters(my_players, state["league"])
    flex_bar = _flex_bar(mine)

    # Only consider free agents with a real projection or genuine demand --
    # the pool has hundreds of players nobody would ever roster.
    cands = []
    for pid, p in byid.items():
        if pid in taken or not p.get("pos"):
            continue
        c = enrich(pid, p)
        if not c["wk_proj"] and pid not in tr:
            continue
        c.update(score_candidate(c, mine, flex_bar, state["league"], tr))
        cands.append(c)
    cands.sort(key=lambda x: -x["score"])

    # Roster weaknesses, stated in the same units as the candidates.
    needs = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        info = mine.get(pos) or {"starters": [], "all": [], "worst_starter": None}
        slots = state["league"].get("slots", {}).get(pos, 0)
        starters = info["starters"]
        needs.append({
            "pos": pos, "slots": slots, "rostered": len(info["all"]),
            "starters": [{"name": s["name"], "wk_proj": s.get("wk_proj")}
                         for s in starters],
            "weakest_starter": (info["worst_starter"] or {}).get("name"),
            "weakest_proj": (info["worst_starter"] or {}).get("wk_proj"),
            "unfilled": max(0, slots - len(info["all"])),
            "hurdle": POS_HURDLE.get(pos, 1.5)})

    # Sleeper exposes the league's FAAB budget; spent-to-date is not in the
    # public API, so this is the season budget and the caller can override.
    budget_left = None
    try:
        lg = db.gj("https://api.sleeper.app/v1/league/%s" % league_id, ttl=600)
        wb = (lg.get("settings") or {}).get("waiver_budget")
        if wb:
            budget_left = int(wb)
    except Exception:
        pass
    for c in cands[:limit]:
        c.update(faab_bid(c, budget_left, state["league"]))

    return {"week": week, "league_id": league_id, "roster_id": roster_id,
            "team_name": next((t["owner"] for t in state["teams"]
                               if t["roster_id"] == roster_id), "me"),
            "league": state["league"], "flex_bar": round(flex_bar, 2),
            "faab_budget": budget_left,
            "my_roster": my_players, "needs": needs,
            "candidates": cands[:limit],
            "dropped": [{"id": k, **v} for k, v in list(dropped.items())[:10]],
            "free_agent_count": len(cands),
            "news": reddit_posts()}


# ------------------------------------------------------------------ AI layer

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["roster_read", "pickups"],
    "properties": {
        "roster_read": {"type": "string"},
        "pickups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "verdict", "faab_pct", "reason", "drop"],
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["PRIORITY", "TARGET", "SPECULATIVE", "PASS"]},
                    "faab_pct": {"type": "number"},
                    "reason": {"type": "string"},
                    "drop": {"type": "string",
                             "description": "who to drop, or 'no drop needed'"},
                    "risk": {"type": "string"},
                },
            },
        },
        "biggest_weakness": {"type": "string"},
        "budget_advice": {"type": "string"},
    },
}

SYSTEM = """You are a fantasy football waiver-wire analyst for a 12-team, \
half-PPR Sleeper league. Roster: QB1, RB2, WR2, TE1, FLEX (RB/WR/TE), K1, DEF1, \
4 bench.

Every number is computed for you. Do not invent statistics, and do not use \
player knowledge beyond the payload -- if something is not there, say it is \
unknown.

Two forces decide a pickup and they pull against each other:

- NEED is "need_delta": weekly projected points above the player he would \
actually replace in this lineup ("replaces", "bar"). This is a fact about the \
roster, not about the player.
- QUALITY is "season_value": his auction value in the abstract. A genuinely \
valuable player is worth rostering even without a need, because rosters churn \
and good players win leagues.

Recommend on both, with one restraint: "hurdle" is the weekly edge a position \
needs before an upgrade is worth a roster spot, and "clears_hurdle" says \
whether he beats it. The hurdle is high at single-slot positions (QB, TE, K, \
DEF) because the backup never plays -- so only recommend a QB when he is \
clearly, substantially better than the starter, not marginally. At RB and WR \
the hurdle is low, because a spare one slots into the flex.

- "demand" and "trend_count" are how many leagues actually added him in the \
last 24 hours. That is measured demand, not opinion, and it is what sets the \
price. High demand on a player who does not help this roster is a reason to \
let him go, not to chase him.
- "faab_pct" is a computed starting bid as a share of the season budget. Adjust \
it when the payload justifies it and say why. Bid aggressively for a player who \
genuinely starts; a bench stash is never worth real money.
- Recommend a specific drop for each add, from the bench, and never drop a \
starter for a speculative add. If no drop is needed, say so.
- "news" holds recent r/fantasyfootball post titles. Treat them as unverified \
chatter that may hint at injuries or role changes -- useful for a lead, never \
a fact. Never state something as true because a post title said it.

Be decisive and brief. Rank by what actually improves this lineup."""


def ai_analyze(board, api_key=None, refresh=False):
    payload = json.dumps({
        "week": board["week"], "team": board["team_name"],
        "flex_bar": board["flex_bar"],
        "needs": board["needs"],
        "my_roster": [{k: v for k, v in p.items() if k != "sleeper_id"}
                      for p in board["my_roster"]],
        "candidates": [{k: v for k, v in c.items() if k != "sleeper_id"}
                       for c in board["candidates"]],
        "news": [n["title"] for n in board["news"][:25]],
    }, default=str)
    key = "waivers-%s-w%s" % (board.get("roster_id"), board.get("week"))
    return sitstart.cached_ai(
        key, lambda: sitstart.run_model(SYSTEM, payload, SCHEMA, api_key=api_key),
        refresh=refresh)


# ------------------------------------------------------------------ output

def report(b, ai=None):
    o = ["%s -- week %s waiver wire" % (b["team_name"], b["week"]),
         "%d free agents considered | flex bar %.1f pts | FAAB budget %s"
         % (b["free_agent_count"], b["flex_bar"],
            ("$%d" % b["faab_budget"]) if b.get("faab_budget") else "unknown"), ""]
    o.append("ROSTER NEEDS")
    for n in b["needs"]:
        st = ", ".join("%s (%.1f)" % (s["name"], s["wk_proj"] or 0)
                       for s in n["starters"]) or "none"
        o.append("  %-4s %d slot(s), %d rostered | starters: %s%s"
                 % (n["pos"], n["slots"], n["rostered"], st,
                    "  <-- UNFILLED" if n["unfilled"] else ""))
    o.append("")
    o.append("%-24s %-4s %-7s %-7s %-8s %-9s %-7s %s"
             % ("PLAYER", "POS", "WK", "NEED", "VALUE", "ADDS/24h", "FAAB", "REPLACES"))
    o.append("-" * 100)
    for c in b["candidates"]:
        o.append("%-24s %-4s %-7s %-7s %-8s %-9s %-7s %s" % (
            c["name"][:24], c["pos"],
            ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "-",
            "%+.1f" % c["need_delta"],
            ("$%.0f" % c["quality"]) if c.get("quality") else "-",
            "{:,}".format(c["trend_count"]) if c.get("trend_count") else "-",
            ("%.0f%%%s" % (c.get("faab_pct", 0),
                           ("/$%d" % c["faab_dollars"]) if c.get("faab_dollars") else "")),
            (c.get("replaces") or "-")))
    if ai and not ai.get("error"):
        o.append("")
        o.append("=" * 100)
        o.append("AI ANALYSIS")
        o.append("  " + ai.get("roster_read", ""))
        for p in ai.get("pickups", []):
            o.append("")
            o.append("  %s -- %s (bid %.0f%%)"
                     % (p["name"], p["verdict"], p.get("faab_pct", 0)))
            o.append("    %s" % p["reason"])
            o.append("    drop: %s" % p.get("drop"))
            if p.get("risk"):
                o.append("    risk: %s" % p["risk"])
        if ai.get("biggest_weakness"):
            o.append("")
            o.append("  WEAKNESS: %s" % ai["biggest_weakness"])
        if ai.get("budget_advice"):
            o.append("  BUDGET  : %s" % ai["budget_advice"])
    elif ai:
        o.append("")
        o.append("AI analysis unavailable (%s): %s" % (ai["error"], ai["detail"][:200]))
    return "\n".join(o)


def ai_cards(ai):
    """Render the AI pickups. Shared by the initial page and the poll endpoint."""
    if not ai:
        return ""
    if ai.get("error"):
        return ('<div class="panel"><h3>AI analysis</h3><div class="mut">'
                'Unavailable (%s). %s</div></div>'
                % (ai["error"], str(ai.get("detail", ""))[:300]))
    cards = ""
    for p in ai.get("pickups", []):
        cls = {"PRIORITY": "v-must", "TARGET": "v-start",
               "SPECULATIVE": "v-flex", "PASS": "v-sit"}.get(p["verdict"], "v-flex")
        cards += ('<div class="card"><div class="ch"><span class="nm">%s</span>'
                  '<span class="verdict %s">%s</span>'
                  '<span class="faab">bid %.0f%%</span></div>'
                  '<div class="dec">%s</div>'
                  '<div class="mut">drop: %s%s</div></div>'
                  % (p["name"], cls, p["verdict"], p.get("faab_pct", 0),
                     p["reason"], p.get("drop"),
                     (" &middot; risk: " + p["risk"]) if p.get("risk") else ""))
    out = ('<div class="panel"><h3>AI analysis</h3><div class="read">%s</div>'
           '</div>%s' % (ai.get("roster_read", ""), cards))
    if ai.get("biggest_weakness") or ai.get("budget_advice"):
        out += ('<div class="panel"><h3>Summary</h3><div>%s</div>'
                '<div class="mut" style="margin-top:6px">%s</div></div>'
                % (ai.get("biggest_weakness", ""), ai.get("budget_advice", "")))
    return out


def html_report(b, ai=None, job_id=None):

    rows = "".join(
        '<tr><td class="nm">%s</td><td><span class="pos %s">%s</span></td>'
        '<td class="big">%s</td><td class="%s">%+.1f</td><td class="mut">%s</td>'
        '<td class="mut">%s</td><td class="faab">%s%%%s</td><td class="mut">%s</td></tr>'
        % (c["name"], c["pos"], c["pos"],
           ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "&mdash;",
           "pl" if c["need_delta"] > 0 else "mn", c["need_delta"],
           ("$%.0f" % c["quality"]) if c.get("quality") else "&mdash;",
           "{:,}".format(c["trend_count"]) if c.get("trend_count") else "&mdash;",
           round(c.get("faab_pct", 0)),
           (" <span class='mut'>$%d</span>" % c["faab_dollars"])
           if c.get("faab_dollars") else "",
           c.get("replaces") or "&mdash;")
        for c in b["candidates"])
    needs = "".join(
        '<div class="row"><span><span class="pos %s">%s</span> %s</span>'
        '<span class="mut">%s</span></div>'
        % (n["pos"], n["pos"],
           "<b class='mn'>UNFILLED</b>" if n["unfilled"] else "",
           ", ".join("%s %.1f" % (x["name"], x["wk_proj"] or 0)
                     for x in n["starters"]) or "none")
        for n in b["needs"])
    news = "".join('<div class="row"><a href="%s" target="_blank">%s</a></div>'
                   % (n["url"], n["title"][:110]) for n in b["news"][:12])
    cards = ai_cards(ai)

    if job_id and not cards:
        cards = sitstart.PENDING_PANEL + (sitstart.POLL_JS % json.dumps(job_id))
    return WV_TPL.replace("__ROWS__", rows).replace("__NEEDS__", needs) \
        .replace("__NEWS__", news).replace("__AI__", cards) \
        .replace("__TEAM__", str(b["team_name"])).replace("__WK__", str(b["week"])) \
        .replace("__N__", str(b["free_agent_count"])) \
        .replace("__BUD__", ("$%d" % b["faab_budget"]) if b.get("faab_budget") else "unknown") \
        .replace("__BAR__", "%.1f" % b["flex_bar"])


WV_TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waiver Wire</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.wksel{color:#8b949e;font-size:12px;padding-right:6px}.wksel select{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:3px 6px;font:inherit;margin-left:4px}
.hd{padding:16px 20px;background:#161b22;border-bottom:1px solid #30363d}
.hd h1{margin:0;font-size:20px}
.wrap{padding:16px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.main{flex:1 1 640px;min-width:0;max-width:100%}.rail{flex:1 1 340px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:12px}
.panel h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:7px;border-bottom:1px solid #30363d;white-space:nowrap}
th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}
td{padding:7px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}
tr:hover td{background:#1c2128}
.nm{font-weight:600;color:#fff}.big{font-size:15px;font-weight:700;color:#58a6ff}
.faab{font-weight:700;color:#3fb950}
.mut{color:#8b949e}.pl{color:#3fb950}.mn{color:#f85149}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.K{background:#30363d;color:#8b949e}.DEF{background:#30363d;color:#8b949e}
.row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px;gap:10px}
.row a{color:#79c0ff;text-decoration:none}.row a:hover{text-decoration:underline}
.card{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:10px}
.ch{display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.verdict{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;border:1px solid}
.v-must{background:#12331d;color:#3fb950;border-color:#238636}
.v-start{background:#0d3050;color:#79c0ff;border-color:#1f6feb}
.v-flex{background:#4a3312;color:#e3b341;border-color:#9e6a03}
.v-sit{background:#3a1518;color:#f85149;border-color:#7d2427}
.dec{font-size:13px;color:#c9d1d9;margin-bottom:6px}.read{font-size:13px;color:#c9d1d9}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:-1px;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:900px){.rail{flex:1 1 100%}}
</style></head><body>
<div class="nav"><a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a><a href="#" data-p="/lookahead">Look Ahead</a><a href="#" data-p="/board">Draft board</a><a href="#" data-p="/analysis">Analysis</a><span class="navsp"></span><span class="wksel">week <select id="wk" onchange="var u=new URL(location.href);u.searchParams.set('week',this.value);u.searchParams.delete('refresh');location.href=u"></select></span></div>
<script>(function(){
var sel=document.getElementById('wk'); if(!sel) return;
var cur=parseInt(new URLSearchParams(location.search).get('week')||'__WK__',10);
for(var i=1;i<=18;i++){var o=document.createElement('option');
 o.value=i;o.textContent=i;if(i===cur)o.selected=true;sel.appendChild(o);}
})();</script>
<script>(function(){var qs=location.search||'';
var here=location.pathname==='/'?'/sitstart':location.pathname;
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
 if(here===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__TEAM__ &mdash; week __WK__ waivers</h1>
<div class="mut">__N__ free agents &middot; flex bar __BAR__ pts &middot; FAAB budget __BUD__</div></div>
<div class="wrap">
 <div class="main"><div class="panel" style="padding:0"><table>
  <thead><tr><th>Player</th><th>Pos</th><th>Wk proj</th><th>Need</th><th>Value</th>
  <th>Adds/24h</th><th>FAAB</th><th>Replaces</th></tr></thead><tbody>__ROWS__</tbody></table></div>
  <div class="mut" style="font-size:12px"><b>Need</b> is weekly points above the player he would
  actually replace in your lineup &middot; <b>Adds/24h</b> is how many Sleeper leagues added him,
  which sets his price, not his value to you &middot; <b>FAAB</b> is a suggested opening bid</div>
 </div>
 <div class="rail" id="ai-slot">__AI__
  <div class="panel"><h3>Roster needs</h3>__NEEDS__</div>
  <div class="panel"><h3>r/fantasyfootball</h3>__NEWS__</div>
 </div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--me", type=int, required=True)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    board = build_board(a.draft, a.me, a.week, a.limit)
    ai = None if a.no_ai else ai_analyze(board)
    if a.json:
        print(json.dumps({"board": board, "ai": ai}, indent=1, default=str))
    else:
        print(report(board, ai))
