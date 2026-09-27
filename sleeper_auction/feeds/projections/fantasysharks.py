"""FantasySharks weekly projections. CSV, names only.

Probed 2026-09-27: .../projections.php?csv=1&Sort=&League=-1&Position=
{1 QB,2 RB,4 WR,5 TE,7 K,6 DEF}&scoring=1&Segment=S&uid=4. The 2026 week-N
segment is 882 + N (885 returned week 3's SEA @WAS). Names are "Last, First";
teams use SFO/GBP/KCC-style codes (base.TEAMS maps them). Receiving and
rushing columns are prefixed ("Rec Yds", "Rush Yds") on the positions probed;
repeated header names are still de-duplicated before mapping in case a
position page repeats one.
"""
import csv
import io

from sleeper_auction.feeds import common
from sleeper_auction.feeds.projections import base

NAME = "fantasysharks"
LABEL = "FantasySharks"
PRODUCER = "fantasysharks"
URL = ("https://www.fantasysharks.com/apps/bert/forecasts/projections.php?csv=1&Sort="
       "&League=-1&Position=%d&scoring=1&Segment=%d&uid=4")
SEGMENT_BASE = 882
POS = {"QB": 1, "RB": 2, "WR": 4, "TE": 5, "K": 7, "DEF": 6}
MAP = {"Att": "pass_att", "Comp": "pass_cmp", "Pass Yds": "pass_yd", "Pass TDs": "pass_td",
       "Int": "pass_int", "Rush": "rush_att", "Rush Yds": "rush_yd", "Rush TDs": "rush_td",
       "Tgt": "rec_tgt", "Rec": "rec", "Rec Yds": "rec_yd", "Rec TDs": "rec_td",
       "Fum Lost": "fum_lost"}


def parse(text, pos):
    rd = csv.reader(io.StringIO(text))
    try:
        hdr = next(rd)
    except StopIteration:
        return []
    seen, cols = {}, []
    for h in hdr:
        h = h.strip()
        seen[h] = seen.get(h, 0) + 1
        cols.append(h if seen[h] == 1 else "%s#%d" % (h, seen[h]))
    out = []
    for vals in rd:
        r = dict(zip(cols, vals))
        name = base.last_first(r.get("Player Name"))
        if not name:
            continue
        st = {MAP[c]: base.num(v) for c, v in r.items() if c in MAP}
        if pos == "DEF":
            out.append(base.row("%s D/ST" % base.team(r.get("Team")), "DEF", r.get("Team"),
                                {}, pts=base.num(r.get("Pts"))))
        else:
            out.append(base.row(name, pos, r.get("Team"), st, pts=base.num(r.get("Pts"))))
    return out


def fetch_week(season, week, ttl=3 * 3600):
    out, first = [], True
    for pos, code in POS.items():
        key = "proj-sharks-%s-%s-%s" % (season, week, pos)
        age = common.cache_age(key)
        if not first and (age is None or age >= ttl):
            base.pause()
        first = False
        out.extend(parse(common.fetch_text(URL % (code, SEGMENT_BASE + int(week)), key, ttl,
                                           feed="projections.fantasysharks"), pos))
    return out
