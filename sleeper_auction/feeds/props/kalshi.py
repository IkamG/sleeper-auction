"""Kalshi NFL player-prop ladders. Public market data, no key.

Python 3.9, stdlib only.

Probed 2026-09-27 (Sunday of week 3, just before the 1 pm ET games):
  * Base https://api.elections.kalshi.com/trade-api/v2 (the documented
    external-api.kalshi.com host answers identically). Read endpoints need no
    auth.
  * GET /series?category=Sports returns ~3,900 series (~6 MB; the category
    filter is not applied server-side), 352 starting KXNFL. Most are season
    or novelty markets. The per-game player ladders are the tickers in
    SERIES below; each market is one rung "Player: N+ <stat>".
  * GET /markets?series_ticker=X&status=open&limit=1000 paginates with
    `cursor` (KXNFLRECYDS and KXNFLREC both exceeded 1,000 open markets).
  * Market fields as documented: yes_bid_dollars / yes_ask_dollars /
    last_price_dollars are dollar strings ("0.5100" = P 0.51),
    open_interest_fp, floor_strike (69.5 for "70+", strike_type "greater").
    Threshold k = floor(floor_strike) + 1.
  * custom_strike.football_player is a UUID that IS Sleeper's `kalshi_id`:
    308 of 338 players joined by id; all 30 misses were D/ST markets.
  * KXNFLTD is an anytime-TD LADDER (1+, 2+, 3+ touchdowns), so E[TDs] is the
    sum of the rungs. KXNFLANYTD and KXNFL2TD had no open markets.
  * KXNFLLADDER* series have no floor_strike (a different contract shape) and
    duplicate the per-game ladders; they are not used.
  * Liquidity is thin: open interest was 0 on most rungs, with 2-cent
    spreads. The pricing rule (mid when spread <= 0.10, else last trade when
    OI >= 50) keeps tight quotes and drops wide ones.
  * Markets stay "active" during a game and then trade on live state, so any
    market for a game that has kicked off is dropped.

    python3 -m sleeper_auction.feeds.props --probe --week N
"""
import math
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))))

from sleeper_auction.feeds import common, ids

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FEED = "props.kalshi"

# Verified series -> canonical market key.
SERIES = {
    "KXNFLPASSYDS": "pass_yd", "KXNFLPASSTDS": "pass_td", "KXNFLPASSINT": "pass_int",
    "KXNFLPASSATT": "pass_att", "KXNFLPASSCOMP": "pass_cmp",
    "KXNFLRSHYDS": "rush_yd", "KXNFLRSHATT": "rush_att",
    "KXNFLREC": "rec", "KXNFLRECYDS": "rec_yd",
    "KXNFLTD": "td",
}
COUNT_MARKETS = ("td", "pass_td", "pass_int", "rec")    # N+ rungs on integers
# Title keywords used only to REPORT KXNFL series that look like per-game
# player stats but are not in SERIES, so new ones get noticed.
_WATCH = ("yards", "receptions", "touchdown", "attempts", "completions", "interception")
MAX_SPREAD = 0.10
MIN_OI_FOR_LAST = 50
POSITIONS = ("QB", "RB", "WR", "TE")


def _ttl(first_kickoff_ts=None):
    if first_kickoff_ts and 0 < first_kickoff_ts - time.time() < 2 * 3600:
        return 300
    return 900


def discover():
    """Unmapped KXNFL series whose titles look like player stats (24 h cache)."""
    try:
        d = common.fetch_json(BASE + "/series?category=Sports", "kalshi-series", 86400,
                              feed=FEED, timeout=60)
    except Exception as e:
        common.fail(FEED, e)
        return []
    out = []
    for s in d.get("series") or []:
        t = s.get("ticker") or ""
        title = (s.get("title") or "").lower()
        if t.startswith("KXNFL") and t not in SERIES and "LADDER" not in t \
                and any(w in title for w in _WATCH) and "season" not in title \
                and "career" not in title and "most" not in title:
            out.append({"ticker": t, "title": s.get("title")})
    return out


def fetch_series(ticker, ttl=900):
    """All open markets for one series, following the cursor."""
    out, cursor, page = [], "", 0
    while True:
        url = BASE + "/markets?series_ticker=%s&status=open&limit=1000" % ticker
        if cursor:
            url += "&cursor=" + cursor
        d = common.fetch_json(url, "kalshi-m-%s-%d" % (ticker, page), ttl, feed=FEED)
        out.extend(d.get("markets") or [])
        cursor = d.get("cursor") or ""
        page += 1
        if not cursor or page >= 10:
            break
        time.sleep(0.1)
    return out


def price(m):
    """Probability for one rung, or None when the quote is not trustworthy."""
    bid = common.num(m.get("yes_bid_dollars")) or 0.0
    ask = common.num(m.get("yes_ask_dollars")) or 0.0
    last = common.num(m.get("last_price_dollars")) or 0.0
    oi = common.num(m.get("open_interest_fp")) or 0.0
    if bid > 0 and ask > 0 and ask - bid <= MAX_SPREAD:
        return round((bid + ask) / 2.0, 4), ask - bid
    if last > 0 and oi >= MIN_OI_FOR_LAST:
        return last, None
    return None, None


def threshold(m):
    f = common.num(m.get("floor_strike"))
    if f is not None:
        return float(math.floor(f) + 1) if (m.get("strike_type") or "greater") == "greater" \
            else f
    import re
    x = re.search(r":\s*(\d+)\+", m.get("title") or "")
    return float(x.group(1)) if x else None


def parse(markets, market_key, cw=None, started=None):
    """{sid: {"ladder": [(k,p)], "spread": avg, "name", "rungs_dropped"}}.

    `started(sid)` -> True drops a player whose game has kicked off.
    """
    cw = cw or ids.crosswalk()
    per, unmatched = {}, set()
    for m in markets:
        name = (m.get("title") or "").split(":")[0].strip()
        uid = ((m.get("custom_strike") or {}).get("football_player")) or ""
        sid = cw["by"]["kalshi"].get(uid) if uid else None
        if not sid:
            for pos in POSITIONS:
                sid = ids.to_sleeper("kalshi", None, name, pos, cw=cw, count=False)
                if sid:
                    break
        if not sid:
            if " D/ST" not in name:
                unmatched.add(name)
            continue
        if started and started(sid):
            continue
        k = threshold(m)
        p, spread = price(m)
        d = per.setdefault(sid, {"name": name, "ladder": [], "spreads": [], "dropped": 0,
                                 "event": m.get("event_ticker"),
                                 "oi": 0.0})
        if k is None or p is None:
            d["dropped"] += 1
            continue
        d["ladder"].append((k, p))
        d["oi"] += common.num(m.get("open_interest_fp")) or 0.0
        if spread is not None:
            d["spreads"].append(spread)
    out = {}
    for sid, d in per.items():
        if len(d["ladder"]) < (1 if market_key in COUNT_MARKETS else 2):
            continue
        out[sid] = {"ladder": sorted(d["ladder"]), "name": d["name"], "market": market_key,
                    "spread": round(sum(d["spreads"]) / len(d["spreads"]), 3)
                    if d["spreads"] else None,
                    "oi": d["oi"], "rungs_dropped": d["dropped"], "event": d["event"]}
    return out, sorted(unmatched)


def week_markets(started=None, first_kickoff_ts=None):
    """{market_key: {sid: ladder-info}} across SERIES, plus unmatched names."""
    if not common.enabled(FEED):
        raise common.FeedDisabled(FEED)
    cw = ids.crosswalk()
    out, unmatched, n = {}, set(), 0
    errs = []
    for ticker, key in SERIES.items():
        try:
            ms = fetch_series(ticker, _ttl(first_kickoff_ts))
        except Exception as e:
            errs.append("%s: %s" % (ticker, common.redact(str(e))))
            continue
        n += len(ms)
        parsed, um = parse(ms, key, cw, started)
        out[key] = parsed
        unmatched |= set(um)
        time.sleep(0.1)
    if not out and errs:
        common.fail(FEED, "; ".join(errs))
    else:
        common.ok(FEED, rows=n, players=len({s for v in out.values() for s in v}),
                  unmatched=len(unmatched), errors=errs or None)
    return out, sorted(unmatched)
