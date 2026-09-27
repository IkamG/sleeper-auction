"""League money: real FAAB remaining, waiver history, and this room's prices.

Probed 2026-09-27 on the user's league:
  * league settings.waiver_budget = 1000, waiver_type 2 (FAAB).
  * rosters[].settings.waiver_budget_used is present for every roster (0 to
    299 after week 2), so FAAB remaining = budget - used. This removes the old
    "Sleeper does not expose FAAB spent" limitation.
  * /league/<id>/transactions/<week>: type "waiver" rows carry status
    ("complete" / "failed"), settings.waiver_bid, adds {player_id: roster_id},
    roster_ids. Week 1: 15 claims, week 2: 24, week 3: 3 so far.
"""
import statistics

from sleeper_auction.feeds import common

API = "https://api.sleeper.app/v1/league/%s"


def _league(league_id):
    return common.fetch_json(API % league_id, "slp-league-%s" % league_id, 3600,
                             feed="league.settings")


def faab(league_id):
    """{roster_id: {"budget", "used", "left"}}; {} when FAAB is off."""
    lg = _league(league_id)
    budget = int((lg.get("settings") or {}).get("waiver_budget") or 0)
    if not budget:
        return {}
    rs = common.fetch_json(API % league_id + "/rosters", "slp-rosters-faab-%s" % league_id,
                           600, feed="league.faab")
    out = {}
    for r in rs:
        used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
        out[r["roster_id"]] = {"budget": budget, "used": used, "left": max(0, budget - used)}
    common.ok("league.faab", rows=len(out), budget=budget)
    return out


def faab_history(league_id, through_week):
    """Every waiver claim through a week: [{week, player, bid, won, roster_id, bids_on_player}]."""
    out = []
    for w in range(1, int(through_week) + 1):
        try:
            tx = common.fetch_json(API % league_id + "/transactions/%d" % w,
                                   "slp-tx-%s-%d" % (league_id, w), 600 if w >= through_week
                                   else 86400, feed="league.transactions")
        except Exception as e:
            common.fail("league.transactions", e)
            continue
        claims = [t for t in tx if t.get("type") == "waiver"]
        per_player = {}
        for t in claims:
            for pid in (t.get("adds") or {}):
                per_player[pid] = per_player.get(pid, 0) + 1
        for t in claims:
            for pid, rid in (t.get("adds") or {}).items():
                out.append({"week": w, "player": pid,
                            "bid": int((t.get("settings") or {}).get("waiver_bid") or 0),
                            "won": t.get("status") == "complete", "roster_id": rid,
                            "bids_on_player": per_player.get(pid, 1)})
    common.ok("league.transactions", rows=len(out))
    return out


def clearing_curve(history, budget, pos_of=None):
    """What this room actually pays, as % of the season budget.

    Bands: contested (another claim on the same player that week) vs not, and
    by position. Only completed, non-zero claims count as a price.
    """
    won = [h for h in history if h["won"] and h["bid"] > 0]
    pct = lambda hs: round(statistics.median(100.0 * h["bid"] / budget for h in hs), 1) \
        if hs else None                                                    # noqa: E731
    bands = {"all": pct(won),
             "contested": pct([h for h in won if h["bids_on_player"] > 1]),
             "uncontested": pct([h for h in won if h["bids_on_player"] <= 1])}
    if pos_of:
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            hs = [h for h in won if pos_of(h["player"]) == pos]
            if len(hs) >= 2:
                bands[pos] = pct(hs)
    return {"median_pct_by_band": bands, "n": len(won),
            "zero_bid_wins": sum(1 for h in history if h["won"] and h["bid"] == 0)}
