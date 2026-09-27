import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from _path import jload, rows, text
from sleeper_auction.feeds import common, ids, league, news, values


def cw():
    return ids.build(jload("sleeper_players.json"), rows("dp_playerids.csv"))


class NewsTest(unittest.TestCase):
    def test_espn_parse_filters_and_strips(self):
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        items = news.parse_espn(jload("espn_news.json"), max_age_days=7, now=now)
        self.assertEqual(len(items), 1)                      # only the Sep 21 blurb
        self.assertEqual(items[0]["type"], "Rotowire")
        self.assertNotIn("<", items[0]["text"])
        self.assertLessEqual(len(items[0]["text"]), news.MAX_CHARS)
        wide = news.parse_espn(jload("espn_news.json"), max_age_days=30, per_player=3, now=now)
        self.assertEqual([i["type"] for i in wide][:2], ["Rotowire", "Rotowire"])

    def test_rotowire_rss(self):
        c = cw()
        # map one RSS item's RotoWire id onto a fixture player
        c["by"]["rotowire"]["16946"] = "8130"
        out = news.parse_rss(text("rotowire_nfl.xml"), c)
        self.assertIn("8130", out)
        it = out["8130"][0]
        self.assertTrue(it["headline"].startswith("Tyson Bagent"))
        self.assertEqual(it["published"], "2026-09-27T17:09Z")   # 10:09 AM PDT
        self.assertNotIn("Visit RotoWire.com", it["text"])


class ValuesTest(unittest.TestCase):
    def test_fantasycalc_join(self):
        out = values.parse_fantasycalc(jload("fantasycalc.json"))
        self.assertEqual(len(out), 3)
        self.assertIn("9221", out)                           # player.sleeperId
        self.assertEqual(out["9221"]["pos"], "RB")

    def test_params(self):
        lg = {"roster_positions": ["QB", "RB", "SUPER_FLEX"], "total_rosters": 10,
              "scoring_settings": {"rec": 1.0}}
        self.assertEqual(values.league_params(lg), {"numQbs": 2, "numTeams": 10, "ppr": "1"})
        lg = {"roster_positions": ["QB", "FLEX"], "total_rosters": 12,
              "scoring_settings": {"rec": 0.5}}
        self.assertEqual(values.league_params(lg)["numQbs"], 1)

    def test_ros_dollars_by_rank(self):
        pool = [{"base": 50}, {"base": 30}, {"base": 10}]
        d = values.ros_dollars({"a": 100, "b": 300, "c": 200, "d": 50}, pool)
        self.assertEqual(d, {"b": 50, "c": 30, "a": 10, "d": 1.0})

    def test_trade_values_percentiles(self):
        fc = {"1": {"value": 100, "pos": "RB"}, "2": {"value": 50, "pos": "RB"},
              "3": {"value": 10, "pos": "RB"}}
        kt = {"1": {"value": 1, "pos": "RB"}, "2": {"value": 9000, "pos": "RB"},
              "3": {"value": 5, "pos": "RB"}}
        with mock.patch.object(ids, "crosswalk", return_value=cw()):
            tv = values.trade_values({}, pool=[{"base": 40}, {"base": 20}, {"base": 5}],
                                     fc=fc, kt=kt)
        self.assertTrue(tv["1"]["disagree"])                 # FC top, KTC bottom
        self.assertAlmostEqual(tv["2"]["consensus"], 0.75)   # (0.5 + 1.0) / 2
        self.assertEqual(tv["2"]["dollars"], 40)             # consensus #1 -> top dollars

    def test_ros_fallback_chain(self):
        c = cw()
        with mock.patch.object(ids, "crosswalk", return_value=c), \
                mock.patch.object(values, "_sleeper_week", side_effect=common.CacheMiss("x")):
            with mock.patch("sleeper_auction.feeds.projections.fanduel.fetch_remaining",
                            return_value=[{"name": "Trey McBride", "pos": "TE", "team": "ARI",
                                           "src_id": None, "id_kind": None,
                                           "stats": {"rec": 60, "rec_yd": 700}, "pts": 100}]):
                r = values.ros_points("2026", 4, sc=None)
            self.assertEqual(r["8130"]["source"], "fanduel-remaining")
            with mock.patch("sleeper_auction.feeds.projections.fanduel.fetch_remaining",
                            side_effect=common.CacheMiss("y")):
                r = values.ros_points("2026", 4, season_proj={"8130": 170.0})
            self.assertEqual(r["8130"]["source"], "season-share")
            self.assertAlmostEqual(r["8130"]["pts"], round(170 * 14 / 17.0, 1))

    def test_ktc_parse(self):
        html = ('<script>var oneQBPlayers = [{"playerName":"A B","playerID":7,"position":"RB",'
                '"team":"SFO","oneQBValues":{"value":8000,"rank":3,"positionalRank":2,'
                '"overallTrend":5},"superflexValues":{"value":7000}}];</script>')
        r = values.parse_ktc(html)
        self.assertEqual(r[0]["ktc_id"], "7")
        self.assertEqual(r[0]["value"], 8000)

    def test_ktc_off_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("KTC_ENABLED", None)
            self.assertEqual(values.ktc({}), {})


class LeagueTest(unittest.TestCase):
    def test_history_and_curve(self):
        tx = jload("sleeper_transactions.json")
        with mock.patch.object(common, "fetch_json", return_value=tx):
            h = league.faab_history("L", 1)
        won = [x for x in h if x["won"]]
        failed = [x for x in h if not x["won"]]
        self.assertEqual(len(won), 4)
        self.assertEqual(len(failed), 2)
        # MIN and 4227 each had a failed competing claim
        self.assertEqual({x["player"] for x in h if x["bids_on_player"] > 1}, {"MIN", "4227"})
        c = league.clearing_curve(h, 1000, lambda p: "DEF" if p.isalpha() else "RB")
        self.assertEqual(c["n"], 3)                          # 130, 1, 220 (0-bid win excluded)
        self.assertEqual(c["median_pct_by_band"]["all"], 13.0)
        self.assertEqual(c["zero_bid_wins"], 1)

    def test_faab_left(self):
        lg = {"settings": {"waiver_budget": 1000}}
        rs = [{"roster_id": 1, "settings": {"waiver_budget_used": 151}},
              {"roster_id": 2, "settings": {}}]
        with mock.patch.object(league, "_league", return_value=lg), \
                mock.patch.object(common, "fetch_json", return_value=rs):
            f = league.faab("L")
        self.assertEqual(f[1], {"budget": 1000, "used": 151, "left": 849})
        self.assertEqual(f[2]["left"], 1000)


if __name__ == "__main__":
    unittest.main()
