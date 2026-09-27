import unittest

from _path import jload, rows
from sleeper_auction.feeds import ids


def _cw():
    slp = jload("sleeper_players.json")
    dp = rows("dp_playerids.csv")
    return ids.build(slp, dp)


class IdsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cw = _cw()

    def test_dp_fills_gsis_sleeper_lacks(self):
        # Sleeper has no gsis for McBride; DynastyProcess does.
        self.assertEqual(ids.to_sleeper("gsis", "00-0037744", cw=self.cw), "8130")
        self.assertEqual(ids.to_sleeper("pfr", "McBrTr01", cw=self.cw), "8130")

    def test_leading_space_stripped(self):
        self.assertEqual(ids.to_sleeper("gsis", "00-0099001", cw=self.cw), "90001")

    def test_sleeper_wins_conflict(self):
        # DP claims a different gsis for 90001; Sleeper's stays, DP's is also mapped.
        self.assertEqual(self.cw["by"]["gsis"]["00-0099001"], "90001")

    def test_rookie_without_gsis_in_sleeper(self):
        self.assertEqual(ids.to_sleeper("gsis", "00-0099003", cw=self.cw), "90003")
        self.assertEqual(ids.to_sleeper("pfr", "NogsRo00", cw=self.cw), "90003")

    def test_ambiguous_name_dropped(self):
        self.assertIsNone(ids.to_sleeper("cbs", None, name="Mike Williams", pos="WR",
                                         cw=self.cw))

    def test_name_fallback(self):
        self.assertEqual(ids.to_sleeper("cbs", "nope", name="Trey McBride", pos="TE",
                                        cw=self.cw), "8130")

    def test_dp_row_without_sleeper_ignored(self):
        self.assertNotIn("00-0099999", self.cw["by"]["gsis"])

    def test_counts(self):
        ids.COUNTS.clear()
        ids.to_sleeper("espn", "missing-id", cw=self.cw)
        self.assertEqual(ids.join_status()["espn"]["miss"], 1)


if __name__ == "__main__":
    unittest.main()
