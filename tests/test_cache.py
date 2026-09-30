import os
import shutil
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401
from sleeper_auction.feeds import cache, common


class ClearTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        for d in ("ai", "history/2026/w03/calib"):
            os.makedirs(os.path.join(self.tmp, d))
        for f in ("proj-cbs-x", "news-espn-1", "oddsapi-ev-abc", "oddsapi-quota.json",
                  "calibration.json", "ai/k.json", "history/2026/w03/calib/1.json"):
            with open(os.path.join(self.tmp, f), "w") as fh:
                fh.write("x")
        for p in (mock.patch.object(common, "CACHE", self.tmp),
                  mock.patch.object(cache, "_dirs", lambda: [self.tmp]),
                  mock.patch.object(cache, "_reset_memory", lambda ai: None)):
            p.start()
            self.addCleanup(p.stop)

    def left(self):
        out = set()
        for root, _, files in os.walk(self.tmp):
            for f in files:
                out.add(os.path.relpath(os.path.join(root, f), self.tmp))
        return out

    def test_default_keeps_ai_history_and_paid_odds(self):
        r = cache.clear()
        self.assertEqual(r["files"], 2)
        self.assertEqual(self.left(), {"oddsapi-ev-abc", "oddsapi-quota.json",
                                       "calibration.json", "ai/k.json",
                                       "history/2026/w03/calib/1.json"})

    def test_everything_optional(self):
        cache.clear(ai=True, odds=True)
        self.assertEqual(self.left(), {"oddsapi-quota.json", "calibration.json",
                                       "history/2026/w03/calib/1.json"})


if __name__ == "__main__":
    unittest.main()
