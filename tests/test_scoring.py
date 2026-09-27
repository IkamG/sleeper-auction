import unittest

import _path  # noqa: F401
from sleeper_auction.feeds import scoring

LINE = {"rec": 6, "rec_yd": 80, "rec_td": 1, "rush_yd": 10, "fum_lost": 1}


class ScoringTest(unittest.TestCase):
    def test_half_ppr(self):
        self.assertAlmostEqual(scoring.points(LINE), 3 + 8 + 6 + 1 - 2)

    def test_full_ppr(self):
        sc = dict(scoring.HALF_PPR, rec=1.0)
        self.assertAlmostEqual(scoring.points(LINE, sc), 6 + 8 + 6 + 1 - 2)

    def test_te_premium(self):
        sc = dict(scoring.HALF_PPR, bonus_rec_te=0.5)
        self.assertAlmostEqual(scoring.points(LINE, sc, pos="TE"), 16 + 3)
        self.assertAlmostEqual(scoring.points(LINE, sc, pos="WR"), 16)

    def test_qb(self):
        self.assertAlmostEqual(scoring.points(
            {"pass_yd": 250, "pass_td": 2, "pass_int": 1}), 10 + 8 - 1)


if __name__ == "__main__":
    unittest.main()
