"""Player news: ESPN per-player fantasy news (RotoWire blurbs), RotoWire's NFL
RSS feed, and Sleeper's news_updated flag. Python 3.9, stdlib only.

Probed 2026-09-27:
  * GET site.api.espn.com/apis/fantasy/v2/games/ffl/news/players?playerId=
    {espn_id}&limit=N with a plain User-Agent: `feed[]` of 20 items for Trey
    McBride (espn 4361307), fields headline, description, story (HTML),
    published, lastModified, type ("Rotowire" / "Story" / ...), playerId.
    headline and description are the same RotoWire sentence, truncated. The
    full response is ~220 KB, so limit=6 is requested. League-wide calls
    (no playerId) 500, so it is per player only.
  * https://www.rotowire.com/rss/news.php?sport=NFL (listed on /rss/) is RSS
    2.0 with only the latest 5 items; each <link> ends in the player slug and
    RotoWire id ("tyson-bagent-16946"), which joins on Sleeper's rotowire_id.
    pubDate is "Sun, 27 Sep 2026 10:09:00 AM PDT".
  * Sleeper players carry news_updated (epoch ms): a cheap "something
    changed" flag across every player.

Pages call player_news(..., cache_only=True): they get what is cached and a
background thread fetches what is missing or older than 30 minutes, so news
never delays a page. Items are REPORTED news (dated, attributed), a different
class from the r/fantasyfootball titles the waiver page labels unverified.
"""
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from sleeper_auction.feeds import common, ids

ESPN = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players?playerId=%s&limit=6"
RW_RSS = "https://www.rotowire.com/rss/news.php?sport=NFL"
TTL = 1800
FEED = "news.espn"
MAX_CHARS = 400
_BG = {"lock": threading.Lock(), "running": False}


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("Visit RotoWire.com for more analysis on this update.", "").strip()
    return s[:MAX_CHARS]


def parse_espn(d, max_age_days=7, per_player=3, now=None):
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=max_age_days)
    items = []
    for x in d.get("feed") or []:
        try:
            pub = datetime.strptime((x.get("published") or "")[:19], "%Y-%m-%dT%H:%M:%S") \
                .replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if pub < cut:
            continue
        text = _clean(x.get("story")) or _clean(x.get("description"))
        items.append({"headline": _clean(x.get("headline"))[:160], "text": text,
                      "published": pub.strftime("%Y-%m-%dT%H:%MZ"), "type": x.get("type"),
                      "source": "espn"})
    # RotoWire blurbs first (player-specific), then newest.
    items.sort(key=lambda i: i["published"], reverse=True)
    items.sort(key=lambda i: i["type"] != "Rotowire")
    return items[:per_player]


def _fetch_one(sid, espn_id, cache_only):
    key = "news-espn-%s" % espn_id
    if cache_only:
        with common.cache_only():
            return common.fetch_json(ESPN % espn_id, key, TTL, headers={"User-Agent":
                                                                          common.PLAIN_UA})
    return common.fetch_json(ESPN % espn_id, key, TTL, headers={"User-Agent": common.PLAIN_UA},
                             feed=FEED)


def _background(pairs):
    with _BG["lock"]:
        if _BG["running"]:
            return
        _BG["running"] = True

    def run():
        try:
            n = 0
            for sid, eid in pairs:
                age = common.cache_age("news-espn-%s" % eid)
                if age is not None and age < TTL:
                    continue
                try:
                    _fetch_one(sid, eid, False)
                    n += 1
                except Exception as e:
                    common.fail(FEED, e)
                time.sleep(0.2)
            if n:
                common.ok(FEED, rows=n)
        finally:
            _BG["running"] = False
    threading.Thread(target=run, daemon=True).start()


def player_news(sleeper_ids, max_age_days=7, per_player=2, cache_only=True):
    """{sid: [items]} from ESPN, merged with RotoWire RSS; newest first."""
    if not common.enabled("news"):
        raise common.FeedDisabled("news")
    cw = ids.crosswalk()
    pairs, out, missing = [], {}, []
    for sid in dict.fromkeys(str(s) for s in sleeper_ids if s):
        eid = ids.external(sid, "espn", cw)
        if not eid:
            continue
        pairs.append((sid, eid))
        try:
            items = parse_espn(_fetch_one(sid, eid, cache_only), max_age_days, per_player)
            if items:
                out[sid] = items
            age = common.cache_age("news-espn-%s" % eid)
            if age is None or age >= TTL:
                missing.append((sid, eid))
        except common.CacheMiss:
            missing.append((sid, eid))
        except Exception as e:
            common.fail(FEED, e)
    if cache_only and missing:
        _background(missing[:60])
    try:
        for sid, items in rotowire_rss().items():
            if sid in pairs_ids(pairs):
                have = {i["headline"][:60].lower() for i in out.get(sid, [])}
                for it in items:
                    if it["headline"][:60].lower() not in have:
                        out.setdefault(sid, []).insert(0, it)
                out[sid] = out[sid][:per_player]
    except Exception as e:
        common.fail("news.rotowire", e)
    common.note(FEED, ok=True, rows=len(out), pending=len(missing))
    return out


def pairs_ids(pairs):
    return {s for s, _ in pairs}


def _rw_time(s):
    for fmt in ("%a, %d %b %Y %I:%M:%S %p %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            d = datetime.strptime(s.replace("PDT", "UTC").replace("PST", "UTC"), fmt)
            off = 7 if "PDT" in s else 8 if "PST" in s else 0
            return (d.replace(tzinfo=timezone.utc) + timedelta(hours=off))
        except ValueError:
            continue
    return None


def parse_rss(txt, cw=None):
    cw = cw or ids.crosswalk()
    out = {}
    root = ET.fromstring(txt)
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        m = re.search(r"-(\d+)/?$", link)
        title = it.findtext("title") or ""
        name = title.split(":")[0].strip()
        sid = None
        if m:
            sid = ids.to_sleeper("rotowire", m.group(1), cw=cw)
        if not sid:
            for pos in ("QB", "RB", "WR", "TE", "K"):
                sid = ids.to_sleeper("rotowire", None, name, pos, cw=cw, count=False)
                if sid:
                    break
        if not sid:
            continue
        pub = _rw_time(it.findtext("pubDate") or "")
        out.setdefault(sid, []).append({
            "headline": _clean(title)[:160], "text": _clean(it.findtext("description")),
            "published": pub.strftime("%Y-%m-%dT%H:%MZ") if pub else None,
            "type": "Rotowire", "source": "rotowire-rss"})
    return out


def rotowire_rss():
    """{sid: [items]} from RotoWire's NFL RSS (latest items only; 30 min cache)."""
    if not common.enabled("news.rotowire"):
        return {}
    txt = common.fetch_text(RW_RSS, "news-rotowire-nfl", TTL, feed="news.rotowire")
    out = parse_rss(txt)
    common.ok("news.rotowire", rows=sum(len(v) for v in out.values()))
    return out


def recently_updated(hours=48):
    """Sleeper ids whose Sleeper news_updated is within `hours`."""
    cut = (time.time() - hours * 3600) * 1000
    return [sid for sid, p in ids.players().items()
            if (p.get("news_updated") or 0) >= cut and p.get("active")
            and p.get("position") in ("QB", "RB", "WR", "TE", "K")]
