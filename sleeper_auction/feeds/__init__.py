"""In-season data feeds (nflverse, props, projections, news, values, league).

Unlike sources/ (draft-ranking adapters with one fetch(season, scoring)
contract), feeds have different shapes and cadences. Every feed is optional:
a failure degrades that one signal and pages still render every existing
number. See docs/data-integration-plan.md.

status() is what GET /api/feeds returns; rail_html() is the one-line-per-feed
"Data" panel on each page.
"""
import html
import time

from sleeper_auction.feeds import common


def status():
    from sleeper_auction.feeds import ids
    now = int(time.time())
    feeds = {}
    for name, s in sorted(common.STATUS.items()):
        d = dict(s)
        d["enabled"] = common.enabled(name)
        d["age_s"] = now - d["checked"] if d.get("checked") else None
        feeds[name] = d
    return {"feeds": feeds, "join": ids.join_status(),
            "disabled": sorted(common.disabled()), "now": now}


def _age(s):
    if s is None:
        return "?"
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def rail_html(names=None):
    """Small freshness panel. `names` limits it to the feeds a page uses."""
    st = status()["feeds"]
    rows = []
    for n, s in st.items():
        if names and not any(n == x or n.startswith(x + ".") for x in names):
            continue
        if not s["enabled"]:
            mark, cls, why = "off", "mut", "disabled"
        elif s.get("ok") is False:
            mark, cls, why = "down", "bad", s.get("error") or ""
        elif s.get("stale"):
            mark, cls, why = "stale", "warn", s.get("error") or ""
        else:
            mark, cls, why = "ok", "good", ""
        age = s.get("cache_age_s")
        rows.append("<div title='%s'><span class='%s'>%s</span> %s <span class='mut'>%s%s</span></div>"
                    % (html.escape(why, quote=True), cls, mark, html.escape(n),
                       ("%s rows · " % s["rows"]) if s.get("rows") is not None else "",
                       "cache " + _age(age) if age is not None else ""))
    if not rows:
        return ""
    return ("<div class='panel'><h3>Data</h3><div style='font-size:12px;line-height:1.6'>%s"
            "</div><div class='mut' style='font-size:11px'>full status: "
            "<a href='/api/feeds'>/api/feeds</a></div></div>" % "".join(rows))
