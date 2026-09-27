import unittest
from unittest import mock

from _path import jload, rows
from sleeper_auction import trades
from sleeper_auction.feeds import ids, values

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BN": 3}


def P(sid, pos, ros, value=10.0):
    return {"sid": sid, "name": sid, "pos": pos, "ros": ros, "value": value}


def league():
    """4 teams. Team 1 is deep at RB and thin at WR; team 2 the reverse; 3 and 4
    are balanced filler.

    Worked by hand, the best fair 1-for-1 is team 1's flex RB r1c (180, $30)
    for team 2's WR2 w2b (190, $30): team 1's lineup goes 1150 -> 1200 (w2b
    takes WR1, r1d 150 takes the flex from r1c) and team 2's goes 1110 -> 1160
    (r1c takes RB1, w2d 150 takes the flex). r1d <-> w2d is +40 / +60."""
    return {
        1: [P("q1", "QB", 250), P("r1a", "RB", 200, 40), P("r1b", "RB", 190, 35),
            P("r1c", "RB", 180, 30), P("r1d", "RB", 150, 12), P("w1a", "WR", 120, 8),
            P("w1b", "WR", 110, 6), P("t1", "TE", 100, 5)],
        2: [P("q2", "QB", 250), P("w2a", "WR", 200, 50), P("w2b", "WR", 190, 30),
            P("w2c", "WR", 180, 45), P("w2d", "WR", 150, 12), P("r2a", "RB", 100, 8),
            P("r2b", "RB", 90, 6), P("t2", "TE", 100, 5)],
        3: [P("q3", "QB", 240), P("r3a", "RB", 150), P("r3b", "RB", 140), P("w3a", "WR", 150),
            P("w3b", "WR", 140), P("t3", "TE", 90), P("x3", "WR", 60)],
        4: [P("q4", "QB", 240), P("r4a", "RB", 150), P("r4b", "RB", 140), P("w4a", "WR", 150),
            P("w4b", "WR", 140), P("t4", "TE", 90), P("x4", "RB", 60)],
    }


class LineupTest(unittest.TestCase):
    def test_flex_takes_best_remaining(self):
        tot, st = trades.lineup(league()[1], SLOTS)
        flex = [p for p in st if p["slot"] == "FLEX"][0]
        self.assertEqual(flex["sid"], "r1c")
        self.assertEqual(tot, 250 + 200 + 190 + 120 + 110 + 100 + 180)

    def test_needs_and_surplus(self):
        nd, _ = trades.needs(league(), SLOTS)
        self.assertIn("WR", nd[1])                   # thin at WR against the median
        self.assertIn("RB", nd[2])
        self.assertEqual([p["sid"] for p in trades.surplus(league(), SLOTS, 1)], ["r1d"])


class SearchTest(unittest.TestCase):
    def test_known_best_one_for_one(self):
        fair, _ = trades.search(league(), 1, SLOTS, limit=50)
        one = [r for r in fair if len(r["give"]) == 1 and len(r["get"]) == 1]
        best = one[0]
        self.assertEqual((best["give"][0]["sid"], best["get"][0]["sid"]), ("r1c", "w2b"))
        self.assertEqual((best["partner"], best["my_gain"], best["their_gain"]), (2, 50.0, 50.0))
        self.assertEqual(best["value_gap"], 0.0)
        self.assertIn(("r1d", "w2d", 40.0, 60.0),
                      {(r["give"][0]["sid"], r["get"][0]["sid"], r["my_gain"], r["their_gain"])
                       for r in one})
        # results are ranked by my_gain, then the smaller gap
        gains = [r["my_gain"] for r in fair]
        self.assertEqual(gains, sorted(gains, reverse=True))

    def test_both_sides_gain(self):
        fair, _ = trades.search(league(), 1, SLOTS, limit=50)
        self.assertTrue(fair)
        self.assertTrue(all(r["my_gain"] > 0 and r["their_gain"] > 0 for r in fair))

    def test_value_gap_filter(self):
        fair, asks = trades.search(league(), 1, SLOTS, limit=50)
        self.assertTrue(all(abs(r["value_gap"]) <= trades.FAIR_GAP for r in fair))
        # r1c ($30) for w2c ($45) helps both lineups but is 33% lopsided: excluded
        pairs = {(tuple(p["sid"] for p in r["give"]), tuple(p["sid"] for p in r["get"]))
                 for r in fair + asks}
        self.assertNotIn((("r1c",), ("w2c",)), pairs)
        self.assertTrue(all(0 < r["value_gap"] <= trades.VALUE_GAP for r in asks))

    def test_two_for_one_drop(self):
        lg = league()
        r = trades.evaluate(lg[1], lg[2], [P("r1c", "RB", 180, 30), P("r1d", "RB", 150, 12)],
                            [P("w2c", "WR", 180, 45)], SLOTS)
        # team 2 receives two for one and must cut its worst bench player
        self.assertEqual(r["their_drop"]["sid"], "r2b")
        self.assertIsNone(r["my_drop"])

    def test_value_floor(self):
        lg = league()
        for p in lg[1]:
            p["value"] = 1.0
        fair, asks = trades.search(lg, 1, SLOTS)
        self.assertEqual(fair + asks, [])                 # nothing worth $3 to offer


class KtcOffTest(unittest.TestCase):
    def test_fantasycalc_only_values(self):
        fc = {"8130": {"value": 500, "pos": "TE"}, "9221": {"value": 9000, "pos": "RB"},
              "4454": {"value": 100, "pos": "WR"}}
        cw = ids.build(jload("sleeper_players.json"), rows("dp_playerids.csv"))
        with mock.patch.object(ids, "crosswalk", return_value=cw), \
                mock.patch.dict("os.environ", {"KTC_ENABLED": "0"}):
            tv = values.trade_values({}, pool=[{"base": 50}, {"base": 20}, {"base": 5}],
                                     fc=fc, kt=values.ktc({}))
        self.assertEqual(tv["9221"]["dollars"], 50)
        self.assertIsNone(tv["9221"]["ktc"])
        self.assertFalse(any(v["disagree"] for v in tv.values()))


if __name__ == "__main__":
    unittest.main()
