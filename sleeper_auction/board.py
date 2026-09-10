#!/usr/bin/env python3
"""Self-contained live auction draft board for Sleeper. Python 3.9 stdlib only.

Deliberately has ZERO imports from the rest of the project so nothing can break it.
    python3 -m sleeper_auction.board --draft 1389690785410064385
"""
import argparse
import gzip
import io
import json
import os
import re
import statistics
import sys
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEASON = "2026"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
# Project root on sys.path so `sources/` and `valuation.py` resolve no matter
# which directory the script was launched from.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

# ---------------------------------------------------------------- http

def get(url, headers=None, key=None, ttl=1800):
    if key:
        p = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", key))
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            return open(p, encoding="utf-8").read()
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    txt = raw.decode("utf-8", "replace")
    if key:
        os.makedirs(CACHE, exist_ok=True)
        open(os.path.join(CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", key)), "w",
             encoding="utf-8").write(txt)
    return txt


def gj(url, headers=None, key=None, ttl=1800):
    return json.loads(get(url, headers, key, ttl))

# ---------------------------------------------------------------- identity

SUF = re.compile(r"\b(jr|sr|ii|iii|iv)\b")
NW = re.compile(r"[^a-z0-9 ]")

# Cross-source spelling variants. MUST stay identical to sources/base._NAME_FIXES
# or adapter keys (built by base.player_key) cannot join the Sleeper spine.
NAME_FIXES = {
    "mitch trubisky": "mitchell trubisky",
    "gabe davis": "gabriel davis",
    "josh palmer": "joshua palmer",
    "chig okonkwo": "chigoziem okonkwo",
    "cam ward": "cameron ward",
    "tank dell": "nathaniel dell",
    "hollywood brown": "marquise brown",
    # Sources that print the legal name where Sleeper prints the nickname.
    "kenneth gainwell": "kenny gainwell",
    "christopher brooks": "chris brooks",
    "andres borregales": "andy borregales",
}
TEAMS = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "raiders": "LV", "chargers": "LAC", "rams": "LAR", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "49ers": "SF",
    "seahawks": "SEA", "buccaneers": "TB", "titans": "TEN", "commanders": "WAS",
}
TEAMFIX = {"JAC": "JAX", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC",
           "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU"}
POSFIX = {"PK": "K", "DST": "DEF", "D/ST": "DEF", "DEFENSE": "DEF", "FB": "RB"}


def npos(p):
    p = (p or "").strip().upper().replace("/", "").replace(".", "")
    return POSFIX.get(p, POSFIX.get((p or "").strip().upper(), p))


def nteam(t):
    t = (t or "").strip().upper()
    return TEAMFIX.get(t, t)


def nname(n):
    # Fold accents to their ASCII letter FIRST. NW would otherwise turn the
    # letter into a space and split the name: FFC ships "Eddy Pineiro" with a
    # tilde, Sleeper ships it without, and "eddy pi eiro" != "eddy pineiro".
    n = unicodedata.normalize("NFKD", (n or "").lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = NW.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\s+", " ", SUF.sub("", n)).strip()
    return NAME_FIXES.get(n, n)


def pkey(name, pos, team=None):
    pos = npos(pos)
    if pos == "DEF":
        t = nteam(team)
        if t and len(t) <= 3:
            return "DEF|" + t
        w = nname(name).replace(" dst", "").replace(" defense", "").strip()
        for k, v in TEAMS.items():
            if w.endswith(k) or w.startswith(k):
                return "DEF|" + v
        return "DEF|" + w
    return "%s|%s" % (pos, nname(name))

# ---------------------------------------------------------------- sources

def src_sleeper():
    """Backbone: native half-PPR projections + ADP + Sleeper player_id.

    Prefers sources/sleeper_src.py when importable: it retries with backoff,
    falls back to a stale cache entry when the API is unreachable, re-requests
    per position if the combined call fails, and drops the ~2400 teamless,
    unprojected rows Sleeper keeps in its database. This is the one source the
    board cannot do without, so its resilience matters more than any other.
    Falls back to the inline fetch below so the app still runs from a bare
    checkout with only this file.
    """
    try:
        from sleeper_auction.sources import sleeper_src as _ad
        rows = _ad.fetch(SEASON, "half_ppr")
        if rows:
            return {r["key"]: {"key": r["key"], "name": r["name"], "pos": r["pos"],
                               "team": r["team"], "sleeper_id": r["sleeper_id"],
                               "proj": r["proj_pts"], "adp": r["adp"],
                               "bye": r.get("bye")}
                    for r in rows if r.get("key") and r.get("pos")}
    except Exception as e:
        print("sleeper_src adapter unavailable (%s); using inline fetch" % e,
              file=sys.stderr)
    return _src_sleeper_inline()


def _src_sleeper_inline():
    pos = "&".join("position[]=" + p for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
    url = ("https://api.sleeper.com/projections/nfl/%s?season_type=regular&%s"
           "&order_by=adp_half_ppr" % (SEASON, pos))
    out = {}
    for row in gj(url, key="slp-proj-half", ttl=3600):
        st = row.get("stats") or {}
        pl = row.get("player") or {}
        # Take the first FANTASY-RELEVANT position, not blindly index 0:
        # Travis Hunter is ["DB","WR"] and index 0 would drop him off the board.
        p = next((q for q in (npos(x) for x in (pl.get("fantasy_positions") or []))
                  if q in ("QB", "RB", "WR", "TE", "K", "DEF")), None)
        if p is None:
            continue
        pid = str(row.get("player_id") or pl.get("player_id") or "")
        nm = (" ".join(filter(None, [pl.get("first_name"), pl.get("last_name")]))).strip()
        if p == "DEF" and not nm:
            nm = pid
        adp = st.get("adp_half_ppr")
        if adp in (None, 999, 999.0):
            adp = st.get("adp_ppr")
        if adp in (None, 999, 999.0):
            adp = None
        k = pkey(nm, p, pl.get("team"))
        out[k] = {"key": k, "name": nm, "pos": p, "team": nteam(pl.get("team")),
                  "sleeper_id": pid, "proj": st.get("pts_half_ppr"), "adp": adp,
                  "bye": None}
    return out


def src_ffc():
    """Real-draft consensus ADP. half-ppr primary, ppr backfill for depth."""
    out = {}
    for fmt in ("ppr", "half-ppr"):          # half-ppr last so it wins
        try:
            d = gj("https://fantasyfootballcalculator.com/api/v1/adp/%s"
                   "?teams=12&year=%s&position=all" % (fmt, SEASON),
                   key="ffc-" + fmt, ttl=1800)
        except Exception:
            continue
        for p in d.get("players", []):
            k = pkey(p.get("name"), p.get("position"), p.get("team"))
            out[k] = {"adp": p.get("adp"), "stdev": p.get("stdev"),
                      "bye": p.get("bye"), "n": p.get("times_drafted")}
    return out


ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
ESPN_TEAM = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
             7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
             14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
             20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
             26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL",
             34: "HOU"}


def src_espn():
    """Real auction dollars from ESPN's live market average.

    The adapter pages far deeper (~1000 players vs ~340). That makes no
    difference at the top of an auction board, where every player already
    carries a market value, but it fills in the tail for deeper leagues.
    """
    try:
        from sleeper_auction.sources import espn as _ad
        rows = _ad.fetch(SEASON, "half_ppr")
        if rows:
            out = {}
            for r in rows:
                if r.get("key") and r.get("auction"):
                    out[r["key"]] = {"auction": float(r["auction"]),
                                     "rank": r.get("rank")}
            if out:
                return out
    except Exception as e:
        print("espn adapter unavailable (%s); using inline fetch" % e,
              file=sys.stderr)
    return _src_espn_inline()


def _src_espn_inline():
    filt = ('{"players":{"limit":450,"sortDraftRanks":'
            '{"sortPriority":100,"sortAsc":true,"value":"PPR"}}}')
    d = gj("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/%s"
           "/segments/0/leaguedefaults/3?view=kona_player_info" % SEASON,
           headers={"x-fantasy-filter": filt}, key="espn-kona", ttl=3600)
    out = {}
    for e in d.get("players", []):
        pl = e.get("player") or {}
        p = ESPN_POS.get(pl.get("defaultPositionId"))
        if not p:
            continue
        team = ESPN_TEAM.get(pl.get("proTeamId"))
        av = (pl.get("ownership") or {}).get("auctionValueAverage")
        dr = (pl.get("draftRanksByRankType") or {})
        rk = (dr.get("PPR") or {}).get("rank")
        k = pkey(pl.get("fullName"), p, team)
        if av is not None and av > 0:
            out[k] = {"auction": float(av), "rank": rk}
    return out

# ---------------------------------------------------------------- pool

EXTRA = ("cbs", "yahoo", "fantasypros")


def src_extra():
    """Optional adapters built by the workflow. Any that fail are simply skipped —
    the board must never depend on the scrapers."""
    import importlib
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    got, errs = {}, {}
    for name in EXTRA:
        try:
            m = importlib.import_module("sleeper_auction.sources." + name)
            rows = m.fetch(SEASON, "half_ppr")
            d = {}
            for r in rows:
                if r.get("key"):
                    d[r["key"]] = {"adp": r.get("adp"), "auction": r.get("auction"),
                                   "rank": r.get("rank")}
            if d:
                got[name] = d
        except Exception as e:
            errs[name] = "%s: %s" % (type(e).__name__, e)
    return got, errs


def build_pool():
    errs = {}
    try:
        sl = src_sleeper()
    except Exception as e:
        raise SystemExit("Sleeper projections failed (%s) — no board without them." % e)
    try:
        ffc = src_ffc()
    except Exception as e:
        ffc, errs["ffc"] = {}, str(e)
    try:
        espn = src_espn()
    except Exception as e:
        espn, errs["espn"] = {}, str(e)
    extra, xerr = src_extra()
    errs.update(xerr)

    ly = last_year_prices()
    players = []
    for k, p in sl.items():
        f, e = ffc.get(k, {}), espn.get(k, {})
        x = {n: d[k] for n, d in extra.items() if k in d}
        sleeper_adp = p.get("adp")          # capture before the median overwrites it
        # Median across sources, not mean: one bad scraper cannot move the board.
        adps = [a for a in [sleeper_adp, f.get("adp")] + [v.get("adp") for v in x.values()] if a]
        p["adp"] = round(statistics.median(adps), 1) if adps else None
        aucs = [a for a in [e.get("auction")] + [v.get("auction") for v in x.values()] if a]
        p["market"] = round(statistics.median(aucs), 1) if aucs else None
        p["adp_by"] = dict([("sleeper", sleeper_adp), ("ffc", f.get("adp"))] +
                           [(n, v.get("adp")) for n, v in x.items()])
        p["stdev"] = f.get("stdev")
        p["bye"] = f.get("bye")
        p["srcs"] = 1 + (1 if f else 0) + (1 if e else 0) + len(x)
        p["ly"] = ly.get(k)
        players.append(p)
    counts = {"sleeper": len(sl), "ffc": len(ffc), "espn": len(espn),
              "last_year": len(ly)}
    counts.update({n: len(d) for n, d in extra.items()})
    return players, errs, counts

LAST_YEAR_DRAFT = "1254176306367561728"   # KHALISTAN FOOTBALL 2025, same 12/$200/half-PPR


def last_year_prices(draft_id=LAST_YEAR_DRAFT):
    """What this exact room paid last season. The single best read on how these
    12 people actually behave, which no public ranking source knows."""
    try:
        picks = gj("https://api.sleeper.app/v1/draft/%s/picks" % draft_id,
                   key="ly-%s" % draft_id, ttl=86400)
    except Exception:
        return {}
    out = {}
    for p in picks:
        m = p.get("metadata") or {}
        nm = (m.get("first_name", "") + " " + m.get("last_name", "")).strip()
        try:
            amt = int(m.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if nm and amt:
            out[pkey(nm, m.get("position"), m.get("team"))] = amt
    return out


# ---------------------------------------------------------------- valuation

LEAGUE = {"teams": 12, "budget": 200,
          "slots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1,
                    "DEF": 1, "BN": 4},
          "flex": ["RB", "WR", "TE"]}
W_MODEL, W_MARKET = 0.55, 0.45


def _value_pool_via_module(players, lg):
    """Delegate the auction math to valuation.py when it is importable.

    That module fixes two things the inline math below gets wrong: tiers cap at
    MAX_TIER_SIZE so a flat position cannot collapse into one 27-player tier,
    and its normalisation lands the board on exactly teams*budget instead of
    leaking a few dollars. It also fills players with no projection from an
    isotonic points-vs-rank curve rather than dropping them to $1, and returns
    a real confidence score. Returns None if unavailable, so the caller falls
    back to the inline implementation.
    """
    try:
        from sleeper_auction import valuation
    except ImportError:
        return None
    league = {"teams": lg["teams"], "budget": lg["budget"],
              "scoring": lg.get("scoring", "half_ppr"),
              "slots": dict(lg["slots"]), "flex_pos": list(lg.get("flex", []))}
    payload = []
    for p in players:
        proj = p.get("proj")
        payload.append({
            "key": p["key"], "sleeper_id": p.get("sleeper_id"), "name": p["name"],
            "pos": p["pos"], "team": p.get("team"), "bye": p.get("bye"),
            "consensus_rank": p.get("adp"), "adp": p.get("adp"),
            "adp_stdev": p.get("stdev"),
            # None, not 0 -- that is what lets the curve fill him in.
            "proj_pts": float(proj) if proj else None,
            "market_auction": p.get("market"),
            "auction_by_source": {}, "adp_by_source": p.get("adp_by") or {},
            "sources_count": p.get("srcs", 1)})
    try:
        out = valuation.compute_base_values(payload, league)
    except Exception as e:
        print("valuation module failed (%s); using inline math" % e, file=sys.stderr)
        return None
    by_key = {o["key"]: o for o in out}
    for p in players:
        o = by_key.get(p["key"])
        if not o:
            continue
        p["base"] = o["base_value"]
        p["model"] = o.get("model_value")
        p["market_adj"] = o.get("market_value")
        p["vorp"] = o.get("vorp")
        p["tier"] = o.get("tier")
        p["pos_rank"] = o.get("pos_rank")
        p["replacement"] = o.get("replacement")
        p["value_conf"] = o.get("value_conf")
        p["proj_source"] = o.get("proj_source")
        p["proj"] = o.get("proj_pts_final") or p.get("proj") or 0.0

    # K and DEF stay $1 -- see the note in the inline path. Freeing those
    # dollars would otherwise shrink the pool, so redistribute them across the
    # rest instead of leaking them (the bug the inline version still has).
    total = lg["teams"] * lg["budget"]
    size = lg["teams"] * sum(lg["slots"].values())
    for p in players:
        if p["pos"] in ("K", "DEF"):
            p["base"] = 1.0
            p["vorp"] = 0.0
    top = sorted(players, key=lambda x: -x["base"])[:size]
    above = sum(max(0.0, p["base"] - 1) for p in top) or 1.0
    scale = (total - size) / above
    for p in players:
        if p["pos"] not in ("K", "DEF"):
            p["base"] = round(1 + max(0.0, p["base"] - 1) * scale, 1)
    return players


def value_pool(players, lg=LEAGUE):
    via = _value_pool_via_module(players, lg)
    if via is not None:
        return via
    return _value_pool_inline(players, lg)


def _value_pool_inline(players, lg=LEAGUE):
    teams, budget, slots = lg["teams"], lg["budget"], lg["slots"]
    roster = sum(slots.values())
    pool_size = teams * roster
    total_dollars = teams * budget

    by_pos = {}
    for p in players:
        if p.get("proj") is None:
            p["proj"] = 0.0
        by_pos.setdefault(p["pos"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -(x["proj"] or 0))
        for i, p in enumerate(by_pos[pos]):
            p["pos_rank"] = i + 1

    # Flex slots go to whichever positions actually own the best flex-eligible
    # players, not an even split.
    flex_total = teams * slots.get("FLEX", 0)
    base_start = {pos: teams * slots.get(pos, 0) for pos in by_pos}
    extra = {pos: 0 for pos in by_pos}
    if flex_total:
        pool = []
        for pos in lg["flex"]:
            for p in by_pos.get(pos, [])[base_start.get(pos, 0):]:
                pool.append((p["proj"] or 0, pos))
        pool.sort(reverse=True)
        for _, pos in pool[:flex_total]:
            extra[pos] = extra.get(pos, 0) + 1

    repl = {}
    for pos, lst in by_pos.items():
        n = base_start.get(pos, 0) + extra.get(pos, 0)
        idx = min(max(n, 1), len(lst) - 1) if len(lst) > 1 else 0
        repl[pos] = lst[idx]["proj"] or 0
    for p in players:
        p["replacement"] = repl.get(p["pos"], 0)
        p["vorp"] = max(0.0, (p["proj"] or 0) - p["replacement"])
        # K and DEF are $1 players. The K1-vs-K12 gap is real in projection but
        # not predictable, and no auction room pays for it — pricing their VORP
        # would silently tax the RB/WR pool that actually decides the league.
        if p["pos"] in ("K", "DEF"):
            p["vorp"] = 0.0

    # K/DEF are forced to $1 below, so they never compete for the priced pool.
    # Only pool_size - n_forced roster spots are actually available to priced
    # players; normalising over pool_size instead spreads the surplus across
    # ~n_forced extra players who will never be rostered, and that money leaks
    # out of the board (0.1% here, 1.4% in a shallow-roster league).
    n_forced = teams * (slots.get("K", 0) + slots.get("DEF", 0))
    n_priced = max(1, pool_size - n_forced)
    priced = [p for p in players if p["pos"] not in ("K", "DEF")]
    surplus = total_dollars - pool_size

    top = sorted(priced, key=lambda x: -x["vorp"])[:n_priced]
    tot_vorp = sum(p["vorp"] for p in top) or 1.0
    dpv = surplus / tot_vorp
    for p in players:
        p["model"] = 1.0 + p["vorp"] * dpv

    # Rescale market dollars onto this league's pool before blending. Target the
    # same total the model column carries over the same n_priced players, so the
    # W_MODEL/W_MARKET blend is a true 55/45 and not a scale mismatch.
    mk = sorted([p for p in priced if p.get("market")],
                key=lambda x: -(x["market"] or 0))[:n_priced]
    msum = sum(p["market"] for p in mk) or 1.0
    scale = float(n_priced + surplus) / msum
    for p in players:
        p["market_adj"] = round(p["market"] * scale, 1) if p.get("market") else None
        if p["pos"] in ("K", "DEF"):
            p["base"] = 1.0
        elif p["market_adj"]:
            p["base"] = W_MODEL * p["model"] + W_MARKET * p["market_adj"]
        else:
            p["base"] = p["model"]

    # Renormalise so the board still sums to the pool after blending. The board
    # a room actually buys is the top n_priced skill players plus n_forced K/DEF
    # at $1, and that is what has to add up to total_dollars.
    top = sorted(priced, key=lambda x: -x["base"])[:n_priced]
    s = sum(max(0.0, p["base"] - 1) for p in top) or 1.0
    adj = surplus / s
    for p in players:
        # Never price a roster spot below the $1 minimum the surplus already
        # reserved for it; a budget too small to cover pool_size would otherwise
        # drive adj negative and print negative dollars.
        p["base"] = max(1.0, round(1 + max(0.0, p["base"] - 1) * adj, 1))
        if p["pos"] in ("K", "DEF"):
            p["base"] = 1.0

    # Tiers cut at genuine gaps in projected points, not fixed buckets.
    for pos, lst in by_pos.items():
        lst2 = [p for p in lst if p["proj"]]
        gaps = [(lst2[i]["proj"] - lst2[i + 1]["proj"]) for i in range(len(lst2) - 1)]
        # Only the starter-relevant top of each position gets tiered; a threshold
        # fitted over 200 replacement-level QBs is meaningless.
        head = gaps[:max(8, teams * 3)]
        if head:
            thr = max(statistics.mean(head) + 0.75 * (statistics.pstdev(head) or 0),
                      0.012 * (lst2[0]["proj"] or 1))
        else:
            thr = 1e9
        t = 1
        for i, p in enumerate(lst2):
            p["tier"] = t
            if i < len(gaps) and gaps[i] > thr:
                t += 1
        for p in lst:
            p.setdefault("tier", t)
    return players

# ---------------------------------------------------------------- live draft

def draft_state(draft_id, lg=LEAGUE):
    d = gj("https://api.sleeper.app/v1/draft/%s" % draft_id, ttl=0)
    st = d.get("settings") or {}
    slots = {"QB": st.get("slots_qb", 1), "RB": st.get("slots_rb", 2),
             "WR": st.get("slots_wr", 2), "TE": st.get("slots_te", 1),
             "FLEX": st.get("slots_flex", 1), "K": st.get("slots_k", 1),
             "DEF": st.get("slots_def", 1), "BN": st.get("slots_bn", 4)}
    if st.get("slots_super_flex"):
        slots["SUPER_FLEX"] = st["slots_super_flex"]
    league = {"teams": st.get("teams", 12), "budget": st.get("budget", 200),
              "slots": {k: v for k, v in slots.items() if v},
              "flex": ["RB", "WR", "TE"],
              "scoring": (d.get("metadata") or {}).get("scoring_type", "half_ppr")}
    picks = gj("https://api.sleeper.app/v1/draft/%s/picks" % draft_id, ttl=0)

    users, owners = {}, {}
    lid = d.get("league_id")
    if lid:
        try:
            for u in gj("https://api.sleeper.app/v1/league/%s/users" % lid,
                        key="users-%s" % lid, ttl=900):
                users[u["user_id"]] = u.get("display_name") or u.get("username")
            for r in gj("https://api.sleeper.app/v1/league/%s/rosters" % lid,
                        key="rosters-%s" % lid, ttl=900):
                owners[r["roster_id"]] = users.get(r.get("owner_id"), "Team %s" % r["roster_id"])
        except Exception:
            pass

    rows = []
    for p in picks:
        m = p.get("metadata") or {}
        try:
            amt = int(m.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0
        rows.append({
            "player_id": str(p.get("player_id") or ""),
            "roster_id": p.get("roster_id"), "amount": amt,
            "name": (m.get("first_name", "") + " " + m.get("last_name", "")).strip(),
            "pos": npos(m.get("position")), "team": nteam(m.get("team")),
            "key": pkey((m.get("first_name", "") + " " + m.get("last_name", "")).strip(),
                        m.get("position"), m.get("team")),
            "pick_no": p.get("pick_no"),
        })

    roster_size = sum(league["slots"].values())
    teams = []
    for rid in range(1, league["teams"] + 1):
        mine = [r for r in rows if r["roster_id"] == rid]
        spent = sum(r["amount"] for r in mine)
        left = league["budget"] - spent
        sl = roster_size - len(mine)
        teams.append({"roster_id": rid, "owner": owners.get(rid, "Team %d" % rid),
                      "spent": spent, "left": left, "filled": len(mine),
                      "slots_left": sl,
                      # A full roster cannot bid at all, however much is left.
                      "max_bid": 0 if sl <= 0 else max(0, left - (sl - 1)),
                      "roster": [{"name": r["name"], "pos": r["pos"],
                                  "amount": r["amount"]} for r in mine]})
    return {"draft": d, "league": league, "status": d.get("status"),
            "is_auction": d.get("type") == "auction", "picks": rows, "teams": teams,
            "name": (d.get("metadata") or {}).get("name", "Draft")}


def live_board(players, state, my_rid=None):
    lg = state["league"]
    drafted_ids = {r["player_id"] for r in state["picks"] if r["player_id"]}
    drafted_keys = {r["key"] for r in state["picks"]}
    avail = [p for p in players
             if p.get("sleeper_id") not in drafted_ids and p["key"] not in drafted_keys]

    teams = state["teams"]
    slots_left = sum(t["slots_left"] for t in teams)
    money_left = sum(t["left"] for t in teams)
    disc = money_left - slots_left                    # $1 reserved per open slot
    remaining = sorted(avail, key=lambda x: -x["base"])[:max(1, slots_left)]
    expected = sum(max(0.0, p["base"] - 1) for p in remaining) or 1.0
    infl = disc / expected if expected > 0 else 1.0
    infl = max(0.3, min(3.0, infl))

    me = next((t for t in teams if t["roster_id"] == my_rid), None)
    my_max = me["max_bid"] if me else None
    # Endgame guard: once the room is out of discretionary money the 0.3 clamp
    # still prints double-digit prices nobody can pay (measured: $21.9 on the
    # best player left when the highest max_bid in the league was $5). No player
    # can go for more than the richest single team can bid.
    room_max = max([t["max_bid"] for t in teams] or [0])
    for p in avail:
        live = round(1 + max(0.0, p["base"] - 1) * infl, 1)
        p["live"] = max(1.0, min(live, float(room_max))) if room_max else 1.0
        p["edge"] = round(p["live"] - p["market_adj"], 1) if p.get("market_adj") else None

    # What the room is actually paying vs model, by position.
    bykey = {p["key"]: p for p in players}
    market = {}
    for r in state["picks"]:
        b = bykey.get(r["key"])
        if not b or not r["amount"]:
            continue
        m = market.setdefault(r["pos"], {"spent": 0, "expected": 0.0, "n": 0})
        m["spent"] += r["amount"]
        m["expected"] += b["base"]
        m["n"] += 1
    for m in market.values():
        m["ratio"] = round(m["spent"] / m["expected"], 3) if m["expected"] else None
        m["expected"] = round(m["expected"], 1)

    # Positional scarcity: starters still unfilled vs players left above replacement.
    scarcity = []
    roster_size = sum(lg["slots"].values())
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        left_above = len([p for p in avail if p["pos"] == pos and p["vorp"] > 0])
        need = lg["teams"] * lg["slots"].get(pos, 0)
        got = len([r for r in state["picks"] if r["pos"] == pos])
        t1 = len([p for p in avail if p["pos"] == pos and p.get("tier") == 1])
        t2 = len([p for p in avail if p["pos"] == pos and p.get("tier") == 2])
        scarcity.append({"pos": pos, "above_repl": left_above,
                         "starters_left": max(0, need - got), "t1": t1, "t2": t2,
                         "cliff": t1 + t2 <= 3 and left_above > 0})

    drafted = []
    for r in sorted(state["picks"], key=lambda x: -(x["pick_no"] or 0))[:40]:
        b = bykey.get(r["key"])
        drafted.append({**r, "base": b["base"] if b else None,
                        "delta": round(r["amount"] - b["base"], 1) if b else None})

    avail.sort(key=lambda x: -x["live"])
    return {"players": avail[:400], "teams": teams, "inflation": round(infl, 3),
            "market": market, "scarcity": scarcity, "drafted": drafted,
            "league": lg, "status": state["status"], "name": state["name"],
            "my_roster_id": my_rid, "my_max": my_max,
            "totals": {"money_left": money_left, "slots_left": slots_left,
                       "picks": len(state["picks"]),
                       "pool": lg["teams"] * lg["budget"]},
            "updated": int(time.time())}

# ---------------------------------------------------------------- server

POOL = {"players": None, "lock": threading.Lock(), "meta": {}}

# Long AI calls run here instead of blocking the page. The page ships with the
# computed numbers immediately -- which is the part you need during a live
# week -- and the narrative arrives when it is ready.
JOBS = {"lock": threading.Lock(), "items": {}}
JOB_TTL = 1800


def start_job(job_id, work, render):
    """Run `work` off-thread. Returns immediately.

    Re-requesting a job that is already running or finished returns the
    existing one rather than paying for the same analysis twice -- a page
    reload while it is thinking must not start a second call.
    """
    with JOBS["lock"]:
        now = time.time()
        for k, v in list(JOBS["items"].items()):
            if now - v.get("started", now) > JOB_TTL:
                del JOBS["items"][k]
        cur = JOBS["items"].get(job_id)
        if cur:
            return cur
        JOBS["items"][job_id] = {"status": "pending", "result": None,
                                 "html": None, "started": now}

    def run():
        try:
            res = work()
            status = "error" if isinstance(res, dict) and res.get("error") else "done"
        except Exception as e:
            res, status = {"error": "job_failed", "detail": str(e)}, "error"
        html = ""
        try:
            html = render(res)
        except Exception as e:
            html = "<div class='panel'><h3>AI analysis</h3><div class='mut'>" \
                   "render failed: %s</div></div>" % e
        with JOBS["lock"]:
            JOBS["items"][job_id] = {"status": status, "result": res, "html": html,
                                     "started": time.time()}

    threading.Thread(target=run, daemon=True).start()
    with JOBS["lock"]:
        return JOBS["items"][job_id]


def ensure_pool():
    with POOL["lock"]:
        if POOL["players"] is None:
            players, errs, counts = build_pool()
            POOL["players"] = value_pool(players)
            POOL["meta"] = {"errors": errs, "counts": counts}
        return POOL["players"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        """Rebuild the source pool without restarting.

        Useful mid-draft when a source was unreachable at startup: the board
        keeps serving the cached pool while this rebuilds it in place.
        """
        if self.path.split("?")[0] != "/api/refresh":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            with POOL["lock"]:
                POOL["players"] = None
                POOL["meta"] = {}
            players = ensure_pool()
            return self._send(200, json.dumps(
                {"ok": True, "players": len(players), "season": SEASON,
                 "sources": POOL["meta"].get("counts", {}),
                 "errors": POOL["meta"].get("errors", {})}))
        except Exception as e:
            return self._send(500, json.dumps(
                {"error": "refresh failed", "detail": str(e)}))

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = v
            if path == "/":
                return self._send(200, HTML, "text/html; charset=utf-8")
            if path == "/api/board":
                did = q.get("draft") or ARGS.draft
                if not did:
                    return self._send(400, json.dumps({"error": "no draft id"}))
                players = ensure_pool()
                st = draft_state(did)
                if not st["is_auction"]:
                    return self._send(400, json.dumps({
                        "error": "not an auction draft",
                        "detail": "Draft %s is type '%s'. This board prices auction "
                                  "bids; a snake draft has no dollar amounts, so the "
                                  "values would be meaningless." % (
                                      did, (st["draft"] or {}).get("type"))}))
                rid = q.get("me")
                out = live_board(players, st, int(rid) if rid and rid.isdigit() else None)
                out["meta"] = POOL["meta"]
                return self._send(200, json.dumps(out))
            if path.startswith("/api/user/") and path.endswith("/drafts"):
                # Username -> draft picker, so a draft id is not required up
                # front. Uses sleeper_draft.py, which walks leagues then drafts
                # and normalises each draft's settings.
                uname = path[len("/api/user/"):-len("/drafts")]
                try:
                    from sleeper_auction import sleeper as sd
                except ImportError:
                    return self._send(501, json.dumps(
                        {"error": "sleeper_draft.py not available"}))
                u = sd.resolve_user(uname)
                if not u:
                    return self._send(404, json.dumps(
                        {"error": "no such Sleeper user", "detail": uname}))
                season = q.get("season") or SEASON
                return self._send(200, json.dumps(
                    {"user": u, "drafts": sd.user_drafts(u["user_id"], season)},
                    default=str))
            if path.startswith("/api/ai/"):
                jid = path[len("/api/ai/"):]
                with JOBS["lock"]:
                    j = JOBS["items"].get(jid)
                if not j:
                    return self._send(404, json.dumps(
                        {"status": "unknown",
                         "detail": "no such job (it may have expired)"}))
                return self._send(200, json.dumps(
                    {"status": j["status"], "html": j.get("html"),
                     "result": j.get("result")}, default=str))
            if path in ("/waivers", "/api/waivers"):
                did = q.get("draft") or ARGS.draft
                rid = q.get("me") or ARGS.me
                if not did or not rid:
                    return self._send(400, json.dumps(
                        {"error": "need draft id and roster id (?draft=..&me=..)"}))
                from sleeper_auction import waivers
                wb = waivers.build_board(did, int(rid), int(q.get("week") or 1),
                                         int(q.get("limit") or 25))
                job = None
                ai = None
                if not q.get("noai"):
                    jid = "wv-%s-%s-%s" % (did, rid, q.get("week") or 1)
                    if path == "/api/waivers":
                        ai = waivers.ai_analyze(wb)                 # API stays sync
                    else:
                        job = start_job(jid, lambda: waivers.ai_analyze(wb),
                                        waivers.ai_cards)
                        if job["status"] != "pending":
                            ai = job.get("result")
                if path == "/api/waivers":
                    return self._send(200, json.dumps({"board": wb, "ai": ai},
                                                      default=str))
                if q.get("text"):
                    return self._send(200, waivers.report(wb, ai),
                                      "text/plain; charset=utf-8")
                return self._send(
                    200, waivers.html_report(
                        wb, ai,
                        job_id=("wv-%s-%s-%s" % (did, rid, q.get("week") or 1))
                        if job and job["status"] == "pending" else None),
                    "text/html; charset=utf-8")
            if path in ("/sitstart", "/api/sitstart"):
                did = q.get("draft") or ARGS.draft
                rid = q.get("me") or ARGS.me
                if not did or not rid:
                    return self._send(400, json.dumps(
                        {"error": "need draft id and roster id (?draft=..&me=..)"}))
                import sleeper_auction.sitstart as sitstart
                wk = int(q.get("week") or 1)
                sl = sitstart.build_slate(did, int(rid), wk)
                ps = sitstart.posture(sl)
                import urllib.parse as _up
                wr = [_up.unquote(x) for x in (q.get("wrcb") or "").split(",") if x]
                if not wr and ARGS.wrcb:
                    wr = list(ARGS.wrcb)
                job = None
                ai = None
                if not q.get("noai"):
                    jid = "ss-%s-%s-%s" % (did, rid, wk)
                    if path == "/api/sitstart":
                        ai = sitstart.ai_analyze(sl, ps, wrcb=wr)   # API stays sync
                    else:
                        job = start_job(
                            jid,
                            lambda: sitstart.ai_analyze(sl, ps, wrcb=wr),
                            sitstart.ai_cards)
                        if job["status"] != "pending":
                            ai = job.get("result")
                if path == "/api/sitstart":
                    return self._send(200, json.dumps(
                        {"slate": sl, "posture": ps, "ai": ai}, default=str))
                if q.get("text"):
                    return self._send(200, sitstart.report(sl, ps, ai),
                                      "text/plain; charset=utf-8")
                return self._send(
                    200, sitstart.html_report(
                        sl, ps, ai,
                        job_id=("ss-%s-%s-%s" % (did, rid, wk)) if job and
                        job["status"] == "pending" else None),
                    "text/html; charset=utf-8")
            if path in ("/analysis", "/api/analysis"):
                did = q.get("draft") or ARGS.draft
                if not did:
                    return self._send(400, json.dumps({"error": "no draft id"}))
                import sleeper_auction.analysis as analyze
                a = analyze.analyze(did, ensure_pool())
                if path == "/api/analysis":
                    return self._send(200, json.dumps(a, default=str))
                if q.get("text"):
                    return self._send(200, analyze.report(a),
                                      "text/plain; charset=utf-8")
                return self._send(200, analyze.html_report(a),
                                  "text/html; charset=utf-8")
            if path == "/api/values":
                return self._send(200, json.dumps({"players": ensure_pool()[:400]}))
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}))


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auction Board</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
 font-variant-numeric:tabular-nums}
.nav{display:flex;gap:2px;align-items:center;padding:0 14px;background:#0b0f14;border-bottom:1px solid #30363d}
.nav a{padding:11px 15px;color:#8b949e;text-decoration:none;font-size:13px;font-weight:600;border-bottom:2px solid transparent}
.nav a:hover{color:#e6edf3}
.nav a.on{color:#fff;border-bottom-color:#1f6feb}
.navsp{flex:1}.navmut{color:#8b949e;font-size:12px}
.bar{display:flex;gap:18px;align-items:center;padding:10px 16px;background:#161b22;border-bottom:1px solid #30363d;
 position:sticky;top:0;z-index:20;flex-wrap:wrap}
.bar b{font-size:19px;color:#fff}
.stat{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.04em}
.stat span{display:block;font-size:19px;color:#e6edf3;font-weight:600}
.stat span.up{color:#f85149}.stat span.dn{color:#3fb950}
#nom{padding:12px 16px;background:#11161d;border-bottom:1px solid #30363d;position:sticky;top:52px;z-index:19}
#q{width:340px;padding:11px 13px;font-size:16px;background:#0d1117;border:1px solid #30363d;border-radius:7px;color:#fff}
#q:focus{outline:none;border-color:#1f6feb}
#card{display:none;margin-top:11px;padding:14px 16px;background:#161b22;border:1px solid #30363d;border-radius:9px;
 max-width:940px}
#card.on{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.big{font-size:42px;font-weight:700;line-height:1;color:#58a6ff}
.verdict{font-size:17px;font-weight:700;padding:8px 15px;border-radius:7px}
.ok{background:#12331d;color:#3fb950;border:1px solid #238636}
.no{background:#3a1518;color:#f85149;border:1px solid #7d2427}
.wrap{display:flex;gap:14px;padding:14px 16px;align-items:flex-start;flex-wrap:wrap}
.main{flex:1 1 620px;min-width:0;max-width:100%}
.rail{flex:0 1 330px;display:flex;flex-direction:column;gap:12px}
.panel{overflow-x:auto;background:#161b22;border:1px solid #30363d;border-radius:9px;padding:11px 13px}
.panel h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b949e;font-weight:600}
table{width:100%;border-collapse:collapse}
th{text-align:right;font-size:10px;text-transform:uppercase;color:#8b949e;padding:6px 7px;border-bottom:1px solid #30363d;
 cursor:pointer;user-select:none;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:6px 7px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}
tr:hover td{background:#1c2128}
tr.poor{opacity:.34}
.nm{font-weight:600;color:#fff}.sub{font-size:11px;color:#8b949e}
.live{font-size:17px;font-weight:700;color:#58a6ff}
.pos{font-size:10px;padding:2px 5px;border-radius:4px;font-weight:700}
.QB{background:#3d2b56;color:#d2a8ff}.RB{background:#0f3a2e;color:#56d364}
.WR{background:#0d3050;color:#79c0ff}.TE{background:#4a3312;color:#e3b341}
.K{background:#30363d;color:#8b949e}.DEF{background:#30363d;color:#8b949e}
.pl{color:#3fb950}.mn{color:#f85149}
.tabs{display:flex;gap:5px;margin-bottom:10px;flex-wrap:wrap}
.tab{padding:6px 13px;background:#21262d;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:13px}
.tab.on{background:#1f6feb;border-color:#1f6feb;color:#fff}
.row{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.mut{color:#8b949e}
.cliff{color:#f85149;font-weight:600}
#err{display:none;padding:9px 16px;background:#3a1518;color:#f85149;font-size:13px}
</style></head><body>
<div class="nav"><a href="#" data-p="/" id="nav-board">Draft board</a>
<a href="#" data-p="/analysis" id="nav-analysis">Analysis</a>
<a href="#" data-p="/sitstart">Sit / Start</a><a href="#" data-p="/waivers">Waivers</a>
<span class="navsp"></span><span class="navmut" id="nav-league"></span></div>
<script>(function(){var qs=location.search||'';
document.querySelectorAll('.nav a').forEach(function(a){a.href=a.dataset.p+qs;
  if(location.pathname===a.dataset.p)a.className='on';});})();</script>
<div class="bar">
  <b id="dn">—</b>
  <div class="stat">My budget<span id="mb">—</span></div>
  <div class="stat">Max bid<span id="mx">—</span></div>
  <div class="stat">Slots left<span id="sl">—</span></div>
  <div class="stat">Inflation<span id="inf">—</span></div>
  <div class="stat">Picks<span id="pk">—</span></div>
  <div class="stat">Updated<span id="up" style="font-size:13px">—</span></div>
</div>
<div id="err"></div>
<div id="nom">
  <input id="q" placeholder="Type the nominated player…  (press /)" autocomplete="off">
  <div id="card"></div>
</div>
<div class="wrap">
  <div class="main">
    <div class="tabs" id="tabs"></div>
    <div class="panel" style="padding:0"><table id="tb"></table></div>
  </div>
  <div class="rail">
    <div class="panel"><h3>Positional scarcity</h3><div id="sc"></div></div>
    <div class="panel"><h3>Room is paying</h3><div id="mkt"></div></div>
    <div class="panel"><h3>Team budgets</h3><div id="tm"></div></div>
    <div class="panel"><h3>Recent picks</h3><div id="rp"></div></div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s), P=new URLSearchParams(location.search);
let D=null, filt='ALL', sort='live', dir=-1, pinned=null;
const money=v=>v==null?'—':'$'+(Math.round(v*10)/10);
const COLS=[['name','Player'],['live','Live $'],['base','Base $'],['market_adj','Mkt $'],
            ['edge','Edge'],['ly','\'25 paid'],['proj','Proj'],['adp','ADP'],['tier','Tier']];

async function poll(){
  try{
    const u='/api/board?draft='+(P.get('draft')||'')+(P.get('me')?'&me='+P.get('me'):'');
    const r=await fetch(u); if(!r.ok) throw new Error((await r.json()).error||r.status);
    D=await r.json(); $('#err').style.display='none'; render();
  }catch(e){ $('#err').textContent='Poll failed: '+e.message; $('#err').style.display='block'; }
}
function render(){
  if(!D) return;
  $('#dn').textContent=D.name+'  ·  '+D.status;
  var nl=$('#nav-league'); if(nl) nl.textContent=D.name;
  const me=(D.teams||[]).find(t=>t.roster_id===D.my_roster_id);
  $('#mb').textContent=me?money(me.left):'—';
  $('#mx').textContent=me?money(me.max_bid):'—';
  $('#sl').textContent=me?me.slots_left:'—';
  const ip=Math.round((D.inflation-1)*100);
  const el=$('#inf'); el.textContent=(ip>0?'+':'')+ip+'%';
  el.className=ip>2?'up':(ip<-2?'dn':'');
  $('#pk').textContent=D.totals.picks;
  $('#up').textContent=new Date(D.updated*1000).toLocaleTimeString();

  const poss=['ALL','QB','RB','WR','TE','K','DEF'];
  $('#tabs').innerHTML=poss.map(p=>`<div class="tab ${p===filt?'on':''}" data-p="${p}">${p}</div>`).join('');
  document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{filt=t.dataset.p;render()});

  let rows=D.players.filter(p=>filt==='ALL'?(p.pos!=='K'&&p.pos!=='DEF'):p.pos===filt);
  rows.sort((a,b)=>((b[sort]??-1e9)-(a[sort]??-1e9))*(dir<0?1:-1));
  const mx=me?me.max_bid:1e9;
  $('#tb').innerHTML='<thead><tr>'+COLS.map(c=>`<th data-k="${c[0]}">${c[1]}</th>`).join('')+'</tr></thead><tbody>'+
    rows.slice(0,220).map(p=>`<tr class="${p.live>mx?'poor':''}">
      <td><span class="pos ${p.pos}">${p.pos}${p.pos_rank||''}</span> <span class="nm">${p.name}</span>
          <span class="sub">${p.team||''}${p.bye?' · bye '+p.bye:''}</span></td>
      <td><span class="live">${money(p.live)}</span></td><td>${money(p.base)}</td>
      <td class="mut">${money(p.market_adj)}</td>
      <td class="${p.edge>0?'pl':(p.edge<0?'mn':'')}">${p.edge==null?'—':(p.edge>0?'+':'')+p.edge}</td>
      <td class="mut">${p.ly?'$'+p.ly:'—'}</td>
      <td class="mut">${p.proj?Math.round(p.proj):'—'}</td><td class="mut">${p.adp??'—'}</td>
      <td class="mut">${p.tier??'—'}</td></tr>`).join('')+'</tbody>';
  document.querySelectorAll('th').forEach(t=>t.onclick=()=>{
    if(sort===t.dataset.k) dir=-dir; else {sort=t.dataset.k;dir=-1} render()});

  $('#sc').innerHTML=D.scarcity.filter(s=>s.pos!=='K'&&s.pos!=='DEF').map(s=>
    `<div class="row"><span><span class="pos ${s.pos}">${s.pos}</span> ${s.cliff?'<span class="cliff">CLIFF</span>':''}</span>
     <span class="mut">${s.above_repl} left · T1:${s.t1} T2:${s.t2}</span></div>`).join('');
  $('#mkt').innerHTML=Object.entries(D.market||{}).sort((a,b)=>b[1].n-a[1].n).map(([p,m])=>
    `<div class="row"><span class="pos ${p}">${p}</span><span class="${m.ratio>1.05?'mn':(m.ratio<0.95?'pl':'mut')}">
     ${m.ratio==null?'—':(m.ratio>1?'+':'')+Math.round((m.ratio-1)*100)+'%'} <span class="mut">(${m.n})</span></span></div>`).join('')
     ||'<div class="mut">No picks yet</div>';
  $('#tm').innerHTML=[...D.teams].sort((a,b)=>b.max_bid-a.max_bid).map(t=>
    `<div class="row" style="${t.roster_id===D.my_roster_id?'background:#1f2b3d;margin:0 -6px;padding:3px 6px;border-radius:4px':''}">
     <span>${t.owner}</span><span class="mut">${money(t.left)} · ${t.slots_left} slots · max ${money(t.max_bid)}</span></div>`).join('');
  $('#rp').innerHTML=(D.drafted||[]).slice(0,14).map(d=>
    `<div class="row"><span><span class="pos ${d.pos}">${d.pos}</span> ${d.name}</span>
     <span>${money(d.amount)} <span class="${d.delta>0?'mn':'pl'}">${d.delta==null?'':(d.delta>0?'+':'')+d.delta}</span></span></div>`).join('')
     ||'<div class="mut">No picks yet</div>';
  if(pinned) showCard(pinned);
}
function showCard(name){
  const p=D&&D.players.find(x=>x.name===name); const c=$('#card');
  if(!p){c.className='';return}
  const me=(D.teams||[]).find(t=>t.roster_id===D.my_roster_id);
  const mx=me?me.max_bid:null, aff=mx==null||p.live<=mx;
  c.className='on';
  c.innerHTML=`<div><div class="sub">${p.pos}${p.pos_rank||''} · ${p.team||''} · tier ${p.tier??'—'}</div>
      <div style="font-size:21px;font-weight:700">${p.name}</div></div>
    <div><div class="sub">LIVE VALUE</div><div class="big">${money(p.live)}</div></div>
    <div><div class="sub">Base / Market</div><div>${money(p.base)} / ${money(p.market_adj)}</div>
      <div class="sub">proj ${p.proj?Math.round(p.proj):'—'} · adp ${p.adp??'—'}</div></div>
    <div><div class="sub">This room paid '25</div><div style="font-size:21px;font-weight:700">${p.ly?'$'+p.ly:'—'}</div></div>
    <div class="verdict ${aff?'ok':'no'}">${aff?'BID UP TO '+money(p.live):'CAN\'T AFFORD (max '+money(mx)+')'}</div>`;
}
$('#q').addEventListener('input',e=>{
  const v=e.target.value.toLowerCase().trim(); if(!v||!D){pinned=null;$('#card').className='';return}
  const hit=D.players.find(p=>p.name.toLowerCase().includes(v));
  pinned=hit?hit.name:null; if(hit) showCard(hit.name); else $('#card').className='';
});
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&document.activeElement!==$('#q')){e.preventDefault();$('#q').focus()}
  if(e.key==='Escape'){$('#q').value='';pinned=null;$('#card').className=''}
});
poll(); setInterval(()=>{if(!document.hidden)poll()},5000);
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", default="")
    ap.add_argument("--me", default="")
    ap.add_argument("--wrcb", action="append", default=[],
                    help="WR/CB chart image or article URL used by /sitstart")
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets your phone reach it on the same wifi")
    ARGS = ap.parse_args()
    print("Loading sources (Sleeper projections + FFC ADP + ESPN auction)…")
    t0 = time.time()
    pl = ensure_pool()
    print("  %d players valued in %.1fs  %s" % (len(pl), time.time() - t0, POOL["meta"]))
    top = sorted(pl, key=lambda x: -x["base"])[:10]
    for p in top:
        print("   $%-6.1f %-24s %-4s proj=%-6s mkt=%s"
              % (p["base"], p["name"], p["pos"], round(p["proj"] or 0), p["market_adj"]))
    url = "http://localhost:%d/?draft=%s%s" % (ARGS.port, ARGS.draft,
                                               "&me=" + ARGS.me if ARGS.me else "")
    print("\n  ==>  %s\n" % url)
    if ARGS.host == "0.0.0.0":
        import socket
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("8.8.8.8", 80))
            print("  phone on same wifi:  http://%s:%d/?draft=%s%s\n"
                  % (sk.getsockname()[0], ARGS.port, ARGS.draft,
                     "&me=" + ARGS.me if ARGS.me else ""))
            sk.close()
        except Exception:
            pass
    ThreadingHTTPServer((ARGS.host, ARGS.port), H).serve_forever()
