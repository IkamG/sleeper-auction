import unittest

from _path import jload, rows
from sleeper_auction.feeds import ids, nflverse

MCBRIDE, ALLGEIER, LOVE, BOURNE = "8130", "8132", "13287", "4454"


def cw():
    return ids.build(jload("sleeper_players.json"), rows("dp_playerids.csv"))


class NgsTest(unittest.TestCase):
    def setUp(self):
        self.n = nflverse.ngs_from_rows(rows("ngs_receiving.csv"), rows("ngs_rushing.csv"),
                                        "2026", cw())

    def test_season_total_row_used(self):
        # McBride has week 0 (23 tgt), 1 and 2 rows; the season line is week 0's.
        wk0 = [r for r in rows("ngs_receiving.csv")
               if r["season"] == "2026" and r["week"] == "0"
               and r["player_display_name"] == "Trey McBride"][0]
        self.assertEqual(self.n[MCBRIDE]["targets"], 23)
        self.assertAlmostEqual(self.n[MCBRIDE]["separation"],
                               round(float(wk0["avg_separation"]), 2))

    def test_other_season_ignored(self):
        # 2025 week-0 rows for McBride (169 targets) must not leak into 2026.
        self.assertNotEqual(self.n[MCBRIDE]["targets"], 169)

    def test_volume_gate(self):
        # Bourne: 11 targets on the season row -> kept. Love: 20 attempts -> kept.
        self.assertIn(BOURNE, self.n)
        self.assertIn("ryoe_att", self.n[LOVE])
        gated = nflverse.ngs_from_rows(
            [dict(r, targets="9") for r in rows("ngs_receiving.csv")], [], "2026", cw())
        self.assertEqual(gated, {})


class PfrTest(unittest.TestCase):
    def test_sums_not_averages(self):
        rush = rows("pfr_rush.csv")
        p = nflverse.pfr_adv_from_rows(rows("pfr_rec.csv"), rush, cw=cw())
        mine = [r for r in rush if r["pfr_player_id"] == "AllgTy00"]
        car = sum(float(r["carries"]) for r in mine)
        yac = sum(float(r["rushing_yards_after_contact"]) for r in mine)
        self.assertEqual(p[ALLGEIER]["carries"], int(car))
        self.assertAlmostEqual(p[ALLGEIER]["yaco_att"], round(yac / car, 2))

    def test_rate_gate(self):
        # Love has 20 carries over two games: exactly at the gate, so rates exist;
        # Brissett (7 carries) gets counts only.
        p = nflverse.pfr_adv_from_rows(rows("pfr_rec.csv"), rows("pfr_rush.csv"), cw=cw())
        self.assertIn("yaco_att", p[LOVE])
        self.assertNotIn("yaco_att", p.get("3257", {}))

    def test_drop_pct_needs_targets(self):
        p = nflverse.pfr_adv_from_rows(rows("pfr_rec.csv"), [], cw=cw(),
                                       targets={MCBRIDE: 23})
        self.assertIsNotNone(p[MCBRIDE]["drop_pct"])
        p = nflverse.pfr_adv_from_rows(rows("pfr_rec.csv"), [], cw=cw(), targets={MCBRIDE: 5})
        self.assertIsNone(p[MCBRIDE]["drop_pct"])


if __name__ == "__main__":
    unittest.main()
