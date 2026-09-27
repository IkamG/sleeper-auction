"""ESPN weekly projections (Mike Clay). JSON, one request per position.

Probed 2026-09-27: lm-api-reads.fantasy.espn.com/.../leaguedefaults/3?view=
kona_player_info with the X-Fantasy-Filter below returned 150 WRs. The
weekly line is the stats[] entry with statSourceId 1, statSplitTypeId 1,
scoringPeriodId == week (Ja'Marr Chase wk3: appliedTotal 18.9 PPR). This host
wants a browser User-Agent (the opposite of site.api.espn.com).
"""
import json

from sleeper_auction.feeds import common
from sleeper_auction.feeds.projections import base

NAME = "espn"
LABEL = "ESPN (Mike Clay)"
PRODUCER = "espn"
URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/%s/segments/0/"
       "leaguedefaults/3?view=kona_player_info")
SLOTS = {"QB": (0, 42), "RB": (2, 100), "WR": (4, 150), "TE": (6, 60), "K": (17, 35),
         "DEF": (16, 32)}
STAT = {"0": "pass_att", "1": "pass_cmp", "3": "pass_yd", "4": "pass_td", "19": "pass_2pt",
        "20": "pass_int", "23": "rush_att", "24": "rush_yd", "25": "rush_td",
        "26": "rush_2pt", "53": "rec", "42": "rec_yd", "43": "rec_td", "44": "rec_2pt",
        "58": "rec_tgt", "72": "fum_lost"}


def _filter(season, week, slot, limit):
    return json.dumps({"players": {
        "filterSlotIds": {"value": [slot]},
        "filterStatsForSourceIds": {"value": [1]},
        "filterStatsForSplitTypeIds": {"value": [1]},
        "sortAppliedStatTotal": {"sortAsc": False, "sortPriority": 3,
                                 "value": "11%s%s" % (season, week)},
        "sortDraftRanks": {"sortPriority": 2, "sortAsc": True, "value": "PPR"},
        "limit": limit, "offset": 0,
        "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": [
            "00%s" % season, "10%s" % season, "11%s%s" % (season, week), "02%s" % season]}}},
        separators=(",", ":"))


def parse(d, week, pos):
    from sleeper_auction import board
    out = []
    for e in d.get("players") or []:
        p = e.get("player") or {}
        st = next((s for s in p.get("stats") or []
                   if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                   and int(s.get("scoringPeriodId") or 0) == int(week)), None)
        if not st:
            continue
        raw = st.get("stats") or {}
        stats = {k: float(raw[i]) for i, k in STAT.items() if i in raw}
        if "rec" not in stats and "41" in raw:
            stats["rec"] = float(raw["41"])
        name = p.get("fullName")
        tm = board.ESPN_TEAM.get(p.get("proTeamId"))
        if pos == "DEF":
            name = "%s D/ST" % tm
        out.append(base.row(name, pos, tm, stats, pts=st.get("appliedTotal"),
                            src_id=p.get("id") if pos != "DEF" else None,
                            id_kind="espn" if pos != "DEF" else None))
    return out


def fetch_week(season, week, ttl=3 * 3600):
    out = []
    for pos, (slot, limit) in SLOTS.items():
        d = common.fetch_json(URL % season, "proj-espn-%s-%s-%s" % (season, week, pos), ttl,
                              headers={"X-Fantasy-Filter": _filter(season, week, slot, limit),
                                       "Accept": "application/json"},
                              feed="projections.espn")
        out.extend(parse(d, week, pos))
    return out
