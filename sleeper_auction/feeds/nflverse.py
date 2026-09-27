"""nflverse / ffverse: usage, snaps, practice, expected points, red zone, schedule.

Python 3.9, stdlib only. Everything keyed by Sleeper id through feeds/ids.py.

Probed 2026-09-27 (Sunday of week 3; TNF final) on this machine:
  * stats_player/stats_player_week_2026.csv.gz: 2,294 rows, weeks 1-2 plus 69
    TNF rows of week 3. Key player_id (GSIS). Rows exist only for players who
    recorded a stat, so team totals are sums over each (game_id, team).
    target_share / air_yards_share / wopr columns exist per game but are
    NOT used: shares are recomputed from sums so a 2-target blowout does not
    weigh the same as a 12-target game.
  * snap_counts/snap_counts_2026.csv.gz: 3,086 rows, key pfr_player_id;
    offense_pct is 0-1.
  * injuries/injuries_2026.csv.gz: 734 rows, exactly one row per
    (gsis_id, week) -- latest status only, so the Wed/Thu/Fri trajectory is
    built by snapshotting (common.snapshot). practice_status strings are the
    long forms ("Did Not Participate In Practice", "Limited Participation in
    Practice", "Full Participation in Practice"). report_status is blank for
    no designation (449/734). practice_primary_injury "Not injury related -
    resting player" is a veteran rest day, not an injury.
  * ffopportunity latest-data: ep_weekly_2026.csv, ep_pbp_pass_2026.csv,
    ep_pbp_rush_2026.csv exist as plain .csv ONLY (the .csv.gz names 404).
    ep_weekly is FULL PPR: Trey McBride wk1 9 rec / 95 yd / 1 TD has
    rec_fantasy_points 24.5, total_fantasy_points_exp 24.2, receptions_exp
    8.88. Half-PPR x = total_fantasy_points_exp - 0.5 * receptions_exp.
    Pass file receiver column is receiver_player_id (rush: rusher_player_id);
    both carry yardline_100, down, qtr, score_differential, vegas_wp, xpass.
  * nfldata games.csv: spread_line is the HOME margin (positive = home
    favoured): 2026 wk3 KC@MIA spread_line -10 -> KC -10, MIA +10.
    gametime is ET. roof is blank for 37 of 272 2026 games (e.g. BAL@DAL wk3
    at the Maracana, location "Neutral"), so neutral venues carry their own
    coordinates and roof default in VENUES below.
  * nflverse writes the Rams as LA; board.nteam() maps LA -> LAR.
  * No routes-run data (no TPRR/YPRR). Snap share and target share are the
    proxies.

    python3 -m sleeper_auction.feeds.nflverse --probe --season 2026
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

from sleeper_auction.feeds import common, ids

SEASON = "2026"
PRIOR = "2025"
SKILL = ("QB", "RB", "WR", "TE")
TTL = 6 * 3600

# Neutral-site venues: (lat, lon, roof default when games.csv leaves it blank).
VENUES = {
    "MEL00": (-37.82, 144.98, "dome"),
    "RIO00": (-22.912, -43.230, "outdoors"),
    "LON00": (51.556, -0.280, "outdoors"),
    "LON01": (51.456, -0.342, "outdoors"),
    "LON02": (51.604, -0.066, "outdoors"),
    "PAR00": (48.924, 2.360, "outdoors"),
    "MAD01": (40.453, -3.688, "retractable"),
    "MUN01": (48.219, 11.625, "outdoors"),
    "FRA00": (50.069, 8.645, "outdoors"),
    "BER00": (52.515, 13.239, "outdoors"),
    "MEX00": (19.303, -99.150, "outdoors"),
    "SAO00": (-23.545, -46.474, "outdoors"),
    "DUB00": (53.335, -6.228, "outdoors"),
}

_MEMO = {}
_MLOCK = threading.Lock()


def _memo(key, ttl, fn):
    with _MLOCK:
        hit = _MEMO.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    v = fn()
    with _MLOCK:
        _MEMO[key] = (time.time(), v)
    return v


def _nteam(t):
    from sleeper_auction import board
    return board.nteam(t)


def _check(feed):
    if not common.enabled(feed):
        raise common.FeedDisabled(feed)


def _asset(tag, name, feed, ttl=TTL):
    _check(feed)
    base = common.nflverse_url(tag, name)
    return common.fetch_csv([base + ".csv.gz", base + ".csv"], "nfv-%s" % name, ttl,
                            feed=feed)


def _ffopp(name, feed, ttl=TTL):
    _check(feed)
    u = common.ffopp_url(name)
    return common.fetch_csv([u + ".csv", u + ".csv.gz"], "ffopp-%s" % name, ttl, feed=feed)


def _i(v):
    x = common.num(v)
    return int(x) if x is not None else 0


def _f(v):
    x = common.num(v)
    return x if x is not None else 0.0


def _weeks(rows, key="week"):
    return sorted({_i(r.get(key)) for r in rows if _i(r.get(key)) > 0})


def _season_rows(loader, season, min_weeks):
    """Current season when it has >= min_weeks weeks, else the prior one."""
    for s in ([season] if season else [SEASON, PRIOR]):
        try:
            rows = loader(s)
        except Exception as e:
            common.fail(getattr(loader, "feed", loader.__name__), e)
            continue
        wk = _weeks(rows)
        if rows and (season or len(wk) >= min_weeks):
            return s, rows, wk
    return None, [], []

# ---------------------------------------------------------------- usage

def player_weeks(season):
    """All stats_player_week rows (skill filtering happens per consumer)."""
    return _memo("pw-%s" % season, 300,
                 lambda: _asset("stats_player", "stats_player_week_%s" % season,
                                "nflverse.stats"))


player_weeks.feed = "nflverse.stats"


def usage_from_rows(rows, recent=3, cw=None, cur_team=None):
    """Pure aggregation (tests call this directly). {sid: {...}}."""
    team_tot = {}
    for r in rows:
        k = (r.get("game_id"), _nteam(r.get("team")))
        t = team_tot.setdefault(k, [0.0, 0.0, 0.0])
        t[0] += _f(r.get("targets"))
        t[1] += _f(r.get("receiving_air_yards"))
        t[2] += _f(r.get("carries"))
    per = {}
    for r in rows:
        if r.get("position") not in SKILL:
            continue
        sid = ids.to_sleeper("gsis", r.get("player_id"), r.get("player_display_name"),
                             r.get("position"), r.get("team"), cw=cw)
        if not sid:
            continue
        per.setdefault(sid, []).append(r)
    out = {}
    for sid, games in per.items():
        games.sort(key=lambda r: _i(r.get("week")))

        def agg(gs):
            s = {"tgt": 0.0, "ay": 0.0, "car": 0.0, "rec": 0.0, "recyd": 0.0, "yac": 0.0,
                 "t_tgt": 0.0, "t_ay": 0.0, "t_car": 0.0}
            for r in gs:
                tt = team_tot.get((r.get("game_id"), _nteam(r.get("team"))), [0, 0, 0])
                s["tgt"] += _f(r.get("targets"))
                s["ay"] += _f(r.get("receiving_air_yards"))
                s["car"] += _f(r.get("carries"))
                s["rec"] += _f(r.get("receptions"))
                s["recyd"] += _f(r.get("receiving_yards"))
                s["yac"] += _f(r.get("receiving_yards_after_catch"))
                s["t_tgt"] += tt[0]
                s["t_ay"] += tt[1]
                s["t_car"] += tt[2]
            return s

        a, b = agg(games), agg(games[-recent:])

        def sh(x, y):
            return round(x / y, 3) if y > 0 else None

        ts, tsr = sh(a["tgt"], a["t_tgt"]), sh(b["tgt"], b["t_tgt"])
        ays, aysr = sh(a["ay"], a["t_ay"]), sh(b["ay"], b["t_ay"])
        cs, csr = sh(a["car"], a["t_car"]), sh(b["car"], b["t_car"])
        n, nr = len(games), len(games[-recent:])
        team = _nteam(games[-1].get("team"))
        ct = (cur_team or {}).get(sid)
        out[sid] = {
            "games": n, "team": team, "team_changed": bool(ct and team and ct != team),
            "pos": games[-1].get("position"),
            "tgt_share": ts, "tgt_share_recent": tsr,
            "air_share": ays, "air_share_recent": aysr,
            "wopr": round(1.5 * ts + 0.7 * ays, 3) if ts is not None and ays is not None else None,
            "wopr_recent": round(1.5 * tsr + 0.7 * aysr, 3)
            if tsr is not None and aysr is not None else None,
            "carry_share": cs, "carry_share_recent": csr,
            "targets_pg": round(a["tgt"] / n, 1), "targets_recent_pg": round(b["tgt"] / nr, 1),
            "carries_pg": round(a["car"] / n, 1),
            "adot": round(a["ay"] / a["tgt"], 1) if a["tgt"] >= 10 else None,
            "racr": round(a["recyd"] / a["ay"], 2) if a["ay"] >= 50 else None,
            "yac_per_rec": round(a["yac"] / a["rec"], 1) if a["rec"] >= 5 else None,
            "trend_tgt": round(100 * (tsr - ts), 1) if ts is not None and tsr is not None else None,
            "trend_carry": round(100 * (csr - cs), 1) if cs is not None and csr is not None else None,
        }
    return out


def _cur_teams(cw):
    return {sid: _nteam(v.get("team")) for sid, v in cw["info"].items() if v.get("team")}


def usage(season=None, recent=3, min_weeks=2):
    def run():
        src, rows, wk = _season_rows(player_weeks, season, min_weeks)
        if not rows:
            return {"season": None, "weeks": 0, "players": {}}
        cw = ids.crosswalk()
        pl = usage_from_rows(rows, recent, cw, _cur_teams(cw))
        common.ok("nflverse.usage", rows=len(pl), season=src, weeks=len(wk))
        return {"season": src, "weeks": len(wk), "last_week": wk[-1] if wk else None,
                "players": pl}
    return _memo("usage-%s-%s-%s" % (season, recent, min_weeks), 300, run)

# ---------------------------------------------------------------- snaps

def snaps(season=None, min_weeks=1):
    """sitstart.snap_trend's shape, keyed by Sleeper id (pfr_player_id join)."""
    def load(s):
        return _asset("snap_counts", "snap_counts_%s" % s, "nflverse.snaps", 86400)
    load.feed = "nflverse.snaps"

    def run():
        src, rows, _ = _season_rows(load, season, min_weeks)
        cw = ids.crosswalk() if rows else None
        by = {}
        for r in rows:
            pct = common.num(r.get("offense_pct"))
            wk = _i(r.get("week"))
            # Offense-only, skill positions: linemen are most of the file and
            # have no fantasy value (or Sleeper mapping).
            if not pct or pct <= 0 or not wk or r.get("position") not in SKILL:
                continue
            sid = ids.to_sleeper("pfr", r.get("pfr_player_id"), r.get("player"),
                                 r.get("position"), r.get("team"), cw=cw)
            if sid:
                by.setdefault(sid, []).append((wk, pct, _nteam(r.get("team"))))
        out = {}
        for sid, vals in by.items():
            vals.sort()
            pcts = [p for _, p, _ in vals]
            rec = pcts[-3:]
            avg = sum(pcts) / len(pcts)
            out[sid] = {"games": len(pcts), "season_pct": round(100 * avg),
                        "recent_pct": round(100 * sum(rec) / len(rec)),
                        "trend": round(100 * (sum(rec) / len(rec) - avg)),
                        "earned_on": vals[-1][2]}
        if out:
            common.ok("nflverse.snaps", rows=len(out), season=src)
        return {"season": src if out else None, "players": out}
    return _memo("snaps-%s" % season, 300, run)

# ---------------------------------------------------------------- practice

def norm_practice(s):
    s = (s or "").strip().lower()
    if not s:
        return None
    if "did not" in s or s in ("dnp", "out"):
        return "DNP"
    if "limited" in s or s in ("lp", "limit"):
        return "LP"
    if "full" in s or s in ("fp",):
        return "FP"
    return None


def practice_from_rows(rows, week, slp_players=None, cw=None):
    """Latest practice line per Sleeper id for `week` (pure; tests use it)."""
    out = {}
    for r in rows:
        # Linemen and defenders are on the report too; they are not fantasy
        # players and most have no Sleeper mapping, so they would only
        # inflate the join-miss rate.
        if _i(r.get("week")) != int(week) or r.get("position") not in SKILL + ("K",):
            continue
        sid = ids.to_sleeper("gsis", r.get("gsis_id"), r.get("full_name"),
                             r.get("position"), r.get("team"), cw=cw)
        if not sid:
            continue
        pinj = r.get("practice_primary_injury") or ""
        out[sid] = {"status": norm_practice(r.get("practice_status")),
                    "report_status": (r.get("report_status") or "").strip() or None,
                    "rest": "not injury related" in pinj.lower(),
                    "injury": (r.get("report_primary_injury") or pinj or "").strip() or None,
                    "source": "nflverse"}
    for sid, p in (slp_players or {}).items():
        st = norm_practice(p.get("practice_participation"))
        if not st and not p.get("practice_description"):
            continue
        cur = out.setdefault(sid, {"status": None, "report_status": None, "rest": False,
                                   "injury": None, "source": "sleeper"})
        # Sleeper updates intraday; prefer it when nflverse has no status yet.
        if st and not cur["status"]:
            cur["status"] = st
        if p.get("practice_description"):
            cur["sleeper_note"] = p["practice_description"][:120]
        if p.get("injury_status") and not cur.get("report_status"):
            cur["sleeper_status"] = p["injury_status"]
    return out


def trajectory(snaps_list, sid):
    """Ordered, de-duplicated statuses from snapshots: [(status, as_of)]."""
    seq = []
    for ts, payload in snaps_list:
        st = ((payload or {}).get(sid) or {}).get("status")
        if st and (not seq or seq[-1][0] != st):
            seq.append((st, ts))
    return seq


def practice(season=SEASON, week=None):
    def run():
        try:
            rows = _asset("injuries", "injuries_%s" % season, "nflverse.injuries",
                          2 * 3600 if time.gmtime().tm_wday in (2, 3, 4, 5) else 12 * 3600)
            fresh = not common.STATUS.get("nflverse.injuries", {}).get("stale")
        except Exception as e:
            common.fail("nflverse.injuries", e)
            rows, fresh = [], False
        wk = week or (max(_weeks(rows)) if rows else None)
        if not wk:
            return {"week": None, "players": {}}
        slp = {}
        try:
            slp = {sid: p for sid, p in ids.players().items()
                   if p.get("practice_participation") or p.get("practice_description")}
        except Exception:
            pass
        cw = ids.crosswalk()
        cur = practice_from_rows(rows, wk, slp, cw)
        if fresh and cur:
            common.snapshot("practice", season, wk,
                            {k: {"status": v["status"]} for k, v in cur.items() if v["status"]})
        hist = common.snapshots("practice", season, wk)
        for sid, v in cur.items():
            t = trajectory(hist, sid)
            if not t and v["status"]:
                t = [(v["status"], None)]
            v["trajectory"] = [s for s, _ in t]
            v["as_of"] = [a for _, a in t]
        common.ok("nflverse.injuries", rows=len(cur), week=wk)
        return {"week": wk, "season": season, "players": cur}
    return _memo("practice-%s-%s" % (season, week), 600, run)

# ---------------------------------------------------------------- xFP

def xfp_from_rows(rows, rec_value=0.5, recent=3, cw=None):
    per = {}
    adj = rec_value - 1.0
    for r in rows:
        if r.get("position") not in SKILL:
            continue
        sid = ids.to_sleeper("gsis", r.get("player_id"), r.get("full_name"),
                             r.get("position"), r.get("posteam"), cw=cw)
        if not sid:
            continue
        per.setdefault(sid, []).append((
            _i(r.get("week")),
            _f(r.get("total_fantasy_points_exp")) + adj * _f(r.get("receptions_exp")),
            _f(r.get("total_fantasy_points")) + adj * _f(r.get("receptions")),
            _f(r.get("total_touchdown_exp")), _f(r.get("total_touchdown"))))
    out = {}
    for sid, g in per.items():
        g.sort()

        def pg(lst, i):
            return round(sum(x[i] for x in lst) / len(lst), 2)
        n = len(g)
        rec = g[-recent:]
        d = {"games": n, "xfp_pg": pg(g, 1), "fp_pg": pg(g, 2),
             "xtd_pg": pg(g, 3), "td_pg": pg(g, 4),
             "xfp_recent": pg(rec, 1), "fp_recent": pg(rec, 2)}
        # diff over one game is noise: only publish it from 2 games.
        d["diff_pg"] = round(d["fp_pg"] - d["xfp_pg"], 2) if n >= 2 else None
        out[sid] = d
    return out


def expected_points(season=None, recent=3, rec_value=0.5, min_weeks=2):
    def load(s):
        return _ffopp("ep_weekly_%s" % s, "ffopportunity.ep")
    load.feed = "ffopportunity.ep"

    def run():
        src, rows, wk = _season_rows(load, season, min_weeks)
        pl = xfp_from_rows(rows, rec_value, recent, ids.crosswalk()) if rows else {}
        if pl:
            common.ok("ffopportunity.ep", rows=len(pl), season=src, weeks=len(wk))
        return {"season": src, "weeks": len(wk), "scoring_rec": rec_value, "players": pl}
    return _memo("xfp-%s-%s-%s" % (season, recent, rec_value), 300, run)

# ---------------------------------------------------------------- red zone

def _neutral(r):
    try:
        return (_i(r.get("down")) in (1, 2) and abs(_f(r.get("score_differential"))) <= 7
                and 0.2 <= _f(r.get("vegas_wp")) <= 0.8 and 1 <= _i(r.get("qtr")) <= 3)
    except Exception:
        return False


def redzone_from_rows(rush, pas, cw=None):
    pl, tm = {}, {}

    def P(sid):
        return pl.setdefault(sid, {"rz_carries": 0, "i10_carries": 0, "gl_carries": 0,
                                   "rz_targets": 0, "team": None})

    def T(t):
        return tm.setdefault(t, {"rz_carries": 0, "gl_carries": 0, "rz_targets": 0,
                                 "n_pass": 0, "n_rush": 0, "xpass": 0.0})
    for r in rush:
        if _i(r.get("two_point_attempt")) or not _i(r.get("rush_attempt")):
            continue
        team = _nteam(r.get("posteam"))
        yl = common.num(r.get("yardline_100"))
        t = T(team)
        if _neutral(r) and common.num(r.get("xpass")) is not None:
            if _i(r.get("qb_scramble")):
                t["n_pass"] += 1
            else:
                t["n_rush"] += 1
            t["xpass"] += _f(r.get("xpass"))
        if yl is None or yl > 20:
            continue
        sid = ids.to_sleeper("gsis", r.get("rusher_player_id"), r.get("full_name"),
                             r.get("position"), team, cw=cw)
        t["rz_carries"] += 1
        if yl <= 5:
            t["gl_carries"] += 1
        if not sid:
            continue
        p = P(sid)
        p["team"] = team
        p["rz_carries"] += 1
        p["i10_carries"] += yl <= 10
        p["gl_carries"] += yl <= 5
    for r in pas:
        if _i(r.get("two_point_attempt")) or not _i(r.get("pass_attempt")):
            continue
        team = _nteam(r.get("posteam"))
        t = T(team)
        if _neutral(r) and common.num(r.get("xpass")) is not None:
            t["n_pass"] += 1
            t["xpass"] += _f(r.get("xpass"))
        yl = common.num(r.get("yardline_100"))
        if yl is None or yl > 20 or not r.get("receiver_player_id"):
            continue
        t["rz_targets"] += 1
        sid = ids.to_sleeper("gsis", r.get("receiver_player_id"), r.get("receiver_full_name"),
                             r.get("receiver_position"), team, cw=cw)
        if sid:
            p = P(sid)
            p["team"] = team
            p["rz_targets"] += 1
    for sid, p in pl.items():
        t = tm.get(p["team"]) or {}
        opp = t.get("rz_carries", 0) + t.get("rz_targets", 0)
        p["rz_share"] = round((p["rz_carries"] + p["rz_targets"]) / opp, 3) if opp else None
        p["rz_carry_share"] = round(p["rz_carries"] / t["rz_carries"], 3) \
            if t.get("rz_carries") else None
        p["gl_share"] = round(p["gl_carries"] / t["gl_carries"], 3) if t.get("gl_carries") else None
    teams = {}
    for team, t in tm.items():
        n = t["n_pass"] + t["n_rush"]
        teams[team] = {"neutral_plays": n,
                       "pass_rate": round(t["n_pass"] / n, 3) if n else None,
                       "xpass": round(t["xpass"] / n, 3) if n else None,
                       "proe": round((t["n_pass"] - t["xpass"]) / n, 3) if n >= 30 else None}
    return {"players": pl, "teams": teams}


def redzone(season=None, min_weeks=2):
    def load(s):
        return _ffopp("ep_pbp_rush_%s" % s, "ffopportunity.pbp")
    load.feed = "ffopportunity.pbp"

    def run():
        src, rush, wk = _season_rows(load, season, min_weeks)
        if not rush:
            return {"season": None, "players": {}, "teams": {}}
        try:
            pas = _ffopp("ep_pbp_pass_%s" % src, "ffopportunity.pbp")
        except Exception as e:
            common.fail("ffopportunity.pbp", e)
            pas = []
        out = redzone_from_rows(rush, pas, ids.crosswalk())
        out.update(season=src, weeks=len(wk))
        common.ok("ffopportunity.pbp", rows=len(out["players"]), season=src)
        return out
    return _memo("rz-%s" % season, 300, run)

# ---------------------------------------------------------------- schedule

def _kickoff_utc(day, hhmm):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        dt = datetime.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M")
        except Exception:
            return None
        # No tz database: EDT until the first Sunday of November, else EST.
        nov1 = datetime(dt.year, 11, 1)
        first_sun = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        off = 4 if dt < first_sun + timedelta(hours=2) and dt.month >= 3 else 5
        return (dt + timedelta(hours=off)).replace(tzinfo=timezone.utc)


def schedule_from_rows(rows, season):
    out = {}
    for r in rows:
        if r.get("season") != str(season) or r.get("game_type") != "REG":
            continue
        wk = _i(r.get("week"))
        home, away = _nteam(r.get("home_team")), _nteam(r.get("away_team"))
        total = common.num(r.get("total_line"))
        sl = common.num(r.get("spread_line"))
        ko = _kickoff_utc(r.get("gameday"), r.get("gametime"))
        venue = VENUES.get(r.get("stadium_id") or "")
        roof = (r.get("roof") or "").strip() or (venue[2] if venue else None)
        for team, opp, is_home in ((home, away, True), (away, home, False)):
            spread = None if sl is None else (-sl if is_home else sl)
            implied = round(total / 2 - spread / 2, 2) if total is not None and spread is not None \
                else (round(total / 2, 2) if total is not None else None)
            out.setdefault(wk, {})[team] = {
                "opp": opp, "home": is_home, "total": total, "spread": spread,
                "implied": implied, "game_id": r.get("game_id"),
                "kickoff_utc": ko.strftime("%Y-%m-%dT%H:%MZ") if ko else None,
                "roof": roof, "stadium": r.get("stadium"), "stadium_id": r.get("stadium_id"),
                "neutral": r.get("location") == "Neutral",
                "venue_coords": venue[:2] if venue else None,
                "rest_days": _i(r.get("home_rest" if is_home else "away_rest")) or None}
    return out


def schedule(season=SEASON):
    def run():
        try:
            _check("nflverse.schedule")
            txt = common.fetch_text(
                "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
                "nfldata-games.csv", 2 * 3600, feed="nflverse.schedule", timeout=90)
            out = schedule_from_rows(common.parse_csv(txt), season)
            common.ok("nflverse.schedule", rows=sum(len(v) for v in out.values()))
            return out
        except Exception as e:
            common.fail("nflverse.schedule", e)
            return {}
    return _memo("sched-%s" % season, 600, run)


def byes(season=SEASON):
    """{team: bye week} from the schedule."""
    s = schedule(season)
    teams = {t for wk in s.values() for t in wk}
    weeks = sorted(s)
    return {t: next((w for w in weeks if t not in s[w]), None) for t in teams} if weeks else {}

# ---------------------------------------------------------------- probe

def _probe(season):
    import collections
    t0 = time.time()
    cw = ids.crosswalk(0)
    rows = player_weeks(season)
    print("stats_player_week_%s: %d rows, weeks %s" % (season, len(rows), _weeks(rows)))
    ids.COUNTS.clear()
    active = [r for r in rows if r.get("position") in SKILL
              and (_f(r.get("targets")) > 0 or _f(r.get("carries")) > 0)]
    uniq = {r["player_id"]: r for r in active}
    miss = [r for r in uniq.values()
            if not ids.to_sleeper("gsis", r["player_id"], r.get("player_display_name"),
                                  r.get("position"), r.get("team"), cw=cw)]
    print("  players with a target or carry: %d, joined %.1f%% (by id/name: %s)" % (
        len(uniq), 100 * (1 - len(miss) / max(1, len(uniq))), ids.join_status().get("gsis")))
    for r in miss[:10]:
        print("   unmatched:", r["player_id"], r.get("player_display_name"), r.get("position"),
              r.get("team"))
    u = usage(season)
    print("usage: season %s weeks %s players %d" % (u["season"], u["weeks"], len(u["players"])))
    top = sorted(u["players"].items(), key=lambda kv: -(kv[1]["wopr"] or 0))[:3]
    for sid, v in top:
        print("  ", cw["info"].get(sid, {}).get("name"), {k: v[k] for k in
                                                          ("tgt_share", "air_share", "wopr", "games")})
    sn = snaps(season)
    print("snaps: season %s players %d" % (sn["season"], len(sn["players"])))
    ids.COUNTS.clear()
    pr = practice(season)
    c = collections.Counter(v["status"] for v in pr["players"].values())
    print("practice wk %s: %d players %s, rest days %d, join %s" % (
        pr["week"], len(pr["players"]), dict(c),
        sum(v["rest"] for v in pr["players"].values()), ids.join_status().get("gsis")))
    x = expected_points(season)
    print("xfp: season %s weeks %s players %d" % (x["season"], x["weeks"], len(x["players"])))
    mc = next((sid for sid, v in cw["info"].items() if v["name"] == "Trey McBride"), None)
    print("  McBride:", x["players"].get(mc))
    rz = redzone(season)
    print("redzone: players %d teams %d; PROE top3 %s" % (
        len(rz["players"]), len(rz["teams"]),
        sorted(((v["proe"], t) for t, v in rz["teams"].items() if v["proe"] is not None),
               reverse=True)[:3]))
    sc = schedule(season)
    print("schedule: weeks %d; wk3 KC %s" % (len(sc), (sc.get(3) or {}).get("KC")))
    print("took %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        s = sys.argv[sys.argv.index("--season") + 1] if "--season" in sys.argv else SEASON
        _probe(s)
    else:
        print(__doc__)
