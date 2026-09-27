"""The Odds API player props (official, keyed). Enabled when ODDS_API_KEY is set.

Python 3.9, stdlib only.

Probed 2026-09-27 with the user's key:
  * GET /v4/sports/americanfootball_nfl/events is free (x-requests-last: 0)
    and returned 29 upcoming events.
  * GET /v4/sports/americanfootball_nfl/events/{id}/odds with
    markets=player_reception_yds, regions=us, bookmakers=draftkings,fanduel
    returned props on the FREE plan: 22 outcomes per book, fields name
    (Over/Under), description (player), price (American), point. It cost 1
    credit (x-requests-last: 1); remaining went 500 -> 499.
  * So the free tier (500 credits a month) includes player props. A full slate
    is ~16 events x 6 markets = ~96 credits, about one pull a week; the 24 h
    per-event cache and the reserve guard keep it inside the quota.

SECRETS: the key is read from ODDS_API_KEY only. It never appears in a cache
key, a status message, a log line or an exception (common.redact strips it),
and the quota file stores only counts.
"""
import json
import os
import statistics
import sys
import time
import urllib.error
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))))

from sleeper_auction.feeds import common, ids
from sleeper_auction.feeds.props import implied

BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
FEED = "props.oddsapi"
MARKETS = {"player_pass_yds": "pass_yd", "player_pass_tds": "pass_td",
           "player_rush_yds": "rush_yd", "player_reception_yds": "rec_yd",
           "player_receptions": "rec", "player_anytime_td": "anytime_td"}
BOOKS = "draftkings,fanduel"
RESERVE = int(os.environ.get("ODDS_API_RESERVE", "60"))
EVENT_TTL = 86400
QUOTA_FILE = "oddsapi-quota.json"
POSITIONS = ("QB", "RB", "WR", "TE")


def _key():
    return os.environ.get("ODDS_API_KEY") or None


def _quota():
    try:
        with open(common.cache_path(QUOTA_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_quota(hdrs, disabled=None):
    q = _quota()
    rem = hdrs.get("x-requests-remaining") or hdrs.get("X-Requests-Remaining")
    used = hdrs.get("x-requests-used") or hdrs.get("X-Requests-Used")
    if rem is not None:
        q["remaining"] = int(float(rem))
    if used is not None:
        q["used"] = int(float(used))
    if disabled:
        q["disabled_until"] = time.time() + 86400
        q["disabled_reason"] = disabled
    q["at"] = int(time.time())
    os.makedirs(common.CACHE, exist_ok=True)
    with open(common.cache_path(QUOTA_FILE), "w", encoding="utf-8") as f:
        json.dump(q, f)


def available():
    if not _key() or not common.enabled(FEED):
        return False, "no ODDS_API_KEY" if not _key() else "disabled"
    q = _quota()
    if q.get("disabled_until", 0) > time.time():
        return False, q.get("disabled_reason") or "disabled for the day"
    return True, None


def _get(path, params, key, ttl):
    url = "%s%s?apiKey=%s&%s" % (BASE, path, _key(), params)
    try:
        txt, hdrs = common.fetch_text(url, key, ttl, feed=FEED, want_headers=True)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 422, 429):
            _save_quota({}, disabled="HTTP %d from The Odds API" % e.code)
        raise urllib.error.HTTPError(common.redact(e.url or ""), e.code,
                                     common.redact(str(e.reason)), None, None)
    if hdrs:
        _save_quota(hdrs)
    return json.loads(txt)


def events():
    return _get("/events", "dateFormat=iso", "oddsapi-events", 3600)


def event_odds(eid, refresh=False):
    q = _quota()
    key = "oddsapi-ev-%s" % eid
    age = common.cache_age(key)
    fresh = age is not None and age < EVENT_TTL and not refresh
    if not fresh and q.get("remaining") is not None and q["remaining"] < RESERVE:
        raise RuntimeError("quota reserve reached (%s left, reserve %d)"
                           % (q["remaining"], RESERVE))
    return _get("/events/%s/odds" % eid,
                "regions=us&markets=%s&oddsFormat=american&bookmakers=%s"
                % (",".join(MARKETS), BOOKS), key, 0 if refresh else EVENT_TTL)


def normalise(ev):
    """Event odds -> [{player, market, line, over_price, under_price, book}]."""
    rows = {}
    for b in ev.get("bookmakers") or []:
        for m in b.get("markets") or []:
            mk = MARKETS.get(m.get("key"))
            if not mk:
                continue
            for o in m.get("outcomes") or []:
                who = o.get("description")
                if not who:
                    continue
                r = rows.setdefault((who, mk, b.get("key"), o.get("point")),
                                    {"player": who, "market": mk, "book": b.get("key"),
                                     "line": o.get("point"), "over_price": None,
                                     "under_price": None})
                nm = (o.get("name") or "").lower()
                if nm in ("over", "yes"):
                    r["over_price"] = o.get("price")
                elif nm in ("under", "no"):
                    r["under_price"] = o.get("price")
    return list(rows.values())


def consensus(rows):
    """Per (player, market): median line and de-vigged p_over at that line."""
    by = {}
    for r in rows:
        by.setdefault((r["player"], r["market"]), []).append(r)
    out = {}
    for (who, mk), rs in by.items():
        if mk == "anytime_td":
            ps = []
            for r in rs:
                if r["over_price"] is None:
                    continue
                po = implied.american_to_prob(r["over_price"])
                if r["under_price"] is not None:
                    ps.append(implied.devig(po, implied.american_to_prob(r["under_price"])))
                else:
                    ps.append(implied.one_sided(po))
            if ps:
                out[(who, mk)] = {"p": round(statistics.mean(ps), 3), "books": len(ps)}
            continue
        pairs = [r for r in rs if r["line"] is not None and r["over_price"] is not None
                 and r["under_price"] is not None]
        if not pairs:
            continue
        # Books often hang different lines (59.5 at one, 58.5 at another).
        # Each book's (line, de-vigged p_over) is its own estimate, so all of
        # them are kept and the mean is averaged across books downstream.
        per = [(r["line"], implied.devig(implied.american_to_prob(r["over_price"]),
                                         implied.american_to_prob(r["under_price"])))
               for r in pairs]
        out[(who, mk)] = {"line": statistics.median(x for x, _ in per),
                          "p_over": round(statistics.mean(p for _, p in per), 3),
                          "books": len(per), "per_book": per}
    return out


def book_mean(mk, v):
    """Average each book's own estimate of the mean (and median, p10, p90)."""
    ests = []
    for line, p in v.get("per_book") or [(v["line"], v["p_over"])]:
        if mk in ("pass_td", "pass_int"):
            ests.append({"mean": implied.lambda_from_ou(line, p)})
        else:
            ests.append(implied.expected_from_ou(mk, line, p))
    return {k: round(statistics.mean(e[k] for e in ests), 2) for k in ests[0]}


def to_sleeper(name, cw):
    for pos in POSITIONS:
        sid = ids.to_sleeper("oddsapi", None, name, pos, cw=cw)
        if sid:
            return sid
    return None


def week_markets(kickoff_window=None, refresh=False):
    """{market_key: {sid: {...}}} for events in the window that have not started."""
    ok, why = available()
    if not ok:
        common.note(FEED, ok=None, error=why)
        return {}, []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        evs = events()
    except Exception as e:
        common.fail(FEED, e)
        return {}, []
    lo, hi = kickoff_window or (now, "9999")
    todo = [e for e in evs if e.get("commence_time", "") > now
            and lo <= e.get("commence_time", "") <= hi]
    cw = ids.crosswalk()
    out, unmatched, errs = {}, set(), []
    for e in todo:
        try:
            ev = event_odds(e["id"], refresh)
        except Exception as ex:
            errs.append(common.redact(str(ex)))
            if "quota" in str(ex):
                break
            continue
        for (who, mk), v in consensus(normalise(ev)).items():
            sid = to_sleeper(who, cw)
            if not sid:
                unmatched.add(who)
                continue
            v = dict(v, name=who, event=e["id"], commence=e.get("commence_time"))
            if mk != "anytime_td":
                v.update(book_mean(mk, v))
            v.pop("per_book", None)
            out.setdefault(mk, {})[sid] = v
    q = _quota()
    if out or not errs:
        common.ok(FEED, rows=sum(len(v) for v in out.values()), events=len(todo),
                  quota_remaining=q.get("remaining"), unmatched=len(unmatched),
                  errors=errs[:3] or None)
    else:
        common.fail(FEED, "; ".join(errs[:3]), quota_remaining=q.get("remaining"))
    return out, sorted(unmatched)


def _probe():
    ok, why = available()
    print("available:", ok, why or "")
    if not ok:
        return
    evs = events()
    print("events:", len(evs), "quota:", _quota())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = next((x for x in evs if x.get("commence_time", "") > now), None)
    if not e:
        return
    ev = _get("/events/%s/odds" % e["id"],
              "regions=us&markets=player_reception_yds&oddsFormat=american&bookmakers=%s"
              % BOOKS, "oddsapi-probe-%s" % e["id"], 3600)
    rows = normalise(ev)
    print("probe event %s @ %s: %d outcomes; quota %s" % (
        e.get("away_team"), e.get("home_team"), len(rows), _quota()))
    for (who, mk), v in list(consensus(rows).items())[:3]:
        print("  ", who, mk, v)


if __name__ == "__main__":
    if "--probe" in sys.argv:
        _probe()
    else:
        print(__doc__)
