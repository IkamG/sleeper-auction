"""FFToday weekly projections. HTML, paginated, names only.

Probed 2026-09-27: www.fftoday.com/rankings/playerwkproj.php?Season=&GameWeek=
&PosID=&LeagueID=1&cur_page=N. Header is a group row (Passing / Rushing /
Receiving) over labels that repeat (Yard, TD), so columns are mapped by
(group, label). Weekly pages have no DST. Player links carry only an FFToday
id, which no crosswalk has, so rows join on name + team. Latin-1 pages.
"""
from sleeper_auction.feeds import common
from sleeper_auction.feeds.projections import base

NAME = "fftoday"
LABEL = "FFToday"
PRODUCER = "fftoday"
URL = ("https://www.fftoday.com/rankings/playerwkproj.php?Season=%s&GameWeek=%s&PosID=%d"
       "&LeagueID=1&cur_page=%d")
POS = {"QB": (10, 1), "RB": (20, 2), "WR": (30, 3), "TE": (40, 1), "K": (80, 1)}
MAP = {("passing", "comp"): "pass_cmp", ("passing", "cmp"): "pass_cmp",
       ("passing", "att"): "pass_att", ("passing", "yard"): "pass_yd",
       ("passing", "td"): "pass_td", ("passing", "int"): "pass_int",
       ("rushing", "att"): "rush_att", ("rushing", "yard"): "rush_yd",
       ("rushing", "td"): "rush_td", ("receiving", "rec"): "rec",
       ("receiving", "yard"): "rec_yd", ("receiving", "td"): "rec_td"}


def parse(text, pos):
    out = []
    for t in base.parse_tables(text):
        hi = next((i for i, r in enumerate(t)
                   if any(c["text"].lower().startswith("player") for c in r)
                   and any(c["text"].lower() == "team" for c in r)), None)
        if hi is None or hi == 0:
            continue
        hdr = base.grouped_header(t[hi - 1], t[hi])
        labels = [lab for _, lab in hdr]
        if "team" not in labels:
            continue
        for r in t[hi + 1:]:
            cells = []
            for c in r:
                cells.extend([c] * c["colspan"])
            if len(cells) < len(hdr) or not any("/stats/players/" in l for c in r
                                                 for l in c["links"]):
                continue
            rec = {"stats": {}, "pts": None, "name": None, "team": None}
            for (g, lab), c in zip(hdr, cells):
                if lab == "player":
                    rec["name"] = c["text"]
                elif lab == "team":
                    rec["team"] = c["text"]
                elif lab in ("fpts", "fantasy", "pts"):
                    rec["pts"] = base.num(c["text"])
                elif (g, lab) in MAP:
                    rec["stats"][MAP[(g, lab)]] = base.num(c["text"])
            if rec["name"]:
                out.append(base.row(rec["name"], pos, rec["team"], rec["stats"],
                                    pts=rec["pts"]))
    return out


def fetch_week(season, week, ttl=3 * 3600):
    out, first = [], True
    for pos, (pid, pages) in POS.items():
        for pg in range(pages):
            key = "proj-fftoday-%s-%s-%s-%d" % (season, week, pos, pg)
            age = common.cache_age(key)
            if not first and (age is None or age >= ttl):
                base.pause()
            first = False
            txt = common.fetch_text(URL % (season, week, pid, pg), key, ttl,
                                    feed="projections.fftoday")
            rows = parse(txt, pos)
            out.extend(rows)
            if not rows:
                break
    return out
