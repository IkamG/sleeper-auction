"""Phase 5: score every projection source against what actually happened.

    python3 -m sleeper_auction.feeds.calibrate --season 2026

Two halves:

  record(season, week)   Snapshot, per player whose game has not kicked off,
                         Sleeper's projection, every consensus source, the
                         consensus median, the props mean (and its coverage)
                         and the final blend. The prefetcher calls it on every
                         refresh; snapshots de-duplicate by content, so the
                         LAST snapshot holding a player is his final
                         pre-kickoff line.
  run(season)            For each completed week with snapshots: MAE and bias
                         by source and position against actual points
                         (Sleeper's weekly stats, league-scored), restricted
                         to players who played. Fits the props weight by grid
                         search (w in 0..1, step 0.05) on players with full
                         props coverage, and writes cache/calibration.json.
                         feeds/blend.py uses that weight once it holds
                         MIN_CAL_WEEKS weeks.

This is how "props predict better than projections" gets tested for THIS
league instead of assumed. The history lives in cache/history/ on this
machine only.
"""
import argparse
import json
import os
import statistics
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

from sleeper_auction.feeds import common, ids

KIND = "calib"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def record(season, week, league_id=None):
    """Snapshot this week's pre-kickoff numbers for every player not started."""
    from sleeper_auction import sitstart
    from sleeper_auction.feeds import blend, scoring
    from sleeper_auction.feeds import props as propsfeed
    from sleeper_auction.feeds.projections import consensus, fetch_all
    sc = scoring.league_scoring(league_id) if league_id else scoring.HALF_PPR
    proj = sitstart.week_projections(week, season)
    res, _ = fetch_all(season, week, cache_only=True)
    cons = consensus.build(season, week, sc, res, snapshot=False)["players"]
    try:
        wp = propsfeed.week_props(season, week)
    except Exception:
        wp = None
    started = set()
    try:
        gs = sitstart.game_states(week, season)
        started = {t for t, g in gs.items() if g.get("state") in ("in", "post")}
    except Exception:
        pass
    cw = ids.crosswalk()
    out = {}
    for sid, pj in proj.items():
        info = cw["info"].get(sid) or {}
        from sleeper_auction import board
        team = board.nteam(info.get("team")) if info.get("team") else (sid if sid.isalpha()
                                                                         else None)
        if team in started or pj.get("proj") is None:
            continue
        c = cons.get(sid) or {}
        pp = None
        if wp and (c.get("stats") or pj.get("stats")):
            try:
                pp = propsfeed.player_props(wp, sid, info.get("pos"),
                                            c.get("stats") or pj.get("stats"), sc)
            except Exception:
                pp = None
        f = blend.final(pj.get("proj"), c or None, pp)
        out[sid] = {"pos": info.get("pos") or ("DEF" if sid.isalpha() else None),
                    "sleeper": pj.get("proj"),
                    "sources": c.get("by_source"),
                    "consensus": c.get("median"),
                    "props": (pp or {}).get("mean"),
                    "props_cov": (pp or {}).get("coverage"),
                    "final": f["value"] if f else None}
    if out:
        common.snapshot(KIND, season, week, out)
    return len(out)


def final_lines(season, week):
    """{sid: last pre-kickoff line} merged over the week's snapshots."""
    last = {}
    for _, payload in common.snapshots(KIND, season, week):
        for sid, v in (payload or {}).items():
            last[sid] = v
    return last


def actual_points(season, week, sc=None):
    """{sid: points} for players who played, league-scored where possible."""
    from sleeper_auction import sitstart
    from sleeper_auction.feeds import scoring
    out = {}
    for sid, a in sitstart.actuals(week, season).items():
        if a.get("played"):
            out[sid] = a["actual"]
    if sc and sc != scoring.HALF_PPR:
        # Re-score skill players from the raw stat line with league scoring.
        try:
            from sleeper_auction import board as db
            d = db.gj("https://api.sleeper.com/stats/nfl/%s/%s?season_type=regular&%s"
                      % (season, week, "&".join("position[]=" + p for p in POSITIONS[:4])),
                      key="slp-act-raw-%s-%s" % (season, week), ttl=3600)
            for r in d:
                sid = str(r.get("player_id") or "")
                if sid in out:
                    out[sid] = scoring.points(r.get("stats") or {}, sc,
                                              (r.get("player") or {}).get("position"))
        except Exception:
            pass
    return out


def week_complete(season, week):
    """True when every game of the week is final (only whole weeks are scored)."""
    from sleeper_auction import sitstart
    try:
        gs = sitstart.game_states(week, season)
    except Exception:
        return False
    return bool(gs) and all(g.get("state") == "post" for g in gs.values())


def errors(lines, actual):
    """{source: {pos: [(pred - actual)]}} over players with both."""
    out = {}
    for sid, ln in lines.items():
        a = actual.get(sid)
        if a is None:
            continue
        pos = ln.get("pos") or "?"
        preds = {"sleeper": ln.get("sleeper"), "consensus": ln.get("consensus"),
                 "props": ln.get("props"), "final": ln.get("final")}
        for s, v in (ln.get("sources") or {}).items():
            if s != "sleeper":
                preds["src:" + s] = v
        for s, v in preds.items():
            if v is not None:
                out.setdefault(s, {}).setdefault(pos, []).append(v - a)
    return out


def summarise(errs):
    tab = {}
    for s, by in errs.items():
        allv = [e for v in by.values() for e in v]
        tab[s] = {"all": {"mae": round(statistics.mean(abs(e) for e in allv), 2),
                          "bias": round(statistics.mean(allv), 2), "n": len(allv)}}
        for pos, v in by.items():
            tab[s][pos] = {"mae": round(statistics.mean(abs(e) for e in v), 2),
                           "bias": round(statistics.mean(v), 2), "n": len(v)}
    return tab


def fit_weight(pairs):
    """Grid-search w minimising MAE of w*props + (1-w)*consensus."""
    if not pairs:
        return None, None
    best = None
    for i in range(21):
        w = i * 0.05
        mae = statistics.mean(abs(w * p + (1 - w) * c - a) for p, c, a in pairs)
        if best is None or mae < best[1] - 1e-9:
            best = (round(w, 2), round(mae, 3))
    return best


def run(season, weeks=None, league_id=None, write=True):
    from sleeper_auction.feeds import scoring
    sc = scoring.league_scoring(league_id) if league_id else None
    d = os.path.join(common.CACHE, "history", str(season))
    if weeks is None:
        weeks = sorted(int(x[1:]) for x in os.listdir(d) if x.startswith("w")) \
            if os.path.isdir(d) else []
    all_errs, pairs, used, per_week = {}, [], [], {}
    for w in weeks:
        lines = final_lines(season, w)
        if not lines or not week_complete(season, w):
            continue
        act = actual_points(season, w, sc)
        if not act:
            continue
        e = errors(lines, act)
        if not e:
            continue
        used.append(w)
        per_week[w] = summarise(e)
        for s, by in e.items():
            for pos, v in by.items():
                all_errs.setdefault(s, {}).setdefault(pos, []).extend(v)
        for sid, ln in lines.items():
            if sid in act and ln.get("props") is not None and ln.get("consensus") is not None \
                    and (ln.get("props_cov") or 0) >= 0.7:
                pairs.append((ln["props"], ln["consensus"], act[sid]))
    w, mae = fit_weight(pairs)
    out = {"season": str(season), "n_weeks": len(used), "weeks": used,
           "n_players": ((summarise(all_errs).get("final") or {}).get("all") or {}).get("n", 0)
           if all_errs else 0,
           "w_props": w, "w_props_mae": mae, "n_props_pairs": len(pairs),
           "mae": summarise(all_errs) if all_errs else {}, "by_week": per_week,
           "at": int(time.time())}
    if write and used:
        with open(common.cache_path("calibration.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return out


def week_errors(season, week, sids):
    """Per-source error for these players in one finished week (post-week page)."""
    lines = final_lines(season, week)
    if not lines:
        return {}
    act = actual_points(season, week)
    out = {}
    for sid in sids:
        ln, a = lines.get(str(sid)), act.get(str(sid))
        if not ln or a is None:
            continue
        e = {}
        for k in ("sleeper", "consensus", "props", "final"):
            if ln.get(k) is not None:
                e[k] = round(ln[k] - a, 1)
        for s, v in (ln.get("sources") or {}).items():
            e["src:" + s] = round(v - a, 1)
        out[str(sid)] = e
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026")
    ap.add_argument("--league")
    ap.add_argument("--record-week", type=int, help="snapshot this week's lines now")
    a = ap.parse_args()
    if a.record_week:
        print("recorded %d players" % record(a.season, a.record_week, a.league))
        return
    r = run(a.season, league_id=a.league)
    print("weeks scored: %s (n=%d)  props weight %s (MAE %s on %d players)" % (
        r["weeks"], r["n_weeks"], r["w_props"], r["w_props_mae"], r["n_props_pairs"]))
    for s, t in sorted(r["mae"].items(), key=lambda kv: kv[1]["all"]["mae"]):
        print("  %-18s MAE %5.2f  bias %+5.2f  n %4d   %s" % (
            s, t["all"]["mae"], t["all"]["bias"], t["all"]["n"],
            "  ".join("%s %.2f" % (p, v["mae"]) for p, v in sorted(t.items()) if p != "all")))
    if r["n_weeks"]:
        print("wrote cache/calibration.json")


if __name__ == "__main__":
    main()
