#!/usr/bin/env python3
"""Post-draft analysis: rate every team against market value and room-clearing price.

    python3 -m sleeper_auction.analysis --draft 1389690785410064385
    python3 -m sleeper_auction.analysis --draft <id> --team IkamG
"""
import argparse
import collections
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleeper_auction import board as db  # noqa: E402

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
                     "market": p.get("market_adj"), "ly": p.get("ly"),
                     "conf": p.get("value_conf"), "tier": p.get("tier"), "_p": p})
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
        sids = {id(p) for p in start}
        for x in mine:
            x["starter"] = id(x["_p"]) in sids
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


def team_detail(a, owner):
    """Full per-player breakdown for one team."""
    t = next((x for x in a["teams"] if x["owner"].lower() == owner.lower()), None)
    if not t:
        return "No team named %r. Teams: %s" % (
            owner, ", ".join(x["owner"] for x in a["teams"]))
    o = ["%s — rank %d of %d, grade %s" % (t["owner"], t["rank"], len(a["teams"]), t["grade"]),
         "$%d spent on %d players | value acquired $%.1f (market) / $%.1f (room)"
         % (t["spent"], t["n"], t["model_value"], t["room_value"]),
         "surplus %+.1f vs market, %+.1f vs room | starting lineup %.1f projected pts"
         % (t["surplus_mkt"], t["surplus_room"], t["starter_pts"]), "",
         "%-2s %-24s %-4s %-6s %-8s %-8s %-8s %-7s %-5s" %
         ("", "PLAYER", "POS", "PAID", "MARKET", "ROOM", "VS ROOM", "PROJ", "CONF"),
         "-" * 82]
    for x in t["picks"]:
        o.append("%-2s %-24s %-4s $%-5d $%-7.1f $%-7.1f %+-8.1f %-7.0f %s" %
                 ("*" if x["starter"] else " ", x["name"], x["pos"], x["paid"],
                  x["model"], x["room"], x["edge_room"], x["proj"],
                  ("%.2f" % x["conf"]) if x.get("conf") is not None else "-"))
    o.append("")
    o.append("(* = starts in the optimal lineup)")
    o.append("spend by position: " + "  ".join(
        "%s $%d" % (k, v) for k, v in t["spend"].items() if v))
    return "\n".join(o)


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


GRADE_COLOR = {"A+": "#3fb950", "A": "#3fb950", "A-": "#56d364", "B+": "#79c0ff",
               "B": "#79c0ff", "B-": "#58a6ff", "C+": "#e3b341", "C": "#e3b341",
               "C-": "#d29922", "D+": "#f85149", "D": "#f85149", "F": "#f85149"}


def html_report(a):
    """Standalone analysis page. Same visual language as the live board."""
    t_rows, detail = [], []
    for t in a["teams"]:
        c = GRADE_COLOR.get(t["grade"], "#8b949e")
        t_rows.append(
            '<tr class="trow" data-r="%d"><td class="rk">%d</td>'
            '<td class="nm">%s</td><td><span class="gr" style="background:%s22;color:%s;'
            'border-color:%s55">%s</span></td><td>$%d</td>'
            '<td class="%s">%+.1f</td><td class="%s big">%+.1f</td><td>%.0f</td>'
            '<td class="mut">%s</td></tr>' % (
                t["roster_id"], t["rank"], t["owner"], c, c, c, t["grade"], t["spent"],
                "pl" if t["surplus_mkt"] > 0 else "mn", t["surplus_mkt"],
                "pl" if t["surplus_room"] > 0 else "mn", t["surplus_room"],
                t["starter_pts"],
                "  ".join("%s $%d" % (k, v) for k, v in t["spend"].items() if v)))
        pr = "".join(
            '<tr><td>%s</td><td class="nm">%s</td><td><span class="pos %s">%s</span></td>'
            '<td>$%d</td><td class="mut">$%.1f</td><td class="mut">$%.1f</td>'
            '<td class="%s">%+.1f</td><td class="mut">%.0f</td>'
            '<td class="mut">%s</td></tr>' % (
                "&#9733;" if x["starter"] else "", x["name"], x["pos"], x["pos"],
                x["paid"], x["model"], x["room"],
                "pl" if x["edge_room"] > 0 else "mn", x["edge_room"], x["proj"],
                ("%.2f" % x["conf"]) if x.get("conf") is not None else "&mdash;")
            for x in t["picks"])
        detail.append(
            '<div class="detail" id="d%d"><div class="dh">%s &mdash; $%d spent, '
            '$%.1f of room value, %+.1f surplus, %.0f starting pts</div>'
            '<table class="pt"><thead><tr><th></th><th>Player</th><th>Pos</th>'
            '<th>Paid</th><th>Market</th><th>Room</th><th>vs Room</th><th>Proj</th><th>Conf</th>'
            '</tr></thead><tbody>%s</tbody></table>'
            '<div class="mut" style="margin-top:8px">&#9733; starts in the optimal '
            'lineup &middot; best buy %s (%+.0f) &middot; worst buy %s (%+.0f)</div></div>'
            % (t["roster_id"], t["owner"], t["spent"], t["room_value"],
               t["surplus_room"], t["starter_pts"], pr,
               t["best"]["name"], t["best"]["edge_room"],
               t["worst"]["name"], t["worst"]["edge_room"]))

    own = {t["roster_id"]: t["owner"] for t in a["teams"]}

    def plist(rows, cls):
        return "".join(
            '<div class="row"><span><span class="pos %s">%s</span> %s '
            '<span class="mut">%s</span></span><span>$%d <span class="mut">vs $%.0f</span> '
            '<b class="%s">%+.0f</b></span></div>' % (
                x["pos"], x["pos"], x["name"], own.get(x["roster_id"], ""),
                x["paid"], x["room"], cls, x["edge_room"])
            for x in rows)

    barg = plist(sorted(a["picks"], key=lambda x: -x["edge_room"])[:10], "pl")
    reach = plist(sorted(a["picks"], key=lambda x: x["edge_room"])[:10], "mn")
    f = a["fit"]
    return TPL.replace("__ROWS__", "".join(t_rows)).replace("__DETAIL__", "".join(detail)) \
        .replace("__BARG__", barg).replace("__REACH__", reach) \
        .replace("__NAME__", a["name"]) \
        .replace("__SUB__", "%d picks &middot; $%d of $%d spent &middot; room price = "
                            "%.3f &times; model %+.2f (fit r=%.3f)"
                 % (a["totals"]["picks"], a["totals"]["spent"], a["totals"]["pool"],
                    f["slope"], f["intercept"], f["r"]))


TPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Draft Analysis</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}
.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.navsp{flex:1}.navmut{color:#8b949e;font-size:12px}
.hd{padding:18px 20px;background:#161b22;border-bottom:1px solid #30363d}
.hd h1{margin:0;font-size:21px}.hd .mut{margin-top:4px;font-size:13px}
.wrap{display:flex;gap:16px;padding:16px 20px;align-items:flex-start;flex-wrap:wrap}
.main{flex:1 1 640px;min-width:0;max-width:100%}.rail{flex:0 1 340px;display:flex;flex-direction:column;gap:12px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:12px 14px}
.panel h3{margin:0 0 10px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:7px 8px;border-bottom:1px solid #30363d;white-space:nowrap}
th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}
td{padding:8px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}
.trow{cursor:pointer}.trow:hover td{background:#1c2128}
.rk{color:#8b949e;width:28px}.nm{font-weight:600;color:#fff}
.gr{display:inline-block;min-width:30px;text-align:center;padding:2px 7px;border-radius:5px;font-weight:700;font-size:12px;border:1px solid}
.big{font-size:16px;font-weight:700}
.pl{color:#3fb950}.mn{color:#f85149}.mut{color:#8b949e}
.detail{display:none;background:#11161d;border:1px solid #30363d;border-radius:9px;margin:0 0 12px;padding:12px 14px}
.detail.on{display:block}
.dh{font-weight:600;margin-bottom:9px}
.pt th{font-size:10px}.pt td{padding:5px 8px;font-size:13px}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.K{background:#30363d;color:#8b949e}.DEF{background:#30363d;color:#8b949e}
.row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px;gap:8px}
.hint{padding:0 20px 4px;color:#8b949e;font-size:12px}
@media(max-width:900px){.rail{flex:1 1 100%}}
</style></head><body>
<div class="nav"><a href="#" data-p="/">Draft board</a><a href="#" data-p="/analysis">Analysis</a><a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a>
<span class="navsp"></span><span class="navmut" id="nav-league"></span></div>
<script>(function(){var qs=location.search||'';
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
  if(location.pathname===a.dataset.p)a.className='on';});})();</script>
<div class="hd"><h1>__NAME__ &mdash; draft analysis</h1><div class="mut">__SUB__</div></div>
<div class="hint">Click any team for its full roster breakdown. <b>vs Room</b> grades against
what this league actually paid, not the model &mdash; the model runs rich at the top of the board.</div>
<div class="wrap">
 <div class="main">
  <div class="panel" style="padding:0;margin-bottom:12px"><table>
   <thead><tr><th>#</th><th>Team</th><th>Grade</th><th>Spent</th><th>vs Mkt</th><th>vs Room</th><th>Start pts</th><th>Spend by pos</th></tr></thead>
   <tbody>__ROWS__</tbody></table></div>
  __DETAIL__
 </div>
 <div class="rail">
  <div class="panel"><h3>Biggest bargains</h3>__BARG__</div>
  <div class="panel"><h3>Biggest reaches</h3>__REACH__</div>
 </div>
</div>
<script>
document.querySelectorAll('.trow').forEach(function(r){
  r.onclick=function(){
    var d=document.getElementById('d'+r.dataset.r), was=d.classList.contains('on');
    document.querySelectorAll('.detail').forEach(function(x){x.classList.remove('on')});
    if(!was){d.classList.add('on'); d.scrollIntoView({behavior:'smooth',block:'nearest'})}
  };
});
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--team", default="")
    args = ap.parse_args()
    a = analyze(args.draft)
    if args.team:
        print(team_detail(a, args.team)); raise SystemExit
    if args.json:
        a2 = {k: v for k, v in a.items() if k != "picks"}
        for t in a2["teams"]:
            t.pop("picks", None); t.pop("best", None); t.pop("worst", None)
        print(json.dumps(a2, indent=1, default=str))
    else:
        print(report(a))
