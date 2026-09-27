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
import statistics
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

# Kickers and defences are streamed, not added. The best available defence is
# worth about a point a week over the one you already have, that edge does not
# persist -- you stream again next week -- and there is no version of the wire
# where a kicker is the pickup that changed your season. They rank on this
# week's projection alone, in their own strip, and never compete for a place
# on the board with a back who might take over a backfield in October.
STREAM_POS = ("K", "DEF")
SKILL_POS = ("RB", "WR", "TE")

# Most seats on the board belong to RB and WR. A tight end can be a real add,
# but a board that is half tight ends is a board that has confused "positive
# number" with "player worth a claim".
BOARD_CAP = {"TE": 4, "QB": 2}

# Usage upside from nflverse (feeds/nflverse.py). Each is gated on sample size:
# a share over one game is a box score, not a role.
RISE_TGT_PP = 8.0        # target share up this many points, last 3 vs season
RISE_MIN_TGT_PG = 4.0    # ...on at least this many targets a game recently
RISE_UPSIDE = 1.5
WOPR_ROLE = 0.45         # weighted opportunity: a real receiving role
WOPR_UPSIDE = 1.0
UNLUCKY_DIFF = -3.0      # scoring 3+ pts/g under his opportunity quality
UNLUCKY_UPSIDE = 1.0
GL_CARRIES = 2           # goal-line carries (inside the 5)...
RZ_SHARE = 0.30          # ...or this share of team red-zone chances
GL_MAX_CARRY_SHARE = 0.5  # ...on a light overall role
GL_UPSIDE = 0.8
MIN_GAMES = 2

# Room pricing (feeds/league.py). Once the league has this many completed,
# non-zero claims, bids scale by what THIS room pays for a contested add
# relative to ROOM_REF_PCT, the community-convention price of one (the middle
# of the 4-7% bands above). Chosen, not fitted; clamped so one odd week
# cannot double every bid.
ROOM_MIN_CLAIMS = 8
ROOM_REF_PCT = 5.0
ROOM_FACTOR_RANGE = (0.6, 1.6)
# From week 3 the preseason auction value is stale: quality switches to
# rest-of-season dollars (trade value, else ROS points) mapped onto the same
# dollar curve.
ROS_QUALITY_FROM_WEEK = 3

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


def score_candidate(fa, mine, flex_bar, league, trend, role=None):
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
    quality = (fa.get("quality_value") if fa.get("quality_value") is not None
               else fa.get("season_value")) or 0.0

    hurdle = POS_HURDLE.get(pos, 1.5)
    clears = need_delta >= hurdle

    tr = trend.get(fa.get("sleeper_id")) or {}
    demand = tr.get("share", 0.0)

    # Need is the spine of the ranking. A player who would make the lineup
    # worse must not out-rank a real upgrade just because the whole internet
    # is adding him -- demand sets his PRICE, not his value to this roster.
    if need_delta <= 0:
        # Not this week's starter is not the same as worthless. At the skill
        # positions the penalty is compressed, because "he would not crack my
        # lineup on Sunday" is true of essentially every add that wins a
        # league in September -- the payoff is a role in October. At a
        # single-slot position it is not compressed: a backup kicker really
        # is worth nothing.
        core = need_delta * (0.45 if pos in SKILL_POS else 1.0)
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

    # Upside: what the job could become. This is the half of a waiver claim
    # that actually wins leagues, and weekly projection cannot see it -- a
    # back who takes over a backfield in October projects four points today.
    # Every term is role evidence (snap share, depth chart, per-touch rate on
    # light usage), never demand: the crowd still only sets the price.
    r = role or {}
    e = r.get("eff") or {}
    tags = r.get("tags") or ()
    upside, drivers = 0.0, []
    if pos in SKILL_POS:
        # How much work the per-touch rate actually describes. A back is
        # judged on touches and a receiver on targets, and both are scored
        # against what a real role looks like at the position.
        vol_raw = (e.get("tgt_pg") if pos in ("WR", "TE")
                   else e.get("touches_pg")) or 0.0
        vol = min(1.0, vol_raw / (4.0 if pos in ("WR", "TE") else 7.0))

        gap = (r.get("gap") or {}).get("gap")
        if gap and gap >= 0.25:
            # A gap computed on two targets a game is arithmetic, not
            # evidence -- 10 YPT on 26 targets says nothing about what the
            # player does with a job. Weight the rate by the volume behind it.
            upside += min(gap, 0.9) * 5.0 * vol
            drivers.append("%s %.2f on %.1f/gm (gap %+.2f)"
                           % ((r["gap"].get("eff_metric") or "eff").upper(),
                              r["gap"].get("eff_value") or 0, vol_raw, gap))
        if "open-committee" in tags:
            upside += 3.0
            drivers.append("backfield unsettled -- no injury needed")
        if "handcuff" in tags:
            upside += 1.5
            drivers.append("direct backup to a workhorse")
        if "rookie-in-line" in tags:
            upside += 2.0
            drivers.append("rookie already top-2 on the depth chart")
        use_pct = (r.get("gap") or {}).get("use_pct")
        if ("red-zone role" in tags and vol_raw >= 3.0
                and (use_pct is None or use_pct < 0.55)):
            # Nearly every backup tight end clears four red-zone targets over
            # a season, and "touches" counts a receiver's catches rather than
            # his targets, so a starter on 90% of snaps reads as light usage.
            # Require a real workload behind the rate and genuinely low usage
            # for the position, or the tag describes half the league.
            upside += 1.0
            drivers.append("red-zone work on a light role")
        u = r.get("usage") or {}
        x = r.get("xfp") or {}
        z = r.get("redzone") or {}
        games = u.get("games") or 0
        if (games >= MIN_GAMES and (u.get("trend_tgt") or 0) >= RISE_TGT_PP
                and (u.get("targets_recent_pg") or 0) >= RISE_MIN_TGT_PG):
            upside += RISE_UPSIDE
            drivers.append("target share up %d pts over last 3" % round(u["trend_tgt"]))
        if games >= MIN_GAMES and (u.get("wopr") or 0) >= WOPR_ROLE:
            upside += WOPR_UPSIDE
            drivers.append("WOPR %.2f" % u["wopr"])
        if (x.get("games") or 0) >= MIN_GAMES and x.get("diff_pg") is not None \
                and x["diff_pg"] <= UNLUCKY_DIFF:
            upside += UNLUCKY_UPSIDE
            drivers.append("xFP says the role is %.1f pts/g better than results"
                           % -x["diff_pg"])
        if ((z.get("gl_carries") or 0) >= GL_CARRIES or (z.get("rz_share") or 0) >= RZ_SHARE) \
                and (u.get("carry_share") or 0) < GL_MAX_CARRY_SHARE and games >= MIN_GAMES:
            upside += GL_UPSIDE
            drivers.append("goal-line work on a light role")
        snap = e.get("snap_share")
        if snap and snap >= 0.55:
            # Being on the field is table stakes, not a thesis -- it separates
            # a player from a name, and nothing more.
            upside += 1.0
            drivers.append("on %d%% of snaps" % round(snap * 100))

    score = core + quality_term + demand_term + upside
    return {"need_delta": need_delta, "bar": round(bar, 2), "clears_hurdle": clears,
            "hurdle": hurdle, "quality": quality, "demand": demand,
            "trend_rank": tr.get("rank"), "trend_count": tr.get("count"),
            "replaces": (worst or {}).get("name"), "score": round(score, 2),
            "upside": round(upside, 2), "upside_why": drivers, "tags": list(tags),
            "snap_share": (r.get("eff") or {}).get("snap_share"),
            "opp_gap": (r.get("gap") or {}).get("gap"),
            "stream_only": pos in STREAM_POS or (pos == "QB" and not clears)}


def faab_bid(cand, budget_left, league, season_budget=None, room=None, opp_max=None):
    """Recommended FAAB as a percentage of the season budget, and in dollars.

    Anchored on measured demand (how many leagues actually added him in the
    last 24 hours) and on how much he improves this specific lineup, then
    capped by what is actually left to spend.
    """
    need = cand["need_delta"]
    demand = cand["demand"]
    q = cand["quality"]
    upside = cand.get("upside") or 0.0

    # What the claim is worth: the lineup help he gives now, plus what the
    # role could become. Pricing off need alone put every September add at
    # zero -- which is how you lose the back you needed in October because
    # you would not bid on him in week 2.
    edge = max(need, 0.0) + upside

    pct = 0.0
    if edge >= 8:
        pct = 26.0
    elif edge >= 6:
        pct = 15.0
    elif edge >= 4:
        pct = 7.0
    elif edge >= 2.5:
        pct = 4.0
    elif edge >= 1:
        pct = 2.0
    else:
        pct = 1.0
    pct *= 1.0 + 0.8 * demand              # crowd demand raises the clearing price
    if q >= 25:
        pct *= 1.25                        # genuine season-long asset
    if demand <= 0.02 and not cand.get("clears_hurdle"):
        # Nobody else is bidding. FAAB is a sealed auction against the room,
        # not a valuation exercise, so a strong case on a player with no
        # measured demand is won at the minimum -- paying more buys nothing
        # but a smaller budget for the week somebody good does come free.
        pct = min(pct, 3.0)
    if cand.get("stream_only"):
        # A one-week rental at a position you will stream again next week.
        # Token money, whatever the projection says.
        pct = min(pct, 1.5)
    elif need <= 0 and not cand["clears_hurdle"] and upside < 2.0:
        # No lineup help this week and no role case behind it. That is a bench
        # body, not a claim -- the model flagged the old 0.5x discount here as
        # far too rich and it was right.
        pct = min(pct * 0.35, 2.0)
    room_factor = None
    if room and room.get("n", 0) >= ROOM_MIN_CLAIMS and pct > 1.0 \
            and not cand.get("stream_only"):
        b = room.get("median_pct_by_band") or {}
        ref = b.get("contested") or b.get("all")
        if ref:
            lo, hi = ROOM_FACTOR_RANGE
            room_factor = round(max(lo, min(hi, ref / ROOM_REF_PCT)), 2)
            pct *= room_factor
    pct = max(0.0, min(55.0, pct))

    band = next((lbl for lo, hi, lbl in FAAB_BANDS if lo <= pct <= hi), "streamer")
    dollars = None
    base = season_budget or budget_left
    if base is not None:
        dollars = int(round(base * pct / 100.0))
        # Never more than you have, and never more than the richest other
        # team can bid: one dollar over their whole budget already wins.
        if budget_left is not None:
            dollars = min(dollars, budget_left)
        if opp_max is not None:
            dollars = min(dollars, opp_max + 1)
    return {"faab_pct": round(pct, 1), "faab_dollars": dollars, "band": band,
            "room_factor": room_factor}


# ------------------------------------------------------------------ board

def _nflverse(league_id, week=None, pool=None):
    """{usage, xfp, redzone} from feeds.nflverse, fetched together; {} if down."""
    try:
        from sleeper_auction.feeds import common, nflverse, scoring
    except Exception:
        return {}
    tasks = {}
    if common.enabled("nflverse.usage"):
        tasks["usage"] = lambda: nflverse.usage()
    if common.enabled("ffopportunity.ep"):
        tasks["xfp"] = lambda: nflverse.expected_points(
            rec_value=scoring.rec_value(scoring.league_scoring(league_id)))
    if common.enabled("ffopportunity.pbp"):
        tasks["redzone"] = lambda: nflverse.redzone()
    if common.enabled("projections"):
        tasks["consensus"] = lambda: sitstart._consensus(week, league_id)
    if common.enabled("league"):
        from sleeper_auction.feeds import league as lgfeed
        tasks["faab"] = lambda: lgfeed.faab(league_id)
        tasks["room"] = lambda: _room(lgfeed, league_id, week)
    if common.enabled("values") and int(week or 1) >= ROS_QUALITY_FROM_WEEK:
        tasks["ros"] = lambda: _ros_values(league_id, week, pool)
    if common.enabled("props"):
        from sleeper_auction.feeds import props
        tasks["props"] = lambda: props.week_props(sitstart.SEASON, week)
        tasks["scoring"] = lambda: scoring.league_scoring(league_id)
    res, errs = common.parallel(tasks, timeout=60)
    for k, e in errs.items():
        sys.stderr.write("waivers: %s unavailable (%s)\n" % (k, e))
    res["_sources"] = {k: {"season": v.get("season"), "weeks": v.get("weeks")}
                       for k, v in res.items() if isinstance(v, dict) and k in
                       ("usage", "xfp", "redzone")}
    if res.get("props"):
        res["_sources"]["props"] = {"as_of": res["props"].get("as_of")}
    if res.get("consensus"):
        res["_sources"]["consensus"] = {"sources": res["consensus"].get("sources"),
                                        "missing": res["consensus"].get("missing")}
    return res


def _room(lgfeed, league_id, week):
    from sleeper_auction.feeds import ids
    cw = ids.crosswalk()
    lg = lgfeed._league(league_id)
    budget = int((lg.get("settings") or {}).get("waiver_budget") or 0)
    if not budget:
        return None
    hist = lgfeed.faab_history(league_id, week)

    def pos_of(pid):
        return (cw["info"].get(pid) or {}).get("pos") or ("DEF" if pid.isalpha() else None)
    return lgfeed.clearing_curve(hist, budget, pos_of)


def _ros_values(league_id, week, pool=None):
    """{sid: {"dollars", "source", "trend30", "ros_pts"}} -- trade value where
    FantasyCalc has the player, else ROS points, both on the draft dollar
    curve so "quality" keeps its units."""
    from sleeper_auction.feeds import scoring, values
    from sleeper_auction.feeds import league as lgfeed
    lg = lgfeed._league(league_id)
    pool = pool or db.value_pool(db.build_pool()[0])
    out = {}
    try:
        tv = values.trade_values(lg, pool)
    except Exception as e:
        sys.stderr.write("waivers: trade values unavailable (%s)\n" % e)
        tv = {}
    ros = {}
    try:
        ros = values.ros_points(sitstart.SEASON, int(week) + 1,
                                scoring.league_scoring(league_id), cache_only=True,
                                season_proj={p["sleeper_id"]: p.get("proj") for p in pool
                                             if p.get("sleeper_id")})
    except Exception as e:
        sys.stderr.write("waivers: ROS points unavailable (%s)\n" % e)
    if not ros or next(iter(ros.values())).get("source") == "season-share":
        from sleeper_auction.feeds import prefetch
        prefetch.kick_ros(sitstart.SEASON, int(week) + 1)
    ros_d = values.ros_dollars({s: v["pts"] for s, v in ros.items()}, pool) if ros else {}
    for s in set(tv) | set(ros_d):
        t = tv.get(s) or {}
        if t.get("dollars") is not None:
            out[s] = {"dollars": t["dollars"], "source": "trade-value",
                      "trend30": t.get("trend30"), "disagree": t.get("disagree")}
        else:
            out[s] = {"dollars": ros_d.get(s), "source": "ros-points"}
        if s in ros:
            out[s]["ros_pts"] = ros[s]["pts"]
            out[s]["ros_source"] = ros[s]["source"]
    return out


def _props_for(fd, pid, pos, pj):
    """Market view where one exists. Fringe players rarely have markets, so
    this is context for the model, never a ranking input."""
    if not fd.get("props") or not pj or not pj.get("stats"):
        return None
    try:
        from sleeper_auction.feeds import props
        return props.player_props(fd["props"], pid, pos, pj["stats"], fd.get("scoring"))
    except Exception:
        return None


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

    # Role evidence, shared with the look-ahead page: per-touch efficiency
    # against usage, and whether a backfield has an owner. This is what lets
    # the board rank a back who is about to matter above a defence that is
    # worth a point on Sunday.
    from sleeper_auction import lookahead
    try:
        index = lookahead.player_index()
        eff = lookahead.efficiency()
        gaps = lookahead.opportunity_gaps(eff, index)
        backfields = lookahead.backfield_map(index, eff)
        roles = {}
        for pid, info in index.items():
            e = eff["players"].get(pid)
            g = gaps.get(pid)
            tags, _why = lookahead.classify(pid, info, e, g, backfields, depth)
            if tags or g or e:
                roles[pid] = {"tags": tags, "gap": g, "eff": e}
    except Exception as exc:          # role data is an enrichment, not a gate
        roles = {}
        sys.stderr.write("waivers: role data unavailable (%s)\n" % exc)

    # Usage, expected points and red-zone share from nflverse. Optional: with
    # the feed down every candidate scores exactly as before.
    fd = _nflverse(league_id, week, pool)
    for pid in set(roles) | set((fd.get("usage") or {}).get("players") or {}):
        v = {"usage": sitstart._usage_view(fd, pid), "xfp": sitstart._xfp_view(fd, pid),
             "redzone": sitstart._rz_view(fd, pid)}
        if any(v.values()):
            roles.setdefault(pid, {"tags": (), "gap": None, "eff": None}).update(v)

    cons = (fd.get("consensus") or {}).get("players") or {}

    def enrich(pid, p):
        team = p.get("team")
        ln = lines.get(team) or {}
        sl = (wk.get(pid) or {}).get("proj")
        c = cons.get(pid) if sl is not None else None
        return {"sleeper_id": pid, "name": p["name"], "pos": p["pos"], "team": team,
                "season_value": p.get("base"), "season_tier": p.get("tier"),
                "value_conf": p.get("value_conf"),
                # Consensus of the projection sources where there is one: props
                # are rare for free agents, so they are context, not the number.
                "wk_proj": c["median"] if c else sl,
                "quality_value": ((fd.get("ros") or {}).get(pid) or {}).get("dollars"),
                "ros_dollars": ((fd.get("ros") or {}).get(pid) or {}).get("dollars"),
                "ros": (fd.get("ros") or {}).get(pid),
                "wk_proj_sleeper": sl,
                "wk_proj_range": [c["lo"], c["hi"], c["n"]] if c else None,
                "opponent": ln.get("opp"), "implied_total": ln.get("implied"),
                "depth": depth.get(pid), "injury": inj.get(db.nname(p["name"])),
                "props": _props_for(fd, pid, p["pos"], wk.get(pid)),
                "usage": (roles.get(pid) or {}).get("usage"),
                "xfp": (roles.get(pid) or {}).get("xfp"),
                "redzone": (roles.get(pid) or {}).get("redzone"),
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
        c.update(score_candidate(c, mine, flex_bar, state["league"], tr,
                                 roles.get(pid)))
        cands.append(c)
    cands.sort(key=lambda x: -x["score"])

    # The board and the streamers are different questions, so they are
    # different lists. Mixing them is what buried every running back under a
    # wall of defences worth a point each.
    streamers = []
    for pos in STREAM_POS + ("QB",):
        pool_pos = [c for c in cands if c["pos"] == pos and c["stream_only"]]
        pool_pos.sort(key=lambda x: -(x.get("wk_proj") or 0))
        streamers.extend(pool_pos[:3])
    cands = [c for c in cands if not c["stream_only"]]

    # Running backs and receivers are where waiver claims are won: they carry
    # the flex, they absorb injuries, and their roles actually change during a
    # season. One-slot positions cannot do any of that, so they are capped
    # rather than allowed to fill a board on arithmetic.
    seen = {}
    kept = []
    for c in cands:
        n = seen.get(c["pos"], 0)
        if n >= BOARD_CAP.get(c["pos"], 99):
            continue
        seen[c["pos"]] = n + 1
        kept.append(c)
    cands = kept

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

    # Real FAAB: rosters carry waiver_budget_used, so this is what is left,
    # for me and for every opponent (feeds/league.py). Falls back to the
    # season budget when the feed is down.
    budget_left = season_budget = opp = room = None
    try:
        lg = db.gj("https://api.sleeper.app/v1/league/%s" % league_id, ttl=600)
        wb = (lg.get("settings") or {}).get("waiver_budget")
        if wb:
            budget_left = season_budget = int(wb)
    except Exception:
        pass
    lf = fd.get("faab") or {}
    if lf.get(roster_id):
        budget_left = lf[roster_id]["left"]
        others = [v["left"] for r, v in lf.items() if r != roster_id]
        if others:
            opp = {"max": max(others), "median": int(statistics.median(others)),
                   "spent_most": max(v["used"] for r, v in lf.items() if r != roster_id)}
    room = fd.get("room")
    for c in cands[:limit] + streamers:
        c.update(faab_bid(c, budget_left, state["league"], season_budget, room,
                          (opp or {}).get("max")))
    news = {}
    try:
        from sleeper_auction.feeds import news as newsfeed
        news = newsfeed.player_news([c["sleeper_id"] for c in cands[:limit]]
                                    + [p["sleeper_id"] for p in my_players])
    except Exception as e:
        sys.stderr.write("waivers: news unavailable (%s)\n" % e)
    for c in cands[:limit]:
        if news.get(c["sleeper_id"]):
            c["news"] = news[c["sleeper_id"]]

    return {"week": week, "league_id": league_id, "roster_id": roster_id,
            "draft_id": draft_id,
            "team_name": next((t["owner"] for t in state["teams"]
                               if t["roster_id"] == roster_id), "me"),
            "league": state["league"], "flex_bar": round(flex_bar, 2),
            "faab_budget": budget_left,
            "faab_season_budget": season_budget,
            "faab_real": bool(lf.get(roster_id)),
            "opp_budgets": opp,
            "room_prices": room,
            "my_roster": my_players, "needs": needs,
            "candidates": cands[:limit], "streamers": streamers,
            "dropped": [{"id": k, **v} for k, v in list(dropped.items())[:10]],
            "free_agent_count": len(cands),
            "feed_sources": fd.get("_sources"),
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

Three forces decide a pickup and they pull against each other:

- NEED is "need_delta": weekly projected points above the player he would \
actually replace in this lineup ("replaces", "bar"). This is a fact about the \
roster, not about the player. It is NEGATIVE for almost every add worth making \
in September, and that is expected -- a good waiver claim usually does not \
crack the lineup the week you make it.
- UPSIDE is "upside" and "upside_why": role evidence that a weekly projection \
cannot see. "opp_gap" is per-touch production percentile minus usage \
percentile within the position -- positive means he produces on the touches he \
gets and does not get many, which is a coaching decision that can reverse. \
"tags" names the situation: open-committee needs no injury at all, handcuff \
pays only if the starter misses time, rookie-in-line is already top-2 on the \
depth chart. "snap_share" says whether he is on the field. THIS is the half \
that wins leagues; need only describes this Sunday.
  Usage drivers from nflverse also feed upside, each gated on at least two \
games: a target share up 8+ points over the last three games on 4+ targets a \
game ("usage.trend_tgt"); a WOPR of 0.45 or more ("usage.wopr", 1.5 x target \
share + 0.7 x air-yards share: a real receiving role); an "xfp.diff_pg" of -3 \
or worse (he has scored 3+ points a game less than the quality of his \
opportunities predicts -- the role is better than the results, which is the \
buy); and goal-line work on a light overall role ("redzone.gl_carries", \
"redzone.rz_share"). "feed_sources" says which season and how many weeks \
those came from. Two games of a share is a hint; say so.
- "wk_proj" is the consensus median of up to six projection sources scored \
with this league's rules (Sleeper's own number where no consensus exists); \
"wk_proj_sleeper" is Sleeper's, and "wk_proj_range" is [low, high, number of \
sources]. Need, the flex bar and every delta are computed on "wk_proj".
- "props", when present, is this week's betting-market view in half-PPR \
points (mean, p10/p90, anytime-TD probability). Most free agents have no \
market at all; when one does, a market mean well above his projection is a \
sign the books expect a bigger role this week.
- QUALITY is "quality" (in draft dollars): from week 3 it is his \
REST-OF-SEASON value -- FantasyCalc trade value where he has one ("ros.source" \
"trade-value"), else his rest-of-season projected points ("ros-points") -- \
mapped onto the draft dollar curve; before that, or with the feeds down, it \
is the preseason "season_value". "ros.trend30" is his 30-day trade-value \
move. A genuinely \
valuable player is worth rostering even without a need, because rosters churn \
and good players win leagues.

Rate a gap against the volume behind it. A back producing on eight touches a \
game is evidence; the same rate on two touches is arithmetic on a tiny \
denominator, and you should say so rather than sell it as a breakout.

The candidate board is running backs, receivers and tight ends only. Kickers \
and defences are in "streamers", deliberately separated: the best available \
defence is worth about a point a week over the one already rostered, that edge \
does not persist because you stream again next week, and no kicker has ever \
been the pickup that changed a season. Never present a streamer as a priority \
claim, and never rank one above a back or receiver whose role is moving. \
Quarterbacks appear in "streamers" too unless one clears the hurdle over the \
current starter -- bye-week and injury cover, not a claim worth budget.

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
- "faab_budget" is what I actually have LEFT (Sleeper's waiver_budget_used), \
"opp_budgets" what the other teams have left (max, median): nobody can bid \
more than the max, so no bid needs to exceed it. "room_prices" is what this \
league has actually paid on completed claims, as a share of the season \
budget (contested vs uncontested, by position); once it has 8+ claims the \
computed bids are scaled by it ("room_factor").
- "news" on a candidate is REPORTED news (RotoWire via ESPN, dated), a \
different class from the Reddit titles. Newer news supersedes older; a \
practice report later in the week supersedes both.
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
        "streamers": [{k: c.get(k) for k in
                       ("name", "pos", "wk_proj", "need_delta", "replaces")}
                      for c in board.get("streamers", [])],
        "news": [n["title"] for n in board["news"][:25]],
        "feed_sources": board.get("feed_sources"),
        "faab_budget": board.get("faab_budget"),
        "opp_budgets": board.get("opp_budgets"),
        "room_prices": board.get("room_prices"),
    }, default=str)
    key = "waivers-%s-w%s" % (board.get("roster_id"), board.get("week"))
    return sitstart.cached_ai(
        key, lambda: sitstart.run_model(SYSTEM, payload, SCHEMA, api_key=api_key),
        refresh=refresh)


# ------------------------------------------------------------------ output

def _budget_text(b):
    if not b.get("faab_budget") and b.get("faab_budget") != 0:
        return "unknown"
    if b.get("faab_real"):
        t = "$%d left of $%d" % (b["faab_budget"], b.get("faab_season_budget") or 0)
        o = b.get("opp_budgets")
        if o:
            t += " (opponents: most $%d, median $%d)" % (o["max"], o["median"])
        r = (b.get("room_prices") or {})
        rb = r.get("median_pct_by_band") or {}
        if r.get("n"):
            t += " | room pays %s%% median, %s%% contested (%d claims)" % (
                rb.get("all"), rb.get("contested"), r["n"])
        return t
    return "$%d season budget (spent unknown)" % b["faab_budget"]


def report(b, ai=None):
    o = ["%s -- week %s waiver wire" % (b["team_name"], b["week"]),
         "%d free agents considered | flex bar %.1f pts | FAAB %s"
         % (b["free_agent_count"], b["flex_bar"], _budget_text(b)), ""]
    o.append("ROSTER NEEDS")
    for n in b["needs"]:
        st = ", ".join("%s (%.1f)" % (s["name"], s["wk_proj"] or 0)
                       for s in n["starters"]) or "none"
        o.append("  %-4s %d slot(s), %d rostered | starters: %s%s"
                 % (n["pos"], n["slots"], n["rostered"], st,
                    "  <-- UNFILLED" if n["unfilled"] else ""))
    o.append("")
    o.append("%-22s %-4s %-6s %-7s %-6s %-8s %-7s %s"
             % ("PLAYER", "POS", "WK", "NEED", "UPSIDE", "ADDS/24h", "FAAB", "WHY"))
    o.append("-" * 118)
    for c in b["candidates"]:
        o.append("%-22s %-4s %-6s %-7s %-6s %-8s %-7s %s" % (
            c["name"][:22], c["pos"],
            ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "-",
            "%+.1f" % c["need_delta"],
            ("%+.1f" % c["upside"]) if c.get("upside") else "-",
            "{:,}".format(c["trend_count"]) if c.get("trend_count") else "-",
            ("%.0f%%%s" % (c.get("faab_pct", 0),
                           ("/$%d" % c["faab_dollars"]) if c.get("faab_dollars") else "")),
            "; ".join(c.get("upside_why") or []) or (c.get("replaces") or "-")))

    if b.get("streamers"):
        o.append("")
        o.append("STREAMERS -- worth about a point a week, and only this week.")
        o.append("Never claim one ahead of a back or receiver whose role is moving.")
        for c in b["streamers"]:
            o.append("  %-22s %-4s %-6s %-7s %s" % (
                c["name"][:22], c["pos"],
                ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "-",
                "%+.1f" % c["need_delta"], c.get("replaces") or "-"))
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
        '<td class="big">%s</td><td class="%s">%+.1f</td><td class="%s">%s</td>'
        '<td class="mut">%s</td><td class="faab">%s%%%s</td><td class="mut">%s</td></tr>'
        % (c["name"], c["pos"], c["pos"],
           ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "&mdash;",
           "pl" if c["need_delta"] > 0 else "mn", c["need_delta"],
           "pl" if c.get("upside") else "mut",
           ("+%.1f" % c["upside"]) if c.get("upside") else "&mdash;",
           "{:,}".format(c["trend_count"]) if c.get("trend_count") else "&mdash;",
           round(c.get("faab_pct", 0)),
           (" <span class='mut'>$%d</span>" % c["faab_dollars"])
           if c.get("faab_dollars") else "",
           " &middot; ".join(c.get("upside_why") or [])
           or ("replaces " + c["replaces"] if c.get("replaces") else "&mdash;"))
        for c in b["candidates"])
    streamers = "".join(
        '<div class="row"><span><span class="pos %s">%s</span> %s</span>'
        '<span class="mut">%s &middot; %+.1f</span></div>'
        % (c["pos"], c["pos"], c["name"],
           ("%.1f" % c["wk_proj"]) if c.get("wk_proj") else "&mdash;",
           c["need_delta"])
        for c in b.get("streamers", []))
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
    opts = "".join('<option value="%d"%s>%d</option>'
                   % (w, " selected" if w == b["week"] else "", w)
                   for w in range(1, 19))
    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (k, v)
                     for k, v in (("draft", b.get("draft_id") or ""),
                                  ("me", b.get("roster_id") or ""))
                     if v)
    return WV_TPL.replace("__OPTS__", opts).replace("__HIDDEN__", hidden) \
        .replace("__ROWS__", rows).replace("__NEEDS__", needs) \
        .replace("__STREAM__", streamers) \
        .replace("__NEWS__", news).replace("__AI__", cards) \
        .replace("__DATA__", sitstart._data_panel()) \
        .replace("__TEAM__", str(b["team_name"])).replace("__WK__", str(b["week"])) \
        .replace("__N__", str(b["free_agent_count"])) \
        .replace("__BUD__", _budget_text(b)) \
        .replace("__BAR__", "%.1f" % b["flex_bar"])


WV_TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waiver Wire</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.wksel{color:#8b949e;font-size:12px;padding-right:10px;display:flex;align-items:center;gap:2px;margin:0}.wksel select{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:3px 6px;font:inherit;margin-left:4px}
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
<div class="nav"><a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a><a href="#" data-p="/lookahead">Look Ahead</a><a href="#" data-p="/trades">Trades</a><a href="#" data-p="/board">Draft board</a><a href="#" data-p="/analysis">Analysis</a><span class="navsp"></span><form class="wksel" method="get" action="">__HIDDEN__week&nbsp;<select name="week" onchange="this.form.submit()">__OPTS__</select><noscript><button type="submit">go</button></noscript></form></div>
<script>(function(){var qs=location.search||'';
var here=location.pathname==='/'?'/sitstart':location.pathname;
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
 if(here===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__TEAM__ &mdash; week __WK__ waivers</h1>
<div class="mut">__N__ free agents &middot; flex bar __BAR__ pts &middot; FAAB __BUD__</div></div>
<div class="wrap">
 <div class="main"><div class="panel" style="padding:0"><table>
  <thead><tr><th>Player</th><th>Pos</th><th>Wk proj</th><th>Need</th><th>Upside</th>
  <th>Adds/24h</th><th>FAAB</th><th>Why he is here</th></tr></thead><tbody>__ROWS__</tbody></table></div>
  <div class="mut" style="font-size:12px"><b>Need</b> is weekly points above the player he would
  actually replace in your lineup, so it is negative for almost every add worth making in
  September &middot; <b>Upside</b> is role evidence &mdash; per-touch production on light usage,
  an unsettled backfield, a rookie already in line &mdash; which is what need cannot see
  &middot; <b>Adds/24h</b> is how many Sleeper leagues added him, which sets his price, not his
  value to you &middot; <b>FAAB</b> is a suggested opening bid.
  Kickers and defences are streamed separately: they are worth about a point a week and that
  edge does not carry into next week.</div>
 </div>
 <div class="rail"><div id="ai-slot">__AI__</div>
  <div class="panel"><h3>Streamers &mdash; K / DEF / QB</h3>__STREAM__
   <div class="mut" style="font-size:11px;margin-top:7px">Worth roughly a point a week,
   and only this week. Kept off the board so they cannot outrank a back whose role is
   about to change.</div></div>
  <div class="panel"><h3>Roster needs</h3>__NEEDS__</div>
  <div class="panel"><h3>r/fantasyfootball</h3>__NEWS__</div>
  __DATA__
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
