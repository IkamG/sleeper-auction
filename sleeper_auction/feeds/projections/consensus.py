"""Per-player consensus of the weekly projection sources.

Every source's stat line is scored with this league's scoring_settings, so
all of them are on one scale. A source's own points are used only when it has
no stat line (K and DEF). One value per PRODUCER. Median when three or more
sources have the player, mean otherwise; sd and the range travel with it.
"""
import statistics

from sleeper_auction.feeds import common, scoring
from sleeper_auction.feeds.projections import ADAPTERS, join

OUT_PTS = 0.5        # a source this close to zero...
OUT_OTHERS = 3.0     # ...while the others' median is above this: "source_thinks_out"
PRODUCER = {a.NAME: a.PRODUCER for a in ADAPTERS}
STAT_KEYS = set(scoring.HALF_PPR) | {"rec_tgt", "pass_att", "pass_cmp", "rush_att"}


def _pts(r, sc):
    st = r.get("stats") or {}
    if r["pos"] in ("K", "DEF") or not any(k in st for k in scoring.HALF_PPR):
        return r.get("pts")
    return scoring.points(st, sc, r["pos"])


def build(season, week, sc, source_rows, snapshot=True):
    cw = None
    per = {}
    for src, rows in (source_rows or {}).items():
        joined, _ = join(rows, cw, src)
        prod = PRODUCER.get(src, src)
        for r in joined:
            p = _pts(r, sc)
            if p is None:
                continue
            d = per.setdefault(r["sid"], {"pos": r["pos"], "by": {}, "lines": {}})
            if prod in d["by"]:
                continue
            d["by"][prod] = (src, round(p, 2))
            if r.get("stats") and r["pos"] not in ("K", "DEF"):
                d["lines"][src] = r["stats"]
    out = {}
    for sid, d in per.items():
        vals = [v for _, v in d["by"].values()]
        n = len(vals)
        med = statistics.median(vals) if n >= 3 else statistics.mean(vals)
        flags = []
        for src, v in d["by"].values():
            others = [x for s2, x in d["by"].values() if s2 != src]
            if others and v <= OUT_PTS and statistics.median(others) > OUT_OTHERS:
                flags.append("source_thinks_out:%s" % src)
        line = {}
        for k in STAT_KEYS:
            xs = [ln[k] for ln in d["lines"].values() if isinstance(ln.get(k), (int, float))]
            if xs:
                line[k] = round(statistics.median(xs), 3)
        out[sid] = {"median": round(med, 2), "mean": round(statistics.mean(vals), 2),
                    "lo": round(min(vals), 1), "hi": round(max(vals), 1),
                    "sd": round(statistics.pstdev(vals), 2) if n >= 2 else None, "n": n,
                    "by_source": {s: v for s, v in d["by"].values()},
                    "stats": line, "flags": flags or None}
    if snapshot and out:
        common.snapshot("consensus", season, week, {s: v["median"] for s, v in out.items()})
    return {"season": str(season), "week": int(week), "sources": sorted(source_rows or {}),
            "players": out}
