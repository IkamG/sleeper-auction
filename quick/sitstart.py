#!/usr/bin/env python3
"""Week-to-week sit/start analyzer.

A deterministic layer computes every number (implied team total, projection,
volatility, weather, injury, matchup). Claude then weighs the tradeoffs and
argues both sides. Claude is never asked to estimate a number it could be
handed -- it is asked to make the judgment call that the numbers do not settle.

    python3 quick/sitstart.py --draft <id> --me 5 --week 1
    python3 quick/sitstart.py --draft <id> --me 5 --week 1 --no-ai   # facts only
"""
import argparse
import json
import os
import statistics
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import draftboard as db  # noqa: E402

SEASON = "2026"
PRIOR_SEASON = "2025"
MODEL = "claude-opus-5"
API_URL = "https://api.anthropic.com/v1/messages"

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
            "proj": (proj.get(pid) or {}).get("proj"),
            "season_proj": base.get("proj"),
            "opponent": ln.get("opp"), "home": ln.get("home"),
            "game_total": ln.get("total"), "spread": ln.get("spread"),
            "implied_total": ln.get("implied"),
            "opp_implied": opp_line.get("implied"),
            "volatility": vol.get(pid),
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


def ai_analyze(slate, pos, api_key=None):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    client = _sdk()
    if not key and not client:
        return {"error": "no_api_key",
                "detail": "Set ANTHROPIC_API_KEY to enable the AI analysis. "
                          "Every number above is computed without it."}
    payload = {
        "model": MODEL,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high",
                          "format": {"type": "json_schema", "schema": SCHEMA}},
        # The rules never change week to week; cache them and pay only for the slate.
        "system": [{"type": "text", "text": SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": json.dumps({
            "week": slate["week"], "team": slate["team_name"],
            "my_projected_total": slate["my_projected"],
            "opponent_projected_total": slate["opp_projected"],
            "risk_posture": pos,
            "roster": [{k: v for k, v in p.items() if k != "id"}
                       for p in slate["players"]],
        }, default=str)}],
    }
    if client:                                   # official SDK path
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

    req = urllib.request.Request(          # stdlib fallback, same request shape
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


# ------------------------------------------------------------------ output

def report(slate, pos, ai=None):
    o = ["%s -- week %s sit/start" % (slate["team_name"], slate["week"]),
         "projected %s vs opponent %s  ->  posture: %s"
         % (slate["my_projected"], slate["opp_projected"], pos["mode"].upper()),
         "  " + pos["guidance"], ""]
    o.append("%-1s %-22s %-4s %-6s %-6s %-7s %-13s %s"
             % ("", "PLAYER", "POS", "PROJ", "IMPL", "VS", "FLOOR/CEIL", "NOTE"))
    o.append("-" * 92)
    for p in slate["players"]:
        v = p["volatility"] or {}
        note = []
        if p["injury"] and p["injury"].get("status") not in (None, "Active"):
            note.append(p["injury"]["status"])
        wx = p["weather"] or {}
        if not wx.get("dome") and (wx.get("wind_mph") or 0) >= 15:
            note.append("wind %smph" % wx["wind_mph"])
        if v.get("cv") and v["cv"] >= 0.65:
            note.append("volatile cv=%.2f" % v["cv"])
        o.append("%-1s %-22s %-4s %-6s %-6s %-7s %-13s %s" % (
            "*" if p["starting"] else "", (p["name"] or "?")[:22], p["pos"],
            p["proj"] if p["proj"] is not None else "-",
            p["implied_total"] if p["implied_total"] is not None else "-",
            ("%s%s" % ("@" if not p["home"] else "", p["opponent"] or "?")),
            "%s / %s" % (v.get("floor", "-"), v.get("ceiling", "-")),
            ", ".join(note)))
    o.append("")
    o.append("* = currently in your Sleeper starting lineup")
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


def html_report(slate, pos, ai=None):
    rows = []
    for p in slate["players"]:
        v = p["volatility"] or {}
        wx = p["weather"] or {}
        notes = []
        if p["injury"] and p["injury"].get("status") not in (None, "Active"):
            notes.append('<b class="mn">%s</b>' % p["injury"]["status"])
        if not wx.get("dome") and (wx.get("wind_mph") or 0) >= 15:
            notes.append("wind %smph" % wx["wind_mph"])
        if v.get("cv") and v["cv"] >= 0.65:
            notes.append('<span class="vol">volatile %.2f</span>' % v["cv"])
        rows.append(
            '<tr class="%s"><td>%s</td><td class="nm">%s</td>'
            '<td><span class="pos %s">%s</span></td><td class="big">%s</td>'
            '<td>%s</td><td class="mut">%s%s</td><td class="mut">%s</td>'
            '<td class="mut">%s / %s</td><td class="mut">%s</td></tr>' % (
                "st" if p["starting"] else "", "&#9733;" if p["starting"] else "",
                p["name"], p["pos"], p["pos"],
                p["proj"] if p["proj"] is not None else "&mdash;",
                p["implied_total"] if p["implied_total"] is not None else "&mdash;",
                "@" if not p["home"] else "", p["opponent"] or "?",
                p["spread"] if p["spread"] is not None else "&mdash;",
                v.get("floor", "&mdash;"), v.get("ceiling", "&mdash;"),
                " &middot; ".join(notes)))

    cards = ""
    if ai and not ai.get("error"):
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
        cards = ('<div class="panel"><h3>AI analysis</h3><div class="read">%s</div>'
                 '</div>%s<div class="panel"><h3>Lineup call</h3><div>%s</div>'
                 '%s</div>' % (
                     ai.get("posture_read", ""), cards, ai.get("lineup_call", ""),
                     '<div class="mut" style="margin-top:8px">Biggest risk: %s</div>'
                     % ai["biggest_risk"] if ai.get("biggest_risk") else ""))
    elif ai:
        cards = ('<div class="panel"><h3>AI analysis</h3><div class="mut">'
                 'Unavailable (%s). %s</div></div>'
                 % (ai["error"], ai["detail"][:300]))

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
.main{flex:1 1 620px;min-width:340px}.rail{flex:1 1 380px}
.panel{background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px;margin-bottom:12px}
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
@media(max-width:900px){.rail{flex:1 1 100%}}
</style></head><body>
<div class="nav"><a href="#" data-p="/">Draft board</a><a href="#" data-p="/analysis">Analysis</a>
<a href="#" data-p="/sitstart">Sit / Start</a><span class="navsp"></span>
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
  <th>Opp</th><th>Spread</th><th>Floor / Ceil</th><th>Flags</th></tr></thead>
  <tbody>__ROWS__</tbody></table></div>
  <div class="mut" style="font-size:12px">&#9733; = in your Sleeper starting lineup &middot;
  <b>Team total</b> is the Vegas implied points for that player's offense &middot;
  <b>Floor / Ceil</b> are 20th/80th percentile weekly half-PPR scores from last season</div>
 </div>
 <div class="rail">__AI__</div>
</div></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--me", type=int, required=True)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sl = build_slate(args.draft, args.me, args.week)
    ps = posture(sl)
    ai = None if args.no_ai else ai_analyze(sl, ps)
    if args.json:
        print(json.dumps({"slate": sl, "posture": ps, "ai": ai}, indent=1, default=str))
    else:
        print(report(sl, ps, ai))
