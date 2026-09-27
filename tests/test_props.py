import math
import unittest

from _path import jload, rows
from sleeper_auction.feeds import ids, scoring
from sleeper_auction.feeds.props import implied, kalshi, merge, oddsapi


def cw():
    return ids.build(jload("sleeper_players.json"), rows("dp_playerids.csv"))


class ImpliedTest(unittest.TestCase):
    def test_exponential_ladder(self):
        # S(x) = exp(-x/40): mean 40, median 40 ln 2.
        lad = [(k, math.exp(-k / 40.0)) for k in range(5, 160, 5)]
        e = implied.expected_from_ladder(lad, discrete=False)
        self.assertAlmostEqual(e["mean"], 40.0, delta=0.6)
        self.assertAlmostEqual(e["median"], 40 * math.log(2), delta=0.6)
        self.assertAlmostEqual(e["p90"], -40 * math.log(0.1), delta=1.5)

    def test_sparse_ladder_uses_tail(self):
        lad = [(20, math.exp(-0.5)), (60, math.exp(-1.5))]
        e = implied.expected_from_ladder(lad, discrete=False)
        # trapezoids to 60 then an exact exponential tail
        # 20*(1+.6065)/2 + 40*(.6065+.2231)/2 + .2231/(1/40) = 41.58
        self.assertAlmostEqual(e["mean"], 41.58, delta=0.05)

    def test_pava_fixes_non_monotone(self):
        lad = implied.clean_ladder([(10, 0.6), (20, 0.65), (30, 0.2)])
        ps = [p for _, p in lad]
        self.assertEqual(ps, sorted(ps, reverse=True))
        self.assertAlmostEqual(ps[0], 0.625)

    def test_devig(self):
        p = implied.american_to_prob
        self.assertAlmostEqual(implied.devig(p(-110), p(-110)), 0.5)
        self.assertAlmostEqual(implied.devig(p(-150), p(130)), 0.58, places=2)

    def test_poisson_td(self):
        self.assertAlmostEqual(implied.td_lambda(0.40), 0.511, places=3)

    def test_pass_td_bisection(self):
        lam = implied.lambda_from_ou(1.5, 0.45)
        # P(N >= 2; lam) == 0.45
        self.assertAlmostEqual(1 - math.exp(-lam) * (1 + lam), 0.45, places=3)

    def test_ou_median(self):
        e = implied.expected_from_ou("rec_yd", 59.5, 0.5)
        self.assertAlmostEqual(e["median"], 59.5)
        self.assertGreater(e["mean"], e["median"])      # right-skewed

    def test_coverage(self):
        sc = scoring.HALF_PPR
        filler = {"rec": 5, "rec_yd": 60, "rec_td": 0.4, "rush_yd": 5}
        f = implied.fantasy_from_markets({"rec_yd": {"mean": 80.0}}, filler, sc, "WR")
        total = 2.5 + 8.0 + 2.4 + 0.5
        self.assertAlmostEqual(f["mean"], total, places=2)
        self.assertAlmostEqual(f["coverage"], round(8.0 / total, 2))
        self.assertLessEqual(f["p10"], f["p90"])

    def test_td_market_split(self):
        f = implied.fantasy_from_markets({"anytime_td": {"p": 0.4}},
                                         {"rush_td": 0.3, "rec_td": 0.1}, scoring.HALF_PPR, "RB")
        self.assertAlmostEqual(f["components"]["rush_td"]["exp"], 0.511 * 0.75, places=2)


class KalshiTest(unittest.TestCase):
    def test_fixture(self):
        ms = jload("kalshi_markets.json")["markets"]
        self.assertEqual(kalshi.threshold(ms[0]), 15.0)          # floor_strike 14.5 -> 15+
        parsed, um = kalshi.parse(ms, "rec_yd", cw())
        (sid, v), = parsed.items()
        self.assertEqual(v["name"], "Jaylen Warren")
        self.assertEqual(len(v["ladder"]), 4)                    # the wide rung is dropped
        self.assertEqual(v["rungs_dropped"], 1)
        self.assertEqual(um, [])
        e = implied.expected_from_ladder(v["ladder"])
        self.assertTrue(20 < e["mean"] < 45)

    def test_started_game_dropped(self):
        ms = jload("kalshi_markets.json")["markets"]
        parsed, _ = kalshi.parse(ms, "rec_yd", cw(), started=lambda sid: True)
        self.assertEqual(parsed, {})

    def test_price_rules(self):
        self.assertEqual(kalshi.price({"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44"})[0],
                         0.42)
        self.assertIsNone(kalshi.price({"yes_bid_dollars": "0.1", "yes_ask_dollars": "0.5",
                                        "last_price_dollars": "0.3",
                                        "open_interest_fp": "10"})[0])
        self.assertEqual(kalshi.price({"yes_bid_dollars": "0.1", "yes_ask_dollars": "0.5",
                                       "last_price_dollars": "0.3",
                                       "open_interest_fp": "80"})[0], 0.3)


class OddsApiTest(unittest.TestCase):
    def test_fixture(self):
        ev = jload("oddsapi_event_odds.json")[0]
        rs = oddsapi.normalise(ev)
        self.assertTrue(all(r["market"] == "rec_yd" for r in rs))
        c = oddsapi.consensus(rs)
        v = c[("Ladd McConkey", "rec_yd")]
        self.assertEqual(v["line"], 59.0)              # DK 59.5, FD 58.5
        self.assertEqual(v["books"], 2)
        self.assertTrue(0.4 < v["p_over"] < 0.6)
        m = oddsapi.book_mean("rec_yd", v)
        a = implied.expected_from_ou("rec_yd", 59.5, v["per_book"][0][1])["mean"]
        b = implied.expected_from_ou("rec_yd", 58.5, v["per_book"][1][1])["mean"]
        self.assertAlmostEqual(m["mean"], round((a + b) / 2, 2), places=1)

    def test_merge_prefers_healthy_kalshi(self):
        k = {"rec_yd": {"1": {"market": "rec_yd", "ladder": [(20, .7), (40, .45), (60, .2)],
                              "spread": 0.02, "oi": 0}}}
        o = {"rec_yd": {"1": {"line": 45.5, "p_over": 0.5, "mean": 49.0},
                        "2": {"line": 30.5, "p_over": 0.5, "mean": 33.0}}}
        m = merge(k, o)
        self.assertEqual(m["1"]["rec_yd"]["source"], "kalshi")
        self.assertEqual(m["2"]["rec_yd"]["source"], "oddsapi")
        k["rec_yd"]["1"]["spread"] = 0.09                     # wide -> odds wins
        self.assertEqual(merge(k, o)["1"]["rec_yd"]["source"], "oddsapi")


if __name__ == "__main__":
    unittest.main()
