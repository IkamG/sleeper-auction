#!/usr/bin/env python3
"""Look forward: where this roster breaks later, and who to stash now.

The waiver page answers "who helps me this week". This answers the question
that actually decides a season: who is being under-used right now relative to
how well he performs when he touches the ball, and what happens to my roster in
six weeks when byes land and the workhorse ahead of someone goes down.

The core signal is the OPPORTUNITY GAP -- efficiency percentile minus usage
percentile within a position. A back at 5.4 yards a carry on eight touches a
game is not a good player being wasted by accident; he is a coaching decision
that can reverse in one week. A back at 3.6 on twenty touches has his job
precisely because nobody better is there, and there is no gap to close.

Efficiency without opportunity is a lottery ticket. Opportunity without
efficiency is a job that will be taken. This ranks the first group, because
that is the one you can buy for nothing.

    python3 -m sleeper_auction.lookahead --draft <id> --me <n>
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleeper_auction import board as db          # noqa: E402
from sleeper_auction import sitstart             # noqa: E402
from sleeper_auction import waivers              # noqa: E402

# A back is a "workhorse" above this snap share; nobody behind him inherits
# much unless he is hurt, which is exactly what makes his backup a handcuff.
WORKHORSE_SNAP = 0.62
# Below this, the backfield is a committee and the job is genuinely open.
COMMITTEE_SNAP = 0.50
MIN_TOUCHES = 20          # below this, efficiency is noise, not signal


def season_stats(season, positions=("QB", "RB", "WR", "TE")):
    """Season-long counting stats, used to derive per-touch efficiency."""
    pos = "&".join("position[]=" + p for p in positions)
    try:
        return db.gj("https://api.sleeper.com/stats/nfl/%s?season_type=regular&%s"
                     "&order_by=pts_half_ppr" % (season, pos),
                     key="slp-season-%s" % season, ttl=86400)
    except Exception:
        return []


def efficiency(season=None, min_weeks_current=3):
    """Per-player usage and efficiency.

    Prefers the current season and falls back to the prior one when too few
    games have been played for rates to mean anything -- three carries at 9.0
    a pop is not a signal, it is one run.
    """
    for src in ([season] if season else [sitstart.SEASON, sitstart.PRIOR_SEASON]):
        rows = season_stats(src)
        played = max((int((r.get("stats") or {}).get("gp") or 0) for r in rows),
                     default=0)
        if played < min_weeks_current and src != sitstart.PRIOR_SEASON:
            continue
        out = {}
        for r in rows:
            st = r.get("stats") or {}
            pl = r.get("player") or {}
            pid = str(r.get("player_id") or "")
            gp = float(st.get("gp") or 0)
            if not pid or gp < 1:
                continue
            att = float(st.get("rush_att") or 0)
            ryd = float(st.get("rush_yd") or 0)
            tgt = float(st.get("rec_tgt") or 0)
            rec = float(st.get("rec") or 0)
            recyd = float(st.get("rec_yd") or 0)
            snp = float(st.get("off_snp") or 0)
            tmsnp = float(st.get("tm_off_snp") or 0)
            touches = att + rec
            out[pid] = {
                "gp": gp, "touches": touches, "touches_pg": round(touches / gp, 1),
                "att": att, "rush_yd": ryd, "tgt": tgt, "rec": rec, "rec_yd": recyd,
                "ypc": round(ryd / att, 2) if att >= 10 else None,
                "ypt": round(recyd / tgt, 2) if tgt >= 10 else None,
                "ypr": round(recyd / rec, 2) if rec >= 8 else None,
                "adot": round(float(st.get("rec_air_yd") or 0) / tgt, 2)
                        if tgt >= 10 else None,
                "rz_tgt": st.get("rec_rz_tgt"),
                "snap_share": round(snp / tmsnp, 3) if tmsnp else None,
                "tgt_pg": round(tgt / gp, 1),
                "td": (float(st.get("rush_td") or 0) + float(st.get("rec_td") or 0)),
            }
        if out:
            return {"season": src, "players": out,
                    "weeks": played if src == sitstart.SEASON else 18}
    return {"season": None, "players": {}, "weeks": 0}


def _pct(values, v):
    """Percentile of v within values, 0-1. Plain rank, no interpolation."""
    vals = sorted(x for x in values if x is not None)
    if not vals or v is None:
        return None
    below = sum(1 for x in vals if x < v)
    return round(below / len(vals), 3)


def opportunity_gaps(eff, index, positions=("RB", "WR", "TE")):
    """Efficiency percentile minus usage percentile, within position."""
    by_pos = {}
    for pid, e in eff["players"].items():
        info = index.get(pid) or {}
        pos = db.npos(info.get("pos") or info.get("position"))
        if pos not in positions or e["touches"] < MIN_TOUCHES:
            continue
        by_pos.setdefault(pos, []).append((pid, e))
    out = {}
    for pos, rows in by_pos.items():
        # Backs are judged on yards per carry, receivers on yards per target --
        # the per-touch rate that their job actually consists of.
        eff_key = "ypc" if pos == "RB" else "ypt"
        effs = [e.get(eff_key) for _, e in rows]
        uses = [e.get("touches_pg") for _, e in rows]
        for pid, e in rows:
            ep = _pct(effs, e.get(eff_key))
            up = _pct(uses, e.get("touches_pg"))
            if ep is None or up is None:
                continue
            out[pid] = {"eff_pct": ep, "use_pct": up, "gap": round(ep - up, 3),
                        "eff_metric": eff_key, "eff_value": e.get(eff_key)}
    return out


def backfield_map(index, eff):
    """Per-team RB picture: is there a workhorse, or is the job open?"""
    teams = {}
    for pid, info in index.items():
        if db.npos(info.get("pos")) != "RB" or not info.get("team"):
            continue
        e = eff["players"].get(pid) or {}
        teams.setdefault(info["team"], []).append(
            {"id": pid, "name": info.get("name"),
             "order": info.get("depth_chart_order"),
             "snap_share": e.get("snap_share"), "touches_pg": e.get("touches_pg")})
    out = {}
    for tm, backs in teams.items():
        backs.sort(key=lambda b: (b["order"] or 99))
        lead = max(backs, key=lambda b: b.get("snap_share") or 0)
        top = lead.get("snap_share") or 0
        out[tm] = {"backs": backs, "lead": lead["name"], "lead_snap": top,
                   "shape": ("workhorse" if top >= WORKHORSE_SNAP
                             else "committee" if top < COMMITTEE_SNAP
                             else "lead-back")}
    return out


def classify(pid, info, e, gap, backfields, depth):
    """Why this player is worth a bench spot, if he is."""
    tags, why = [], []
    pos = db.npos(info.get("pos"))
    team = info.get("team")
    d = depth.get(pid) or {}
    order = d.get("order") or info.get("depth_chart_order")

    if info.get("years_exp") == 0:
        tags.append("rookie")
        if order and order <= 2:
            tags.append("rookie-in-line")
            why.append("rookie already %s on the depth chart, so the role could "
                       "arrive without an injury ahead of him"
                       % ("first" if order == 1 else "second"))
        else:
            why.append("rookie with no usage history to argue from; depth chart "
                       "order %s" % (order if order else "unlisted"))
    if gap and gap["gap"] >= 0.30:
        tags.append("efficient-unused")
        why.append("%s %.2f sits at the %d%% mark for the position while his "
                   "workload sits at %d%% -- productive on the touches he gets"
                   % (gap["eff_metric"].upper(), gap["eff_value"],
                      round(gap["eff_pct"] * 100), round(gap["use_pct"] * 100)))
    if pos == "RB" and team:
        bf = backfields.get(team) or {}
        if bf.get("shape") == "workhorse" and order in (2, 3):
            tags.append("handcuff")
            why.append("direct backup to %s, who is on %d%% of snaps -- the whole "
                       "backfield transfers if he misses time"
                       % (bf.get("lead"), round((bf.get("lead_snap") or 0) * 100)))
        elif bf.get("shape") == "committee" and order in (1, 2):
            tags.append("open-committee")
            why.append("no back on %s clears %d%% of snaps, so the job is "
                       "genuinely unsettled" % (team, int(COMMITTEE_SNAP * 100)))
    if e and (e.get("rz_tgt") or 0) >= 4 and (e.get("touches_pg") or 0) < 8:
        tags.append("red-zone role")
        why.append("%d red-zone targets on light overall usage" % e["rz_tgt"])
    return tags, why


def stash_board(draft_id, roster_id, limit=20):
    """Rank unrostered players on long-term payoff rather than this week."""
    pool = db.value_pool(db.build_pool()[0])
    byid = {p["sleeper_id"]: p for p in pool if p.get("sleeper_id")}
    state = db.draft_state(draft_id)
    league_id = (state["draft"] or {}).get("league_id")
    taken, by_roster = waivers.rostered_ids(league_id)

    raw = db.gj("https://api.sleeper.app/v1/players/nfl", key="slp-players",
                ttl=86400)
    index = {}
    for pid, p in raw.items():
        pos = db.npos((p.get("fantasy_positions") or [p.get("position")])[0])
        if pos in ("QB", "RB", "WR", "TE") and p.get("team"):
            index[str(pid)] = {"name": p.get("full_name") or pid, "pos": pos,
                               "team": db.nteam(p.get("team")),
                               "years_exp": p.get("years_exp"),
                               "age": p.get("age"),
                               "depth_chart_order": p.get("depth_chart_order"),
                               "injury_status": p.get("injury_status")}

    team_bye = {}
    for pl in pool:
        if pl.get("team") and pl.get("bye"):
            team_bye.setdefault(pl["team"], pl["bye"])
    eff = efficiency()
    gaps = opportunity_gaps(eff, index)
    depth = sitstart.depth_charts()
    backfields = backfield_map(index, eff)
    trend = waivers.trending("add")

    cands = []
    for pid, info in index.items():
        if pid in taken:
            continue
        if (info.get("injury_status") or "") in ("IR", "PUP", "NA", "Sus"):
            continue
        e = eff["players"].get(pid)
        g = gaps.get(pid)
        tags, why = classify(pid, info, e, g, backfields, depth)
        if not tags:
            continue
        base = byid.get(pid) or {}
        # Weight the reasons by how much they actually change a season.
        # A stash is a bet on future value, so age is a direct discount: a
        # 31-year-old backup with a great per-touch rate has been a backup for
        # a reason, and there is no career arc left for the role to arrive in.
        age = info.get("age") or 26
        age_pen = max(0.0, age - 27) * 0.22
        score = (2.5 * (g["gap"] if g else 0)
                 + (1.2 if "handcuff" in tags else 0)
                 + (1.0 if "open-committee" in tags else 0)
                 + (0.5 if "rookie" in tags else 0)
                 + (1.1 if "rookie-in-line" in tags else 0)
                 + (0.5 if "red-zone role" in tags else 0)
                 + 0.4 * (trend.get(pid, {}).get("share") or 0)
                 - age_pen)
        cands.append({
            "sleeper_id": pid, "name": info["name"], "pos": info["pos"],
            "team": info["team"], "age": info.get("age"),
            "years_exp": info.get("years_exp"),
            "depth": (depth.get(pid) or {}).get("order")
                     or info.get("depth_chart_order"),
            "injury_status": info.get("injury_status"),
            "season_value": base.get("base"),
            "bye": base.get("bye") or team_bye.get(info["team"]),
            "tags": tags, "why": why,
            "gap": g, "eff": e,
            "adds_24h": (trend.get(pid) or {}).get("count"),
            "score": round(score, 3)})
    cands.sort(key=lambda c: -c["score"])

    # --- roster outlook
    mine_ids = by_roster.get(roster_id) or []
    mine = []
    for pid in mine_ids:
        info = index.get(pid) or {}
        base = byid.get(pid) or {}
        mine.append({"sleeper_id": pid,
                     "name": base.get("name") or info.get("name") or pid,
                     "pos": base.get("pos") or info.get("pos"),
                     "team": base.get("team") or info.get("team"),
                     "age": info.get("age"), "years_exp": info.get("years_exp"),
                     "bye": base.get("bye") or team_bye.get(
                         base.get("team") or info.get("team")),
                     "season_value": base.get("base"),
                     "injury_status": info.get("injury_status"),
                     "eff": eff["players"].get(pid),
                     "gap": gaps.get(pid)})
    byes = {}
    for m in mine:
        if m.get("bye"):
            byes.setdefault(m["bye"], []).append("%s (%s)" % (m["name"], m["pos"]))
    slots = state["league"].get("slots", {})
    thin = []
    for pos in ("QB", "RB", "WR", "TE"):
        have = [m for m in mine if m["pos"] == pos]
        need = slots.get(pos, 0)
        if need and len(have) <= need:
            thin.append({"pos": pos, "rostered": len(have), "starters": need,
                         "note": "no bench cover at all" if len(have) == need
                                 else "below the starting requirement"})

    return {"roster_id": roster_id, "league_id": league_id,
            "team_name": next((t["owner"] for t in state["teams"]
                               if t["roster_id"] == roster_id), "me"),
            "stats_season": eff["season"], "stats_weeks": eff["weeks"],
            "my_roster": mine,
            "bye_clusters": sorted(
                ({"week": w, "players": p, "count": len(p)}
                 for w, p in byes.items() if len(p) >= 2),
                key=lambda x: -x["count"]),
            "thin_positions": thin,
            "backfields": {k: v for k, v in backfields.items()
                           if v["shape"] in ("committee", "workhorse")},
            "candidates": cands[:limit]}


# ------------------------------------------------------------------ AI

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outlook", "stashes"],
    "properties": {
        "outlook": {"type": "string"},
        "weak_points": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue", "when", "severity"],
                "properties": {
                    "issue": {"type": "string"},
                    "when": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["high", "medium", "low"]},
                    "fix": {"type": "string"},
                },
            },
        },
        "stashes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "case", "payoff", "confidence"],
                "properties": {
                    "name": {"type": "string"},
                    "case": {"type": "string"},
                    "payoff": {"type": "string",
                               "description": "what has to happen for this to pay"},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "drop": {"type": "string"},
                },
            },
        },
        "avoid": {"type": "string"},
    },
}

SYSTEM = """You are a fantasy football analyst looking past this week, for a \
12-team half-PPR Sleeper league. Roster: QB1, RB2, WR2, TE1, FLEX, K1, DEF1, \
4 bench.

Every number is computed for you. Do not invent statistics, and do not use \
player knowledge beyond the payload -- if something is not there, say so.

You are answering two questions, and neither is "who helps me Sunday":

1. Where does this roster break LATER? Look at "bye_clusters" (several starters \
idle the same week), "thin_positions" (no bench cover), age, and any starter \
whose production depends on staying healthy with nothing behind him.

2. Who is worth a bench spot now for a payoff weeks away?

The central signal is the opportunity gap in "gap": efficiency percentile minus \
usage percentile within the position. A high gap means he produces when touched \
but is not touched much -- a coaching decision, and those reverse. A negative \
gap means he has volume because nobody better is there, which is a job waiting \
to be taken from him, not an opportunity.

Weigh the tags honestly:
- "handcuff": the back directly behind a workhorse. His value is entirely \
conditional -- it is insurance, not an asset, and it pays only if someone gets \
hurt. Say that plainly rather than selling him as a breakout.
- "open-committee": no back on that team has the job. Genuinely unsettled, so \
the upside is real and does not require an injury.
- "efficient-unused": the strongest standalone signal here. Quote the actual \
rate and the two percentiles.
- "rookie": no usage history exists, so there is nothing to project from. Low \
confidence by construction -- do not manufacture a case from a draft slot the \
payload does not contain.
- "red-zone role": scoring chances without volume; touchdown-dependent and \
volatile.

Be honest about what a stash costs. A bench spot is a real price in a 13-man \
roster, and most stashes never pay. Rank ruthlessly, recommend few, and name \
who to drop. Say which candidates are NOT worth it and why -- that is as useful \
as the list itself.

Your own roster rows carry "gap" and "eff" too. A starter with a strongly \
NEGATIVE gap has volume his per-touch production does not justify -- that is a \
job at risk, and a weakness worth naming even though he is currently producing.

Note which season the usage data is from ("stats_season"). Early in a year the \
rates come from last season and describe roles that may no longer exist."""


def ai_analyze(board, api_key=None, refresh=False):
    payload = json.dumps({
        "team": board["team_name"],
        "stats_season": board["stats_season"], "stats_weeks": board["stats_weeks"],
        "bye_clusters": board["bye_clusters"],
        "thin_positions": board["thin_positions"],
        "my_roster": [{k: v for k, v in m.items() if k != "sleeper_id"}
                      for m in board["my_roster"]],
        "candidates": [{k: v for k, v in c.items() if k != "sleeper_id"}
                       for c in board["candidates"]],
    }, default=str)
    key = "lookahead-%s-%s" % (board.get("roster_id"), board.get("stats_season"))
    return sitstart.cached_ai(
        key, lambda: sitstart.run_model(SYSTEM, payload, SCHEMA, api_key=api_key),
        refresh=refresh)


# ------------------------------------------------------------------ output

def report(b, ai=None):
    o = ["%s -- look ahead  (usage data: %s season, %s weeks)"
         % (b["team_name"], b["stats_season"], b["stats_weeks"]), ""]
    if b["thin_positions"]:
        o.append("THIN POSITIONS")
        for t in b["thin_positions"]:
            o.append("  %-4s %d rostered for %d starting slot(s) -- %s"
                     % (t["pos"], t["rostered"], t["starters"], t["note"]))
        o.append("")
    if b["bye_clusters"]:
        o.append("BYE CLUSTERS")
        for c in b["bye_clusters"]:
            o.append("  week %-3s %d players: %s"
                     % (c["week"], c["count"], ", ".join(c["players"])))
        o.append("")
    o.append("%-24s %-4s %-4s %-6s %-4s %-9s %-7s %-6s %s"
             % ("PLAYER", "POS", "TM", "AGE/EXP", "BYE", "EFF", "USE/GM",
                "GAP", "WHY"))
    o.append("-" * 118)
    for c in b["candidates"]:
        g = c.get("gap") or {}
        e = c.get("eff") or {}
        o.append("%-24s %-4s %-4s %-6s %-4s %-9s %-7s %-6s %s" % (
            c["name"][:24], c["pos"], c["team"] or "-",
            "%s/%s" % (c.get("age") or "?", c.get("years_exp")
                       if c.get("years_exp") is not None else "?"),
            c.get("bye") or "-",
            ("%s %s" % (g.get("eff_metric", "").upper(), g.get("eff_value"))
             if g.get("eff_value") else "-"),
            e.get("touches_pg") if e.get("touches_pg") is not None else "-",
            ("%+.0f%%" % (g["gap"] * 100)) if g.get("gap") is not None else "-",
            ", ".join(c["tags"])))
    if ai and not ai.get("error"):
        o.append("")
        o.append("=" * 118)
        o.append("AI OUTLOOK")
        o.append("  " + ai.get("outlook", ""))
        for w in ai.get("weak_points", []):
            o.append("")
            o.append("  WEAK POINT [%s] %s" % (w["severity"].upper(), w["issue"]))
            o.append("    when: %s" % w["when"])
            if w.get("fix"):
                o.append("    fix : %s" % w["fix"])
        for st in ai.get("stashes", []):
            o.append("")
            o.append("  STASH %s (%s confidence)" % (st["name"], st["confidence"]))
            o.append("    %s" % st["case"])
            o.append("    pays off if: %s" % st["payoff"])
            if st.get("drop"):
                o.append("    drop: %s" % st["drop"])
        if ai.get("avoid"):
            o.append("")
            o.append("  AVOID: %s" % ai["avoid"])
    elif ai:
        o.append("")
        o.append("AI unavailable (%s): %s" % (ai["error"], str(ai["detail"])[:200]))
    return "\n".join(o)


def ai_cards(ai):
    if not ai:
        return ""
    if ai.get("error"):
        return ('<div class="panel"><h3>AI outlook</h3><div class="mut">'
                'Unavailable (%s). %s</div></div>'
                % (ai["error"], str(ai.get("detail", ""))[:300]))
    stamp = ('<a class="rerun" href="#" onclick="var u=new URL(location.href);'
             "u.searchParams.set('refresh','1');location.href=u;return false\">"
             'cached &middot; re-run</a>' if ai.get("_cached") else "")
    out = ('<div class="panel"><h3>AI outlook %s</h3><div class="read">%s</div></div>'
           % (stamp, ai.get("outlook", "")))
    for w in ai.get("weak_points", []):
        cls = {"high": "v-sit", "medium": "v-flex", "low": "v-start"}.get(
            w["severity"], "v-flex")
        out += ('<div class="card"><div class="ch"><span class="verdict %s">%s</span>'
                '<span class="nm">%s</span></div><div class="dec">%s</div>%s</div>'
                % (cls, w["severity"].upper(), w["issue"], w["when"],
                   ('<div class="mut">fix: %s</div>' % w["fix"]) if w.get("fix") else ""))
    for st in ai.get("stashes", []):
        out += ('<div class="card"><div class="ch"><span class="nm">%s</span>'
                '<span class="verdict v-must">STASH</span>'
                '<span class="mut">%s confidence</span></div>'
                '<div class="dec">%s</div>'
                '<div class="mut">pays off if: %s%s</div></div>'
                % (st["name"], st["confidence"], st["case"], st["payoff"],
                   (" &middot; drop: " + st["drop"]) if st.get("drop") else ""))
    if ai.get("avoid"):
        out += ('<div class="panel"><h3>Not worth it</h3><div class="mut">%s</div>'
                '</div>' % ai["avoid"])
    return out


def html_report(b, ai=None, job_id=None):
    rows = "".join(
        '<tr><td class="nm">%s</td><td><span class="pos %s">%s</span></td>'
        '<td class="mut">%s</td><td class="mut">%s</td><td class="mut">%s</td>'
        '<td class="big">%s</td>'
        '<td class="mut">%s</td><td class="%s">%s</td><td>%s</td></tr>' % (
            c["name"], c["pos"], c["pos"], c["team"] or "&mdash;",
            "%s / %s" % (c.get("age") or "?",
                         c["years_exp"] if c.get("years_exp") is not None else "?"),
            c.get("bye") or "&mdash;",
            ("%s %s" % ((c.get("gap") or {}).get("eff_metric", "").upper(),
                        (c.get("gap") or {}).get("eff_value"))
             if (c.get("gap") or {}).get("eff_value") else "&mdash;"),
            (c.get("eff") or {}).get("touches_pg")
            if (c.get("eff") or {}).get("touches_pg") is not None else "&mdash;",
            "pl" if ((c.get("gap") or {}).get("gap") or 0) > 0 else "mut",
            ("%+.0f%%" % ((c.get("gap") or {}).get("gap") * 100))
            if (c.get("gap") or {}).get("gap") is not None else "&mdash;",
            " ".join('<span class="tag t-%s">%s</span>'
                     % (t.split("-")[0], t) for t in c["tags"]))
        for c in b["candidates"])
    thin = "".join('<div class="row"><span><span class="pos %s">%s</span></span>'
                   '<span class="mut">%d for %d slot(s) &middot; %s</span></div>'
                   % (t["pos"], t["pos"], t["rostered"], t["starters"], t["note"])
                   for t in b["thin_positions"]) or '<div class="mut">None.</div>'
    byes = "".join('<div class="row"><span>week <b>%s</b></span>'
                   '<span class="mut">%d: %s</span></div>'
                   % (c["week"], c["count"], ", ".join(c["players"]))
                   for c in b["bye_clusters"]) or '<div class="mut">No clusters.</div>'
    cards = ai_cards(ai)
    if job_id and not cards:
        cards = sitstart.PENDING_PANEL + (sitstart.POLL_JS % json.dumps(job_id))
    return LA_TPL.replace("__ROWS__", rows).replace("__THIN__", thin) \
        .replace("__BYES__", byes).replace("__AI__", cards) \
        .replace("__TEAM__", str(b["team_name"])) \
        .replace("__SEASON__", str(b["stats_season"])) \
        .replace("__WEEKS__", str(b["stats_weeks"]))


LA_TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Look Ahead</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.hd{padding:16px 20px;background:#161b22;border-bottom:1px solid #30363d}
.hd h1{margin:0;font-size:20px}
.wrap{padding:16px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.main{flex:1 1 660px;min-width:0;max-width:100%}.rail{flex:1 1 340px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:12px}
.panel h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:7px;border-bottom:1px solid #30363d;white-space:nowrap}
th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}
th:last-child,td:last-child{text-align:left}
td{padding:7px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}
tr:hover td{background:#1c2128}
.nm{font-weight:600;color:#fff}.big{font-size:15px;font-weight:700;color:#58a6ff}
.mut{color:#8b949e}.pl{color:#3fb950}.mn{color:#f85149}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.tag{font-size:10px;padding:2px 6px;border-radius:10px;margin-right:3px;border:1px solid}
.t-efficient{background:#0d3050;color:#79c0ff;border-color:#1f6feb}
.t-handcuff{background:#3d2b56;color:#d2a8ff;border-color:#6e40c9}
.t-rookie{background:#12331d;color:#3fb950;border-color:#238636}
.t-open{background:#4a3312;color:#e3b341;border-color:#9e6a03}
.t-red{background:#3a1518;color:#f85149;border-color:#7d2427}
.row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px;gap:10px}
.card{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:10px}
.ch{display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.verdict{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;border:1px solid}
.v-must{background:#12331d;color:#3fb950;border-color:#238636}
.v-start{background:#0d3050;color:#79c0ff;border-color:#1f6feb}
.v-flex{background:#4a3312;color:#e3b341;border-color:#9e6a03}
.v-sit{background:#3a1518;color:#f85149;border-color:#7d2427}
.dec{font-size:13px;color:#c9d1d9;margin-bottom:6px}.read{font-size:13px;color:#c9d1d9}
.rerun{float:right;font-size:10px;color:#58a6ff;text-decoration:none;text-transform:none;letter-spacing:0;font-weight:400}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:-1px;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:900px){.rail{flex:1 1 100%}}
</style></head><body>
<div class="nav"><a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a><a href="#" data-p="/lookahead">Look Ahead</a><a href="#" data-p="/board">Draft board</a><a href="#" data-p="/analysis">Analysis</a></div>
<script>(function(){var qs=location.search||'';
var here=location.pathname==='/'?'/sitstart':location.pathname;
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
 if(here===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__TEAM__ &mdash; look ahead</h1>
<div class="mut">Usage rates from the __SEASON__ season (__WEEKS__ weeks). Ranked on
opportunity gap: how well a player performs per touch versus how often he is used.</div></div>
<div class="wrap">
 <div class="main"><div class="panel" style="padding:0"><table>
  <thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Age / exp</th>
  <th>Bye</th><th>Efficiency</th><th>Touches/gm</th><th>Gap</th><th>Why</th></tr></thead>
  <tbody>__ROWS__</tbody></table></div>
  <div class="mut" style="font-size:12px"><b>Gap</b> is efficiency percentile minus
  usage percentile within the position &mdash; high means productive but under-used, which
  is a coaching decision that can reverse &middot; <b>handcuff</b> pays only on an injury
  ahead of him &middot; <b>open-committee</b> needs no injury at all</div>
 </div>
 <div class="rail">__AI__
  <div class="panel"><h3>Thin positions</h3>__THIN__</div>
  <div class="panel"><h3>Bye clusters</h3>__BYES__</div>
 </div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--me", type=int, required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    board = stash_board(a.draft, a.me, a.limit)
    ai = None if a.no_ai else ai_analyze(board, refresh=a.refresh)
    if a.json:
        print(json.dumps({"board": board, "ai": ai}, indent=1, default=str))
    else:
        print(report(board, ai))
