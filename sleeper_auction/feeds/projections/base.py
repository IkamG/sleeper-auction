"""Shared pieces for the weekly projection adapters. Python 3.9, stdlib.

Adapter contract (one module per source):

    NAME = "cbs"; LABEL = "CBS Sports"; PRODUCER = "cbs"   # independence key
    def fetch_week(season, week) -> [{"name", "pos", "team", "src_id",
                                      "id_kind", "stats": {canonical}, "pts"}]

"stats" uses Sleeper's stat keys so feeds.scoring.points() scores every
source with this league's rules. "pts" is the source's own number, used only
for K/DEF, which are not rebuilt from components.
"""
import html
import re
import time
from html.parser import HTMLParser

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
PAGE_DELAY = 2.0          # seconds between pages of one site (5 for FantasyPros)

# Team spellings seen across the scraped sites.
TEAMS = {"SFO": "SF", "GBP": "GB", "KCC": "KC", "NEP": "NE", "NOS": "NO", "TBB": "TB",
         "LVR": "LV", "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "LAR": "LAR", "ARZ": "ARI",
         "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def team(t):
    t = (t or "").strip().upper().lstrip("@")
    return TEAMS.get(t, t)


def row(name, pos, team_, stats, pts=None, src_id=None, id_kind=None):
    return {"name": (name or "").strip(), "pos": pos, "team": team(team_),
            "src_id": str(src_id) if src_id not in (None, "") else None,
            "id_kind": id_kind, "stats": {k: v for k, v in stats.items() if v is not None},
            "pts": pts}


def num(v):
    try:
        v = str(v).replace(",", "").strip()
        if v in ("", "-", "--", "—"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def last_first(n):
    """'Smith-Njigba, Jaxon' -> 'Jaxon Smith-Njigba'."""
    if "," in (n or ""):
        a, b = n.split(",", 1)
        return "%s %s" % (b.strip(), a.strip())
    return (n or "").strip()


def pause(seconds=PAGE_DELAY):
    time.sleep(seconds)


class Tables(HTMLParser):
    """Every <table> as rows of cells: {"text", "colspan", "links", "cls"}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack = [], []
        self._cell = None
        self._row = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            try:
                cs = int(a.get("colspan") or 1)
            except ValueError:
                cs = 1
            self._cell = {"text": "", "colspan": cs, "links": [], "cls": a.get("class") or "",
                          "tag": tag}
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._cell["links"].append(a["href"])
        elif tag == "span" and self._cell is not None and a.get("class"):
            self._cell.setdefault("spans", []).append(a["class"])
            self._cell["text"] += "\x1f%s\x1e" % a["class"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["raw"] = self._cell["text"]
            self._cell["text"] = re.sub(r"\s+", " ", re.sub(r"\x1f[^\x1e]*\x1e", " ",
                                                            self._cell["text"])).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_tables(text):
    p = Tables()
    p.feed(text)
    p.close()
    return p.tables


def grouped_header(group_row, label_row):
    """Expand a colspan group row over the label row: [(group, label)]."""
    groups = []
    for c in group_row or []:
        groups.extend([c["text"].strip().lower()] * c["colspan"])
    out = []
    col = 0
    for c in label_row:
        # CBS puts a tooltip in the header cell ("tgt Targets"): the label is
        # the first token.
        lab = (c["text"].strip().lower().split(" ") or [""])[0]
        for _ in range(c["colspan"]):
            out.append((groups[col] if col < len(groups) else "", lab))
            col += 1
    return out


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
