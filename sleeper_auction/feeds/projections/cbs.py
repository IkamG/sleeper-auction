"""CBS Sports weekly projections. HTML table per position.

Probed 2026-09-27: www.cbssports.com/fantasy/football/stats/{POS}/{season}/
{week}/projections/ppr/ returns a TableBase table with a colspan group row
(Passing / Rushing / Receiving / Misc) over short labels (att, yds, td, ...).
Columns are mapped by (group, label), never by position. The player cell
carries a short and a long name span; the long one is used, and the CBS id
is in the /nfl/players/<id>/ link (crosswalked via DynastyProcess cbs_id).
Stat lines are identical under the ppr and nonppr slugs.
"""
import re

from sleeper_auction.feeds import common
from sleeper_auction.feeds.projections import base

NAME = "cbs"
LABEL = "CBS Sports"
PRODUCER = "cbs"
URL = "https://www.cbssports.com/fantasy/football/stats/%s/%s/%s/projections/ppr/"
POS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DEF": "DST"}
MAP = {("passing", "att"): "pass_att", ("passing", "cmp"): "pass_cmp",
       ("passing", "yds"): "pass_yd", ("passing", "td"): "pass_td",
       ("passing", "int"): "pass_int", ("rushing", "att"): "rush_att",
       ("rushing", "yds"): "rush_yd", ("rushing", "td"): "rush_td",
       ("receiving", "tgt"): "rec_tgt", ("receiving", "rec"): "rec",
       ("receiving", "yds"): "rec_yd", ("receiving", "td"): "rec_td",
       ("misc", "fl"): "fum_lost", ("misc", "fpts"): "pts", ("", "fpts"): "pts"}


def parse(text, pos):
    out = []
    for t in base.parse_tables(text):
        if len(t) < 3 or not any(c["text"].lower() == "player" for c in t[1]):
            continue
        hdr = base.grouped_header(t[0], t[1])
        for r in t[2:]:
            if not r or not r[0]["links"]:
                continue
            raw = r[0].get("raw") or ""
            m = re.search(r"\x1fCellPlayerName--long\x1e(.*?)\x1fCellPlayerName-position\x1e"
                          r"(.*?)\x1fCellPlayerName-team\x1e(.*)", raw, re.S)
            if m:
                name = re.sub(r"\x1f[^\x1e]*\x1e", " ", m.group(1))
                team = re.sub(r"\x1f[^\x1e]*\x1e", " ", m.group(3))
            else:
                name, team = r[0]["text"], None
            name = re.sub(r"\s+", " ", name).strip()
            team = re.sub(r"\s+", " ", team or "").strip().split(" ")[0] or None
            cid = re.search(r"/players/(\d+)/", r[0]["links"][0])
            stats, pts = {}, None
            for (g, lab), c in zip(hdr, r):
                k = MAP.get((g, lab))
                if k == "pts":
                    pts = base.num(c["text"])
                elif k:
                    stats[k] = base.num(c["text"])
            if pos == "DEF":
                out.append(base.row("%s D/ST" % team, "DEF", team, {}, pts=pts))
            else:
                out.append(base.row(name, pos, team, stats, pts=pts,
                                    src_id=cid.group(1) if cid else None, id_kind="cbs"))
    return out


def fetch_week(season, week, ttl=3 * 3600):
    out, first = [], True
    for pos, slug in POS.items():
        key = "proj-cbs-%s-%s-%s" % (season, week, pos)
        age = common.cache_age(key)
        if not first and (age is None or age >= ttl):
            base.pause()
        first = False
        out.extend(parse(common.fetch_text(URL % (slug, season, week), key, ttl,
                                           feed="projections.cbs"), pos))
    return out
