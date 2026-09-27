import gzip
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _path  # noqa: F401
from sleeper_auction.feeds import common


class _Resp:
    def __init__(self, body, headers=None):
        self.body, self.headers = body, headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CommonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.object(common, "CACHE", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp)

    def test_gunzip_content(self):
        body = gzip.compress(b"a,b\n1,2\n")
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            rows = common.fetch_csv(["http://x/f.csv.gz"], "t-gz", 60)
        self.assertEqual(rows, [{"a": "1", "b": "2"}])
        # cached decompressed
        self.assertEqual(open(common.cache_path("t-gz")).read(), "a,b\n1,2\n")

    def test_double_gzip(self):
        body = gzip.compress(gzip.compress(b"x"))
        self.assertEqual(common._decode(body, "gzip"), "x")

    def test_stale_if_error(self):
        path = common.cache_path("t-stale")
        with open(path, "w") as f:
            f.write('{"v": 1}')
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(common.fetch_json("http://x", "t-stale", 60, feed="t"), {"v": 1})
        self.assertTrue(common.STATUS["t"]["stale"])

    def test_error_without_cache_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                common.fetch_text("http://x", "t-none", 60)

    def test_redact(self):
        s = common.redact("GET https://h/v4?apiKey=abc123def&x=1 failed")
        self.assertNotIn("abc123def", s)
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "zzzsecretzzz"}):
            self.assertEqual(common.redact("oops zzzsecretzzz"), "oops ***")

    def test_enabled(self):
        with mock.patch.dict(os.environ, {"FEEDS_DISABLED": "props, news"}):
            self.assertFalse(common.enabled("props.kalshi"))
            self.assertFalse(common.enabled("news"))
            self.assertTrue(common.enabled("nflverse.usage"))

    def test_parallel(self):
        res, err = common.parallel({"a": lambda: 1, "b": lambda: 1 / 0})
        self.assertEqual(res, {"a": 1})
        self.assertIn("ZeroDivisionError", err["b"])

    def test_snapshot_dedup(self):
        self.assertTrue(common.snapshot("k", 2026, 3, {"a": 1}, ts=1e9))
        self.assertIsNone(common.snapshot("k", 2026, 3, {"a": 1}, ts=1e9 + 3600))
        self.assertTrue(common.snapshot("k", 2026, 3, {"a": 2}, ts=1e9 + 7200))
        self.assertEqual([p for _, p in common.snapshots("k", 2026, 3)], [{"a": 1}, {"a": 2}])

    def test_num(self):
        self.assertIsNone(common.num("NA"))
        self.assertEqual(common.num("1.5"), 1.5)
        self.assertEqual(common.sid_str(" 00-0012345 "), "00-0012345")


if __name__ == "__main__":
    unittest.main()
