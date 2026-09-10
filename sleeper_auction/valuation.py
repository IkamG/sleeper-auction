"""Auction valuation engine for half-PPR Sleeper auction drafts.

Pure / importable / no network. Python 3.9 compatible, stdlib only.

Public contract (server.py + UI are written against this):

    compute_base_values(players, league) -> list          # same list, dicts enriched in place
    compute_live(players, league, draft)  -> dict
    positional_scarcity(players, league, drafted_keys) -> list

The money math, in one paragraph, because every mistake in auction valuation
comes from getting one of these steps subtly wrong:

  1. Every player gets a half-PPR point projection (real if we have one,
     otherwise read off a per-position points-vs-rank curve fitted to the
     players at that position that DO have projections).
  2. Replacement level per position = the player one past the last STARTER at
     that position, where starters = teams * dedicated slots + that position's
     realized share of the FLEX / SUPER_FLEX slots.
  3. VORP = max(0, points - replacement).
  4. Dollars: every one of the teams * roster_size drafted players costs at
     least $1, so only  surplus = teams*budget - teams*roster_size  is actually
     bid over the minimum.  dollars_per_vorp = surplus / (sum of VORP over the
     top teams*roster_size players).  model_value = 1 + vorp * dollars_per_vorp.
     By construction the top teams*roster_size model_values sum to teams*budget.
  5. Market dollars are rescaled the same way FIRST (a $300/14-team source and a
     $100/10-team source cannot be averaged raw), then blended, then the blend
     is renormalized so the board still sums to the pool.
  6. Live inflation is computed on the DOLLARS ABOVE THE $1 MINIMUM, never on
     raw values -- that is the classic bug.

Scoring: half-PPR everywhere. `proj_pts` on the input dicts is assumed to
already be half-PPR (sources that only publish PPR and Standard synthesize
half-PPR as the mean of the two; that happens in the source adapters, not here).
"""

import bisect
import math

try:  # keep the position vocabulary identical to the source adapters
    from sleeper_auction.sources.base import POSITIONS
except Exception:  # pragma: no cover - valuation must import standalone
    POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


# --------------------------------------------------------------------------
# Tunables.  server.py may override any of these at import time.
# --------------------------------------------------------------------------

W_MODEL = 0.55            # weight on VORP model dollars when market exists
W_MARKET = 0.45           # weight on (rescaled) real market auction dollars
TIER_GAP_Z = 0.8          # tier break when a gap > mean + Z*stdev of the gaps
TIER_MIN_GAP = 0.75       # never break on sub-point float noise
MAX_TIER_SIZE = 12        # force a split at the biggest internal gap past this
INFLATION_MIN = 0.30      # clamp: the room cannot really go below/above these
INFLATION_MAX = 3.00
CONF_FULL_SOURCES = 4.0   # sources_count at which the "coverage" term saturates
CONF_SPREAD_SCALE = 0.35  # relative dispersion that halves the agreement term
CLIFF_WITHIN = 3          # tier break inside this many players => cliff flag

DEFAULT_LEAGUE = {
    "teams": 12,
    "budget": 200,
    "scoring": "half_ppr",
    "slots": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 6},
    "flex_pos": ["RB", "WR", "TE"],
}

_FLEX_SLOTS = {"FLEX", "W_R_T", "WRT", "RB_WR_TE", "WRRB_FLEX", "REC_FLEX",
               "WR_RB", "WR_TE", "W_R", "W_T", "FLEX_WRT", "R_W_T_E"}
_SFLEX_SLOTS = {"SUPER_FLEX", "SUPERFLEX", "SF", "QB_RB_WR_TE", "OP", "Q_W_R_T"}
_BENCH_SLOTS = {"BN", "BE", "BENCH"}
_SKIP_SLOTS = {"IR", "TAXI", "RES", "RESERVE", "IR_TAXI"}
_SLOT_POS_FIX = {"DST": "DEF", "D_ST": "DEF", "D": "DEF", "DEFENSE": "DEF", "PK": "K"}

# Fallback shape used only when a position has too few real projections to fit
# anything (points at positional rank, half-PPR, 17 games).  Anchors, not gospel.
_DEFAULT_CURVES = {
    "QB":  [(1, 385), (3, 345), (6, 320), (12, 285), (18, 255), (24, 225), (32, 180)],
    "RB":  [(1, 300), (3, 260), (6, 225), (12, 190), (18, 165), (24, 145),
            (36, 112), (48, 88), (60, 68), (72, 50)],
    "WR":  [(1, 285), (3, 255), (6, 228), (12, 196), (18, 172), (24, 155),
            (36, 128), (48, 106), (60, 88), (72, 72), (96, 48)],
    "TE":  [(1, 225), (3, 175), (6, 140), (12, 105), (18, 82), (24, 66), (32, 48)],
    "K":   [(1, 148), (6, 137), (12, 128), (18, 120), (24, 112), (32, 100)],
    "DEF": [(1, 138), (6, 118), (12, 103), (18, 92), (24, 84), (32, 72)],
}
_GENERIC_CURVE = [(1, 200), (6, 150), (12, 120), (24, 90), (48, 60), (96, 35)]


# --------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------

def _f(v):
    """float(v) or None; NaN is None."""
    if v is None or v is True or v is False:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x or x in (float("inf"), float("-inf")) else x


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _mean(vals):
    vals = list(vals)
    return sum(vals) / float(len(vals)) if vals else 0.0


def _stdev(vals):
    vals = list(vals)
    n = len(vals)
    if n < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / float(n - 1))


def _median(vals):
    v = sorted(vals)
    n = len(v)
    if n == 0:
        return None
    if n % 2:
        return v[n // 2]
    return 0.5 * (v[n // 2 - 1] + v[n // 2])


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------
# League normalization
# --------------------------------------------------------------------------

def _normalize_league(league):
    """Turn a loose league dict into the numbers the math needs."""
    lg = dict(league or DEFAULT_LEAGUE)

    teams = _f(lg.get("teams")) or DEFAULT_LEAGUE["teams"]
    teams = int(max(2, round(teams)))
    budget = _f(lg.get("budget")) or DEFAULT_LEAGUE["budget"]
    budget = float(max(1.0, budget))

    raw = lg.get("slots") or DEFAULT_LEAGUE["slots"]
    base_slots, flex_n, sflex_n, bench_n = {}, 0, 0, 0
    for k, v in dict(raw).items():
        n = _f(v)
        if n is None or n <= 0:
            continue
        n = int(round(n))
        key = "".join(ch if ch.isalnum() else "_" for ch in str(k).strip().upper())
        if key in _SKIP_SLOTS:
            continue
        if key in _BENCH_SLOTS:
            bench_n += n
        elif key in _SFLEX_SLOTS:
            sflex_n += n
        elif key in _FLEX_SLOTS:
            flex_n += n
        else:
            base_slots[_SLOT_POS_FIX.get(key, key)] = \
                base_slots.get(_SLOT_POS_FIX.get(key, key), 0) + n

    flex_pos = [str(p).strip().upper() for p in (lg.get("flex_pos") or
                                                 DEFAULT_LEAGUE["flex_pos"])]
    flex_pos = [_SLOT_POS_FIX.get(p, p) for p in flex_pos] or ["RB", "WR", "TE"]
    sflex_pos = list(flex_pos)
    if "QB" not in sflex_pos:
        sflex_pos = ["QB"] + sflex_pos

    roster_size = sum(base_slots.values()) + flex_n + sflex_n + bench_n
    roster_size = max(1, roster_size)

    total_dollars = teams * budget
    n_rostered = teams * roster_size
    return {
        "teams": teams,
        "budget": budget,
        "scoring": lg.get("scoring") or "half_ppr",
        "base_slots": base_slots,
        "flex_per_team": flex_n,
        "sflex_per_team": sflex_n,
        "bench_per_team": bench_n,
        "flex_total": flex_n * teams,
        "sflex_total": sflex_n * teams,
        "flex_pos": flex_pos,
        "sflex_pos": sflex_pos,
        "roster_size": roster_size,
        "total_dollars": total_dollars,
        "n_rostered": n_rostered,
        # dollars that are actually bid, above the $1-per-body minimum
        "surplus": max(0.0, total_dollars - n_rostered),
    }


# --------------------------------------------------------------------------
# Points-vs-rank curve fitting (fills in players with no real projection)
# --------------------------------------------------------------------------

def _pava_decreasing(vals):
    """Pool-adjacent-violators: nearest non-increasing sequence (L2)."""
    out = []  # list of [mean, count]
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


def _make_interp(xs, ys):
    """Monotone-decreasing interpolator over knots (xs ascending, ys non-increasing).

    Below xs[0]: linear extrapolation on the first segment's slope, capped.
    Above xs[-1]: exponential decay whose rate comes from the tail segment.
    """
    xs = list(xs)
    ys = [max(0.0, float(y)) for y in ys]
    n = len(xs)
    if n == 1:
        y0 = ys[0]
        return lambda q: y0

    head_slope = max(0.0, (ys[0] - ys[1]) / float(xs[1] - xs[0]))
    head_cap = ys[0] * 1.6 + 1.0

    j = max(0, n - 4)
    dx = float(xs[-1] - xs[j])
    y0t, y1t = ys[j], ys[-1]
    if dx > 0 and y1t > 1e-9 and y0t > y1t:
        k_tail = _clamp(math.log(y0t / y1t) / dx, 1e-4, 0.25)
    else:
        k_tail = 1e-3

    def f(q):
        q = float(q)
        if q <= xs[0]:
            return min(head_cap, ys[0] + head_slope * (xs[0] - q))
        if q >= xs[-1]:
            return max(0.0, ys[-1] * math.exp(-k_tail * (q - xs[-1])))
        hi = bisect.bisect_left(xs, q)
        if xs[hi] == q:
            return ys[hi]
        lo = hi - 1
        span = float(xs[hi] - xs[lo])
        t = (q - xs[lo]) / span if span > 0 else 0.0
        return ys[lo] + (ys[hi] - ys[lo]) * t

    return f


def _fit_curve(pairs, pos):
    """pairs: [(positional_rank_index, points|None), ...] ordered by consensus rank."""
    known = [(int(i), float(v)) for i, v in pairs if v is not None and v > 0]
    if len(known) >= 3:
        known.sort(key=lambda t: t[0])
        xs = [i for i, _ in known]
        ys = _pava_decreasing([v for _, v in known])
        return _make_interp(xs, ys), "fit"
    anchors = _DEFAULT_CURVES.get(pos) or _GENERIC_CURVE
    fn = _make_interp([a for a, _ in anchors], [b for _, b in anchors])
    if known:  # anchor the default curve to the one/two real projections we have
        scales = [v / fn(i) for i, v in known if fn(i) > 1e-9]
        s = _median(scales) or 1.0
        s = _clamp(s, 0.4, 2.5)
        return (lambda q: fn(q) * s), "default_scaled"
    return fn, "default"


def _consensus_order(lst):
    """Order players within a position by consensus rank, then ADP, then input order."""
    dec = []
    for i, p in enumerate(lst):
        r = _f(p.get("consensus_rank"))
        if r is None:
            r = _f(p.get("adp"))
        # projections are a last-resort ordering signal so the axis is never random
        if r is None:
            pp = _f(p.get("proj_pts"))
            dec.append((2, -(pp if pp is not None else 0.0), i, p))
        else:
            dec.append((0, r, i, p))
    dec.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in dec]


def _project_points(players_by_pos):
    """Fill proj_pts_final / proj_source for every player, per position."""
    for pos, lst in players_by_pos.items():
        ordered = _consensus_order(lst)
        pairs = []
        for idx, p in enumerate(ordered, start=1):
            pp = _f(p.get("proj_pts"))
            pairs.append((idx, pp if (pp is not None and pp > 0) else None))
        curve, kind = _fit_curve(pairs, pos)
        for idx, p in enumerate(ordered, start=1):
            pp = _f(p.get("proj_pts"))
            if pp is not None and pp > 0:
                p["proj_pts_final"] = round(pp, 1)
                p["proj_source"] = "projection"
            else:
                est = max(0.0, curve(idx))
                p["proj_pts_final"] = round(est, 1)
                p["proj_source"] = "curve" if kind == "fit" else kind


# --------------------------------------------------------------------------
# Replacement level
# --------------------------------------------------------------------------

def _group_sorted(players):
    """{pos: [players sorted by proj_pts_final desc]}"""
    by_pos = {}
    for p in players:
        by_pos.setdefault(p.get("pos") or "UNK", []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda q: (-(q.get("proj_pts_final") or 0.0),
                                        _f(q.get("consensus_rank")) or 9999.0,
                                        str(q.get("name") or "")))
    return by_pos


def _allocate_starters(by_pos_sorted, cfg):
    """Dedicated slots + realized FLEX / SUPER_FLEX share, per position, league-wide.

    Flex slots are handed to whichever positions the best flex-eligible players
    who are NOT already dedicated starters actually come from -- not split evenly.
    """
    starters = {}
    for pos in by_pos_sorted:
        starters[pos] = cfg["base_slots"].get(pos, 0) * cfg["teams"]
    for pos, per_team in cfg["base_slots"].items():
        starters.setdefault(pos, per_team * cfg["teams"])

    for n_slots, elig in ((cfg["flex_total"], cfg["flex_pos"]),
                          (cfg["sflex_total"], cfg["sflex_pos"])):
        if n_slots <= 0:
            continue
        pool = []
        for pos in elig:
            lst = by_pos_sorted.get(pos) or []
            for i in range(min(starters.get(pos, 0), len(lst)), len(lst)):
                pool.append((lst[i].get("proj_pts_final") or 0.0, pos))
        pool.sort(key=lambda t: -t[0])
        for _pts, pos in pool[:n_slots]:
            starters[pos] = starters.get(pos, 0) + 1
    return starters


def _replacement_levels(by_pos_sorted, starters):
    """Points of the player one past the last starter (guarded for short pools)."""
    repl = {}
    for pos, lst in by_pos_sorted.items():
        if not lst:
            repl[pos] = 0.0
            continue
        idx = starters.get(pos, 0)  # 0-based index of the first non-starter
        if idx >= len(lst):
            idx = len(lst) - 1      # pool is short: use the last player we know of
        repl[pos] = float(lst[idx].get("proj_pts_final") or 0.0)
    return repl


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------

def _assign_tiers(lst, starters_n):
    """Cut tiers at genuine gaps in proj_pts_final, not at fixed sizes."""
    n = len(lst)
    if n == 0:
        return
    if n == 1:
        lst[0]["tier"] = 1
        return
    pts = [float(p.get("proj_pts_final") or 0.0) for p in lst]
    gaps = [pts[i] - pts[i + 1] for i in range(n - 1)]

    # Gap statistics come from the fantasy-relevant top of the position; the long
    # replacement-level tail has near-zero gaps and would flatten the threshold.
    win = min(len(gaps), max(18, 2 * int(starters_n or 0) + 6))
    sample = gaps[:win]
    thr = max(TIER_MIN_GAP, _mean(sample) + TIER_GAP_Z * _stdev(sample))

    bounds = [0] + [i + 1 for i, g in enumerate(gaps) if g > thr] + [n]
    bounds = sorted(set(bounds))

    # Cap runaway tiers by splitting at their largest internal gap.
    changed = True
    while changed:
        changed = False
        for bi in range(len(bounds) - 1):
            a, b = bounds[bi], bounds[bi + 1]
            if b - a > MAX_TIER_SIZE:
                seg = [(gaps[i], i) for i in range(a, b - 1)]
                if not seg:
                    continue
                _g, at = max(seg, key=lambda t: (t[0], -t[1]))
                bounds.append(at + 1)
                bounds = sorted(set(bounds))
                changed = True
                break

    tier = 0
    for bi in range(len(bounds) - 1):
        tier += 1
        for i in range(bounds[bi], bounds[bi + 1]):
            lst[i]["tier"] = tier


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def _source_scales(players):
    """Per-source multiplier that puts every auction source on one budget scale.

    Sources publish dollars for whatever league size/budget they assume; comparing
    a player's ESPN $ to their Yahoo $ raw would read scale differences as
    disagreement.  Normalize each source's whole column to the median column
    total first, then dispersion means something.
    """
    tot = {}
    for p in players:
        for s, v in (p.get("auction_by_source") or {}).items():
            v = _f(v)
            if v is not None and v > 0:
                tot[s] = tot.get(s, 0.0) + v
    if not tot:
        return {}
    med = _median(list(tot.values())) or 1.0
    return {s: (med / t if t > 0 else 1.0) for s, t in tot.items()}


def _value_conf(p, scales):
    auc = p.get("auction_by_source") or {}
    adps = p.get("adp_by_source") or {}

    n = _f(p.get("sources_count"))
    if n is None:
        n = float(len(set(list(auc.keys()) + list(adps.keys()))))
    c_n = _clamp((n or 0.0) / CONF_FULL_SOURCES, 0.0, 1.0)

    parts = [(0.40, c_n)]

    vals = [scales.get(s, 1.0) * v for s, v in
            ((s, _f(v)) for s, v in auc.items()) if v is not None and v > 0]
    if len(vals) >= 2:
        cv = _stdev(vals) / max(1.0, _mean(vals))
        parts.append((0.35, 1.0 / (1.0 + cv / CONF_SPREAD_SCALE)))

    rel = None
    av = [v for v in (_f(x) for x in adps.values()) if v is not None]
    if len(av) >= 2:
        rel = _stdev(av) / (_mean(av) + 10.0)
    else:
        sd, ad = _f(p.get("adp_stdev")), _f(p.get("adp"))
        if sd is not None and ad is not None:
            rel = sd / (ad + 10.0)
    if rel is not None:
        parts.append((0.25, 1.0 / (1.0 + rel / CONF_SPREAD_SCALE)))

    tw = sum(w for w, _ in parts)
    conf = sum(w * v for w, v in parts) / tw if tw else 0.0
    if len(parts) == 1:
        conf *= 0.80  # agreement is unmeasurable with one source: discount it
    return round(_clamp(conf, 0.05, 1.0), 3)


# --------------------------------------------------------------------------
# Dollar scaling helpers
# --------------------------------------------------------------------------

def _scale_to_pool(values, cfg, n_slots=None):
    """Scale the dollars-ABOVE-$1 of a column so its top-N sums to the pool.

    `values` is a list of raw dollars.  Returns the multiplier k such that
    1 + (v-1)*k over the top n_rostered entries sums to teams*budget.  Players a
    column does not price implicitly go for $1, so the surplus target is the
    same for every column: teams*budget - teams*roster_size.
    """
    n = cfg["n_rostered"] if n_slots is None else n_slots
    top = sorted((v for v in values if v is not None), reverse=True)[:n]
    excess = sum(max(0.0, v - 1.0) for v in top)
    if excess <= 1e-9 or cfg["surplus"] <= 0:
        return 0.0
    return cfg["surplus"] / excess


# --------------------------------------------------------------------------
# 1) BASE VALUES
# --------------------------------------------------------------------------

def compute_base_values(players, league=None):
    """Enrich each player dict in place with the full pre-draft valuation.

    Adds: proj_pts_final, proj_source, pos_rank, replacement, vorp, model_value,
          market_value, base_value, tier, value_conf.
    Returns the same list object.
    """
    cfg = _normalize_league(league)
    if not players:
        return players

    # -- points -------------------------------------------------------------
    raw_by_pos = {}
    for p in players:
        raw_by_pos.setdefault(p.get("pos") or "UNK", []).append(p)
    _project_points(raw_by_pos)

    by_pos = _group_sorted(players)
    for pos, lst in by_pos.items():
        for i, p in enumerate(lst, start=1):
            p["pos_rank"] = i

    # -- replacement / VORP -------------------------------------------------
    starters = _allocate_starters(by_pos, cfg)
    repl = _replacement_levels(by_pos, starters)
    for pos, lst in by_pos.items():
        r = repl.get(pos, 0.0)
        for p in lst:
            p["replacement"] = round(r, 1)
            p["vorp"] = round(max(0.0, (p.get("proj_pts_final") or 0.0) - r), 1)

    # -- model dollars ------------------------------------------------------
    ordered = sorted(players, key=lambda q: -(q.get("vorp") or 0.0))
    top = ordered[:cfg["n_rostered"]]
    sum_vorp = sum(p.get("vorp") or 0.0 for p in top)
    dpv = (cfg["surplus"] / sum_vorp) if sum_vorp > 1e-9 else 0.0
    for p in players:
        p["model_value"] = _r(1.0 + (p.get("vorp") or 0.0) * dpv)

    # -- tiers --------------------------------------------------------------
    for pos, lst in by_pos.items():
        _assign_tiers(lst, starters.get(pos, 0))

    # -- market column, rescaled to OUR pool before it is allowed to blend ---
    mk_raw = [_f(p.get("market_auction")) for p in players]
    k_mk = _scale_to_pool([v for v in mk_raw if v is not None and v > 0], cfg)
    have_market = k_mk > 0
    for p, m in zip(players, mk_raw):
        if have_market and m is not None and m > 0:
            p["market_value"] = _r(1.0 + max(0.0, m - 1.0) * k_mk)
        else:
            p["market_value"] = None

    # -- blend, then renormalize so the board still sums to the pool ---------
    blended = []
    for p in players:
        mv = p["model_value"]
        kv = p["market_value"]
        if kv is None:
            b = mv
            p["blend_weights"] = (1.0, 0.0)
        else:
            wm, wk = float(W_MODEL), float(W_MARKET)
            tw = wm + wk
            if tw <= 0:
                wm, wk, tw = 1.0, 0.0, 1.0
            b = (wm * mv + wk * kv) / tw
            p["blend_weights"] = (round(wm / tw, 3), round(wk / tw, 3))
        blended.append(b)

    k_b = _scale_to_pool(blended, cfg)
    for p, b in zip(players, blended):
        p["base_value"] = _r(max(1.0, 1.0 + max(0.0, b - 1.0) * k_b))

    # -- confidence ---------------------------------------------------------
    scales = _source_scales(players)
    for p in players:
        p["value_conf"] = _value_conf(p, scales)

    return players


def valuation_meta(players, league=None):
    """Diagnostics for the same math compute_base_values ran (no mutation)."""
    cfg = _normalize_league(league)
    by_pos = _group_sorted(players)
    starters = _allocate_starters(by_pos, cfg)
    return {
        "league": cfg,
        "starters": starters,
        "replacement": _replacement_levels(by_pos, starters),
        "pos_counts": dict((k, len(v)) for k, v in by_pos.items()),
    }


# --------------------------------------------------------------------------
# 2) LIVE LAYER
# --------------------------------------------------------------------------

def _ensure_valued(players, league):
    for p in players:
        if p.get("base_value") is None:
            return compute_base_values(players, league)
    return players


def _drafted_set(draft, players):
    """Keys AND sleeper ids that are off the board (picks are authoritative)."""
    draft = draft or {}
    out = set()
    for k in (draft.get("drafted_keys") or []):
        if k:
            out.add(str(k))
    for i in (draft.get("drafted_ids") or []):
        if i is not None:
            out.add(str(i))
    for pk in (draft.get("picks") or []):
        for fld in ("key", "player_id"):
            v = pk.get(fld)
            if v:
                out.add(str(v))
    return out


def _is_drafted(p, drafted):
    if str(p.get("key") or "") in drafted:
        return True
    sid = p.get("sleeper_id")
    return bool(sid) and str(sid) in drafted


def _team_rows(cfg, draft, n_drafted, spent_total):
    """Normalize team rows; synthesize them if the caller has none."""
    rows = []
    for t in (draft.get("teams") or []):
        left = _f(t.get("left"))
        spent = _f(t.get("spent"))
        if left is None:
            left = cfg["budget"] - (spent or 0.0)
        if spent is None:
            spent = cfg["budget"] - left
        sl = _f(t.get("slots_left"))
        if sl is None:
            sl = cfg["roster_size"] - _f(t.get("filled") or 0) if t.get("filled") is not None \
                else cfg["roster_size"]
        sl = int(max(0, round(sl)))
        left = max(0.0, left)
        mb = left - max(0, sl - 1)  # $1 must be reserved for every OTHER open slot
        rows.append({
            "roster_id": t.get("roster_id"),
            "owner": t.get("owner"),
            "left": _r(left),
            "spent": _r(max(0.0, spent)),
            "slots_left": sl,
            "max_bid": int(max(0, math.floor(mb))) if sl > 0 else 0,
        })
    if rows:
        return rows, False
    # No team rows: fall back to league-wide aggregates so inflation still works.
    n = cfg["teams"]
    per_slots = max(0, cfg["n_rostered"] - int(n_drafted))
    per_money = max(0.0, cfg["total_dollars"] - float(spent_total))
    for i in range(n):
        sl = per_slots // n + (1 if i < per_slots % n else 0)
        lf = per_money / float(n)
        mb = lf - max(0, sl - 1)
        rows.append({
            "roster_id": i + 1, "owner": None,
            "left": _r(lf), "spent": _r(cfg["budget"] - lf),
            "slots_left": int(sl),
            "max_bid": int(max(0, math.floor(mb))) if sl > 0 else 0,
        })
    return rows, True


def compute_live(players, league=None, draft=None):
    """Inflation-adjusted live board.

    {"players", "inflation", "market", "teams", "scarcity", ...}
    """
    cfg = _normalize_league(league)
    draft = dict(draft or {})
    players = _ensure_valued(list(players), league)

    drafted = _drafted_set(draft, players)
    picks = list(draft.get("picks") or [])

    by_key, by_id = {}, {}
    for p in players:
        if p.get("key"):
            by_key.setdefault(str(p["key"]), p)
        if p.get("sleeper_id"):
            by_id.setdefault(str(p["sleeper_id"]), p)

    # ---- what the room has actually paid, per position ---------------------
    market, spent_total, price_by_player = {}, 0.0, {}
    for pk in picks:
        amt = _f(pk.get("amount"))
        pl = by_key.get(str(pk.get("key") or "")) or by_id.get(str(pk.get("player_id") or ""))
        pos = pk.get("pos") or (pl.get("pos") if pl else None) or "UNK"
        d = market.setdefault(pos, {"spent": 0.0, "expected": 0.0, "n": 0,
                                    "spent_total": 0.0, "n_total": 0})
        d["n_total"] += 1
        if amt is not None:
            spent_total += amt
            d["spent_total"] += amt
            if pl is not None:
                price_by_player[id(pl)] = amt
        exp = pl.get("base_value") if pl else None
        if amt is not None and exp is not None:
            # only compare picks we can actually price, so the ratio is honest
            d["spent"] += amt
            d["expected"] += exp
            d["n"] += 1
    for pos, d in market.items():
        d["ratio"] = _r(d["spent"] / d["expected"], 3) if d["expected"] > 1e-9 else None
        for f in ("spent", "expected", "spent_total"):
            d[f] = _r(d[f])
    mo_spent = sum((market[p]["spent"] or 0.0) for p in market)
    mo_exp = sum((market[p]["expected"] or 0.0) for p in market)
    market_overall = {
        "spent": _r(mo_spent), "expected": _r(mo_exp),
        "ratio": _r(mo_spent / mo_exp, 3) if mo_exp > 1e-9 else None,
        "n": sum(market[p]["n"] for p in market),
        "n_total": sum(market[p]["n_total"] for p in market),
    }

    n_drafted = len([p for p in players if _is_drafted(p, drafted)]) or len(picks)
    teams_rows, synthesized = _team_rows(cfg, draft, n_drafted, spent_total)

    # ---- inflation, on the dollars ABOVE the $1 minimum ---------------------
    slots_left_total = int(sum(t["slots_left"] for t in teams_rows))
    money_left = float(sum(t["left"] or 0.0 for t in teams_rows))
    discretionary_left = money_left - slots_left_total

    undrafted = [p for p in players if not _is_drafted(p, drafted)]
    undrafted.sort(key=lambda q: -(q.get("base_value") or 0.0))
    remaining_pool = undrafted[:max(0, slots_left_total)]
    expected_left = sum(max(0.0, (p.get("base_value") or 0.0) - 1.0) for p in remaining_pool)

    if expected_left > 1e-6 and slots_left_total > 0:
        inflation = _clamp(discretionary_left / expected_left, INFLATION_MIN, INFLATION_MAX)
    else:
        inflation = 1.0

    # ---- per-player live numbers -------------------------------------------
    my_id = draft.get("my_roster_id")
    me = None
    if my_id is not None:
        for t in teams_rows:
            if t["roster_id"] is not None and str(t["roster_id"]) == str(my_id):
                me = t
                break
    my_max = float(me["max_bid"]) if me else float(max([t["max_bid"] for t in teams_rows] or [0]))

    for p in players:
        bv = p.get("base_value") or 1.0
        adj = 1.0 + max(0.0, bv - 1.0) * inflation
        p["adj_value"] = _r(adj)
        d = _is_drafted(p, drafted)
        p["drafted"] = d
        p["draft_amount"] = _r(price_by_player.get(id(p)))
        p["max_bid_me"] = 0 if d else int(max(0, math.floor(my_max)))
        ref = p.get("market_value")
        if ref is None:
            ref = bv  # no market column for this player: edge is the pure inflation delta
        p["edge"] = _r(adj - ref)

    board = sorted(players, key=lambda q: (bool(q.get("drafted")),
                                           -(q.get("adj_value") or 0.0)))

    return {
        "players": board,
        "inflation": round(float(inflation), 4),
        "market": market,
        "market_overall": market_overall,
        "teams": teams_rows,
        "scarcity": positional_scarcity(players, league, drafted),
        "slots_left_total": slots_left_total,
        "money_left": _r(money_left),
        "discretionary_left": _r(discretionary_left),
        "expected_left": _r(expected_left),
        "spent_total": _r(spent_total),
        "picks_count": len(picks),
        "drafted_count": n_drafted,
        "my_max_bid": int(max(0, math.floor(my_max))),
        "teams_synthesized": synthesized,
    }


# --------------------------------------------------------------------------
# 3) SCARCITY  (drives nomination advice)
# --------------------------------------------------------------------------

def positional_scarcity(players, league=None, drafted_keys=None):
    cfg = _normalize_league(league)
    players = _ensure_valued(list(players), league)
    drafted = set(str(x) for x in (drafted_keys or []) if x is not None)

    by_pos = _group_sorted(players)
    starters = _allocate_starters(by_pos, cfg)
    repl = _replacement_levels(by_pos, starters)

    out = []
    for pos in list(POSITIONS) + sorted(k for k in by_pos if k not in POSITIONS):
        lst = by_pos.get(pos)
        if not lst:
            continue
        r = repl.get(pos, 0.0)
        avail = [p for p in lst if not _is_drafted(p, drafted)]
        gone = len(lst) - len(avail)

        above = sum(1 for p in avail if (p.get("proj_pts_final") or 0.0) > r + 1e-9)
        slots_left = max(0, starters.get(pos, 0) - gone)

        t1 = sum(1 for p in avail if p.get("tier") == 1)
        t2 = sum(1 for p in avail if p.get("tier") == 2)

        cur_tier, break_in = None, None
        if avail:
            cur_tier = avail[0].get("tier")
            break_in = len(avail)
            for i, p in enumerate(avail):
                if p.get("tier") != cur_tier:
                    break_in = i
                    break
        cliff = bool(avail) and break_in is not None and 0 < break_in <= CLIFF_WITHIN \
            and break_in < len(avail)

        out.append({
            "pos": pos,
            "avail": len(avail),
            "drafted": gone,
            "replacement": _r(r, 1),
            "above_replacement": above,
            "starting_slots_left": slots_left,
            "starters_total": starters.get(pos, 0),
            "supply_ratio": _r(above / float(slots_left), 2) if slots_left > 0 else None,
            "tier1_left": t1,
            "tier2_left": t2,
            "current_tier": cur_tier,
            "next_break_in": break_in,
            "cliff": cliff,
            "best_available": avail[0].get("name") if avail else None,
            "best_value": (avail[0].get("adj_value") or avail[0].get("base_value")) if avail else None,
        })
    return out


# ==========================================================================
# Self-test / demo.  `python3 valuation.py`
# ==========================================================================

if __name__ == "__main__":
    import random

    random.seed(20260908)

    LEAGUE = dict(DEFAULT_LEAGUE)
    CFG = _normalize_league(LEAGUE)

    # ---- synthetic 300-player pool ---------------------------------------
    COUNTS = {"QB": 36, "RB": 72, "WR": 96, "TE": 32, "K": 32, "DEF": 32}
    # market's positional bias vs. pure points, and a deliberately DIFFERENT
    # budget scale ($300 / 14 teams) so the rescale step has to earn its keep.
    MK_MULT = {"QB": 0.80, "RB": 1.18, "WR": 1.00, "TE": 0.85, "K": 0.10, "DEF": 0.12}

    pool = []
    for pos, n in COUNTS.items():
        anchors = _DEFAULT_CURVES[pos]
        curve = _make_interp([a for a, _ in anchors], [b for _, b in anchors])
        for i in range(1, n + 1):
            true_pts = max(1.0, curve(i) * random.uniform(0.94, 1.06))
            p = {
                "key": "%s|synth %s%02d" % (pos, pos.lower(), i),
                "sleeper_id": "%s%03d" % (pos, i),
                "name": "%s%d Player" % (pos, i),
                "pos": pos, "team": None, "bye": (i % 14) + 4,
                "consensus_rank": None, "adp": None, "adp_stdev": None,
                "proj_pts": round(true_pts, 1),
                "market_auction": None, "auction_by_source": {},
                "adp_by_source": {}, "sources_count": 0,
                "_true_pts": true_pts, "_pidx": i,
            }
            pool.append(p)

    # Global consensus rank / ADP: value order with realistic noise.
    scored = []
    for p in pool:
        v = p["_true_pts"] - _make_interp(
            [a for a, _ in _DEFAULT_CURVES[p["pos"]]],
            [b for _, b in _DEFAULT_CURVES[p["pos"]]])(
                {"QB": 13, "RB": 31, "WR": 46, "TE": 13, "K": 13, "DEF": 13}[p["pos"]])
        scored.append((v * random.uniform(0.9, 1.1), p))
    scored.sort(key=lambda t: -t[0])
    for rank, (_v, p) in enumerate(scored, start=1):
        p["consensus_rank"] = rank
        p["adp"] = round(max(1.0, rank * random.uniform(0.85, 1.15)), 1)
        p["adp_stdev"] = round(max(0.5, rank * random.uniform(0.05, 0.30)), 1)
        # market dollars on a $300/14-team scale, biased by position
        raw = 300.0 * math.exp(-rank / 42.0) * MK_MULT[p["pos"]] * random.uniform(0.8, 1.2)
        mk = max(1.0, round(raw, 0))
        p["market_auction"] = mk
        nsrc = random.choice([1, 2, 3, 3, 4])
        for si, s in enumerate(["espn", "yahoo", "fantasypros", "cbs"][:nsrc]):
            p["auction_by_source"][s] = max(1.0, round(mk * (0.7 + 0.25 * si) *
                                                       random.uniform(0.75, 1.25), 0))
            p["adp_by_source"][s] = round(max(1.0, rank * random.uniform(0.8, 1.2)), 1)
        p["sources_count"] = nsrc

    # 30% of the pool has NO projection -> must be priced off the fitted curve.
    missing = random.sample(pool, int(len(pool) * 0.30))
    for p in missing:
        p["proj_pts"] = None

    print("=" * 92)
    print("SYNTHETIC POOL: %d players | league: %d teams x $%d, roster %d "
          "(pool $%d, %d bodies, surplus $%d)"
          % (len(pool), CFG["teams"], CFG["budget"], CFG["roster_size"],
             CFG["total_dollars"], CFG["n_rostered"], CFG["surplus"]))
    print("             %d/%d players (%.0f%%) have NO projection and are curve-fitted"
          % (len(missing), len(pool), 100.0 * len(missing) / len(pool)))
    print("=" * 92)

    compute_base_values(pool, LEAGUE)
    meta = valuation_meta(pool, LEAGUE)

    print("\nSTARTERS / REPLACEMENT (flex allocated by who the top flex players actually are)")
    print("  %-5s %8s %8s %10s" % ("pos", "starters", "repl_pts", "players"))
    for pos in POSITIONS:
        print("  %-5s %8d %8.1f %10d" % (pos, meta["starters"].get(pos, 0),
                                         meta["replacement"].get(pos, 0.0),
                                         meta["pos_counts"].get(pos, 0)))
    fl = {}
    for pos in CFG["flex_pos"]:
        fl[pos] = meta["starters"].get(pos, 0) - CFG["base_slots"].get(pos, 0) * CFG["teams"]
    print("  flex slots (%d) allocated: %s" % (CFG["flex_total"], fl))

    board = sorted(pool, key=lambda p: -p["base_value"])
    print("\nTOP 30 BY BASE VALUE")
    print("  %-3s %-13s %-4s %5s %6s %7s %7s %7s %7s %5s %5s"
          % ("#", "player", "pos", "rank", "pts", "vorp", "model$", "mkt$", "BASE$",
             "tier", "conf"))
    for i, p in enumerate(board[:30], start=1):
        print("  %-3d %-13s %-4s %5d %6.1f %7.1f %7.1f %7s %7.1f %5d %5.2f"
              % (i, p["name"], p["pos"], p["pos_rank"], p["proj_pts_final"], p["vorp"],
                 p["model_value"],
                 ("%.1f" % p["market_value"]) if p["market_value"] is not None else "-",
                 p["base_value"], p["tier"], p["value_conf"]))

    print("\nCHEAPEST POSITIONS (K / DEF land where VORP puts them - not hacked up)")
    for pos in ("K", "DEF"):
        row = [p for p in board if p["pos"] == pos][:3]
        print("  %-4s %s" % (pos, "  ".join("%s $%.1f (vorp %.1f)"
                                            % (q["name"], q["base_value"], q["vorp"])
                                            for q in row)))

    # ---- assertions -------------------------------------------------------
    print("\n" + "-" * 92)
    N = CFG["n_rostered"]
    TOT = CFG["total_dollars"]

    s_model = sum(p["model_value"] for p in sorted(pool, key=lambda q: -q["model_value"])[:N])
    s_base = sum(p["base_value"] for p in sorted(pool, key=lambda q: -q["base_value"])[:N])
    s_mkt_l = [p["market_value"] for p in pool if p["market_value"] is not None]
    s_mkt = sum(sorted(s_mkt_l, reverse=True)[:N])

    for label, tot in (("model_value", s_model), ("market_value", s_mkt), ("base_value", s_base)):
        err = abs(tot - TOT) / TOT
        print("  ASSERT sum(top %d %-12s) = $%8.2f  vs pool $%d   err %.4f%%  %s"
              % (N, label, tot, TOT, err * 100, "OK" if err < 0.01 else "FAIL"))
        assert err < 0.01, "%s top-%d sum $%.2f != pool $%d (err %.3f%%)" % (
            label, N, tot, TOT, err * 100)

    assert all(p["base_value"] >= 1.0 for p in pool), "base_value must be >= $1"
    assert all(p.get("proj_pts_final") is not None for p in pool), "every player must be priced"
    assert all(p.get("tier") and p["tier"] >= 1 for p in pool), "every player needs a tier"
    assert all(0.0 <= p["value_conf"] <= 1.0 for p in pool), "conf out of range"
    for pos, lst in _group_sorted(pool).items():
        tiers = [p["tier"] for p in lst]
        assert tiers == sorted(tiers), "tiers must be monotone within %s" % pos
    print("  ASSERT base_value >= $1, every player projected+tiered, tiers monotone   OK")

    nt = {}
    for p in pool:
        nt[p["pos"]] = max(nt.get(p["pos"], 0), p["tier"])
    print("  tiers per position: %s" % nt)

    # ---- live auction simulation -----------------------------------------
    def fresh_teams():
        return [{"roster_id": i + 1, "owner": "Team%d" % (i + 1),
                 "left": float(CFG["budget"]), "slots_left": CFG["roster_size"],
                 "spent": 0.0} for i in range(CFG["teams"])]

    def simulate(mode, factor, n_picks=60, watch=None):
        """Nominate roughly in value order; the room pays `factor` x base value."""
        rnd = random.Random(7)
        teams = fresh_teams()
        picks, drafted = [], set()
        nomination = [p for p in sorted(pool, key=lambda q: -q["base_value"]) if p is not watch]
        rows = []

        def snapshot(n):
            live = compute_live(pool, LEAGUE,
                               {"drafted_keys": sorted(drafted), "drafted_ids": [],
                                "picks": picks, "teams": teams, "my_roster_id": 1})
            w = next(q for q in live["players"] if q["key"] == watch["key"])
            rows.append((n, live["inflation"], w["adj_value"], live["money_left"],
                         live["slots_left_total"], live["market_overall"]["ratio"]))
            return live

        snapshot(0)
        i = 0
        while len(picks) < n_picks and i < len(nomination):
            # jittered nomination order: the room does not nominate strictly by value
            wnd = nomination[i:i + 6]
            if not wnd:
                break
            p = rnd.choice(wnd[:3]) if len(wnd) >= 3 else wnd[0]
            nomination.remove(p)
            price = max(1.0, p["base_value"] * factor * rnd.uniform(0.85, 1.15))
            cand = [t for t in teams if t["slots_left"] > 0]
            if not cand:
                break
            cand.sort(key=lambda t: -(t["left"] - max(0, t["slots_left"] - 1)))
            buyer = cand[0]
            cap = buyer["left"] - max(0, buyer["slots_left"] - 1)
            price = float(max(1, min(round(price), int(cap))))
            buyer["left"] -= price
            buyer["spent"] += price
            buyer["slots_left"] -= 1
            drafted.add(p["key"])
            picks.append({"player_id": p["sleeper_id"], "key": p["key"],
                          "amount": price, "roster_id": buyer["roster_id"], "pos": p["pos"]})
            if len(picks) % 10 == 0:
                snapshot(len(picks))
            i = 0
        return rows, snapshot(len(picks))

    watch = board[0]
    print("\n" + "=" * 92)
    print("LIVE SIMULATION - watching %s (%s), base $%.1f, never nominated"
          % (watch["name"], watch["pos"], watch["base_value"]))
    print("=" * 92)

    finals = {}
    for mode, factor in (("NEUTRAL  (room pays base)", 1.00),
                         ("UNDERSPEND (room pays 0.70x)", 0.70),
                         ("OVERSPEND  (room pays 1.40x)", 1.40)):
        rows, live = simulate(mode, factor, 60, watch)
        print("\n  %s" % mode)
        print("    %6s %10s %12s %11s %11s %8s"
              % ("picks", "inflation", "watch adj$", "money_left", "slots_left", "paid/exp"))
        for n, inf, adj, ml, sl, ratio in rows:
            print("    %6d %10.3f %12.1f %11.1f %11d %8s"
                  % (n, inf, adj, ml, sl, ("%.2f" % ratio) if ratio else "-"))
        finals[factor] = rows[-1]
        pos_rows = sorted(live["market"].items(),
                          key=lambda kv: -(kv[1]["ratio"] or 0))
        print("    positional pace (paid/expected): %s"
              % "  ".join("%s %.2f(n=%d)" % (k, v["ratio"], v["n"])
                          for k, v in pos_rows if v["ratio"]))

    n_, inf_, adj_, _, _, _ = finals[1.00]
    u_ = finals[0.70]
    o_ = finals[1.40]
    print("\n" + "-" * 92)
    print("  ASSERT after 60 picks, $%.0f stud is:" % watch["base_value"])
    print("    neutral    inflation %.3f -> $%.1f  (%+.1f%% vs base)"
          % (inf_, adj_, 100 * (adj_ - watch["base_value"]) / watch["base_value"]))
    print("    underspend inflation %.3f -> $%.1f  (%+.1f%% vs base)"
          % (u_[1], u_[2], 100 * (u_[2] - watch["base_value"]) / watch["base_value"]))
    print("    overspend  inflation %.3f -> $%.1f  (%+.1f%% vs base)"
          % (o_[1], o_[2], 100 * (o_[2] - watch["base_value"]) / watch["base_value"]))
    assert abs(inf_ - 1.0) < 0.06, "neutral market should hold inflation ~1.0, got %.3f" % inf_
    assert abs(adj_ - watch["base_value"]) / watch["base_value"] < 0.06, \
        "neutral market should hold the stud near base"
    assert u_[1] > inf_ + 0.05 and u_[2] > adj_, "underspending must inflate the stud"
    assert o_[1] < inf_ - 0.05 and o_[2] < adj_, "overspending must deflate the stud"
    print("  ASSERT neutral holds / underspend inflates / overspend deflates          OK")

    # ---- scarcity ---------------------------------------------------------
    rnd = random.Random(3)
    some = set(p["key"] for p in board[:45])
    print("\nPOSITIONAL SCARCITY after 45 picks off the top")
    print("  %-5s %6s %6s %9s %9s %6s %6s %8s %6s  %s"
          % ("pos", "avail", "abvR", "slotsLeft", "supply", "T1", "T2", "breakIn",
             "cliff", "best available"))
    for s in positional_scarcity(pool, LEAGUE, some):
        print("  %-5s %6d %6d %9d %9s %6d %6d %8s %6s  %s"
              % (s["pos"], s["avail"], s["above_replacement"], s["starting_slots_left"],
                 ("%.2f" % s["supply_ratio"]) if s["supply_ratio"] else "-",
                 s["tier1_left"], s["tier2_left"], s["next_break_in"],
                 "YES" if s["cliff"] else "", s["best_available"]))

    # ---- edge cases -------------------------------------------------------
    print("\nEDGE CASES")
    tiny = [dict(p) for p in pool[:5]]
    compute_base_values(tiny, LEAGUE)
    print("  5-player pool                      -> ok (top $%.1f)"
          % max(p["base_value"] for p in tiny))
    noproj = [dict(p, proj_pts=None) for p in pool]
    compute_base_values(noproj, LEAGUE)
    print("  zero real projections anywhere     -> ok (top $%.1f, all priced: %s)"
          % (max(p["base_value"] for p in noproj),
             all(p["proj_pts_final"] is not None for p in noproj)))
    nomkt = [dict(p, market_auction=None, auction_by_source={}) for p in pool]
    compute_base_values(nomkt, LEAGUE)
    tn = sum(sorted((p["base_value"] for p in nomkt), reverse=True)[:N])
    print("  no market column (pure model)      -> ok (top-%d sum $%.1f vs $%d)" % (N, tn, TOT))
    assert abs(tn - TOT) / TOT < 0.01
    empty = compute_live([], LEAGUE, {})
    print("  empty pool / empty draft           -> ok (inflation %.2f)" % empty["inflation"])
    l2 = compute_live(pool, {"teams": 10, "budget": 100, "slots": {"QB": 1, "RB": 2, "WR": 2,
                                                                  "SUPER_FLEX": 1, "TE": 1,
                                                                  "K": 1, "DEF": 1, "BN": 5},
                             "flex_pos": ["RB", "WR", "TE"]}, {})
    print("  10-team $100 superflex league      -> ok (inflation %.2f, QB1 $%s)"
          % (l2["inflation"],
             next(p["base_value"] for p in l2["players"] if p["pos"] == "QB" and p["pos_rank"] == 1)))
    broke = compute_live(pool, LEAGUE, {"picks": [], "teams": [
        {"roster_id": i + 1, "left": 1.0, "slots_left": 1, "spent": 199.0}
        for i in range(12)]})
    print("  everybody broke (clamped)          -> inflation %.2f (floor %.2f)"
          % (broke["inflation"], INFLATION_MIN))

    print("\n" + "=" * 92)
    print("ALL ASSERTIONS PASSED")
    print("=" * 92)
