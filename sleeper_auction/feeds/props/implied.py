"""Market prices -> expected stat lines -> fantasy points and a distribution.

Python 3.9, stdlib only. The maths is appendix A of
docs/data-integration-plan.md:

  * American odds -> probability, and de-vig of an over/under pair.
  * Kalshi ladder (P(X >= k) at several k) -> mean by integrating the
    survival curve (trapezoids plus an exponential tail fitted on the last two
    rungs), and median/p10/p90 by inverting it. PAVA enforces a
    non-increasing curve first.
  * Over/under line L with de-vigged p_over -> median L + sigma * z(p_over),
    sigma = CV * L, mean = median * MEAN_OVER_MEDIAN (yardage is skewed).
  * Anytime TD p -> Poisson lambda = -ln(1 - p). Pass-TD O/U -> lambda by
    bisection on P(N >= ceil(L)) = p_over.
  * Fantasy mean = sum(coef * E[stat]) by linearity of expectation, whatever
    the correlation. Components without a market come from filler_stats.

MEAN_OVER_MEDIAN and CV below come from `python3 -m
sleeper_auction.feeds.props.implied --fit` (see _fit()).
"""
import math
import os
import random
import sys
from statistics import NormalDist

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))))

# Fitted 2026-09-27 from nflverse stats_player_week_2024 + _2025, regular
# season, player-seasons with >= 8 qualifying games (QB >= 15 att; RB >= 8
# carries for rush_yd; >= 3 targets for rec/rec_yd): median across players of
# weekly mean/median and of weekly sd/mean. Re-run --fit to refresh.
# n player-seasons: pass 66, rush 80, receiving 302.
MEAN_OVER_MEDIAN = {"pass_yd": 1.009, "rush_yd": 1.063, "rec_yd": 1.096, "rec": 1.064,
                    "pass_att": 1.015, "pass_cmp": 1.003, "rush_att": 1.026}
CV = {"pass_yd": 0.273, "rush_yd": 0.451, "rec_yd": 0.567, "rec": 0.423,
      "pass_att": 0.220, "pass_cmp": 0.238, "rush_att": 0.260}
VIG_ONE_SIDED = 0.07   # haircut for a one-sided "Yes" price until calibration
RHO = 0.5              # yards <-> TD correlation for the floor/ceiling sampler
DRAWS = 2000

_N = NormalDist()
# market key -> the Sleeper stat key(s) it prices
STAT_OF = {"pass_yd": "pass_yd", "pass_td": "pass_td", "pass_int": "pass_int",
           "pass_att": "pass_att", "pass_cmp": "pass_cmp", "rush_yd": "rush_yd",
           "rush_att": "rush_att", "rec": "rec", "rec_yd": "rec_yd"}
TD_KEYS = ("anytime_td", "td")


def american_to_prob(o):
    o = float(o)
    return 100.0 / (o + 100.0) if o > 0 else -o / (-o + 100.0)


def devig(p_over, p_under):
    s = p_over + p_under
    return p_over / s if s > 0 else None


def one_sided(p_yes):
    return p_yes * (1.0 - VIG_ONE_SIDED)


def pava_decreasing(vals):
    try:
        from sleeper_auction.valuation import _pava_decreasing
        return _pava_decreasing(vals)
    except Exception:                      # same algorithm, inline
        out = []
        for v in vals:
            out.append([float(v), 1])
            while len(out) > 1 and out[-2][0] < out[-1][0]:
                m2, c2 = out.pop()
                m1, c1 = out.pop()
                out.append([(m1 * c1 + m2 * c2) / float(c1 + c2), c1 + c2])
        res = []
        for m, c in out:
            res.extend([m] * c)
        return res

# ---------------------------------------------------------------- ladders

def clean_ladder(ladder):
    """[(k, P(X>=k))] -> sorted, de-duplicated, PAVA non-increasing, in (0,1)."""
    by = {}
    for k, p in ladder:
        if p is None:
            continue
        by.setdefault(float(k), []).append(float(p))
    ks = sorted(by)
    ps = pava_decreasing([sum(by[k]) / len(by[k]) for k in ks])
    return [(k, min(0.999, max(0.001, p))) for k, p in zip(ks, ps)]


def _surv(lad, lam, x):
    """S(x) with S(0)=1, linear between rungs, exponential beyond the last."""
    if x <= 0:
        return 1.0
    pk, pp = 0.0, 1.0
    for k, p in lad:
        if x <= k:
            return pp + (p - pp) * (x - pk) / (k - pk) if k > pk else p
        pk, pp = k, p
    return pp * math.exp(-lam * (x - pk))


def _tail_rate(lad):
    if not lad:
        return 1.0
    if len(lad) >= 2:
        (k1, p1), (k2, p2) = lad[-2], lad[-1]
        if p2 < p1 and k2 > k1:
            return math.log(p1 / p2) / (k2 - k1)
    k, p = lad[-1]
    return max(1e-3, -math.log(max(p, 1e-3)) / max(k, 1.0))


def _inv(lad, lam, q):
    """Smallest x with S(x) <= q."""
    pk, pp = 0.0, 1.0
    for k, p in lad:
        if p <= q:
            return pk + (k - pk) * (pp - q) / (pp - p) if pp > p else k
        pk, pp = k, p
    return pk + math.log(pp / q) / lam if q < pp else pk


def expected_from_ladder(ladder, discrete=True):
    """Ladder [(N, P(X >= N))] -> mean/median/p10/p90.

    For integer stats (yards, catches, TDs) P(X >= N) = P(X > N - 0.5), so the
    survival curve is integrated at N - 0.5 (a continuity correction; Kalshi's
    own floor_strike is exactly that, 69.5 for "70+").
    """
    if discrete:
        ladder = [(k - 0.5, p) for k, p in ladder]
    lad = clean_ladder(ladder)
    if len(lad) < 2:
        return None
    lam = _tail_rate(lad)
    area, pk, pp = 0.0, 0.0, 1.0
    for k, p in lad:
        area += (k - pk) * (pp + p) / 2.0
        pk, pp = k, p
    area += pp / lam
    return {"mean": round(area, 2), "median": round(_inv(lad, lam, 0.5), 2),
            "p10": round(_inv(lad, lam, 0.9), 2), "p90": round(_inv(lad, lam, 0.1), 2),
            "rungs": len(lad)}


def expected_count_from_ladder(ladder):
    """Discrete counts (TDs, receptions) where rungs are N+: E = sum P(X>=k)."""
    lad = clean_ladder(ladder)
    if not lad:
        return None
    ks = [k for k, _ in lad]
    if ks != [float(i) for i in range(1, len(ks) + 1)]:
        return None
    return {"mean": round(sum(p for _, p in lad), 3), "p_any": lad[0][1], "rungs": len(lad)}

# ---------------------------------------------------------------- O/U

def expected_from_ou(market, line, p_over):
    cv = CV.get(market, 0.5)
    sigma = cv * float(line)
    z = _N.inv_cdf(min(0.999, max(0.001, p_over)))
    med = float(line) + sigma * z
    mean = med * MEAN_OVER_MEDIAN.get(market, 1.0)
    # log-normal with this median and CV for the floor/ceiling only
    s = math.sqrt(math.log(1 + cv * cv))
    return {"mean": round(mean, 2), "median": round(med, 2),
            "p10": round(med * math.exp(s * _N.inv_cdf(0.1)), 2),
            "p90": round(med * math.exp(s * _N.inv_cdf(0.9)), 2)}


def td_lambda(p_anytime):
    p = min(0.999, max(0.0, p_anytime))
    return -math.log(1.0 - p)


def _pois_ge(n, lam):
    """P(N >= n) for Poisson(lam)."""
    if n <= 0:
        return 1.0
    term = cdf = math.exp(-lam)
    for i in range(1, n):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def lambda_from_ou(line, p_over, lo=0.0, hi=6.0):
    """Poisson rate with P(N >= ceil(line)) = p_over, by bisection."""
    n = int(math.ceil(float(line)))
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _pois_ge(n, mid) < p_over:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)

# ---------------------------------------------------------------- fantasy

def fantasy_from_markets(markets, filler_stats, scoring, pos=None, seed=7):
    """Expected fantasy points and an approximate p10/p50/p90.

    markets: {key: {"mean", ... optional "ladder"/"line"/"p_over"/"dist"}}
      with keys among STAT_OF plus "td" (expected rush+rec TDs) or
      "anytime_td" ({"p": probability}).
    filler_stats: the projected stat line (Sleeper keys) for everything
      without a market.
    """
    sc = scoring or {}
    filler = dict(filler_stats or {})
    comps, mkt_pts, total = {}, 0.0, 0.0
    exp = {}
    for mk, stat in STAT_OF.items():
        m = markets.get(mk)
        if m and m.get("mean") is not None:
            exp[stat] = (m["mean"], True)
    lam = None
    if markets.get("td") and markets["td"].get("mean") is not None:
        lam = markets["td"]["mean"]
    elif markets.get("anytime_td") and markets["anytime_td"].get("p") is not None:
        lam = td_lambda(markets["anytime_td"]["p"])
    if lam is not None:
        # Split the combined TD rate by the filler's rush/rec TD mix.
        rt, ct = float(filler.get("rush_td") or 0), float(filler.get("rec_td") or 0)
        share = rt / (rt + ct) if rt + ct > 0 else (0.7 if pos == "RB" else 0.0)
        exp["rush_td"] = (lam * share, True)
        exp["rec_td"] = (lam * (1 - share), True)
    keys = set(k for k in filler if isinstance(filler.get(k), (int, float))) | set(exp)
    for k in keys:
        c = sc.get(k)
        if not c:
            continue
        v, from_mkt = exp.get(k, (filler.get(k), False))
        if not isinstance(v, (int, float)):
            continue
        pts = c * v
        comps[k] = {"exp": round(v, 2), "pts": round(pts, 2), "market": from_mkt}
        total += pts
        if from_mkt:
            mkt_pts += pts
    if pos == "TE" and sc.get("bonus_rec_te"):
        v, fm = exp.get("rec", (filler.get("rec") or 0, False))
        total += sc["bonus_rec_te"] * v
        if fm:
            mkt_pts += sc["bonus_rec_te"] * v
    coverage = round(mkt_pts / total, 2) if total > 0 else 0.0
    dist = _simulate(markets, comps, sc, lam, seed)
    out = {"mean": round(total, 2), "coverage": max(0.0, min(1.0, coverage)),
           "components": comps}
    out.update(dist)
    return out


def _simulate(markets, comps, sc, lam, seed):
    """Monte Carlo floor/ceiling: yards by inverse survival (or log-normal for an
    O/U), TDs as Poisson, a Gaussian copula at RHO between them. Approximate;
    for display only."""
    rng = random.Random(seed)
    yard_keys = [k for k in STAT_OF if markets.get(k) and k in comps]
    if not yard_keys and lam is None:
        return {"p10": None, "p50": None, "p90": None}
    fixed = sum(c["pts"] for k, c in comps.items()
                if k not in yard_keys and k not in ("rush_td", "rec_td"))
    td_coef = None
    if lam is not None:
        a, b = comps.get("rush_td"), comps.get("rec_td")
        e = (a or {}).get("exp", 0) + (b or {}).get("exp", 0)
        td_coef = ((a or {}).get("pts", 0) + (b or {}).get("pts", 0)) / e if e else 6.0
    draws = []
    for _ in range(DRAWS):
        z1 = rng.gauss(0, 1)
        z2 = RHO * z1 + math.sqrt(1 - RHO * RHO) * rng.gauss(0, 1)
        u1, u2 = _N.cdf(z1), _N.cdf(z2)
        pts = fixed
        for k in yard_keys:
            m = markets[k]
            c = sc.get(STAT_OF[k]) or 0
            if m.get("ladder"):
                lad = clean_ladder(m["ladder"])
                x = _inv(lad, _tail_rate(lad), max(1e-4, 1 - u1)) if len(lad) >= 2 else m["mean"]
            else:
                cv = CV.get(k, 0.5)
                s = math.sqrt(math.log(1 + cv * cv))
                x = (m.get("median") or m["mean"]) * math.exp(s * _N.inv_cdf(min(0.9999, max(1e-4, u1))))
            pts += c * x
        if lam is not None:
            n, cdf, term = 0, math.exp(-lam), math.exp(-lam)
            while cdf < u2 and n < 8:
                n += 1
                term *= lam / n
                cdf += term
            pts += td_coef * n
        draws.append(pts)
    draws.sort()
    q = lambda f: round(draws[int(f * (len(draws) - 1))], 1)   # noqa: E731
    return {"p10": q(0.1), "p50": q(0.5), "p90": q(0.9)}

# ---------------------------------------------------------------- fit

def _fit():
    """Print MEAN_OVER_MEDIAN and CV per market from 2024-2025 weekly stats."""
    import statistics
    from sleeper_auction.feeds import nflverse
    spec = {"pass_yd": ("passing_yards", "attempts", 15, ("QB",)),
            "pass_att": ("attempts", "attempts", 15, ("QB",)),
            "pass_cmp": ("completions", "attempts", 15, ("QB",)),
            "rush_yd": ("rushing_yards", "carries", 8, ("RB",)),
            "rush_att": ("carries", "carries", 8, ("RB",)),
            "rec_yd": ("receiving_yards", "targets", 3, ("WR", "TE", "RB")),
            "rec": ("receptions", "targets", 3, ("WR", "TE", "RB"))}
    per = {k: {} for k in spec}
    for season in ("2024", "2025"):
        for r in nflverse.player_weeks(season):
            if r.get("season_type") not in (None, "", "REG"):
                continue
            for k, (col, gate, mn, poss) in spec.items():
                if r.get("position") in poss and (nflverse.common.num(r.get(gate)) or 0) >= mn:
                    per[k].setdefault((season, r["player_id"]), []).append(
                        nflverse.common.num(r.get(col)) or 0.0)
    for k, d in per.items():
        mom, cv = [], []
        for vals in d.values():
            if len(vals) < 8:
                continue
            m, md = statistics.mean(vals), statistics.median(vals)
            if md > 0 and m > 0:
                mom.append(m / md)
                cv.append(statistics.pstdev(vals) / m)
        print("%-9s n=%4d  MEAN_OVER_MEDIAN %.3f  CV %.3f" % (
            k, len(mom), statistics.median(mom) if mom else float("nan"),
            statistics.median(cv) if cv else float("nan")))


if __name__ == "__main__":
    if "--fit" in sys.argv:
        _fit()
    else:
        print(__doc__)
