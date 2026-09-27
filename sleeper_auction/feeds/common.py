"""Shared plumbing for the in-season data feeds. Python 3.9, stdlib only.

Everything a feed needs to fetch, cache, report on and snapshot lives here so
the feed modules stay about parsing and maths:

  * fetch_text / fetch_json / fetch_csv / post_json -- same cache directory and
    key rules as board.get(), plus two things board.get() does not do:
      1. a body that starts with the gzip magic (\\x1f\\x8b) is gunzipped. The
         nflverse `.csv.gz` release assets are gzip *content*, not
         Content-Encoding, so urllib hands back the compressed bytes;
      2. stale-if-error: when the live fetch fails and any cached copy exists,
         whatever its age, return it and mark the feed stale. This matches
         sources/sleeper_src._get_json.
  * status registry: each feed records ok/rows/as_of/stale/error/join counters
    with note(); feeds.status() returns them for /api/feeds.
  * FEEDS_DISABLED=props,news,... switches feeds off; enabled(name) checks it.
  * parallel(): run independent fetches together so new feeds do not add
    their latencies to a page load.
  * snapshot store under cache/history/ for practice trajectories, calibration
    and line movement. cache/ is gitignored, so history is local only.

Secrets: redact() strips apiKey=... from anything that could reach a status
message, an exception string or a log line. Never build a cache key from a URL
that carries a key; pass an explicit `key`.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CACHE = os.path.join(_ROOT, "cache")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")
# site.api.espn.com 403s a browser UA (see sitstart.ESPN_HDRS); hosts that
# want a plain client get this one.
PLAIN_UA = "python-urllib/3 sleeper-auction"

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/%s/%s"
FFOPP = "https://github.com/ffverse/ffopportunity/releases/download/latest-data/%s"


def nflverse_url(tag, file):
    return NFLVERSE % (tag, file)


def ffopp_url(file):
    return FFOPP % file

# ---------------------------------------------------------------- switches

def disabled():
    return {x.strip().lower() for x in os.environ.get("FEEDS_DISABLED", "").split(",")
            if x.strip()}


def enabled(name):
    """False when FEEDS_DISABLED names this feed or its group ('props.kalshi'
    is off when either 'props' or 'props.kalshi' is listed)."""
    off = disabled()
    if "all" in off:
        return False
    parts = name.lower().split(".")
    return not any(".".join(parts[:i + 1]) in off for i in range(len(parts)))


class FeedDisabled(Exception):
    pass

# ---------------------------------------------------------------- status

STATUS = {}
_SLOCK = threading.Lock()


def note(feed, **kw):
    """Merge fields into a feed's status. Strings are redacted."""
    with _SLOCK:
        s = STATUS.setdefault(feed, {"ok": None, "rows": None, "as_of": None,
                                     "cache_age_s": None, "stale": False,
                                     "error": None})
        for k, v in kw.items():
            s[k] = redact(v) if isinstance(v, str) else v
        if kw.get("cache_age_s") is not None:
            s["as_of"] = time.strftime("%Y-%m-%dT%H:%MZ",
                                       time.gmtime(time.time() - kw["cache_age_s"]))
        s["checked"] = int(time.time())


def ok(feed, rows=None, **kw):
    note(feed, ok=True, rows=rows, error=None, **kw)


def fail(feed, err, **kw):
    if isinstance(err, FeedDisabled):
        note(feed, ok=None, error="disabled by FEEDS_DISABLED", **kw)
        return
    note(feed, ok=False, error="%s: %s" % (type(err).__name__, redact(str(err)))
         if isinstance(err, BaseException) else redact(str(err)), **kw)


_KEYRE = re.compile(r"(api_?key|apikey|token|key)=([^&\s\"']+)", re.I)


def redact(s):
    if not isinstance(s, str):
        return s
    s = _KEYRE.sub(lambda m: m.group(1) + "=***", s)
    k = os.environ.get("ODDS_API_KEY")
    if k and len(k) > 6:
        s = s.replace(k, "***")
    return s

# ---------------------------------------------------------------- http + cache

def cache_path(key):
    return os.path.join(CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", key))


def cache_age(key):
    p = cache_path(key)
    return time.time() - os.path.getmtime(p) if os.path.exists(p) else None


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _decode(raw, ce=None):
    if ce == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        # A .csv.gz asset served with Content-Encoding: gzip is gzip twice.
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", "replace")


def _request(url, headers=None, data=None, timeout=45):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _decode(r.read(), r.headers.get("Content-Encoding")), dict(r.headers)


def fetch_text(url, key, ttl, headers=None, stale_ok=True, feed=None, data=None,
               timeout=45, want_headers=False):
    """GET (or POST when `data` is bytes) through the cache.

    `key` names the cache file and is required: never derive it from a URL
    that could carry a secret. Returns the text (and the response headers
    when want_headers; headers are {} on a cache hit).
    """
    p = cache_path(key)
    age = cache_age(key)
    if age is not None and age < ttl:
        if feed:
            note(feed, cache_age_s=int(age), stale=False)
        txt = _read(p)
        return (txt, {}) if want_headers else txt
    try:
        txt, hdrs = _request(url, headers, data, timeout)
    except Exception as e:
        if stale_ok and age is not None:
            if feed:
                note(feed, stale=True, cache_age_s=int(age),
                     error="live fetch failed, serving stale cache: %s" % redact(str(e)))
            txt = _read(p)
            return (txt, {}) if want_headers else txt
        raise type(e)(redact(str(e))) if isinstance(e, (ValueError, OSError)) and \
            not isinstance(e, urllib.error.HTTPError) else e
    os.makedirs(CACHE, exist_ok=True)
    tmp = p + ".tmp%d" % threading.get_ident()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, p)
    if feed:
        note(feed, cache_age_s=0, stale=False)
    return (txt, hdrs) if want_headers else txt


def fetch_json(url, key, ttl, headers=None, stale_ok=True, feed=None, timeout=45):
    return json.loads(fetch_text(url, key, ttl, headers, stale_ok, feed, timeout=timeout))


def post_json(url, body, key, ttl, headers=None, stale_ok=True, feed=None, timeout=45):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    return json.loads(fetch_text(url, key, ttl, h, stale_ok, feed,
                                 data=json.dumps(body).encode(), timeout=timeout))


def parse_csv(txt):
    return list(csv.DictReader(io.StringIO(txt)))


def fetch_csv(urls, key, ttl, feed=None, stale_ok=True, timeout=90):
    """Try each candidate URL in order (.csv.gz first, then .csv)."""
    if isinstance(urls, str):
        urls = [urls]
    last = None
    for u in urls:
        try:
            return parse_csv(fetch_text(u, key, ttl, stale_ok=False, feed=feed,
                                        timeout=timeout))
        except Exception as e:
            last = e
    if stale_ok and cache_age(key) is not None:
        if feed:
            note(feed, stale=True, cache_age_s=int(cache_age(key)),
                 error="live fetch failed, serving stale cache: %s" % redact(str(last)))
        return parse_csv(_read(cache_path(key)))
    raise last


def num(v):
    """Tolerant float: None for '', 'NA', NaN and junk (as sources/base.record)."""
    try:
        if v is None or v == "" or v == "NA":
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def sid_str(v):
    """Normalise an external ID to a stripped string, or None."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return None if s in ("", "NA", "None", "null", "0") else s

# ---------------------------------------------------------------- parallel

def parallel(tasks, max_workers=8, timeout=60):
    """Run {name: thunk} concurrently. Returns (results, errors) dicts.

    A thunk that raises lands in errors; one that is still running at the
    timeout lands in errors as 'timeout' (its thread finishes in the
    background; the cache keeps its work).
    """
    results, errors = {}, {}
    if not tasks:
        return results, errors
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(tasks)))
    futs = {ex.submit(fn): name for name, fn in tasks.items()}
    done, pending = wait(futs, timeout=timeout)
    for f in done:
        n = futs[f]
        try:
            results[n] = f.result()
        except Exception as e:
            errors[n] = "%s: %s" % (type(e).__name__, redact(str(e)))
    for f in pending:
        errors[futs[f]] = "timeout"
    ex.shutdown(wait=False)
    return results, errors

# ---------------------------------------------------------------- snapshots

def _snapdir(kind, season, week):
    return os.path.join(CACHE, "history", str(season), "w%02d" % int(week), kind)


def snapshot(kind, season, week, payload, ts=None):
    """Write payload unless identical to the latest snapshot. Returns path or None."""
    if payload is None:
        return None
    body = json.dumps(payload, sort_keys=True, default=str)
    h = hashlib.sha1(body.encode()).hexdigest()
    d = _snapdir(kind, season, week)
    os.makedirs(d, exist_ok=True)
    prev = sorted(x for x in os.listdir(d) if x.endswith(".json"))
    if prev:
        try:
            with open(os.path.join(d, prev[-1]), encoding="utf-8") as f:
                if hashlib.sha1(json.dumps(json.load(f), sort_keys=True,
                                           default=str).encode()).hexdigest() == h:
                    return None
        except Exception:
            pass
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime(ts or time.time()))
    p = os.path.join(d, stamp + ".json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def snapshots(kind, season, week):
    """[(utc 'yyyymmddHHMM', payload)] oldest first."""
    d = _snapdir(kind, season, week)
    if not os.path.isdir(d):
        return []
    out = []
    for x in sorted(os.listdir(d)):
        if x.endswith(".json"):
            try:
                with open(os.path.join(d, x), encoding="utf-8") as f:
                    out.append((x[:-5], json.load(f)))
            except Exception:
                continue
    return out
