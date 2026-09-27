import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401
from sleeper_auction.feeds import blend, calibrate, common


class CalibrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(common, "CACHE", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_last_snapshot_wins(self):
        common.snapshot("calib", 2026, 1, {"1": {"final": 10}, "2": {"final": 5}}, ts=1e9)
        common.snapshot("calib", 2026, 1, {"1": {"final": 12}}, ts=1e9 + 3600)
        self.assertEqual(calibrate.final_lines(2026, 1),
                         {"1": {"final": 12}, "2": {"final": 5}})

    def test_errors_and_summary(self):
        lines = {"1": {"pos": "WR", "sleeper": 10, "consensus": 12, "final": 11,
                       "sources": {"espn": 14, "sleeper": 10}},
                 "2": {"pos": "RB", "sleeper": 8, "consensus": 6},
                 "3": {"pos": "RB", "sleeper": 5}}
        e = calibrate.errors(lines, {"1": 12.0, "2": 10.0})       # 3 did not play
        self.assertEqual(e["sleeper"]["WR"], [-2.0])
        self.assertEqual(e["src:espn"]["WR"], [2.0])
        self.assertNotIn("src:sleeper", e)
        t = calibrate.summarise(e)
        self.assertEqual(t["sleeper"]["all"], {"mae": 2.0, "bias": -2.0, "n": 2})

    def test_fit_weight_known_answer(self):
        # actual == props exactly -> w = 1; actual == consensus -> w = 0
        self.assertEqual(calibrate.fit_weight([(10, 20, 10), (5, 1, 5)])[0], 1.0)
        self.assertEqual(calibrate.fit_weight([(10, 20, 20), (5, 1, 1)])[0], 0.0)
        w, _ = calibrate.fit_weight([(10, 20, 15), (0, 10, 5)])
        self.assertEqual(w, 0.5)

    def test_run_writes_and_blend_uses_it(self):
        for wk in range(1, 5):
            common.snapshot("calib", 2026, wk, {
                "1": {"pos": "WR", "sleeper": 10, "consensus": 10, "props": 14,
                      "props_cov": 0.9, "final": 12}}, ts=1e9 + wk)
        with mock.patch.object(calibrate, "actual_points", return_value={"1": 14.0}), \
                mock.patch.object(calibrate, "week_complete", return_value=True):
            r = calibrate.run(2026)
        self.assertEqual(r["n_weeks"], 4)
        self.assertEqual(r["w_props"], 1.0)
        with open(os.path.join(self.tmp, "calibration.json")) as f:
            self.assertEqual(json.load(f)["n_weeks"], 4)
        full, partial, how = blend.weights()
        self.assertEqual(full, 1.0)
        self.assertTrue(how.startswith("calibrated"))

    def test_blend_ignores_thin_calibration(self):
        with open(os.path.join(self.tmp, "calibration.json"), "w") as f:
            json.dump({"n_weeks": 2, "w_props": 0.9}, f)
        self.assertEqual(blend.weights()[2], "chosen")

    def test_incomplete_week_skipped(self):
        common.snapshot("calib", 2026, 3, {"1": {"pos": "WR", "final": 12}}, ts=1e9)
        with mock.patch.object(calibrate, "actual_points", return_value={"1": 14.0}), \
                mock.patch.object(calibrate, "week_complete", return_value=False):
            self.assertEqual(calibrate.run(2026)["n_weeks"], 0)


if __name__ == "__main__":
    unittest.main()
