#!/usr/bin/env python3
"""Post-draft analysis: rate every team against market value and room-clearing price.

    python3 quick/analyze.py --draft 1389690785410064385
    python3 quick/analyze.py --draft <id> --html out.html
"""
import argparse
import collections
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import draftboard as db  # noqa: E402

STARTERS = [("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1), ("K", 1), ("DEF", 1)]
FLEX_POS = ("RB", "WR", "TE")


def fit_room_price(rows):
    """Least-squares fit of what the room actually paid onto model value.

    The model is known to run rich at the top of the board, so grading purely
    against it would punish whoever bought the studs. The fitted line is the
    room's own clearing price for a player of that model value — grading against
    it asks "did you beat THIS room", which is the question that decides a league.
    Returns (slope, intercept, r).
    """
    xs = [r["model"] for r in rows]
    ys = [float(r["paid"]) for r in rows]
    n = len(xs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 1.0
    inter = my - slope * mx
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    r = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n / (sx * sy)) if sx and sy else 0.0
    return slope, inter, r


def best_lineup(players):
    """Greedy optimal starting lineup by projected points, then the FLEX."""
    pool = sorted(players, key=lambda p: -(p["proj"] or 0))
    used, start = set(), []
    for pos, count in STARTERS:
        picked = 0
        for p in pool:
            if picked >= count:
                break
            if p["pos"] == pos and id(p) not in used:
                used.add(id(p))
                start.append(p)
                picked += 1
    for p in pool:
        if p["pos"] in FLEX_POS and id(p) not in used:
            used.add(id(p))
            start.append(p)
            break
    return start


GRADE_CURVE = ["A+", "A", "A-", "B+", "B", "B", "B-", "C+", "C", "C-", "D+", "D"]


def grade(rank, n, z):
    """Grade on the league curve, not an absolute z-score.

    A 12-team draft is zero-sum: every dollar of value one manager captures comes
    out of someone else's. Grading against an absolute scale buried six of twelve
    teams in Ds, including one with POSITIVE surplus, which is nonsense. Position
    on the curve is the honest measure. The z-score only moves the extremes: a
    genuinely dominant draft earns the A+, a genuinely bad one the F.
    """
    i = int(round((rank - 1) / max(1, n - 1) * (len(GRADE_CURVE) - 1)))
    g = GRADE_CURVE[min(i, len(GRADE_CURVE) - 1)]
    if rank == 1 and z < 0.8:
        g = "A"
    if rank == n and z < -1.5:
        g = "F"
    return g


def analyze(draft_id, players=None):
    players = players or db.value_pool(db.build_pool()[0])
    byid = {p["sleeper_id"]: p for p in players if p.get("sleeper_id")}
    bykey = {p["key"]: p for p in players}

    st = db.draft_state(draft_id)
    if not st["is_auction"]:
        raise SystemExit("Draft %s is a %s draft, not an auction — there are no "
                         "prices to grade." % (draft_id, st["draft"].get("type")))

    rows, unmatched = [], []
    for pk in st["picks"]:
        p = byid.get(pk["player_id"]) or bykey.get(pk["key"])
        if not p:
            unmatched.append(pk["name"])
            continue
        rows.append({"name": p["name"], "pos": p["pos"], "team": p["team"],
                     "roster_id": pk["roster_id"], "paid": pk["amount"],
                     "model": p["base"], "proj": p["proj"] or 0.0,
                     "market": p.get("market_adj"), "ly": p.get("ly"), "_p": p})
    if not rows:
        raise SystemExit("No picks to analyze — is the draft complete?")

    slope, inter, r = fit_room_price(rows)
    for x in rows:
        x["room"] = round(slope * x["model"] + inter, 1)   # room's clearing price
        x["edge_mkt"] = round(x["model"] - x["paid"], 1)   # vs market value
        x["edge_room"] = round(x["room"] - x["paid"], 1)   # vs this room's price

    owners = {t["roster_id"]: t["owner"] for t in st["teams"]}
    teams = []
    for rid, mine in sorted(collections.defaultdict(
            list, {k: [x for x in rows if x["roster_id"] == k]
                   for k in {x["roster_id"] for x in rows}}).items()):
        start = best_lineup([x["_p"] for x in mine])
        spent = sum(x["paid"] for x in mine)
        teams.append({
            "roster_id": rid, "owner": owners.get(rid, "Team %d" % rid),
            "spent": spent, "n": len(mine),
            "model_value": round(sum(x["model"] for x in mine), 1),
            "room_value": round(sum(x["room"] for x in mine), 1),
            "surplus_mkt": round(sum(x["edge_mkt"] for x in mine), 1),
            "surplus_room": round(sum(x["edge_room"] for x in mine), 1),
            "starter_pts": round(sum(p["proj"] or 0 for p in start), 1),
            "spend": {pos: sum(x["paid"] for x in mine if x["pos"] == pos)
                      for pos in ("QB", "RB", "WR", "TE", "K", "DEF")},
            "best": max(mine, key=lambda x: x["edge_room"]),
            "worst": min(mine, key=lambda x: x["edge_room"]),
            "picks": sorted(mine, key=lambda x: -x["paid"]),
        })

    for field, key in (("surplus_room", "z_room"), ("starter_pts", "z_pts")):
        vals = [t[field] for t in teams]
        m, sd = statistics.mean(vals), statistics.pstdev(vals) or 1.0
        for t in teams:
            t[key] = (t[field] - m) / sd
    for t in teams:
        # Value captured and points started are both real; neither alone is the
        # draft. Weight points slightly higher — surplus you cannot start is
        # cheaper than surplus you can.
        t["score"] = 0.45 * t["z_room"] + 0.55 * t["z_pts"]
    teams.sort(key=lambda t: -t["score"])
    zs = [t["score"] for t in teams]
    m, sd = statistics.mean(zs), statistics.pstdev(zs) or 1.0
    for i, t in enumerate(teams):
        t["rank"] = i + 1
        t["grade"] = grade(i + 1, len(teams), (t["score"] - m) / sd)

    return {"teams": teams, "picks": rows, "unmatched": unmatched,
            "fit": {"slope": round(slope, 3), "intercept": round(inter, 2),
                    "r": round(r, 3)},
            "totals": {"spent": sum(x["paid"] for x in rows), "picks": len(rows),
                       "pool": st["league"]["teams"] * st["league"]["budget"]},
            "name": st["name"], "status": st["status"]}


def report(a):
    o = []
    o.append("%s — %s" % (a["name"], a["status"]))
    o.append("%d picks, $%d spent of $%d" % (a["totals"]["picks"],
                                             a["totals"]["spent"], a["totals"]["pool"]))
    f = a["fit"]
    o.append("Room clearing price = %.3f x model %+.2f   (fit r=%.3f)"
             % (f["slope"], f["intercept"], f["r"]))
    if a["unmatched"]:
        o.append("UNMATCHED (no value found): %s" % ", ".join(a["unmatched"]))
    o.append("")
    o.append("%-3s %-24s %-3s %-7s %-9s %-9s %-9s" %
             ("#", "TEAM", "GR", "SPENT", "VS MKT", "VS ROOM", "STARTER PTS"))
    o.append("-" * 76)
    for t in a["teams"]:
        o.append("%-3d %-24s %-3s $%-6d %+-9.1f %+-9.1f %.1f" %
                 (t["rank"], t["owner"], t["grade"], t["spent"],
                  t["surplus_mkt"], t["surplus_room"], t["starter_pts"]))
    o.append("")
    o.append("BEST AND WORST BUY PER TEAM (vs this room's own clearing price)")
    for t in a["teams"]:
        b, w = t["best"], t["worst"]
        o.append("  %-22s best: %-20s $%-3d (%+.0f)   worst: %-20s $%-3d (%+.0f)"
                 % (t["owner"], b["name"], b["paid"], b["edge_room"],
                    w["name"], w["paid"], w["edge_room"]))
    o.append("")
    o.append("LEAGUE-WIDE BARGAINS")
    for x in sorted(a["picks"], key=lambda x: -x["edge_room"])[:8]:
        o.append("  %-24s %-4s $%-4d  room price $%-6.1f  %+.1f   (%s)"
                 % (x["name"], x["pos"], x["paid"], x["room"], x["edge_room"],
                    dict((t["roster_id"], t["owner"]) for t in a["teams"])[x["roster_id"]]))
    o.append("")
    o.append("LEAGUE-WIDE REACHES")
    for x in sorted(a["picks"], key=lambda x: x["edge_room"])[:8]:
        o.append("  %-24s %-4s $%-4d  room price $%-6.1f  %+.1f   (%s)"
                 % (x["name"], x["pos"], x["paid"], x["room"], x["edge_room"],
                    dict((t["roster_id"], t["owner"]) for t in a["teams"])[x["roster_id"]]))
    return "\n".join(o)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    a = analyze(args.draft)
    if args.json:
        a2 = {k: v for k, v in a.items() if k != "picks"}
        for t in a2["teams"]:
            t.pop("picks", None); t.pop("best", None); t.pop("worst", None)
        print(json.dumps(a2, indent=1, default=str))
    else:
        print(report(a))
