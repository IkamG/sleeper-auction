"""Player props: this week's betting-market view of each player.

week_props(season, week) merges two providers per Sleeper id and market:
Kalshi ladders (keyless) when the ladder is healthy, else The Odds API
over/under lines (ODDS_API_KEY), else whatever Kalshi has. It records the
source and as-of time and snapshots the result for line movement and
calibration.

player_props(wp, sid, pos, filler_stats, scoring) turns that into fantasy
points: {"mean", "p10", "p90", "anytime_td", "lines", "coverage", "source",
"as_of"}.

    python3 -m sleeper_auction.feeds.props --probe --week N
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))))

from sleeper_auction.feeds import common, ids
from sleeper_auction.feeds.props import implied, kalshi, oddsapi

HEALTHY_RUNGS = 3        # a Kalshi ladder needs this many priced rungs...
HEALTHY_SPREAD = 0.05    # ...quoted this tight on average, or real open interest
HEALTHY_OI = 50

_MEMO = {"at": 0, "key": None, "val": None}
_LOCK = threading.Lock()


def _parse_ts(s):
    try:
        return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _healthy(k):
    if not k:
        return False
    if k.get("market") in ("td", "rec", "pass_td", "pass_int"):
        return len(k["ladder"]) >= 1 and ((k.get("spread") or 1) <= HEALTHY_SPREAD
                                          or k.get("oi", 0) >= HEALTHY_OI)
    return len(k["ladder"]) >= HEALTHY_RUNGS and (
        (k.get("spread") or 1) <= HEALTHY_SPREAD or k.get("oi", 0) >= HEALTHY_OI)


def _from_kalshi(k):
    mk = k["market"]
    lad = k["ladder"]
    if mk == "td":
        ks = [int(x) for x, _ in lad]
        if ks[:1] == [1] and len(ks) == 1:
            return {"p_any": lad[0][1], "mean": round(implied.td_lambda(lad[0][1]), 3)}
        c = implied.expected_count_from_ladder(lad)
        if c:
            return c
    if mk in ("pass_td", "pass_int") and len(lad) == 1:
        return {"mean": implied.lambda_from_ou(lad[0][0] - 0.5, lad[0][1])}
    return implied.expected_from_ladder(lad) if len(lad) >= 2 else None


def merge(kal, odds):
    """{sid: {market: {...,"source"}}} preferring healthy Kalshi ladders."""
    out = {}
    keys = set(kal) | set(odds)
    for mk in keys:
        ks, os_ = kal.get(mk) or {}, odds.get(mk) or {}
        for sid in set(ks) | set(os_):
            k, o = ks.get(sid), os_.get(sid)
            pick = None
            if k and _healthy(k):
                e = _from_kalshi(k)
                if e:
                    pick = dict(e, source="kalshi", ladder=k["ladder"], spread=k.get("spread"))
            if not pick and o:
                pick = dict(o, source="oddsapi")
            if not pick and k:
                e = _from_kalshi(k)
                if e:
                    pick = dict(e, source="kalshi-thin", ladder=k["ladder"],
                                spread=k.get("spread"))
            if pick:
                out.setdefault(sid, {})[mk] = pick
    return out


def week_props(season, week, refresh=False):
    """Merged markets for players whose games have not kicked off. Memoised 5 min."""
    key = (str(season), int(week))
    with _LOCK:
        if _MEMO["key"] == key and time.time() - _MEMO["at"] < 300 and not refresh:
            return _MEMO["val"]
    if not common.enabled("props"):
        raise common.FeedDisabled("props")
    from sleeper_auction.feeds import nflverse
    sched = (nflverse.schedule(season) or {}).get(int(week)) or {}
    kick = {t: _parse_ts(g.get("kickoff_utc") or "") for t, g in sched.items()}
    now = datetime.now(timezone.utc)
    cw = ids.crosswalk()

    def team_of(sid):
        from sleeper_auction import board
        return board.nteam((cw["info"].get(sid) or {}).get("team"))

    def started(sid):
        k = kick.get(team_of(sid))
        return bool(k and k <= now)
    future = [k for k in kick.values() if k and k > now]
    first = min(future).timestamp() if future else None
    last = max(k for k in kick.values() if k) if any(kick.values()) else None
    res, errs = common.parallel({
        "kalshi": lambda: kalshi.week_markets(started, first),
        "odds": lambda: oddsapi.week_markets(
            (now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             (last + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ") if last else "9999"),
            refresh)}, timeout=90)
    kal, kal_um = res.get("kalshi") or ({}, [])
    odds, odds_um = res.get("odds") or ({}, [])
    merged = merge(kal, odds)
    as_of = now.strftime("%Y-%m-%dT%H:%MZ")
    val = {"season": str(season), "week": int(week), "as_of": as_of, "players": merged,
           "unmatched": {"kalshi": kal_um, "oddsapi": odds_um}, "errors": errs or None}
    if merged:
        common.snapshot("props", season, week,
                        {sid: {mk: round(v.get("mean") or 0, 2) for mk, v in m.items()}
                         for sid, m in merged.items()})
    with _LOCK:
        _MEMO.update(at=time.time(), key=key, val=val)
    return val


def player_props(wp, sid, pos, filler_stats, scoring):
    """Fantasy view of one player's markets, or None when he has none."""
    m = ((wp or {}).get("players") or {}).get(str(sid))
    if not m:
        return None
    mk = dict(m)
    if "td" in mk:
        td = mk["td"]
        if td.get("p_any") is None and td.get("ladder"):
            td["p_any"] = td["ladder"][0][1] if int(td["ladder"][0][0]) == 1 else None
    f = implied.fantasy_from_markets(mk, filler_stats, scoring, pos)
    any_td = (mk.get("td") or {}).get("p_any") or (mk.get("anytime_td") or {}).get("p")
    lines = {}
    for k, v in m.items():
        if v.get("line") is not None:
            lines[k] = {"line": v["line"], "p_over": v.get("p_over")}
        elif v.get("median") is not None:
            lines[k] = {"median": v["median"]}
        elif v.get("mean") is not None:
            lines[k] = {"mean": v["mean"]}
    return {"mean": f["mean"], "p10": f.get("p10"), "p90": f.get("p90"),
            "anytime_td": round(any_td, 3) if any_td is not None else None,
            "lines": lines, "coverage": f["coverage"],
            "source": sorted({v.get("source") for v in m.values()}),
            "as_of": wp.get("as_of")}


def _probe(week):
    from sleeper_auction import sitstart
    from sleeper_auction.feeds import scoring
    t0 = time.time()
    print("unmapped KXNFL player-stat series:", [s["ticker"] for s in kalshi.discover()])
    wp = week_props(sitstart.SEASON, week, refresh=False)
    pl = wp["players"]
    by = {}
    for sid, m in pl.items():
        for mk, v in m.items():
            by.setdefault((mk, v["source"]), 0)
            by[(mk, v["source"])] += 1
    print("players with props:", len(pl), "| by market/source:", dict(sorted(by.items())))
    print("unmatched:", {k: v[:10] for k, v in wp["unmatched"].items()}, "errors:", wp["errors"])
    proj = sitstart.week_projections(week)
    cw = ids.crosswalk()
    sc = scoring.HALF_PPR
    top = {"QB": 24, "TE": 24, "RB": 36, "WR": 48}
    have = tot = 0
    for pos, n in top.items():
        ps = sorted(((v["proj"], sid) for sid, v in proj.items()
                     if (cw["info"].get(sid) or {}).get("pos") == pos), reverse=True)[:n]
        for _, sid in ps:
            tot += 1
            have += sid in pl
    print("fantasy starters with a props mean: %d/%d (%.0f%%) -- games already started "
          "are excluded by design" % (have, tot, 100.0 * have / max(1, tot)))
    shown = 0
    for sid in pl:
        v = proj.get(sid)
        if not v or not v.get("stats"):
            continue
        pp = player_props(wp, sid, cw["info"][sid]["pos"], v["stats"], sc)
        print("  %-22s sleeper %5.1f  props %5.1f (p10 %s p90 %s) cov %.2f %s" % (
            cw["info"][sid]["name"], v["proj"], pp["mean"], pp["p10"], pp["p90"],
            pp["coverage"], pp["source"]))
        shown += 1
        if shown >= 8:
            break
    print("took %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        w = int(sys.argv[sys.argv.index("--week") + 1]) if "--week" in sys.argv else 1
        _probe(w)
    else:
        print(__doc__)
