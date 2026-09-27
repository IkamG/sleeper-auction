"""League scoring: Sleeper scoring_settings -> points(stats). Python 3.9, stdlib.

The canonical stat vocabulary is Sleeper's stat keys (pass_yd, pass_td,
pass_int, pass_2pt, rush_yd, rush_td, rush_2pt, rec, rec_yd, rec_td, rec_2pt,
fum_lost, bonus_rec_te, ...). They are the keys scoring_settings uses, so
points() is a plain dot product and TE premium falls out for free once the
caller adds bonus_rec_te = rec for tight ends (te_stats()).

K and DEF are not rebuilt here: use the source's own points or Sleeper's.
"""
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

from sleeper_auction.feeds import common

# Standard half-PPR; used when the league is unreachable.
HALF_PPR = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0,
}
# Keys that matter for offensive skill players; everything else in
# scoring_settings (IDP, kicking, defence) is ignored by points().
OFFENSE = set(HALF_PPR) | {"bonus_rec_te", "bonus_rec_rb", "bonus_rec_wr",
                           "pass_cmp", "pass_inc", "pass_att", "rush_att",
                           "pass_sack", "fum", "rec_fd", "rush_fd", "pass_fd",
                           "bonus_pass_yd_300", "bonus_pass_yd_400",
                           "bonus_rush_yd_100", "bonus_rush_yd_200",
                           "bonus_rec_yd_100", "bonus_rec_yd_200"}


def league_scoring(league_id):
    """scoring_settings for the league (1 day TTL); HALF_PPR on failure."""
    if not league_id or not common.enabled("league.scoring"):
        return dict(HALF_PPR)
    try:
        lg = common.fetch_json("https://api.sleeper.app/v1/league/%s" % league_id,
                               "slp-league-%s" % league_id, 86400, feed="league.scoring")
        sc = lg.get("scoring_settings") or {}
        if not sc:
            raise ValueError("league has no scoring_settings")
        out = {k: float(v) for k, v in sc.items()
               if k in OFFENSE and isinstance(v, (int, float))}
        common.ok("league.scoring", rows=len(out), rec=out.get("rec"))
        return out
    except Exception as e:
        common.fail("league.scoring", e)
        return dict(HALF_PPR)


def te_stats(stats, pos):
    """Add the position bonus keys so TE/RB/WR premiums apply."""
    if not stats or "rec" not in stats:
        return stats
    s = dict(stats)
    b = {"TE": "bonus_rec_te", "RB": "bonus_rec_rb", "WR": "bonus_rec_wr"}.get(pos)
    if b and b not in s:
        s[b] = s["rec"]
    return s


def points(stats, sc=None, pos=None):
    """Dot product of a canonical stat line with scoring settings."""
    sc = sc or HALF_PPR
    if pos:
        stats = te_stats(stats, pos)
    t = 0.0
    for k, v in (stats or {}).items():
        c = sc.get(k)
        if c and isinstance(v, (int, float)):
            t += c * v
    return round(t, 2)


def rec_value(sc=None):
    return (sc or HALF_PPR).get("rec", 0.5)
