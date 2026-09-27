import shutil
import tempfile
import unittest
from unittest import mock

from _path import jload, rows
from sleeper_auction.feeds import common, ids, nflverse


def fixture_cw():
    slp = jload("sleeper_players.json")
    return ids.build(slp, rows("dp_playerids.csv"))


MCBRIDE = "8130"


class UsageTest(unittest.TestCase):
    def test_share_from_sums_not_average(self):
        # Game 1: 2 of 4 team targets (50%). Game 2: 3 of 12 (25%).
        # Average of shares = 37.5%; share of sums = 5/16 = 31.25%.
        r = []
        for g, mine, team in (("g1", 2, 4), ("g2", 3, 12)):
            r.append({"game_id": g, "team": "ARI", "position": "TE", "week": g[1],
                      "player_id": "00-0037744", "player_display_name": "Trey McBride",
                      "targets": mine, "receiving_air_yards": 10 * mine, "carries": 0})
            r.append({"game_id": g, "team": "ARI", "position": "WR", "week": g[1],
                      "player_id": "00-X", "player_display_name": "Other Guy",
                      "targets": team - mine, "receiving_air_yards": 10 * (team - mine),
                      "carries": 0})
        u = nflverse.usage_from_rows(r, recent=1, cw=fixture_cw())
        self.assertAlmostEqual(u[MCBRIDE]["tgt_share"], 0.312, places=3)
        self.assertAlmostEqual(u[MCBRIDE]["tgt_share_recent"], 0.25, places=3)
        self.assertAlmostEqual(u[MCBRIDE]["trend_tgt"], -6.2, places=1)
        self.assertAlmostEqual(u[MCBRIDE]["wopr"], round(1.5 * 0.312 + 0.7 * 0.312, 3), places=2)

    def test_fixture(self):
        u = nflverse.usage_from_rows(rows("stats_player_week.csv"), cw=fixture_cw())
        m = u[MCBRIDE]
        self.assertEqual(m["games"], 2)
        self.assertTrue(0 < m["tgt_share"] < 1)
        self.assertEqual(m["team"], "ARI")


class XfpTest(unittest.TestCase):
    def test_half_ppr_conversion_mcbride(self):
        wk1 = [r for r in rows("ep_weekly.csv") if r["week"] == "1"]
        x = nflverse.xfp_from_rows(wk1, rec_value=0.5, cw=fixture_cw())[MCBRIDE]
        # full-PPR 24.5 actual with 9 rec -> 20.0 half; 24.2 exp - 0.5*8.88 = 19.76
        self.assertAlmostEqual(x["fp_pg"], 20.0, places=2)
        self.assertAlmostEqual(x["xfp_pg"], 19.76, places=2)
        self.assertIsNone(x["diff_pg"])      # one game: not published

    def test_two_games_diff(self):
        x = nflverse.xfp_from_rows(rows("ep_weekly.csv"), cw=fixture_cw())[MCBRIDE]
        self.assertEqual(x["games"], 2)
        self.assertIsNotNone(x["diff_pg"])


class PracticeTest(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(nflverse.norm_practice("Did Not Participate In Practice"), "DNP")
        self.assertEqual(nflverse.norm_practice("Limited Participation in Practice"), "LP")
        self.assertEqual(nflverse.norm_practice("Full Participation in Practice"), "FP")
        self.assertIsNone(nflverse.norm_practice(""))

    def test_rest_day_and_skill_filter(self):
        r = [{"week": "3", "gsis_id": "00-0037744", "full_name": "Trey McBride",
              "position": "TE", "team": "ARI",
              "practice_status": "Did Not Participate In Practice",
              "practice_primary_injury": "Not injury related - resting player",
              "report_status": "", "report_primary_injury": ""},
             {"week": "3", "gsis_id": "00-LINEMAN", "full_name": "Big Guy",
              "position": "G", "team": "ARI", "practice_status": "Full Participation in Practice",
              "practice_primary_injury": "", "report_status": ""}]
        out = nflverse.practice_from_rows(r, 3, cw=fixture_cw())
        self.assertEqual(list(out), [MCBRIDE])
        self.assertTrue(out[MCBRIDE]["rest"])
        self.assertEqual(out[MCBRIDE]["status"], "DNP")
        self.assertIsNone(out[MCBRIDE]["report_status"])

    def test_fixture_parses(self):
        out = nflverse.practice_from_rows(rows("injuries.csv"), 3, cw=fixture_cw())
        self.assertTrue(all(v["status"] in ("DNP", "LP", "FP", None) for v in out.values()))

    def test_trajectory_from_snapshots(self):
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.object(common, "CACHE", tmp):
                for i, st in enumerate(("DNP", "DNP", "LP", "FP")):
                    common.snapshot("practice", 2026, 3, {"1": {"status": st}, "x": i},
                                    ts=1.8e9 + 86400 * i)
                t = nflverse.trajectory(common.snapshots("practice", 2026, 3), "1")
            self.assertEqual([s for s, _ in t], ["DNP", "LP", "FP"])
        finally:
            shutil.rmtree(tmp)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.s = nflverse.schedule_from_rows(rows("games.csv"), "2026")[3]

    def test_spread_orientation(self):
        # nflverse spread_line is the HOME margin: -10 = away (KC) favoured by 10.
        self.assertEqual(self.s["KC"]["spread"], -10.0)
        self.assertEqual(self.s["MIA"]["spread"], 10.0)
        self.assertEqual(self.s["KC"]["implied"], round(45.5 / 2 + 5, 2))
        self.assertFalse(self.s["KC"]["home"])

    def test_kickoff_utc(self):
        self.assertEqual(self.s["KC"]["kickoff_utc"], "2026-09-27T17:00Z")

    def test_neutral_venue(self):
        self.assertTrue(self.s["BAL"]["neutral"])
        self.assertEqual(self.s["BAL"]["roof"], "outdoors")
        self.assertIsNotNone(self.s["BAL"]["venue_coords"])


class RedzoneTest(unittest.TestCase):
    def test_fixture(self):
        rz = nflverse.redzone_from_rows(rows("ep_pbp_rush.csv"), rows("ep_pbp_pass.csv"),
                                        cw=fixture_cw())
        self.assertIn("ARI", rz["teams"])
        for p in rz["players"].values():
            if p["rz_share"] is not None:
                self.assertTrue(0 < p["rz_share"] <= 1)


if __name__ == "__main__":
    unittest.main()
