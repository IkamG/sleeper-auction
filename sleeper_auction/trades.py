#!/usr/bin/env python3
"""Trades: offers that help both rosters and are fair by the market.

The other tools ask "who do I start" and "who do I add". This asks "what can
I get for what I have", and it only proposes what another manager might
actually accept:

  * both lineups get better over the rest of the season (my_gain > 0 AND
    their_gain > 0), measured as starting-lineup ROS points with the league's
    own slots; and
  * the package is fair by consensus trade value (FantasyCalc, plus
    KeepTradeCut when enabled): the two sides within 10% of each other.

Separately, up to three "value" packages help me and lean my way by up to
20% -- labelled as asks the other manager may decline.

The search is deterministic (1-for-1, 2-for-1, 1-for-2 against every other
roster, players worth $3+ only); the AI then argues both sides of the best
ones and writes the pitch. Kickers and defences are never traded.

    python3 -m sleeper_auction.trades --draft <id> --me <n> [--limit 15] [--no-ai] [--json]
"""
import argparse
import itertools
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleeper_auction import board as db          # noqa: E402
from sleeper_auction import sitstart             # noqa: E402

TRADE_POS = ("QB", "RB", "WR", "TE")
FLEX_POS = ("RB", "WR", "TE")
MIN_VALUE = 3.0          # $ of trade value: below this a player is not a trade chip
FAIR_GAP = 0.10          # |value gap| for a fair package
VALUE_GAP = 0.20         # how far a "value" ask may lean my way
MAX_VALUE_ASKS = 3
MAX_PER_PARTNER = 3      # keep the list from being one partner's roster, permuted
MAX_PER_TARGET = 2       # ... or one player I want, packaged five ways
SHAPES = ((1, 1), (2, 1), (1, 2))

# ------------------------------------------------------------------ lineups


def lineup(players, slots, key="ros"):
    """(total, starters) -- best legal lineup by `key` with the league's slots.

    Dedicated slots first, then FLEX from the best remaining RB/WR/TE. Greedy
    is optimal for this slot shape (no player is eligible for two dedicated
    slots)."""
    by = {}
    for p in players:
        by.setdefault(p["pos"], []).append(p)
    for lst in by.values():
        lst.sort(key=lambda x: -(x.get(key) or 0))
    starters, used = [], set()
    for pos, n in slots.items():
        if pos in ("FLEX", "BN", "SUPER_FLEX", "IDP_FLEX"):
            continue
        for p in by.get(pos, [])[:n]:
            starters.append(dict(p, slot=pos))
            used.add(p["sid"])
    rest = sorted((p for p in players if p["pos"] in FLEX_POS and p["sid"] not in used),
                  key=lambda x: -(x.get(key) or 0))
    for p in rest[:slots.get("FLEX", 0)]:
        starters.append(dict(p, slot="FLEX"))
        used.add(p["sid"])
    if slots.get("SUPER_FLEX"):
        rest = sorted((p for p in players if p["pos"] in TRADE_POS and p["sid"] not in used),
                      key=lambda x: -(x.get(key) or 0))
        for p in rest[:slots["SUPER_FLEX"]]:
            starters.append(dict(p, slot="SUPER_FLEX"))
    return round(sum(p.get(key) or 0 for p in starters), 1), starters


def slot_table(rosters, slots):
    """{slot label: [ros of that slot's starter per roster]} e.g. RB1, RB2, FLEX1."""
    tab = {}
    for rid, pl in rosters.items():
        _, st = lineup(pl, slots)
        seen = {}
        for p in st:
            seen[p["slot"]] = seen.get(p["slot"], 0) + 1
            tab.setdefault("%s%d" % (p["slot"], seen[p["slot"]]), {})[rid] = p.get("ros") or 0
    return tab


def needs(rosters, slots):
    """{rid: {pos: ROS points below the league-median starter}} and medians."""
    tab = slot_table(rosters, slots)
    med = {k: statistics.median(v.values()) for k, v in tab.items() if v}
    out = {}
    for rid in rosters:
        d = {}
        for k, v in tab.items():
            pos = k.rstrip("0123456789")
            gap = med[k] - v.get(rid, 0)
            if gap > 0:
                d[pos] = round(d.get(pos, 0) + gap, 1)
        out[rid] = d
    return out, med


def surplus(rosters, slots, rid):
    """My bench players whose ROS points would start for some other roster."""
    _, st = lineup(rosters[rid], slots)
    starting = {p["sid"] for p in st}
    worst = {}
    for orid, pl in rosters.items():
        if orid == rid:
            continue
        _, ost = lineup(pl, slots)
        for p in ost:
            key = p["slot"]
            worst.setdefault(orid, {})[key] = min(worst.get(orid, {}).get(key, 1e9),
                                                  p.get("ros") or 0)
    out = []
    for p in rosters[rid]:
        if p["sid"] in starting or p["pos"] not in TRADE_POS:
            continue
        for orid, w in worst.items():
            fits = [w[s] for s in (p["pos"], "FLEX" if p["pos"] in FLEX_POS else None)
                    if s and s in w]
            if fits and (p.get("ros") or 0) > min(fits):
                out.append(p)
                break
    return out

# ------------------------------------------------------------------ search


def _after(roster, give, get, slots):
    """Roster after a trade, dropping the worst bench player if it overflows."""
    gids = {p["sid"] for p in give}
    new = [p for p in roster if p["sid"] not in gids] + list(get)
    drop = None
    if len(get) > len(give):
        _, st = lineup(new, slots)
        starting = {p["sid"] for p in st}
        incoming = {p["sid"] for p in get}
        bench = [p for p in new if p["sid"] not in starting and p["sid"] not in incoming]
        if bench:
            drop = min(bench, key=lambda x: (x.get("ros") or 0, x.get("value") or 0))
            new = [p for p in new if p["sid"] != drop["sid"]]
    return new, drop


def chips(roster):
    return [p for p in roster if p["pos"] in TRADE_POS and (p.get("value") or 0) >= MIN_VALUE]


def evaluate(mine, theirs, give, get, slots, base_mine=None, base_theirs=None):
    my_now = base_mine if base_mine is not None else lineup(mine, slots)[0]
    th_now = base_theirs if base_theirs is not None else lineup(theirs, slots)[0]
    my_new, my_drop = _after(mine, give, get, slots)
    th_new, th_drop = _after(theirs, get, give, slots)
    gv = sum(p.get("value") or 0 for p in give)
    gt = sum(p.get("value") or 0 for p in get)
    return {"my_gain": round(lineup(my_new, slots)[0] - my_now, 1),
            "their_gain": round(lineup(th_new, slots)[0] - th_now, 1),
            "give_value": round(gv, 1), "get_value": round(gt, 1),
            # positive = I receive more value than I send
            "value_gap": round((gt - gv) / max(gv, gt), 3) if max(gv, gt) > 0 else 0.0,
            "my_drop": my_drop, "their_drop": th_drop}


def search(rosters, me, slots, limit=15):
    """(fair proposals, value asks) against every other roster."""
    mine = rosters[me]
    my_now = lineup(mine, slots)[0]
    my_chips = chips(mine)
    fair, asks = [], []
    for rid, theirs in rosters.items():
        if rid == me:
            continue
        th_now = lineup(theirs, slots)[0]
        their_chips = chips(theirs)
        for ng, nr in SHAPES:
            for give in itertools.combinations(my_chips, ng):
                for get in itertools.combinations(their_chips, nr):
                    r = evaluate(mine, theirs, give, get, slots, my_now, th_now)
                    if r["my_gain"] <= 0:
                        continue
                    r.update(partner=rid, give=list(give), get=list(get))
                    if r["their_gain"] > 0 and abs(r["value_gap"]) <= FAIR_GAP:
                        fair.append(r)
                    elif 0 < r["value_gap"] <= VALUE_GAP:
                        asks.append(r)
    fair.sort(key=lambda r: (-r["my_gain"], abs(r["value_gap"])))
    # Asks the other side might actually take first: ones that do not make
    # their lineup worse, then by what they do for mine.
    asks.sort(key=lambda r: (r["their_gain"] < 0, -r["my_gain"], r["value_gap"]))

    def diverse(lst, n):
        out, per_partner, per_target = [], {}, {}
        for r in lst:
            tgt = tuple(sorted(p["sid"] for p in r["get"]))
            if per_partner.get(r["partner"], 0) >= MAX_PER_PARTNER \
                    or per_target.get(tgt, 0) >= MAX_PER_TARGET:
                continue
            per_partner[r["partner"]] = per_partner.get(r["partner"], 0) + 1
            per_target[tgt] = per_target.get(tgt, 0) + 1
            out.append(r)
            if len(out) >= n:
                break
        return out
    return diverse(fair, limit), diverse(asks, MAX_VALUE_ASKS)

# ------------------------------------------------------------------ data


def build_board(draft_id, roster_id, limit=15):
    """Every roster with ROS points and trade values, then the search."""
    from sleeper_auction.feeds import common, ids, prefetch, scoring, values
    from sleeper_auction.feeds import league as lgfeed
    st = db.draft_state(draft_id)
    league_id = (st["draft"] or {}).get("league_id")
    if not league_id:
        raise SystemExit("draft %s has no league_id" % draft_id)
    season, week = prefetch.current_week()
    pool = db.value_pool(db.build_pool()[0])
    byid = {p["sleeper_id"]: p for p in pool if p.get("sleeper_id")}
    cw = ids.crosswalk()
    lg = lgfeed._league(league_id)
    sc = scoring.league_scoring(league_id)
    res, errs = common.parallel({
        "rosters": lambda: common.fetch_json(
            "https://api.sleeper.app/v1/league/%s/rosters" % league_id,
            "slp-rosters-faab-%s" % league_id, 600),
        "ros": lambda: values.ros_points(season, week + 1, sc, cache_only=True,
                                         season_proj={s: p.get("proj") for s, p in byid.items()}),
        "tv": lambda: values.trade_values(lg, pool),
        "byes": lambda: __import__("sleeper_auction.feeds.nflverse",
                                   fromlist=["byes"]).byes(season),
        "practice": lambda: __import__("sleeper_auction.feeds.nflverse",
                                       fromlist=["practice"]).practice(season, week),
    }, timeout=60)
    if not res.get("rosters"):
        raise SystemExit("no rosters for league %s: %s" % (league_id, errs.get("rosters")))
    ros = res.get("ros") or {}
    if not ros or next(iter(ros.values())).get("source") == "season-share":
        prefetch.kick_ros(season, week + 1)
    tv = res.get("tv") or {}
    ros_d = values.ros_dollars({s: v["pts"] for s, v in ros.items()}, pool) if ros else {}
    slp = ids.players()
    prac = (res.get("practice") or {}).get("players") or {}
    owners = {t["roster_id"]: t["owner"] for t in st["teams"]}

    rosters = {}
    for r in res["rosters"]:
        pl = []
        for sid in r.get("players") or []:
            info = cw["info"].get(sid) or {}
            pos = db.npos(info.get("pos")) if info.get("pos") else ("DEF" if sid.isalpha()
                                                                     else None)
            if not pos:
                continue
            t = tv.get(sid) or {}
            val = t.get("dollars")
            src = "trade-value"
            if val is None:
                val, src = ros_d.get(sid), "ros-points"
            if val is None:
                val, src = (byid.get(sid) or {}).get("base"), "preseason"
            team = db.nteam(info.get("team")) if info.get("team") else sid
            pl.append({"sid": sid, "name": info.get("name") or sid, "pos": pos, "team": team,
                       "ros": (ros.get(sid) or {}).get("pts"), "value": val,
                       "value_source": src, "fc": t.get("fc"), "ktc": t.get("ktc"),
                       "disagree": bool(t.get("disagree")), "trend30": t.get("trend30"),
                       "bye": (res.get("byes") or {}).get(team),
                       "injury": (slp.get(sid) or {}).get("injury_status"),
                       "practice": (prac.get(sid) or {}).get("trajectory")})
        rosters[r["roster_id"]] = pl
    slots = dict(st["league"].get("slots") or {})
    if roster_id not in rosters:
        raise SystemExit("roster %s not in league %s" % (roster_id, league_id))
    fair, asks = search(rosters, roster_id, slots, limit)
    nd, med = needs(rosters, slots)
    sur = surplus(rosters, slots, roster_id)

    # News headline for everyone in a proposal (cache-only; fetched behind).
    try:
        from sleeper_auction.feeds import news as newsfeed
        sids = {p["sid"] for r in fair + asks for p in r["give"] + r["get"]}
        nw = newsfeed.player_news(sids)
    except Exception:
        nw = {}
    sell = _sell_high(rosters[roster_id])
    return {"draft_id": draft_id, "roster_id": roster_id, "league_id": league_id,
            "season": season, "week": week, "team_name": owners.get(roster_id, "me"),
            "owners": owners, "slots": slots,
            "my_lineup_ros": lineup(rosters[roster_id], slots)[0],
            "proposals": [_view(r, owners, nw) for r in fair],
            "value_asks": [_view(r, owners, nw) for r in asks],
            "my_need": nd.get(roster_id), "my_surplus": [_pv(p, nw) for p in sur],
            "opp_needs": {owners.get(k, k): v for k, v in nd.items() if k != roster_id and v},
            "slot_medians": med, "sell_high": sell,
            "sources": {"ros": (next(iter(ros.values())) if ros else {}).get("source"),
                        "values": "FantasyCalc" + (" + KeepTradeCut" if any(
                            (v or {}).get("ktc") for v in tv.values()) else ""),
                        "missing": sorted(errs) or None}}


def _pv(p, nw=None):
    out = {k: p.get(k) for k in ("name", "pos", "team", "ros", "value", "value_source", "fc",
                                 "ktc", "disagree", "trend30", "bye", "injury", "practice")}
    n = (nw or {}).get(p["sid"])
    if n:
        out["news"] = "%s (%s)" % (n[0]["headline"], (n[0].get("published") or "")[:10])
    return out


def _view(r, owners, nw):
    return {"partner": owners.get(r["partner"], r["partner"]), "partner_rid": r["partner"],
            "give": [_pv(p, nw) for p in r["give"]], "get": [_pv(p, nw) for p in r["get"]],
            "my_gain": r["my_gain"], "their_gain": r["their_gain"],
            "give_value": r["give_value"], "get_value": r["get_value"],
            "value_gap": r["value_gap"],
            "my_drop": r["my_drop"]["name"] if r.get("my_drop") else None,
            "their_drop": r["their_drop"]["name"] if r.get("their_drop") else None}


def _sell_high(mine):
    """Phase 1's sell-high tag on my roster, with trade values attached."""
    try:
        from sleeper_auction import lookahead
        fd = lookahead.usage_feeds()
    except Exception:
        return []
    out = []
    for p in mine:
        nfv = {"usage": sitstart._usage_view(fd, p["sid"]), "xfp": sitstart._xfp_view(fd, p["sid"])}
        tags, why = lookahead.classify(p["sid"], {}, None, None, {}, {}, nfv, mine=True)
        if "sell-high" in tags:
            out.append(dict(_pv(p), why=why[tags.index("sell-high")]))
    return out

# ------------------------------------------------------------------ AI


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["roster_read", "proposals"],
    "properties": {
        "roster_read": {"type": "string"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["give", "get", "partner", "verdict", "pitch", "risk"],
                "properties": {
                    "give": {"type": "string"}, "get": {"type": "string"},
                    "partner": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["PROPOSE", "CONSIDER", "AVOID"]},
                    "pitch": {"type": "string",
                              "description": "what THEIR roster gains, in one or two lines"},
                    "risk": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM = """You are a fantasy football trade analyst for a 12-team half-PPR \
Sleeper league. Every number is computed for you; do not invent statistics or \
use player knowledge beyond the payload.

Each proposal was found by a deterministic search and already passes two \
tests: "my_gain" and "their_gain" are the change in each side's \
rest-of-season starting-lineup points (league slots, ROS projections), both \
positive; and "value_gap" is the difference in consensus trade value \
(FantasyCalc, plus KeepTradeCut when present) as a share of the larger side, \
within 10% -- fair by the market. "value_asks" lean my way by up to 20% and \
are asks the other manager may decline; say so.

For each proposal, argue BOTH sides, then commit: PROPOSE (send it), \
CONSIDER (worth it with a caveat), AVOID (the numbers pass but something in \
the payload says no). Write the pitch from the OTHER manager's side: what \
their lineup gains ("their_gain", their need at the position). Name the risk: \
injuries and practice status, a bye in a thin week, news, a falling \
"trend30".

"disagree": true on a player means FantasyCalc (real trades) and \
KeepTradeCut (crowd votes) disagree by 15+ percentile points: the value read \
is unreliable, so say so. "value_source" other than "trade-value" means the \
player has no trade-market value and it was derived from ROS points. \
"my_drop"/"their_drop" is who must be cut to make a 2-for-1 legal. \
"sell_high" lists my players scoring well above their expected points on \
touchdowns: good trade chips. "opp_needs" says who needs what I have.

Be decisive and brief. Rank by what actually helps my lineup."""


def ai_analyze(b, api_key=None, refresh=False):
    payload = json.dumps({k: b.get(k) for k in (
        "team_name", "week", "my_lineup_ros", "proposals", "value_asks", "my_need",
        "my_surplus", "opp_needs", "sell_high", "sources")}, default=str)
    key = "trades-%s-w%s" % (b.get("roster_id"), b.get("week"))
    return sitstart.cached_ai(
        key, lambda: sitstart.run_model(SYSTEM, payload, SCHEMA, api_key=api_key),
        refresh=refresh)


def ai_cards(ai):
    if not ai:
        return ""
    if ai.get("error"):
        return ('<div class="panel"><h3>AI read</h3><div class="mut">Unavailable (%s). %s'
                '</div></div>' % (ai["error"], str(ai.get("detail", ""))[:300]))
    esc = sitstart._esc
    out = '<div class="panel"><h3>AI read</h3><div class="read">%s</div></div>' % esc(
        ai.get("roster_read", ""))
    for p in ai.get("proposals", []):
        cls = {"PROPOSE": "v-must", "CONSIDER": "v-flex", "AVOID": "v-sit"}.get(
            p["verdict"], "v-flex")
        out += ('<div class="card"><div class="ch"><span class="verdict %s">%s</span>'
                '<span class="nm">give %s &rarr; get %s</span><span class="mut">%s</span></div>'
                '<div class="dec">%s</div><div class="mut">risk: %s</div></div>'
                % (cls, p["verdict"], esc(p["give"]), esc(p["get"]), esc(p["partner"]),
                   esc(p["pitch"]), esc(p["risk"])))
    return out

# ------------------------------------------------------------------ output


def _names(ps):
    return " + ".join("%s (%s)" % (p["name"], p["pos"]) for p in ps)


def report(b, ai=None):
    o = ["%s -- trades  (my lineup %.1f ROS pts; ROS from %s; values: %s)"
         % (b["team_name"], b["my_lineup_ros"], b["sources"]["ros"], b["sources"]["values"]),
         ""]
    o.append("%-14s %-38s %-38s %7s %7s %6s" % ("PARTNER", "GIVE", "GET", "MY +", "THEIR +",
                                               "GAP"))
    o.append("-" * 118)
    for r in b["proposals"]:
        o.append("%-14s %-38s %-38s %+7.1f %+7.1f %+5.0f%%" % (
            str(r["partner"])[:14], _names(r["give"])[:38], _names(r["get"])[:38],
            r["my_gain"], r["their_gain"], 100 * r["value_gap"]))
        if r.get("my_drop") or r.get("their_drop"):
            o.append("%-14s drops: me %s, them %s" % ("", r.get("my_drop") or "-",
                                                     r.get("their_drop") or "-"))
    if not b["proposals"]:
        o.append("  (no package passes both tests right now)")
    if b["value_asks"]:
        o += ["", "VALUE ASKS (lean my way; they may decline)"]
        for r in b["value_asks"]:
            o.append("%-14s %-38s %-38s %+7.1f %+7.1f %+5.0f%%" % (
                str(r["partner"])[:14], _names(r["give"])[:38], _names(r["get"])[:38],
                r["my_gain"], r["their_gain"], 100 * r["value_gap"]))
    o += ["", "MY NEED (ROS pts below league-median starter): %s" % (b["my_need"] or "none"),
          "MY SURPLUS: %s" % (", ".join(p["name"] for p in b["my_surplus"]) or "none")]
    if b.get("sell_high"):
        o.append("SELL HIGH: %s" % ", ".join(p["name"] for p in b["sell_high"]))
    if ai and not ai.get("error"):
        o += ["", "AI: " + ai.get("roster_read", "")]
        for p in ai.get("proposals", []):
            o.append("  %-8s give %s -> get %s (%s): %s" % (p["verdict"], p["give"], p["get"],
                                                           p["partner"], p["pitch"]))
    return "\n".join(o)


def _prow(r):
    esc = sitstart._esc

    def side(ps):
        return "<br>".join(
            '<span class="pos %s">%s</span> %s <span class="mut">$%s%s%s</span>' % (
                p["pos"], p["pos"], esc(p["name"]),
                ("%.0f" % p["value"]) if p.get("value") is not None else "?",
                " &middot; %s ROS" % ("%.0f" % p["ros"]) if p.get("ros") is not None else "",
                (' &middot; <b class="vol" title="FantasyCalc and KeepTradeCut disagree">'
                 'values disagree</b>') if p.get("disagree") else "")
            for p in ps)
    drops = ""
    if r.get("my_drop") or r.get("their_drop"):
        drops = '<div class="mut" style="font-size:11px">drops: me %s, them %s</div>' % (
            esc(r.get("my_drop") or "-"), esc(r.get("their_drop") or "-"))
    return ('<tr><td class="nm">%s</td><td>%s</td><td>%s%s</td><td class="pl">%+.1f</td>'
            '<td class="%s">%+.1f</td><td class="%s">%+.0f%%</td></tr>' % (
                esc(r["partner"]), side(r["give"]), side(r["get"]), drops, r["my_gain"],
                "pl" if r["their_gain"] > 0 else "mn", r["their_gain"],
                "pl" if r["value_gap"] >= 0 else "mn", 100 * r["value_gap"]))


def html_report(b, ai=None, job_id=None):
    esc = sitstart._esc
    rows = "".join(_prow(r) for r in b["proposals"]) or (
        '<tr><td colspan="6" class="mut">No package passes both tests right now: every '
        'offer that helps you either hurts them or is lopsided by the market.</td></tr>')
    asks = "".join(_prow(r) for r in b["value_asks"])
    need = "".join('<div class="row"><span><span class="pos %s">%s</span></span>'
                   '<span class="mut">%.1f ROS pts below the league-median starter</span></div>'
                   % (k, k, v) for k, v in sorted((b.get("my_need") or {}).items(),
                                                  key=lambda kv: -kv[1])) \
        or '<div class="mut">At or above the league median everywhere.</div>'
    sur = "".join('<div class="row"><span>%s <span class="mut">%s</span></span>'
                  '<span class="mut">%s ROS &middot; $%s</span></div>'
                  % (esc(p["name"]), p["pos"], p.get("ros"), p.get("value"))
                  for p in b["my_surplus"]) or '<div class="mut">No bench player would start '\
                                               'elsewhere.</div>'
    opp = "".join('<div class="row"><span>%s</span><span class="mut">%s</span></div>'
                  % (esc(o), ", ".join("%s %.0f" % kv for kv in sorted(
                      n.items(), key=lambda kv: -kv[1])))
                  for o, n in sorted(b["opp_needs"].items()))
    sell = "".join('<div class="row"><span>%s</span><span class="mut">%s</span></div>'
                   % (esc(p["name"]), esc(p.get("why"))) for p in b["sell_high"]) \
        or '<div class="mut">None tagged.</div>'
    cards = ai_cards(ai)
    if job_id and not cards:
        cards = sitstart.PENDING_PANEL + (sitstart.POLL_JS % json.dumps(job_id))
    return TR_TPL.replace("__ROWS__", rows).replace("__AI__", cards) \
        .replace("__ASKS__", ('<div class="panel" style="padding:0"><table><thead><tr>'
                              '<th>Value asks</th><th>Give</th><th>Get</th><th>My +</th>'
                              '<th>Their +</th><th>Gap</th></tr></thead><tbody>%s</tbody>'
                              '</table></div>' % asks) if asks else "") \
        .replace("__NEED__", need).replace("__SUR__", sur).replace("__OPP__", opp) \
        .replace("__SELL__", sell).replace("__DATA__", sitstart._data_panel(
            ("values", "league", "fantasypros", "nflverse", "news"))) \
        .replace("__TEAM__", esc(b["team_name"])) \
        .replace("__LU__", "%.1f" % b["my_lineup_ros"]) \
        .replace("__SRC__", esc("ROS points from %s; trade values from %s" % (
            b["sources"]["ros"], b["sources"]["values"])))


TR_TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trades</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d;overflow-x:auto}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent;white-space:nowrap}
.nav a:hover{color:#e6edf3}.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.hd{padding:16px 20px;background:#161b22;border-bottom:1px solid #30363d}
.hd h1{margin:0;font-size:20px}
.wrap{padding:16px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.main{flex:1 1 660px;min-width:0;max-width:100%}.rail{flex:1 1 340px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:12px}
.panel h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:7px;border-bottom:1px solid #30363d;white-space:nowrap}
th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}
td{padding:7px;text-align:right;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#1c2128}
.nm{font-weight:600;color:#fff}.mut{color:#8b949e}.pl{color:#3fb950}.mn{color:#f85149}.vol{color:#e3b341}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px;gap:10px}
.card{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:10px}
.ch{display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.verdict{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;border:1px solid}
.v-must{background:#12331d;color:#3fb950;border-color:#238636}
.v-flex{background:#4a3312;color:#e3b341;border-color:#9e6a03}
.v-sit{background:#3a1518;color:#f85149;border-color:#7d2427}
.dec{font-size:13px;color:#c9d1d9;margin-bottom:6px}.read{font-size:13px;color:#c9d1d9}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:-1px;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:900px){.rail{flex:1 1 100%}.wrap{padding:12px 16px}}
</style></head><body>
<div class="nav"><a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a><a href="#" data-p="/lookahead">Look Ahead</a><a href="#" data-p="/trades">Trades</a><a href="#" data-p="/board">Draft board</a><a href="#" data-p="/analysis">Analysis</a></div>
<script>(function(){var qs=location.search||'';
var here=location.pathname==='/'?'/sitstart':location.pathname;
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
 if(here===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__TEAM__ &mdash; trades</h1>
<div class="mut">My starting lineup: <b>__LU__</b> rest-of-season points. __SRC__.</div></div>
<div class="wrap">
 <div class="main"><div class="panel" style="padding:0"><table>
  <thead><tr><th>Partner</th><th>Give</th><th>Get</th><th>My +</th><th>Their +</th><th>Gap</th></tr></thead>
  <tbody>__ROWS__</tbody></table></div>
  __ASKS__
  <div class="mut" style="font-size:12px"><b>My + / Their +</b> is the change in each side's
  rest-of-season starting-lineup points with your league's slots: both must be positive
  &middot; <b>Gap</b> is the difference in consensus trade value (FantasyCalc, plus
  KeepTradeCut when enabled) as a share of the larger side, positive when you receive more;
  fair packages are within 10% &middot; <b>Value asks</b> lean your way by up to 20% and
  may be declined &middot; $ is trade value on the draft dollar curve</div>
 </div>
 <div class="rail"><div id="ai-slot">__AI__</div>
  <div class="panel"><h3>My needs vs the league</h3>__NEED__</div>
  <div class="panel"><h3>My surplus</h3>__SUR__</div>
  <div class="panel"><h3>Who needs what</h3>__OPP__</div>
  <div class="panel"><h3>Sell high</h3>__SELL__</div>
  __DATA__
 </div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--me", type=int, required=True)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    brd = build_board(a.draft, a.me, a.limit)
    ai_ = None if a.no_ai else ai_analyze(brd)
    if a.json:
        print(json.dumps({"board": brd, "ai": ai_}, indent=1, default=str))
    else:
        print(report(brd, ai_))
