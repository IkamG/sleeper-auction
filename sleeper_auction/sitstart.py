#!/usr/bin/env python3
"""Week-to-week sit/start analyzer.

A deterministic layer computes every number (implied team total, projection,
volatility, weather, injury, matchup). Claude then weighs the tradeoffs and
argues both sides. Claude is never asked to estimate a number it could be
handed -- it is asked to make the judgment call that the numbers do not settle.

    python3 -m sleeper_auction.sitstart --draft <id> --me 5 --week 1
    python3 -m sleeper_auction.sitstart --draft <id> --me 5 --week 1 --no-ai   # facts only
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleeper_auction import board as db  # noqa: E402

SEASON = "2026"
PRIOR_SEASON = "2025"
MODEL = "claude-opus-5"
API_URL = "https://api.anthropic.com/v1/messages"
CACHE_WRCB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "wrcb")

# lat, lon, roof_is_closed. Weather is irrelevant indoors, so domes short-circuit.
STADIUM = {
    "ARI": (33.53, -112.26, True), "ATL": (33.76, -84.40, True),
    "BAL": (39.28, -76.62, False), "BUF": (42.77, -78.79, False),
    "CAR": (35.23, -80.85, False), "CHI": (41.86, -87.62, False),
    "CIN": (39.10, -84.52, False), "CLE": (41.51, -81.70, False),
    "DAL": (32.75, -97.09, True), "DEN": (39.74, -105.02, False),
    "DET": (42.34, -83.05, True), "GB": (44.50, -88.06, False),
    "HOU": (29.68, -95.41, True), "IND": (39.76, -86.16, True),
    "JAX": (30.32, -81.64, False), "KC": (39.05, -94.48, False),
    "LAC": (33.95, -118.34, True), "LAR": (33.95, -118.34, True),
    "LV": (36.09, -115.18, True), "MIA": (25.96, -80.24, False),
    "MIN": (44.97, -93.26, True), "NE": (42.09, -71.26, False),
    "NO": (29.95, -90.08, True), "NYG": (40.81, -74.07, False),
    "NYJ": (40.81, -74.07, False), "PHI": (39.90, -75.17, False),
    "PIT": (40.45, -80.02, False), "SEA": (47.60, -122.33, False),
    "SF": (37.40, -121.97, False), "TB": (27.98, -82.50, False),
    "TEN": (36.17, -86.77, False), "WAS": (38.91, -76.86, False),
}

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
# site.api.espn.com 403s on a browser User-Agent (the opposite of ESPN's
# fantasy host, which requires one). Send a plain client UA here.
ESPN_HDRS = {"User-Agent": "python-urllib/3.9"}


# ------------------------------------------------------------------ facts

def vegas(week):
    """Spread and over/under per team -> implied team total.

    The single most predictive free input for weekly fantasy output: it is the
    market's own forecast of how much offense each side will actually produce.
    """
    d = db.gj("%s/scoreboard?week=%s&seasontype=2&dates=%s" % (ESPN, week, SEASON),
              headers=ESPN_HDRS, key="espn-sb-%s-%s" % (SEASON, week), ttl=1800)
    out = {}
    for ev in d.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        odds = (comp.get("odds") or [{}])[0]
        total = odds.get("overUnder")
        teams = {}
        for c in comp.get("competitors", []):
            ab = db.nteam((c.get("team") or {}).get("abbreviation"))
            teams[c.get("homeAway")] = ab
        fav, spread = None, None
        det = odds.get("details")            # e.g. "SEA -3" or "EVEN"
        if det and " " in det:
            fav, sp = det.rsplit(" ", 1)
            try:
                spread = abs(float(sp))
                fav = db.nteam(fav.strip())
            except ValueError:
                spread = None
        for side, ab in teams.items():
            opp = teams.get("away" if side == "home" else "home")
            imp = None
            if total is not None and spread is not None and fav:
                half = float(total) / 2.0
                imp = round(half + spread / 2.0 if ab == fav else half - spread / 2.0, 2)
            elif total is not None:
                imp = round(float(total) / 2.0, 2)
            out[ab] = {"opp": opp, "home": side == "home", "total": total,
                       "spread": (-spread if ab == fav else spread) if spread else None,
                       "implied": imp, "kickoff": ev.get("date"),
                       "favored": ab == fav if fav else None}
    return out


def week_projections(week, season=SEASON):
    """Sleeper's native half-PPR weekly projections."""
    pos = "&".join("position[]=" + p for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
    d = db.gj("https://api.sleeper.com/projections/nfl/%s/%s?season_type=regular&%s"
              "&order_by=pts_half_ppr" % (season, week, pos),
              key="slp-wk-%s-%s" % (season, week), ttl=1800)
    out = {}
    for row in d:
        st, pl = row.get("stats") or {}, row.get("player") or {}
        pid = str(row.get("player_id") or pl.get("player_id") or "")
        if pid and st.get("pts_half_ppr") is not None:
            out[pid] = {"proj": st["pts_half_ppr"], "opp": st.get("opponent"),
                        "gp": st.get("gp")}
    return out


def volatility(season=PRIOR_SEASON, weeks=18):
    """Boom/bust profile from last season's actual weekly half-PPR scores.

    A projection is a mean. Two players with the same mean are not the same
    start: one floors at 8 every week, the other alternates 2 and 22. That
    difference is the entire sit/start question in a close matchup.
    """
    games = {}
    pos = "&".join("position[]=" + p for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
    for wk in range(1, weeks + 1):
        try:
            d = db.gj("https://api.sleeper.com/stats/nfl/%s/%s?season_type=regular&%s"
                      "&order_by=pts_half_ppr" % (season, wk, pos),
                      key="slp-stats-%s-%s" % (season, wk), ttl=86400 * 7)
        except Exception:
            continue
        for row in d:
            st = row.get("stats") or {}
            pid = str(row.get("player_id") or "")
            v = st.get("pts_half_ppr")
            if pid and v is not None:
                games.setdefault(pid, []).append(float(v))
    out = {}
    for pid, vals in games.items():
        if len(vals) < 4:
            continue
        mean = statistics.mean(vals)
        sd = statistics.pstdev(vals)
        out[pid] = {
            "n": len(vals), "mean": round(mean, 1), "sd": round(sd, 1),
            # Coefficient of variation is the comparable volatility measure --
            # a sd of 6 means something very different at a 6-point mean than
            # at an 18-point mean.
            "cv": round(sd / mean, 2) if mean > 1 else None,
            "boom": round(100.0 * sum(1 for v in vals if v >= mean * 1.5) / len(vals)),
            "bust": round(100.0 * sum(1 for v in vals if v <= mean * 0.5) / len(vals)),
            "floor": round(sorted(vals)[max(0, len(vals) // 5)], 1),      # ~20th pct
            "ceiling": round(sorted(vals)[min(len(vals) - 1, 4 * len(vals) // 5)], 1),
        }
    return out


def _weekly_stats(season, weeks=18):
    """Actual weekly half-PPR results, with the opponent each was scored against."""
    pos = "&".join("position[]=" + x for x in ("QB", "RB", "WR", "TE", "K", "DEF"))
    out = []
    for wk in range(1, weeks + 1):
        try:
            d = db.gj("https://api.sleeper.com/stats/nfl/%s/%s?season_type=regular&%s"
                      "&order_by=pts_half_ppr" % (season, wk, pos),
                      key="slp-stats-%s-%s" % (season, wk), ttl=86400 * 7)
        except Exception:
            continue
        out.extend(d)
    return out


def def_vs_position(season=None, min_weeks=3):
    """Fantasy points each defense allows, by position, computed from real results.

    Not scraped from anyone's rankings -- every weekly score is attributed to the
    defense it was scored against, so this is exactly what happened. Team-level
    implied totals cannot answer "is this a bad matchup for a WR specifically",
    and that is usually the actual sit/start question.

    Prefers the current season and falls back to the prior one when too few
    weeks have been played. The return value says which was used, because a
    defense-vs-position table from last season is a much weaker signal after
    an offseason of roster and scheme turnover.
    """
    for src in ([season] if season else [SEASON, PRIOR_SEASON]):
        rows = _weekly_stats(src)
        weeks = {r.get("week") for r in rows if r.get("week")}
        if len(weeks) < min_weeks and src != PRIOR_SEASON:
            continue
        allowed, games = {}, {}
        for r in rows:
            opp = db.nteam(r.get("opponent"))
            pos = db.npos(((r.get("player") or {}).get("fantasy_positions") or [None])[0])
            pts = (r.get("stats") or {}).get("pts_half_ppr")
            if not opp or pos not in ("QB", "RB", "WR", "TE", "K", "DEF") or pts is None:
                continue
            allowed.setdefault(opp, {}).setdefault(pos, 0.0)
            allowed[opp][pos] += float(pts)
            games.setdefault(opp, set()).add(r.get("week"))
        table = {}
        for team, byp in allowed.items():
            n = max(1, len(games.get(team, ())))
            table[team] = {pos: {"ppg": round(v / n, 1), "games": n}
                           for pos, v in byp.items()}
        # Rank 1 = stingiest. A rank means nothing without the scale, so the
        # points-per-game figure travels with it.
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            ranked = sorted((t for t in table if pos in table[t]),
                            key=lambda t: table[t][pos]["ppg"])
            for i, t in enumerate(ranked):
                table[t][pos]["rank"] = i + 1
                table[t][pos]["of"] = len(ranked)
        if table:
            return {"season": src, "teams": table, "weeks": len(weeks)}
    return {"season": None, "teams": {}, "weeks": 0}


def snap_trend(season=None):
    """Season and recent snap share per player, plus the delta between them.

    Snap share is the cleanest available read on whether a coaching staff is
    actually using someone. A rising share ahead of a soft matchup is the
    strongest start signal in this dataset; a falling one is the earliest
    warning that a projection is stale.
    """
    for src in ([season] if season else [SEASON, PRIOR_SEASON]):
        try:
            txt = db.get("https://github.com/nflverse/nflverse-data/releases/download"
                         "/snap_counts/snap_counts_%s.csv" % src,
                         key="snaps-%s" % src, ttl=86400)
        except Exception:
            continue
        import csv
        import io as _io
        by = {}
        for r in csv.DictReader(_io.StringIO(txt)):
            try:
                pct = float(r.get("offense_pct") or 0)
                wk = int(r.get("week") or 0)
            except ValueError:
                continue
            if not r.get("player") or pct <= 0:
                continue
            by.setdefault(db.nname(r["player"]), []).append(
                (wk, pct, db.nteam(r.get("team"))))
        out = {}
        for k, vals in by.items():
            vals.sort()
            pcts = [p for _, p, _ in vals]
            recent = pcts[-3:]
            season_avg = sum(pcts) / len(pcts)
            out[k] = {"games": len(pcts),
                      "season_pct": round(100 * season_avg),
                      "recent_pct": round(100 * sum(recent) / len(recent)),
                      "trend": round(100 * (sum(recent) / len(recent) - season_avg)),
                      "earned_on": vals[-1][2]}
        if out:
            return {"season": src, "players": out}
    return {"season": None, "players": {}}


def depth_charts():
    """Current depth-chart slot per player, from Sleeper's live player database.

    Historical snap share is backward-looking and, for anyone who changed teams,
    describes a role that no longer exists. Depth-chart order is the opposite:
    current team, current season. depth_chart_position also distinguishes slot
    (SWR) from outside (LWR/RWR), which is exactly the split that decides
    whether a WR/CB matchup applies to a given receiver.
    """
    try:
        d = db.gj("https://api.sleeper.app/v1/players/nfl",
                  key="slp-players", ttl=86400)
    except Exception:
        return {}
    out = {}
    for pid, p in d.items():
        if p.get("depth_chart_order") and p.get("team"):
            out[str(pid)] = {"order": p["depth_chart_order"],
                             "slot": p.get("depth_chart_position"),
                             "team": db.nteam(p.get("team"))}
    return out


def injuries():
    d = db.gj("%s/injuries" % ESPN, headers=ESPN_HDRS, key="espn-inj", ttl=1800)
    out = {}
    for team in d.get("injuries", []):
        for it in team.get("injuries", []):
            ath = it.get("athlete") or {}
            nm = ath.get("displayName")
            if not nm:
                continue
            status = it.get("status")
            det = it.get("details") or {}
            out[db.nname(nm)] = {
                "status": status, "type": det.get("type"),
                "detail": (it.get("shortComment") or "")[:220],
                "return": det.get("returnDate")}
    return out


def weather(team, kickoff=None):
    """Wind and precipitation at the home stadium. Domes short-circuit."""
    s = STADIUM.get(db.nteam(team))
    if not s:
        return None
    lat, lon, dome = s
    if dome:
        return {"dome": True}
    try:
        d = db.gj("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                  "&hourly=temperature_2m,wind_speed_10m,precipitation_probability"
                  "&forecast_days=7&temperature_unit=fahrenheit&wind_speed_unit=mph"
                  % (lat, lon), key="wx-%s" % team, ttl=10800)
    except Exception:
        return {"dome": False}
    h = d.get("hourly") or {}
    wind = [w for w in (h.get("wind_speed_10m") or []) if w is not None]
    temp = [t for t in (h.get("temperature_2m") or []) if t is not None]
    pcp = [p for p in (h.get("precipitation_probability") or []) if p is not None]
    return {"dome": False,
            "wind_mph": round(max(wind)) if wind else None,
            "temp_f": round(statistics.mean(temp)) if temp else None,
            "precip_pct": max(pcp) if pcp else None}


def league_matchup(league_id, roster_id, week):
    """Who you actually play this week, and their roster."""
    try:
        ms = db.gj("https://api.sleeper.app/v1/league/%s/matchups/%s" % (league_id, week),
                   ttl=600)
    except Exception:
        return None
    mine = next((m for m in ms if m.get("roster_id") == roster_id), None)
    if not mine:
        return None
    opp = next((m for m in ms if m.get("matchup_id") == mine.get("matchup_id")
                and m.get("roster_id") != roster_id), None)
    return {"matchup_id": mine.get("matchup_id"),
            "my_starters": mine.get("starters") or [],
            "opp_roster_id": opp.get("roster_id") if opp else None,
            "opp_players": (opp.get("players") or []) if opp else []}


# ------------------------------------------------------------------ slate

def _snaps_for(snaps, name, current_team):
    """Snap history, flagged when it was earned on a different team.

    A player who changed teams carries a snap share that describes a role he no
    longer has. Left in place because the raw usage is still informative, but
    marked so neither the UI nor the model reads it as current.
    """
    v = snaps["players"].get(db.nname(name or ""))
    if not v:
        return None
    v = dict(v)
    earned = v.get("earned_on")
    v["team_changed"] = bool(earned and current_team and earned != current_team)
    return v


def build_slate(draft_id, roster_id, week):
    """Assemble every fact about my roster for this week. No LLM involved."""
    pool = db.value_pool(db.build_pool()[0])
    byid = {p["sleeper_id"]: p for p in pool if p.get("sleeper_id")}
    st = db.draft_state(draft_id)
    mine = [pk for pk in st["picks"] if pk["roster_id"] == roster_id]
    if not mine:
        raise SystemExit("No players found for roster_id %s in draft %s"
                         % (roster_id, draft_id))

    lines = vegas(week)
    proj = week_projections(week)
    vol = volatility()
    inj = injuries()
    dvp = def_vs_position()
    snaps = snap_trend()
    depth = depth_charts()
    league_id = (st["draft"] or {}).get("league_id")
    mu = league_matchup(league_id, roster_id, week) if league_id else None

    players = []
    for pk in mine:
        pid = pk["player_id"]
        base = byid.get(pid) or {}
        team = db.nteam(pk["team"] or base.get("team"))
        ln = lines.get(team) or {}
        opp_line = lines.get(ln.get("opp")) or {}
        wx = weather(team if ln.get("home") else ln.get("opp"))
        players.append({
            "id": pid, "name": pk["name"] or base.get("name"),
            "pos": pk["pos"] or base.get("pos"), "team": team,
            "paid": pk["amount"], "season_value": base.get("base"),
            "season_tier": base.get("tier"),
            "value_conf": base.get("value_conf"),
            "proj_source": base.get("proj_source"),
            "proj": (proj.get(pid) or {}).get("proj"),
            "season_proj": base.get("proj"),
            "opponent": ln.get("opp"), "home": ln.get("home"),
            "game_total": ln.get("total"), "spread": ln.get("spread"),
            "implied_total": ln.get("implied"),
            "opp_implied": opp_line.get("implied"),
            "volatility": vol.get(pid),
            "dvp": ((dvp["teams"].get(ln.get("opp")) or {}).get(pk["pos"] or "")
                    if ln.get("opp") else None),
            "snaps": _snaps_for(snaps, pk["name"], team),
            "depth": depth.get(pid),
            "injury": inj.get(db.nname(pk["name"] or "")),
            "weather": wx,
            "starting": pid in (mu or {}).get("my_starters", []),
        })
    players.sort(key=lambda p: -(p["proj"] or 0))

    my_total = round(sum(p["proj"] or 0 for p in players if p["starting"]), 1)
    opp_total = None
    if mu and mu.get("opp_players"):
        opp_total = round(sum((proj.get(x) or {}).get("proj", 0)
                              for x in mu["opp_players"][:9]), 1)
    return {"week": week, "season": SEASON, "roster_id": roster_id,
            "players": players, "matchup": mu,
            "dvp_source": {"season": dvp["season"], "weeks": dvp["weeks"]},
            "snap_source": {"season": snaps["season"]},
            "my_projected": my_total, "opp_projected": opp_total,
            "league": st["league"], "team_name": next(
                (t["owner"] for t in st["teams"] if t["roster_id"] == roster_id), "me")}


def posture(slate):
    """How much variance you should want this week, derived from the matchup.

    Favourites win by not losing: take the floor. Underdogs cannot win a median
    game, so the boom/bust player is the correct play precisely because he is
    boom/bust. Same player, opposite call, depending on the opponent.
    """
    me, opp = slate.get("my_projected"), slate.get("opp_projected")
    if not me or not opp:
        return {"mode": "neutral", "margin": None,
                "guidance": "No opponent projection available -- play the highest "
                            "median projection and ignore variance."}
    m = round(me - opp, 1)
    if m >= 12:
        mode, g = "conservative", ("Heavy favourite by %.1f. Minimise variance: "
                                   "start the high-floor player even when his "
                                   "ceiling is lower. You lose this week only by "
                                   "a zero." % m)
    elif m >= 4:
        mode, g = "slightly conservative", ("Favoured by %.1f. Mild floor "
                                            "preference; do not chase ceiling." % m)
    elif m > -4:
        mode, g = "neutral", ("Within %.1f -- a coin flip. Take the best median "
                              "projection; variance barely matters." % abs(m))
    elif m > -12:
        mode, g = "slightly aggressive", ("Underdog by %.1f. Lean ceiling on close "
                                          "calls." % abs(m))
    else:
        mode, g = "aggressive", ("Underdog by %.1f. You cannot win a median game -- "
                                 "start the boom/bust player deliberately. A high "
                                 "floor is worthless here." % abs(m))
    return {"mode": mode, "margin": m, "guidance": g}


# ------------------------------------------------------------------ AI layer

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["posture_read", "players", "lineup_call"],
    "properties": {
        "posture_read": {"type": "string"},
        "players": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "verdict", "confidence", "deciding_factor",
                             "pros", "cons"],
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["START", "FLEX", "SIT", "MUST START"]},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "deciding_factor": {"type": "string"},
                    "pros": {"type": "array", "items": {"type": "string"}},
                    "cons": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "lineup_call": {"type": "string"},
        "biggest_risk": {"type": "string"},
    },
}

SYSTEM = """You are a fantasy football sit/start analyst for a 12-team, half-PPR \
Sleeper league. Roster: QB1, RB2, WR2, TE1, FLEX (RB/WR/TE), K1, DEF1, 4 bench.

Every number you need has already been computed and is given to you. Do not \
estimate, invent, or recompute any statistic, and do not use any player \
knowledge beyond what appears in the data -- if a fact is not in the payload, \
say it is unknown rather than filling it in.

Your job is the judgment the numbers do not settle: given a projection, a \
volatility profile, a Vegas implied team total, an injury note and the weather, \
argue honestly for BOTH sitting and starting each player, then commit to a call.

What actually matters:
- Implied team total is the market's forecast of how much offense exists to go \
around. A low total caps everyone in that game regardless of talent.
- Game script follows the spread. A heavy favourite runs the ball late, which \
helps a lead back and hurts a pass-catcher. A heavy underdog throws, which does \
the reverse.
- A projection is a mean. Volatility (cv, boom%, bust%, floor, ceiling) decides \
whether that mean is reliable or a coin flip, and that is what separates two \
players with the same projection.
- The risk posture is not a preference, it is arithmetic. Big favourite: take \
floor, a zero is the only way to lose. Big underdog: take ceiling deliberately, \
because a median week loses anyway. Say so plainly when it flips a call.
- Weather matters mainly through wind above roughly 15 mph, which suppresses \
passing and kicking. Domes are irrelevant.
- "dvp" is how many half-PPR points that opponent allows per game to this \
player's position, with a rank where 1 is the stingiest of 32. A team total \
cannot tell you a defense is fine against RBs and porous against WRs; this can, \
and that is usually the real question. Weigh it against the sample: it is \
computed from completed games, and the payload states which season it came \
from. A table from last season is a weak signal after an offseason of roster \
and scheme turnover -- say so instead of leaning on it hard.
- A WR/CB matchup chart may be supplied as an image. Its weekly matchup score \
combines a receiver's target and yards per route run against the specific \
cornerback projected to cover him -- a genuinely different signal from \
defense-vs-position, which averages over a whole unit. A strongly negative \
score against a shadow corner can outweigh a soft team ranking. Use it only \
for receivers actually listed; the charts are partial.
- "value_conf" is 0-1 confidence in that player's season valuation, from how \
many sources covered him and how much they disagreed. A low number means the \
market itself has not settled on him -- treat his season value as soft.
- "proj_source" of "curve" means no source published a projection for him and \
the number was inferred from a points-vs-rank curve. Say so rather than \
presenting an inferred figure as a real projection.
- "depth" is the CURRENT depth-chart order (1 = first on the chart) and slot on \
the player's present team, so unlike snap history it is never stale. \
depth_chart_position distinguishes slot (SWR) from outside (LWR/RWR); a WR/CB \
chart entry applies to an outside receiver far more cleanly than to a slot one.
- "snaps" carries "team_changed": when true the player has since moved, so that \
usage describes a role on a different roster. Say so and lean on depth and \
projection instead; do not present it as current.
- "snaps" is season snap share, recent (last 3 games) share, and the trend \
between them. A rising share is the strongest start signal in this data; a \
falling one is the earliest sign a projection is stale. Snap share for a \
committee back matters more than his projection.

Be decisive and brief. Two to four pros and cons each, one line apiece. Name the \
single deciding factor. Where the data is thin or a projection is missing, say \
so rather than guessing -- an honest "unknown" is worth more than a confident \
fabrication."""


def _sdk():
    """The official SDK when it is importable, else None.

    The app's defining property is that it runs on the macOS system Python with
    no installs, and that Python (3.9) cannot host the current SDK. So the SDK
    is an upgrade, never a requirement: run under .venv/bin/python and this
    returns a client; run under /usr/bin/python3 and it returns None and the
    raw-HTTP path below carries the same request.
    """
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic()
    except Exception:
        return None


def _extract(content_blocks):
    for blk in content_blocks:
        btype = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
        if btype == "text":
            txt = blk.get("text") if isinstance(blk, dict) else blk.text
            try:
                return json.loads(txt)
            except ValueError:
                return {"error": "unparseable", "detail": txt[:600]}
    return None


_WP_SIZE = re.compile(r"-\d+x\d+(?=\.(?:png|jpe?g)$)", re.I)
_IMG_RE = re.compile(r"https://[^\"' >]+/wp-content/uploads/[^\"' >]+\.(?:png|jpe?g)", re.I)


def wrcb_from_article(url):
    """Pull every chart image off a WR/CB matchups article.

    WordPress emits a family of resized copies per upload (-300x167, -1220x677,
    ...). Strip the size suffix to collapse them and keep the full-size
    original, then keep only images actually shaped like a data table -- the
    page is mostly ads and thumbnails.
    """
    try:
        html = db.get(url, headers={"User-Agent": db.UA},
                      key="wrcb-art-" + re.sub(r"\W+", "-", url)[-60:], ttl=3600)
    except Exception as e:
        print("wrcb article fetch failed: %s" % e, file=sys.stderr)
        return []
    originals = {}
    for u in _IMG_RE.findall(html):
        originals.setdefault(_WP_SIZE.sub("", u), set()).add(u)
    out = []
    for base in sorted(originals):
        out.append(base if base in originals[base] else sorted(originals[base])[-1])
    return out


def _is_chart(path, min_w=900, min_h=180):
    """Keep images shaped like a table. PNG/JPEG header read, no dependencies."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                return w >= min_w and h >= min_h
            if head[:2] == b"\xff\xd8":          # JPEG: walk the segments
                fh.seek(2)
                while True:
                    b = fh.read(1)
                    if not b:
                        return False
                    if b != b"\xff":
                        continue
                    m = fh.read(1)
                    if m in (b"\xc0", b"\xc1", b"\xc2"):
                        fh.read(3)
                        h = int.from_bytes(fh.read(2), "big")
                        w = int.from_bytes(fh.read(2), "big")
                        return w >= min_w and h >= min_h
                    ln = int.from_bytes(fh.read(2), "big")
                    fh.seek(ln - 2, 1)
    except Exception:
        return False
    return False


def fetch_wrcb(sources):
    """Download WR/CB matchup chart images for Claude to read directly.

    RotoBaller publishes these as screenshots, and the underlying tool is
    paywalled, so there is nothing to scrape and nothing worth scraping. Handing
    the image to a model that can read it is both simpler and the only approach
    that respects how the data is actually published. Accepts URLs or local
    paths; returns local file paths.
    """
    expanded = []
    for src in sources or []:
        # An article URL fans out into every chart image on the page.
        if not os.path.exists(src) and "/wp-content/uploads/" not in src \
                and src.startswith("http"):
            expanded.extend(wrcb_from_article(src))
        else:
            expanded.append(src)
    out = []
    for i, src in enumerate(expanded):
        if os.path.exists(src):
            out.append(os.path.abspath(src))
            continue
        try:
            ext = ".jpg" if src.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg") else ".png"
            dest = os.path.join(CACHE_WRCB, "wrcb-%d%s" % (i, ext))
            os.makedirs(CACHE_WRCB, exist_ok=True)
            req = urllib.request.Request(src, headers={"User-Agent": db.UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            if _is_chart(dest):
                out.append(dest)
            else:
                os.remove(dest)
        except Exception as e:
            print("wrcb fetch failed for %s: %s" % (src, e), file=sys.stderr)
    if out:
        print("wrcb: %d chart image(s)" % len(out), file=sys.stderr)
    return out


def _via_cli(system, user_json, images=None, timeout=900, schema=None):
    """Run the analysis through the local Claude Code CLI in headless mode.

    This uses the Claude Code subscription already installed on this machine
    rather than a separate API credential, so the analyzer costs nothing extra
    to run. Requires no ANTHROPIC_API_KEY. Structured output is not available
    on this path, so the schema is described in the prompt and the reply is
    parsed leniently.
    """
    img = ""
    if images:
        img = ("\n\nWR/CB matchup charts are at these local image paths. Read "
               "each one and use the per-receiver rows for any of my receivers "
               "that appear. The key column is the weekly matchup score, where "
               "positive favours the receiver. These tables are partial -- if "
               "one of my receivers is not shown, say his matchup is unknown "
               "rather than guessing:\n" + "\n".join(images))
    prompt = (system + "\n\nHere is this week's data:\n" + user_json + img +
              "\n\nRespond with ONLY a JSON object matching this shape, no "
              "markdown fence and no prose around it:\n" +
              json.dumps(schema or SCHEMA, indent=1))
    cmd = ["claude", "-p", "--output-format", "json"]
    if images:
        cmd += ["--allowed-tools", "Read"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "cli_timeout", "detail": "claude -p exceeded %ss" % timeout}
    except Exception as e:
        return {"error": "cli_failed", "detail": str(e)}
    if r.returncode != 0:
        return {"error": "cli_error", "detail": (r.stderr or r.stdout)[:600]}
    try:
        body = json.loads(r.stdout)
    except ValueError:
        return {"error": "cli_unparseable", "detail": r.stdout[:600]}
    if body.get("is_error"):
        return {"error": "cli_error", "detail": str(body.get("result"))[:600]}
    txt = (body.get("result") or "").strip()
    if txt.startswith("```"):                       # strip a stray code fence
        txt = txt.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        out = json.loads(txt)
    except ValueError:
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j < 0:
            return {"error": "cli_unparseable", "detail": txt[:600]}
        try:
            out = json.loads(txt[i:j + 1])
        except ValueError:
            return {"error": "cli_unparseable", "detail": txt[:600]}
    out["_via"] = "claude-code-cli"
    out["_cost_usd"] = body.get("total_cost_usd")
    return out


def run_model(system, user_json, schema, api_key=None, images=None):
    """Send one structured request to Claude, whichever backend is available.

    Backend priority: an explicit API key (official SDK, else raw HTTP), then
    the local Claude Code CLI on the user's existing subscription. Shared by
    every feature that needs the model, so they cannot drift apart.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    client = _sdk() if key else None
    if not key and shutil.which("claude"):
        return _via_cli(system, user_json, images, schema=schema)
    if not key and not client:
        return {"error": "no_api_key",
                "detail": "Set ANTHROPIC_API_KEY, or install Claude Code to use "
                          "your existing subscription. Every number above is "
                          "computed without either."}
    payload = {
        "model": MODEL, "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high",
                          "format": {"type": "json_schema", "schema": schema}},
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_json}],
    }
    if client:
        try:
            msg = client.messages.create(**payload)
        except Exception as e:
            return {"error": "sdk_error", "detail": "%s: %s" % (type(e).__name__, e)}
        if getattr(msg, "stop_reason", None) == "refusal":
            return {"error": "refusal", "detail": str(getattr(msg, "stop_details", None))}
        out = _extract(msg.content)
        if out is None:
            return {"error": "empty_response", "detail": str(msg.stop_reason)}
        out["_via"] = "sdk"
        return out
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": "api_error", "detail": e.read().decode()[:600]}
    except Exception as e:
        return {"error": "request_failed", "detail": str(e)}
    if body.get("stop_reason") == "refusal":
        return {"error": "refusal", "detail": str(body.get("stop_details"))}
    out = _extract(body.get("content", []))
    if out is None:
        return {"error": "empty_response", "detail": str(body.get("stop_reason"))}
    out["_via"] = "urllib"
    return out


def ai_analyze(slate, pos, api_key=None, wrcb=None):
    user_json = json.dumps({
        "week": slate["week"], "team": slate["team_name"],
        "my_projected_total": slate["my_projected"],
        "opponent_projected_total": slate["opp_projected"],
        "risk_posture": pos,
        "dvp_from_season": slate.get("dvp_source", {}).get("season"),
        "snaps_from_season": slate.get("snap_source", {}).get("season"),
        "roster": [{k: v for k, v in p.items() if k != "id"}
                   for p in slate["players"]],
    }, default=str)
    return run_model(SYSTEM, user_json, SCHEMA, api_key=api_key,
                     images=fetch_wrcb(wrcb))


# ------------------------------------------------------------------ output

def report(slate, pos, ai=None):
    o = ["%s -- week %s sit/start" % (slate["team_name"], slate["week"]),
         "projected %s vs opponent %s  ->  posture: %s"
         % (slate["my_projected"], slate["opp_projected"], pos["mode"].upper()),
         "  " + pos["guidance"], ""]
    o.append("%-1s %-22s %-4s %-6s %-6s %-7s %-13s %-9s %-8s %s"
             % ("", "PLAYER", "POS", "PROJ", "IMPL", "VS", "FLOOR/CEIL",
                "DvP", "SNAP%", "NOTE"))
    o.append("-" * 112)
    for p in slate["players"]:
        v = p["volatility"] or {}
        dv = p.get("dvp") or {}
        sn = p.get("snaps") or {}
        note = []
        if p["injury"] and p["injury"].get("status") not in (None, "Active"):
            note.append(p["injury"]["status"])
        wx = p["weather"] or {}
        if not wx.get("dome") and (wx.get("wind_mph") or 0) >= 15:
            note.append("wind %smph" % wx["wind_mph"])
        if v.get("cv") and v["cv"] >= 0.65:
            note.append("volatile cv=%.2f" % v["cv"])
        if sn.get("team_changed"):
            note.append("snaps were on %s" % sn.get("earned_on"))
        elif sn.get("trend") and abs(sn["trend"]) >= 8:
            note.append("snaps %+d%%" % sn["trend"])
        dp = p.get("depth") or {}
        if dp.get("order"):
            note.append("depth %s%s" % (dp["order"],
                                        "/" + dp["slot"] if dp.get("slot") else ""))
        o.append("%-1s %-22s %-4s %-6s %-6s %-7s %-13s %-9s %-8s %s" % (
            "*" if p["starting"] else "", (p["name"] or "?")[:22], p["pos"],
            p["proj"] if p["proj"] is not None else "-",
            p["implied_total"] if p["implied_total"] is not None else "-",
            ("%s%s" % ("@" if not p["home"] else "", p["opponent"] or "?")),
            "%s / %s" % (v.get("floor", "-"), v.get("ceiling", "-")),
            ("#%s %s" % (dv["rank"], dv["ppg"]) if dv.get("rank") else "-"),
            ("%s%%" % sn["recent_pct"] if sn.get("recent_pct") else "-"),
            ", ".join(note)))
    o.append("")
    o.append("* = in your Sleeper starting lineup | DvP = opponent rank (1=toughest)"
             " and pts/gm allowed to this position, from %s | SNAP%% = last 3 games, %s"
             % (slate.get("dvp_source", {}).get("season") or "n/a",
                slate.get("snap_source", {}).get("season") or "n/a"))
    if ai and not ai.get("error"):
        o.append("")
        o.append("=" * 92)
        o.append("AI ANALYSIS")
        o.append("  " + ai.get("posture_read", ""))
        for p in ai.get("players", []):
            o.append("")
            o.append("  %s -- %s (%s confidence)"
                     % (p["name"], p["verdict"], p["confidence"]))
            o.append("    deciding: %s" % p["deciding_factor"])
            for x in p.get("pros", []):
                o.append("    +  %s" % x)
            for x in p.get("cons", []):
                o.append("    -  %s" % x)
        o.append("")
        o.append("  LINEUP: %s" % ai.get("lineup_call", ""))
        if ai.get("biggest_risk"):
            o.append("  RISK  : %s" % ai["biggest_risk"])
    elif ai:
        o.append("")
        o.append("AI analysis unavailable (%s): %s" % (ai["error"], ai["detail"][:200]))
    return "\n".join(o)


def ai_cards(ai):
    """Render the AI verdicts. Shared by the initial page and the poll endpoint,
    so a streamed-in analysis looks identical to one that was ready up front."""
    if not ai:
        return ""
    if ai.get("error"):
        return ('<div class="panel"><h3>AI analysis</h3><div class="mut">'
                'Unavailable (%s). %s</div></div>'
                % (ai["error"], str(ai.get("detail", ""))[:300]))
    cards = ""
    for p in ai.get("players", []):
        cls = {"MUST START": "v-must", "START": "v-start",
               "FLEX": "v-flex", "SIT": "v-sit"}.get(p["verdict"], "v-flex")
        cards += (
            '<div class="card"><div class="ch"><span class="nm">%s</span>'
            '<span class="verdict %s">%s</span><span class="mut">%s confidence'
            '</span></div><div class="dec">%s</div><div class="pc">'
            '<div><h4 class="pl">Start because</h4>%s</div>'
            '<div><h4 class="mn">Sit because</h4>%s</div></div></div>' % (
                p["name"], cls, p["verdict"], p["confidence"],
                p["deciding_factor"],
                "".join("<div>+ %s</div>" % x for x in p.get("pros", [])),
                "".join("<div>&minus; %s</div>" % x for x in p.get("cons", []))))
    return ('<div class="panel"><h3>AI analysis</h3><div class="read">%s</div>'
            '</div>%s<div class="panel"><h3>Lineup call</h3><div>%s</div>%s</div>'
            % (ai.get("posture_read", ""), cards, ai.get("lineup_call", ""),
               '<div class="mut" style="margin-top:8px">Biggest risk: %s</div>'
               % ai["biggest_risk"] if ai.get("biggest_risk") else ""))


PENDING_PANEL = """<div class="panel" id="ai-pending"><h3>AI analysis</h3>
<div class="mut"><span class="spin"></span> Analysing the slate&hellip;
<span id="ai-elapsed">0s</span><br><span style="font-size:12px">The numbers above
are final. This usually takes one to three minutes.</span></div></div>"""

POLL_JS = """<script>(function(){
var id=%s; if(!id) return;
var box=document.getElementById('ai-slot'), t0=Date.now(), tries=0;
var el=document.getElementById('ai-elapsed');
var tick=setInterval(function(){if(el)el.textContent=Math.round((Date.now()-t0)/1000)+'s';},1000);
function poll(){
  fetch('/api/ai/'+id).then(function(r){return r.json()}).then(function(j){
    if(j.status==='pending'){ tries++; setTimeout(poll, tries>40?10000:3000); return; }
    clearInterval(tick);
    box.innerHTML = j.html || '<div class="panel"><h3>AI analysis</h3><div class="mut">No result.</div></div>';
  }).catch(function(){ tries++; if(tries<60) setTimeout(poll,5000); else clearInterval(tick); });
}
setTimeout(poll,2000);
})();</script>"""


def html_report(slate, pos, ai=None, job_id=None):

    rows = []
    for p in slate["players"]:
        v = p["volatility"] or {}
        wx = p["weather"] or {}
        dv = p.get("dvp") or {}
        sn = p.get("snaps") or {}
        notes = []
        if p["injury"] and p["injury"].get("status") not in (None, "Active"):
            notes.append('<b class="mn">%s</b>' % p["injury"]["status"])
        if not wx.get("dome") and (wx.get("wind_mph") or 0) >= 15:
            notes.append("wind %smph" % wx["wind_mph"])
        if v.get("cv") and v["cv"] >= 0.65:
            notes.append('<span class="vol">volatile %.2f</span>' % v["cv"])
        if sn.get("team_changed"):
            notes.append('<b class="vol">snaps were on %s</b>' % sn.get("earned_on"))
        elif sn.get("trend") and abs(sn["trend"]) >= 8:
            notes.append('<b class="%s">snaps %+d%%</b>'
                         % ("pl" if sn["trend"] > 0 else "mn", sn["trend"]))
        dp = p.get("depth") or {}
        if dp.get("order"):
            notes.append("depth %s%s" % (dp["order"],
                                         "/" + dp["slot"] if dp.get("slot") else ""))
        rows.append(
            '<tr class="%s"><td>%s</td><td class="nm">%s</td>'
            '<td><span class="pos %s">%s</span></td><td class="big">%s</td>'
            '<td>%s</td><td class="mut">%s%s</td><td class="mut">%s</td>'
            '<td class="mut">%s / %s</td><td class="%s">%s</td>'
            '<td class="mut">%s</td><td class="mut">%s</td></tr>' % (
                "st" if p["starting"] else "", "&#9733;" if p["starting"] else "",
                p["name"], p["pos"], p["pos"],
                p["proj"] if p["proj"] is not None else "&mdash;",
                p["implied_total"] if p["implied_total"] is not None else "&mdash;",
                "@" if not p["home"] else "", p["opponent"] or "?",
                p["spread"] if p["spread"] is not None else "&mdash;",
                v.get("floor", "&mdash;"), v.get("ceiling", "&mdash;"),
                ("pl" if (dv.get("rank") or 99) >= 22 else
                 "mn" if (dv.get("rank") or 0) <= 10 else "mut"),
                ("#%s &middot; %s" % (dv["rank"], dv["ppg"])
                 if dv.get("rank") else "&mdash;"),
                ("%s%%" % sn["recent_pct"] if sn.get("recent_pct") else "&mdash;"),
                " &middot; ".join(notes)))

    cards = ai_cards(ai)

    if job_id and not cards:
        cards = PENDING_PANEL + (POLL_JS % json.dumps(job_id))
    return SS_TPL.replace("__ROWS__", "".join(rows)).replace("__AI__", cards) \
        .replace("__TEAM__", str(slate["team_name"])).replace("__WK__", str(slate["week"])) \
        .replace("__MODE__", pos["mode"].upper()).replace("__GUIDE__", pos["guidance"]) \
        .replace("__MINE__", str(slate["my_projected"])) \
        .replace("__OPP__", str(slate["opp_projected"]))


SS_TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sit / Start</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.navsp{flex:1}.navmut{color:#8b949e;font-size:12px}
.hd{padding:16px 20px;background:#161b22;border-bottom:1px solid #30363d}
.hd h1{margin:0;font-size:20px}
.post{margin-top:9px;padding:10px 13px;border-radius:8px;background:#11161d;border:1px solid #30363d}
.post b{font-size:15px}
.wrap{padding:16px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.main{flex:1 1 620px;min-width:0;max-width:100%}.rail{flex:1 1 380px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:12px}
.panel h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:7px;border-bottom:1px solid #30363d;white-space:nowrap}
th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}
td{padding:7px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}
tr.st td{background:#111b26}
.nm{font-weight:600;color:#fff}.big{font-size:16px;font-weight:700;color:#58a6ff}
.mut{color:#8b949e}.pl{color:#3fb950}.mn{color:#f85149}
.vol{color:#e3b341}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.K{background:#30363d;color:#8b949e}.DEF{background:#30363d;color:#8b949e}
.card{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:10px}
.ch{display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.verdict{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;border:1px solid}
.v-must{background:#12331d;color:#3fb950;border-color:#238636}
.v-start{background:#0d3050;color:#79c0ff;border-color:#1f6feb}
.v-flex{background:#4a3312;color:#e3b341;border-color:#9e6a03}
.v-sit{background:#3a1518;color:#f85149;border-color:#7d2427}
.dec{font-size:13px;color:#c9d1d9;margin-bottom:8px}
.pc{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}
.pc>div{flex:1 1 240px}.pc h4{margin:0 0 4px;font-size:11px;text-transform:uppercase}
.pc div div{padding:2px 0;color:#c9d1d9}
.read{font-size:13px;color:#c9d1d9}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:-1px;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:900px){.rail{flex:1 1 100%}}
</style></head><body>
<div class="nav"><a href="#" data-p="/">Draft board</a><a href="#" data-p="/analysis">Analysis</a>
<a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a><span class="navsp"></span>
<span class="navmut">week __WK__</span></div>
<script>(function(){var qs=location.search||'';
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
 if(location.pathname===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__TEAM__ &mdash; week __WK__ sit / start</h1>
<div class="post">You <b>__MINE__</b> vs opponent <b>__OPP__</b> &nbsp;&rarr;&nbsp;
posture <b>__MODE__</b><div class="mut" style="margin-top:4px">__GUIDE__</div></div></div>
<div class="wrap">
 <div class="main"><div class="panel" style="padding:0"><table>
  <thead><tr><th></th><th>Player</th><th>Pos</th><th>Proj</th><th>Team total</th>
  <th>Opp</th><th>Spread</th><th>Floor / Ceil</th><th>DvP</th><th>Snap%</th>
  <th>Flags</th></tr></thead>
  <tbody>__ROWS__</tbody></table></div>
  <div class="mut" style="font-size:12px">&#9733; = in your Sleeper starting lineup &middot;
  <b>Team total</b> is the Vegas implied points for that player's offense &middot;
  <b>Floor / Ceil</b> are 20th/80th percentile weekly half-PPR scores &middot;
  <b>DvP</b> is the opponent's rank (1 = toughest of 32) and half-PPR points per game
  allowed to this position, computed from completed games &middot;
  <b>Snap%</b> is last-3-game snap share; green/red flags a shift of 8+ points</div>
 </div>
 <div class="rail" id="ai-slot">__AI__</div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--me", type=int, required=True)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--wrcb", action="append", default=[],
                    help="URL or path to a WR/CB matchup chart image (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sl = build_slate(args.draft, args.me, args.week)
    ps = posture(sl)
    ai = None if args.no_ai else ai_analyze(sl, ps, wrcb=args.wrcb)
    if args.json:
        print(json.dumps({"slate": sl, "posture": ps, "ai": ai}, indent=1, default=str))
    else:
        print(report(sl, ps, ai))
