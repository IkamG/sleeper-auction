"""Rest-of-season value: trade values, ROS rankings, ROS points, schedule.

Python 3.9, stdlib only. Probed 2026-09-27:
  * FantasyCalc api.fantasycalc.com/values/current?isDynasty=false&numQbs=1
    &numTeams=12&ppr=0.5: a list of 198 players; per entry value, redraftValue,
    overallRank, positionRank, trend30Day, maybeTier, player.sleeperId
    (so the join is exact). Values come from real completed trades.
    isDynasty is always sent as false (one public integration shipped a bug
    deriving it from league type); numQbs/numTeams/ppr come from the league.
  * FantasyPros ros-half-point-ppr-overall.php: ecrData with 397 players,
    rank_ecr / rank_ave / rank_std / pos_rank. NO tier field (weekly pages
    have none either), so pos_rank is what the UI shows.
  * Sleeper publishes weekly projections for future weeks (week 9 returned
    200 on week 3), so ROS points are summed from Sleeper's own weekly
    numbers. FanDuel REMAINING (numberFire ROS points, 491 players) is the
    fallback, then season projection x remaining games / 17.
  * KeepTradeCut keeptradecut.com/fantasy-rankings (the redraft page): the
    page embeds `var oneQBPlayers = [...]` and `var superflexPlayers = [...]`,
    each entry with playerID, playerName, position, team and oneQBValues /
    superflexValues {value, rank, positionalRank, overallTrend, ...}. BUT the
    static HTML carried only 3 players (the other ~370 of
    `filteredPlayersIds` load client-side), so the adapter yields almost
    nothing today. KTC's terms prohibit scrapers: it is opt-in (KTC_ENABLED=1),
    one fetch a day at most, and every consumer works without it.

The Sleeper future-week sums (14 requests of ~850 KB) and the FantasyPros ROS
page are slow: the prefetcher fills them, and pages read the cache.
"""
import os
import statistics

from sleeper_auction.feeds import common, ids

FC_URL = ("https://api.fantasycalc.com/values/current?isDynasty=false&numQbs=%d"
          "&numTeams=%d&ppr=%s")
KTC_URL = "https://keeptradecut.com/fantasy-rankings"
LAST_FANTASY_WEEK = 17
DISAGREE = 0.15


def league_params(lg):
    slots = lg.get("roster_positions") or []
    rec = (lg.get("scoring_settings") or {}).get("rec", 0.5)
    return {"numQbs": 2 if "SUPER_FLEX" in slots else 1,
            "numTeams": int(lg.get("total_rosters") or 12),
            "ppr": ("%g" % float(rec)) if rec is not None else "0.5"}


def parse_fantasycalc(d):
    out = {}
    for e in d or []:
        sid = common.sid_str((e.get("player") or {}).get("sleeperId"))
        if not sid:
            continue
        out[sid] = {"value": e.get("value"), "redraft_value": e.get("redraftValue"),
                    "rank": e.get("overallRank"), "pos_rank": e.get("positionRank"),
                    "trend30": e.get("trend30Day"), "tier": e.get("maybeTier"),
                    "pos": (e.get("player") or {}).get("position")}
    return out


def fantasycalc(lg):
    if not common.enabled("values.fantasycalc"):
        raise common.FeedDisabled("values.fantasycalc")
    p = league_params(lg)
    d = common.fetch_json(FC_URL % (p["numQbs"], p["numTeams"], p["ppr"]),
                          "fc-values-%d-%d-%s" % (p["numQbs"], p["numTeams"], p["ppr"]),
                          12 * 3600, feed="values.fantasycalc")
    out = parse_fantasycalc(d)
    common.ok("values.fantasycalc", rows=len(out), params=p)
    return out


def fp_ros(cache_only=True):
    """{sid: {rank_ecr, pos_rank, rank_std}} from FantasyPros ROS ECR."""
    if not common.enabled("fantasypros.ros"):
        raise common.FeedDisabled("fantasypros.ros")
    from sleeper_auction.sources import fantasypros
    if cache_only:
        with common.cache_only():
            d = fantasypros.fetch_ros()
    else:
        d = fantasypros.fetch_ros()
    cw = ids.crosswalk()
    out = {}
    for r in d["players"]:
        sid = ids.to_sleeper("fantasypros", r.get("fp_id"), r.get("name"), r.get("pos"),
                             r.get("team"), cw=cw)
        if sid:
            out[sid] = {k: r.get(k) for k in ("rank_ecr", "pos_rank", "rank_std")}
    common.ok("fantasypros.ros", rows=len(out))
    return out


def _sleeper_week(season, week, cache_only):
    url = ("https://api.sleeper.com/projections/nfl/%s/%s?season_type=regular&%s"
           "&order_by=pts_half_ppr" % (season, week, "&".join(
               "position[]=" + p for p in ("QB", "RB", "WR", "TE", "K", "DEF"))))
    key = "slp-wk-%s-%s" % (season, week)
    if cache_only:
        with common.cache_only():
            return common.fetch_json(url, key, 12 * 3600)
    return common.fetch_json(url, key, 12 * 3600, feed="values.ros")


def ros_points(season, from_week, sc=None, cache_only=True, season_proj=None):
    """{sid: {"pts", "weeks", "source"}} over from_week..LAST_FANTASY_WEEK.

    Sleeper's weekly projections summed (league scoring where the stat line
    is there), else FanDuel REMAINING, else season_proj * weeks / 17.
    """
    from sleeper_auction.feeds import scoring
    if not common.enabled("values.ros"):
        raise common.FeedDisabled("values.ros")
    weeks = list(range(int(from_week), LAST_FANTASY_WEEK + 1))
    out, got = {}, 0
    for w in weeks:
        try:
            rows = _sleeper_week(season, w, cache_only)
        except (common.CacheMiss, Exception):
            continue
        got += 1
        for r in rows:
            sid = str(r.get("player_id") or "")
            st = r.get("stats") or {}
            if not sid or st.get("pts_half_ppr") is None:
                continue
            pos = ((r.get("player") or {}).get("position"))
            pts = scoring.points(st, sc, pos) if sc and pos not in ("K", "DEF") and \
                any(k in st for k in ("rec", "rush_yd", "pass_yd")) else st["pts_half_ppr"]
            d = out.setdefault(sid, {"pts": 0.0, "weeks": 0, "source": "sleeper-weekly"})
            d["pts"] += pts
            d["weeks"] += 1
    if got == len(weeks) and out:
        for d in out.values():
            d["pts"] = round(d["pts"], 1)
        common.ok("values.ros", rows=len(out), source="sleeper-weekly", weeks=got)
        return out
    try:
        from sleeper_auction.feeds.projections import fanduel, join
        if cache_only:
            with common.cache_only():
                rows = fanduel.fetch_remaining()
        else:
            rows = fanduel.fetch_remaining()
        joined, _ = join(rows, source="fanduel")
        fd = {r["sid"]: {"pts": round(scoring.points(r["stats"], sc, r["pos"]), 1)
                         if sc else r["pts"], "weeks": len(weeks),
                         "source": "fanduel-remaining"} for r in joined}
        if fd:
            common.ok("values.ros", rows=len(fd), source="fanduel-remaining",
                      sleeper_weeks_cached=got)
            return fd
    except (common.CacheMiss, Exception):
        pass
    out = {sid: {"pts": round(p * len(weeks) / 17.0, 1), "weeks": len(weeks),
                 "source": "season-share"} for sid, p in (season_proj or {}).items() if p}
    common.note("values.ros", ok=bool(out), rows=len(out), source="season-share",
                error=None if out else "no ROS source cached yet")
    return out


def ros_dollars(values, pool):
    """Map a value dict {sid: value} onto the league's dollar curve by rank.

    Same idea as valuation._scale_to_pool: the i-th most valuable player gets
    the i-th largest auction `base` in the pool, so "quality" stays in the
    dollars the waiver model already uses.
    """
    curve = sorted((p.get("base") or 0 for p in pool), reverse=True)
    ranked = sorted(((v, s) for s, v in values.items() if v is not None), reverse=True)
    return {s: round(curve[i], 1) if i < len(curve) else 1.0
            for i, (_, s) in enumerate(ranked)}


def sos(season, from_week, dvp):
    """{team: {pos: {"avg_dvp_rank", "games"}}} over the remaining schedule.

    `dvp` is sitstart.def_vs_position()["teams"]: rank 1 = stingiest. A
    higher average means softer remaining opponents.
    """
    from sleeper_auction.feeds import nflverse
    sched = nflverse.schedule(season) or {}
    out = {}
    for w in range(int(from_week), LAST_FANTASY_WEEK + 1):
        for team, g in (sched.get(w) or {}).items():
            opp = (dvp or {}).get(g["opp"]) or {}
            for pos, v in opp.items():
                if v.get("rank") is None:
                    continue
                d = out.setdefault(team, {}).setdefault(pos, {"sum": 0.0, "games": 0})
                d["sum"] += v["rank"]
                d["games"] += 1
    return {t: {p: {"avg_dvp_rank": round(v["sum"] / v["games"], 1), "games": v["games"]}
                for p, v in ps.items() if v["games"]} for t, ps in out.items()}


def parse_ktc(text, superflex=False):
    from sleeper_auction.sources.fantasypros import _extract_js_literal
    import json
    raw = _extract_js_literal(text, "var superflexPlayers" if superflex else "var oneQBPlayers",
                              "[")
    if not raw:
        return []
    out = []
    for p in json.loads(raw):
        v = p.get("superflexValues" if superflex else "oneQBValues") or {}
        out.append({"ktc_id": str(p.get("playerID")), "name": p.get("playerName"),
                    "pos": p.get("position"), "team": p.get("team"), "value": v.get("value"),
                    "rank": v.get("rank"), "pos_rank": v.get("positionalRank"),
                    "trend": v.get("overallTrend")})
    return out


def ktc(lg):
    """KeepTradeCut redraft values. Opt-in (KTC_ENABLED=1); see module docstring."""
    if os.environ.get("KTC_ENABLED") != "1" or not common.enabled("values.ktc"):
        return {}
    txt = common.fetch_text(KTC_URL, "ktc-redraft", 86400, feed="values.ktc")
    rows = parse_ktc(txt, league_params(lg)["numQbs"] == 2)
    cw = ids.crosswalk()
    out = {}
    for r in rows:
        sid = ids.to_sleeper("ktc", r["ktc_id"], r["name"], r["pos"], r.get("team"), cw=cw)
        if sid:
            out[sid] = r
    common.ok("values.ktc", rows=len(out), embedded=len(rows))
    return out


def _pct_by_pos(vals, pos_of):
    by = {}
    for s, v in vals.items():
        if v is not None:
            by.setdefault(pos_of(s), []).append((v, s))
    out = {}
    for pos, lst in by.items():
        lst.sort()
        n = len(lst)
        for i, (_, s) in enumerate(lst):
            out[s] = i / (n - 1) if n > 1 else 0.5
    return out


def trade_values(lg, pool=None, fc=None, kt=None):
    """{sid: {"fc", "ktc", "consensus", "dollars", "disagree", "trend30"}}.

    The two sources use different scales, so raw values are never averaged:
    each becomes a percentile within position, the percentiles are averaged
    (FantasyCalc alone when KTC is off), and the consensus is mapped back to
    dollars by position rank on FantasyCalc's dollar curve.
    """
    fc = fc if fc is not None else fantasycalc(lg)
    kt = kt if kt is not None else ktc(lg)
    cw = ids.crosswalk()

    def pos_of(s):
        return (fc.get(s) or {}).get("pos") or (cw["info"].get(s) or {}).get("pos")
    pf = _pct_by_pos({s: v["value"] for s, v in fc.items()}, pos_of)
    pk = _pct_by_pos({s: v["value"] for s, v in kt.items()}, pos_of) if kt else {}
    dollars = ros_dollars({s: v["value"] for s, v in fc.items()}, pool) if pool else {}
    cons = {}
    for s in set(pf) | set(pk):
        xs = [x for x in (pf.get(s), pk.get(s)) if x is not None]
        cons[s] = statistics.mean(xs)
    # consensus rank within position -> the dollars FantasyCalc gives that rank
    by_pos = {}
    for s, c in cons.items():
        by_pos.setdefault(pos_of(s), []).append((c, s))
    fc_dollars_by_pos = {}
    for s, d in dollars.items():
        fc_dollars_by_pos.setdefault(pos_of(s), []).append(d)
    out = {}
    for pos, lst in by_pos.items():
        lst.sort(reverse=True)
        curve = sorted(fc_dollars_by_pos.get(pos, []), reverse=True)
        for i, (c, s) in enumerate(lst):
            out[s] = {"fc": (fc.get(s) or {}).get("value"), "ktc": (kt.get(s) or {}).get("value"),
                      "consensus": round(c, 3),
                      "dollars": curve[i] if i < len(curve) else None,
                      "disagree": (s in pf and s in pk and abs(pf[s] - pk[s]) >= DISAGREE),
                      "trend30": (fc.get(s) or {}).get("trend30"),
                      "pos_rank": (fc.get(s) or {}).get("pos_rank")}
    return out
