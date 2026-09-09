"""Shared helpers for ranking-source adapters. Python 3.9, stdlib only."""
import gzip
import io
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
DEFAULT_TTL = 6 * 3600
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

SCORINGS = ("half_ppr", "ppr", "std")
DEFAULT_SCORING = "half_ppr"  # league default; half-PPR is the priority format

# Canonical positions we care about.
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

_POS_ALIASES = {
    "PK": "K", "K": "K",
    "DST": "DEF", "D/ST": "DEF", "DEF": "DEF", "D": "DEF", "DEFENSE": "DEF",
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "FB": "RB",
}

_TEAM_ALIASES = {
    "JAX": "JAX", "JAC": "JAX", "WAS": "WAS", "WSH": "WAS", "LAR": "LAR", "LA": "LAR",
    "STL": "LAR", "SD": "LAC", "LAC": "LAC", "OAK": "LV", "LV": "LV", "LVR": "LV",
    "ARZ": "ARI", "ARI": "ARI", "BLT": "BAL", "BAL": "BAL", "CLV": "CLE", "CLE": "CLE",
    "HST": "HOU", "HOU": "HOU", "SF": "SF", "SFO": "SF", "TB": "TB", "TBB": "TB",
    "NE": "NE", "NEP": "NE", "NO": "NO", "NOS": "NO", "GB": "GB", "GBP": "GB",
    "KC": "KC", "KCC": "KC", "NYG": "NYG", "NYJ": "NYJ", "PHI": "PHI", "PIT": "PIT",
}

# City / nickname -> team abbreviation, for matching D/ST rows across sources.
TEAM_NAMES = {
    "arizona": "ARI", "cardinals": "ARI", "atlanta": "ATL", "falcons": "ATL",
    "baltimore": "BAL", "ravens": "BAL", "buffalo": "BUF", "bills": "BUF",
    "carolina": "CAR", "panthers": "CAR", "chicago": "CHI", "bears": "CHI",
    "cincinnati": "CIN", "bengals": "CIN", "cleveland": "CLE", "browns": "CLE",
    "dallas": "DAL", "cowboys": "DAL", "denver": "DEN", "broncos": "DEN",
    "detroit": "DET", "lions": "DET", "green bay": "GB", "packers": "GB",
    "houston": "HOU", "texans": "HOU", "indianapolis": "IND", "colts": "IND",
    "jacksonville": "JAX", "jaguars": "JAX", "kansas city": "KC", "chiefs": "KC",
    "las vegas": "LV", "raiders": "LV", "oakland": "LV",
    "los angeles chargers": "LAC", "chargers": "LAC",
    "los angeles rams": "LAR", "rams": "LAR",
    "miami": "MIA", "dolphins": "MIA", "minnesota": "MIN", "vikings": "MIN",
    "new england": "NE", "patriots": "NE", "new orleans": "NO", "saints": "NO",
    "new york giants": "NYG", "giants": "NYG", "new york jets": "NYJ", "jets": "NYJ",
    "philadelphia": "PHI", "eagles": "PHI", "pittsburgh": "PIT", "steelers": "PIT",
    "san francisco": "SF", "49ers": "SF", "niners": "SF",
    "seattle": "SEA", "seahawks": "SEA", "tampa bay": "TB", "buccaneers": "TB",
    "tennessee": "TEN", "titans": "TEN", "washington": "WAS", "commanders": "WAS",
}

_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]")
_WS_RE = re.compile(r"\s+")

# Cross-source nickname collisions we resolve by hand. MUST stay identical to
# quick/draftboard.NAME_FIXES -- the board joins adapter keys built here against
# a spine keyed by draftboard.pkey(), so any divergence silently drops a source.
# NOTE: "kenneth walker iii" / "brian robinson jr" are unreachable, because
# _SUFFIX_RE already strips the suffix before this lookup runs. Kept verbatim so
# the two tables stay diffable.
_NAME_FIXES = {
    "mitch trubisky": "mitchell trubisky",
    "gabe davis": "gabriel davis",
    "josh palmer": "joshua palmer",
    "chig okonkwo": "chigoziem okonkwo",
    "cam ward": "cameron ward",
    "tank dell": "nathaniel dell",
    "hollywood brown": "marquise brown",
    "kenneth walker iii": "kenneth walker",
    "brian robinson jr": "brian robinson",
    # Sources that print the legal name where Sleeper prints the nickname.
    "kenneth gainwell": "kenny gainwell",
    "christopher brooks": "chris brooks",
    "andres borregales": "andy borregales",
}


def norm_pos(pos):
    if not pos:
        return None
    p = str(pos).strip().upper().replace("/", "").replace(".", "")
    return _POS_ALIASES.get(p, p if p in POSITIONS else p)


def norm_team(team):
    if not team:
        return None
    t = str(team).strip().upper()
    return _TEAM_ALIASES.get(t, t)


def norm_name(name):
    """Lowercase, strip punctuation and generational suffixes, collapse spaces."""
    if not name:
        return ""
    n = str(name).lower().strip()
    n = n.replace("&", " and ")
    # Fold accents to their ASCII letter before _NONWORD_RE runs, or the letter
    # becomes a space and the name splits ("eddy pi eiro" vs "eddy pineiro").
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = _NONWORD_RE.sub(" ", n)
    n = _WS_RE.sub(" ", n).strip()
    n = _SUFFIX_RE.sub("", n)
    n = _WS_RE.sub(" ", n).strip()
    return _NAME_FIXES.get(n, n)


def defense_key(name, team=None):
    """Map any spelling of a team defense to 'DEF|<TEAM>'. Returns None if unknown."""
    if team:
        t = norm_team(team)
        if t and len(t) <= 3:
            return "DEF|%s" % t
    n = norm_name(name)
    n = re.sub(r"\b(dst|d st|defense|special teams)\b", "", n).strip()
    if n in TEAM_NAMES:
        return "DEF|%s" % TEAM_NAMES[n]
    for key in sorted(TEAM_NAMES, key=len, reverse=True):
        if n.endswith(key) or n.startswith(key):
            return "DEF|%s" % TEAM_NAMES[key]
    return None


def player_key(name, pos, team=None):
    """Stable cross-source identity key: 'POS|normalized name' (defenses keyed by team)."""
    p = norm_pos(pos)
    if p == "DEF":
        return defense_key(name, team) or ("DEF|%s" % norm_name(name))
    return "%s|%s" % (p, norm_name(name))


def _cache_path(key):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return os.path.join(CACHE_DIR, safe)


def http_get(url, headers=None, cache_key=None, ttl=DEFAULT_TTL, timeout=30):
    """GET with on-disk caching. Returns decoded text."""
    if cache_key:
        path = _cache_path(cache_key)
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    text = raw.decode("utf-8", errors="replace")
    if cache_key:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(cache_key), "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def get_json(url, headers=None, cache_key=None, ttl=DEFAULT_TTL, timeout=30):
    return json.loads(http_get(url, headers, cache_key, ttl, timeout))


def record(name, pos, team=None, adp=None, auction=None, rank=None, proj_pts=None,
           sleeper_id=None, tier=None):
    """Build one normalized source record. Numeric fields are floats or None."""
    def num(v):
        try:
            if v is None or v == "":
                return None
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None
    p = norm_pos(pos)
    return {
        "name": (name or "").strip(),
        "pos": p,
        "team": norm_team(team),
        "key": player_key(name, p, team),
        "adp": num(adp),
        "auction": num(auction),
        "rank": num(rank),
        "proj_pts": num(proj_pts),
        "sleeper_id": str(sleeper_id) if sleeper_id else None,
        "tier": num(tier),
    }
