import json
import unittest
from unittest import mock

from _path import jload, rows as fxrows, text
from sleeper_auction.feeds import blend, ids, scoring
from sleeper_auction.feeds.projections import (base, cbs, consensus, espn, fanduel,
                                               fantasysharks, fftoday)


class AdapterTest(unittest.TestCase):
    def test_cbs(self):
        r = cbs.parse(text("cbs_qb.html"), "QB")
        self.assertEqual([x["name"] for x in r], ["Josh Allen", "Patrick Mahomes", "Brock Purdy"])
        a = r[0]
        self.assertEqual((a["team"], a["src_id"], a["id_kind"]), ("BUF", "2181054", "cbs"))
        self.assertEqual(a["stats"]["pass_yd"], 233.0)
        self.assertEqual(a["stats"]["rush_yd"], 41.3)       # same label "yds", other group
        self.assertNotIn("pass_yd/g", a["stats"])

    def test_fftoday_repeated_labels(self):
        r = fftoday.parse(text("fftoday_wr.html"), "WR")
        jsn = r[0]
        self.assertEqual(jsn["name"], "Jaxon Smith-Njigba")
        self.assertEqual(jsn["stats"]["rec_yd"], 101.0)     # "Yard" under Receiving
        self.assertEqual(jsn["stats"]["rush_yd"], 0.0)      # "Yard" under Rushing
        self.assertEqual(jsn["pts"], 16.7)

    def test_sharks(self):
        r = fantasysharks.parse(text("sharks_qb.csv"), "QB")
        self.assertEqual(r[0]["name"], "Josh Allen")        # "Allen, Josh"
        self.assertEqual(r[0]["stats"]["pass_yd"], 240.0)
        self.assertEqual(r[0]["stats"]["rush_att"], 8.4)

    def test_sharks_duplicate_headers(self):
        csv = "Player Name,Team,Yds,Yds,Pts\n\"Doe, John\",SFO,10,20,5\n"
        r = fantasysharks.parse(csv, "RB")
        self.assertEqual(r[0]["team"], "SF")

    def test_espn(self):
        r = espn.parse(json.loads(text("espn_wr.json")), 3, "WR")
        self.assertEqual(r[0]["name"], "Ja'Marr Chase")
        self.assertAlmostEqual(r[0]["stats"]["rec_yd"], 86.42, places=2)
        self.assertEqual(r[0]["id_kind"], "espn")

    def test_fanduel_week_filter(self):
        d = json.loads(text("fanduel.json"))
        all_rows = fanduel.parse(d)
        self.assertEqual(all_rows[0]["stats"]["pass_cmp"], 22.11)   # "22.11/33.49"
        self.assertEqual(fanduel.parse(d, matchups={"BAL": "DAL", "DAL": "BAL"})[0]["team"],
                         "BAL")
        self.assertEqual([r for r in fanduel.parse(d, matchups={"BAL": "PIT"})
                          if r["team"] == "BAL"], [])

    def test_last_first(self):
        self.assertEqual(base.last_first("St. Brown, Amon-Ra"), "Amon-Ra St. Brown")


def _row(src, pts, pos="WR", stats=None):
    return {"name": "X", "pos": pos, "team": "SEA", "src_id": "1", "id_kind": "sleeper",
            "stats": stats or {"rec": pts / 2.0, "rec_yd": pts * 5}, "pts": pts}


class ConsensusTest(unittest.TestCase):
    def build(self, rows):
        # Offline: the crosswalk comes from fixtures, never the network.
        cw = ids.build(jload("sleeper_players.json"), fxrows("dp_playerids.csv"))
        with mock.patch.object(ids, "crosswalk", return_value=cw):
            return consensus.build("2026", 3, scoring.HALF_PPR,
                                   {k: [v] for k, v in rows.items()}, snapshot=False)

    def test_median_and_rescoring(self):
        rows = {"espn": _row("espn", 10), "cbs": _row("cbs", 12), "fftoday": _row("fftoday", 20)}
        c = self.build(rows)["players"]["1"]
        # rescored with half-PPR: rec*0.5 + rec_yd*0.1 = pts/4 + pts/2 = 0.75*pts
        self.assertEqual(c["n"], 3)
        self.assertAlmostEqual(c["median"], 9.0)
        self.assertEqual((c["lo"], c["hi"]), (7.5, 15.0))

    def test_mean_under_three(self):
        c = self.build({"espn": _row("espn", 10), "cbs": _row("cbs", 20)})["players"]["1"]
        self.assertAlmostEqual(c["median"], 11.25)

    def test_producer_dedupe(self):
        with mock.patch.dict(consensus.PRODUCER, {"cbs": "espn"}):
            c = self.build({"espn": _row("espn", 10), "cbs": _row("cbs", 20)})["players"]["1"]
        self.assertEqual(c["n"], 1)

    def test_source_thinks_out(self):
        c = self.build({"espn": _row("espn", 0.2), "cbs": _row("cbs", 12),
                        "fftoday": _row("fftoday", 14)})["players"]["1"]
        self.assertEqual(c["flags"], ["source_thinks_out:espn"])


class BlendTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(blend, "weights", lambda: (0.55, 0.25, "chosen"))
        p.start()
        self.addCleanup(p.stop)

    def test_full_coverage(self):
        b = blend.final(10.0, {"median": 12.0, "lo": 10, "hi": 14}, {"mean": 16.0, "coverage": 0.9})
        self.assertAlmostEqual(b["value"], 0.55 * 16 + 0.45 * 12)
        self.assertEqual(b["basis"], {"consensus": 0.45, "props": 0.55})

    def test_partial_and_none(self):
        self.assertAlmostEqual(
            blend.final(10.0, {"median": 12.0}, {"mean": 16.0, "coverage": 0.5})["value"],
            0.25 * 16 + 0.75 * 12)
        self.assertEqual(blend.final(10.0, {"median": 12.0},
                                     {"mean": 16.0, "coverage": 0.1})["value"], 12.0)

    def test_sleeper_fallback(self):
        self.assertEqual(blend.final(10.0, None, None)["value"], 10.0)
        self.assertIsNone(blend.final(None, None, None))


if __name__ == "__main__":
    unittest.main()
