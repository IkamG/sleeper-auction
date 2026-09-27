"""Weekly projections from several independent sources, and their consensus.

Sources (probed 2026-09-27; each module's docstring has the details):

    sleeper_rw     Sleeper (RotoWire)      JSON, request path (already fetched)
    espn           ESPN (Mike Clay)        JSON
    cbs            CBS Sports              HTML
    fanduel        FanDuel / numberFire    GraphQL, current week only
    fftoday        FFToday                 HTML, paginated
    fantasysharks  FantasySharks           CSV

NFL.com is NOT here: fantasy.nfl.com/research/projections now 301s to an
nfl.com news page, and api.fantasy.nfl.com announces "ESPN Fantasy is now the
Official Fantasy Game of the NFL" (probed 2026-09-27). Yahoo is skipped because
it shows FantasyPros projections and would double-count. FantasyPros weekly
ECR is a rank signal (sources/fantasypros.fetch_week_ecr), not points, so it
travels alongside the consensus rather than inside it.

fetch_all(season, week, cache_only=True) is what pages call: it reads only
what the prefetcher (feeds/prefetch.py) has cached, so a cold page never waits
on a scraper. The probe and the prefetcher call it with cache_only=False.

    python3 -m sleeper_auction.feeds.projections --probe --week N
"""
import time

from sleeper_auction.feeds import common, ids
from sleeper_auction.feeds.projections import (cbs, espn, fanduel, fantasysharks, fftoday,
                                               sleeper_rw)

ADAPTERS = [sleeper_rw, espn, cbs, fanduel, fftoday, fantasysharks]
SLOW = ("cbs", "fftoday", "fantasysharks", "espn", "fanduel")


def _run(ad, season, week, cache_only):
    feed = "projections.%s" % ad.NAME
    if not common.enabled(feed):
        raise common.FeedDisabled(feed)
    t0 = time.time()
    if cache_only and ad.NAME in SLOW:
        with common.cache_only():
            rows = ad.fetch_week(season, week)
    else:
        rows = ad.fetch_week(season, week)
    common.ok(feed, rows=len(rows), week=int(week), fetch_s=round(time.time() - t0, 1))
    return rows


def fetch_all(season, week, cache_only=True, adapters=None):
    """({source: rows}, {source: error}) -- sources run in parallel."""
    ads = adapters or ADAPTERS
    res, errs = common.parallel(
        {a.NAME: (lambda a=a: _run(a, season, week, cache_only)) for a in ads},
        max_workers=6, timeout=30 if cache_only else 240)
    for name, e in errs.items():
        if "CacheMiss" in e:
            common.note("projections.%s" % name, ok=None,
                        error="not cached yet (the prefetcher fills it)")
        else:
            common.fail("projections.%s" % name, e)
    return res, errs


def join(rows, cw=None, source=None):
    """Attach a Sleeper id to each row (by id, else unique name key)."""
    cw = cw or ids.crosswalk()
    out, miss = [], []
    for r in rows:
        if r["pos"] == "DEF":
            sid = r["team"] if r.get("team") else None
        elif r.get("id_kind") == "sleeper":
            sid = r["src_id"]
        else:
            sid = ids.to_sleeper(r.get("id_kind") or ("name:%s" % (source or "?")),
                                 r.get("src_id"), r["name"], r["pos"], r.get("team"), cw=cw)
        if sid:
            out.append(dict(r, sid=sid))
        else:
            miss.append(r)
    return out, miss


def _probe(week):
    from sleeper_auction import sitstart
    t0 = time.time()
    res, errs = fetch_all(sitstart.SEASON, week, cache_only=False)
    cw = ids.crosswalk()
    top = sorted(((v["proj"], k) for k, v in sitstart.week_projections(week).items()),
                 reverse=True)[:200]
    top = {k for _, k in top}
    for name, rows in sorted(res.items()):
        joined, miss = join(rows, cw, name)
        got = {r["sid"] for r in joined}
        st = common.STATUS.get("projections.%s" % name, {})
        print("%-14s rows %4d  joined %4d  miss %3d  top-200 coverage %5.1f%%  fetch %ss" % (
            name, len(rows), len(joined), len(miss), 100.0 * len(got & top) / len(top),
            st.get("fetch_s")))
        miss_top = [m["name"] for m in miss if m["pos"] != "K"][:6]
        if miss_top:
            print("    unmatched e.g.", miss_top)
    for name, e in errs.items():
        print("%-14s ERROR %s" % (name, e))
    from sleeper_auction.feeds.projections import consensus
    from sleeper_auction.feeds import scoring
    c = consensus.build(sitstart.SEASON, week, scoring.HALF_PPR, res)
    n = [v["n"] for v in c["players"].values()]
    print("consensus: %d players, sources live %d, n>=3 for %d" % (
        len(n), len(res), sum(1 for x in n if x >= 3)))
    for sid in list(c["players"])[:0]:
        pass
    shown = sorted(c["players"].items(), key=lambda kv: -kv[1]["median"])[:5]
    for sid, v in shown:
        print("   %-22s median %5.1f  n %d  lo-hi %s-%s  %s" % (
            cw["info"].get(sid, {}).get("name", sid), v["median"], v["n"], v["lo"], v["hi"],
            v["by_source"]))
    out = [(cw["info"].get(s, {}).get("name"), v["flags"]) for s, v in c["players"].items()
           if v.get("flags")][:5]
    print("flags e.g.", out)
    print("took %.1fs" % (time.time() - t0))


def week_ecr(cache_only=True):
    """FantasyPros weekly ECR by Sleeper id (a rank signal, not points)."""
    from sleeper_auction.sources import fantasypros
    from sleeper_auction import board
    if not common.enabled("fantasypros.week"):
        raise common.FeedDisabled("fantasypros.week")
    cw = ids.crosswalk()
    out, week = {}, None
    for pos in fantasypros.WEEK_PAGES:
        try:
            if cache_only:
                with common.cache_only():
                    d = fantasypros.fetch_week_ecr(pos)
            else:
                d = fantasypros.fetch_week_ecr(pos)
        except common.CacheMiss:
            continue
        week = d.get("week") or week
        for r in d["players"]:
            if pos == "DEF":
                sid = board.nteam(r.get("team"))
            else:
                sid = ids.to_sleeper("fantasypros", r.get("fp_id"), r.get("name"), pos,
                                     r.get("team"), cw=cw)
            if sid:
                out[sid] = {k: r.get(k) for k in ("rank_ecr", "pos_rank", "rank_std",
                                                   "start_sit_grade", "tag", "r2p_pts")
                            if r.get(k) is not None}
    if out:
        common.ok("fantasypros.week", rows=len(out), week=week)
    return {"week": week, "players": out}
