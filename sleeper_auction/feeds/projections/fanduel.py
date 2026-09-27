"""FanDuel Research / numberFire projections. GraphQL, current week only.

Probed 2026-09-27: POST fdresearch-api.fanduel.com/graphql, operationName
GetProjections, input {type: WEEKLY|REMAINING, position: NFL_SKILL|
NFL_KICKER|NFL_D_ST, sport: NFL}, header Origin: https://www.fanduel.com.
Returned the week-3 slate. TRAP: gameInfo.gameTime came back null, so the
week check compares each row's home/away teams against the schedule's
matchups for the requested week instead of the kickoff time.
completionsAttempts is a "22.11/33.49" string. numberFire ids are not in any
crosswalk, so rows join on name + team.
"""
from sleeper_auction.feeds import common
from sleeper_auction.feeds.projections import base

NAME = "fanduel"
LABEL = "FanDuel Research (numberFire)"
PRODUCER = "numberfire"
URL = "https://fdresearch-api.fanduel.com/graphql"
Q = ("query GetProjections($input: ProjectionsInput!) { getProjections(input: $input) { "
     "... on NflSkill { player { numberFireId name position } team { abbreviation } "
     "gameInfo { homeTeam { abbreviation } awayTeam { abbreviation } gameTime } "
     "completionsAttempts passingYards passingTouchdowns interceptionsThrown "
     "rushingAttempts rushingYards rushingTouchdowns receptions targets receivingYards "
     "receivingTouchdowns fantasy } } }")


def _call(kind, ttl):
    return common.post_json(URL, {"operationName": "GetProjections", "query": Q,
                                  "variables": {"input": {"type": kind, "position": "NFL_SKILL",
                                                          "sport": "NFL"}}},
                            "proj-fanduel-%s" % kind.lower(), ttl,
                            headers={"Origin": "https://www.fanduel.com",
                                     "Referer": "https://www.fanduel.com/"},
                            feed="projections.fanduel")


def parse(d, matchups=None):
    out = []
    for e in ((d.get("data") or {}).get("getProjections") or []):
        p, t, g = e.get("player") or {}, e.get("team") or {}, e.get("gameInfo") or {}
        tm = base.team(t.get("abbreviation"))
        if matchups is not None:
            h = base.team((g.get("homeTeam") or {}).get("abbreviation"))
            a = base.team((g.get("awayTeam") or {}).get("abbreviation"))
            opp = a if tm == h else h
            if matchups.get(tm) != opp:
                continue          # not this week's game
        ca = (e.get("completionsAttempts") or "").split("/")
        st = {"pass_cmp": base.num(ca[0]) if len(ca) == 2 else None,
              "pass_att": base.num(ca[1]) if len(ca) == 2 else None,
              "pass_yd": e.get("passingYards"), "pass_td": e.get("passingTouchdowns"),
              "pass_int": e.get("interceptionsThrown"), "rush_att": e.get("rushingAttempts"),
              "rush_yd": e.get("rushingYards"), "rush_td": e.get("rushingTouchdowns"),
              "rec": e.get("receptions"), "rec_tgt": e.get("targets"),
              "rec_yd": e.get("receivingYards"), "rec_td": e.get("receivingTouchdowns")}
        out.append(base.row(p.get("name"), p.get("position"), tm, st, pts=e.get("fantasy"),
                            src_id=p.get("numberFireId"), id_kind="numberfire"))
    return out


def fetch_week(season, week, ttl=3 * 3600):
    from sleeper_auction.feeds import nflverse
    sched = (nflverse.schedule(season) or {}).get(int(week)) or {}
    matchups = {t: g["opp"] for t, g in sched.items()} if sched else None
    return parse(_call("WEEKLY", ttl), matchups)


def fetch_remaining(ttl=12 * 3600):
    """Rest-of-season projections (numberFire REMAINING)."""
    return parse(_call("REMAINING", ttl))
