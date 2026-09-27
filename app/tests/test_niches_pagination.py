"""Regression tests for GET /api/niches pagination.

The endpoint hardcoded `LIMIT 50` and threw the query string away, so
`?offset=50` returned the *same* first 50 rows. Any inventory or consolidation
tool built on it silently saw a 50-row catalog no matter how large the site
actually was -- which is how 491 indexable niches got mistaken for 50.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

import server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestClampInt(unittest.TestCase):
    def test_defaults_and_bounds(self):
        f = server._clamp_int
        self.assertEqual(f(None, 50, 1, 500), 50)
        self.assertEqual(f("", 50, 1, 500), 50)
        self.assertEqual(f("abc", 50, 1, 500), 50)
        self.assertEqual(f("1e9", 50, 1, 500), 50)       # not an int literal
        self.assertEqual(f(None, 50, 1, 500), 50)
        self.assertEqual(f("  25 ", 50, 1, 500), 25)
        self.assertEqual(f("-5", 50, 1, 500), 1)         # clamped up
        self.assertEqual(f("0", 50, 1, 500), 1)
        self.assertEqual(f("999", 50, 1, 500), 500)      # clamped down
        self.assertEqual(f("7", 0, 0, 10 ** 7), 7)       # offset range

    def test_never_raises(self):
        for raw in ("+", "--1", " ", "٣", "0x10", "1_0", "None"):
            server._clamp_int(raw, 7, 1, 500)


class TestNichePagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="niches_page_")
        cls.db = os.path.join(cls.tmp, "t.db")
        shutil.copy(os.path.join(REPO, "pstore.db"), cls.db)
        # The shipped seed holds only 43 niches, which fits in a single page and
        # therefore cannot exercise pagination at all. Top the catalogue up well
        # past one page so paging is actually tested.
        with sqlite3.connect(cls.db) as conn:
            for i in range(60):
                conn.execute(
                    "INSERT INTO niches (keyword, market, score, saturation,"
                    " products) VALUES (?,?,?,?,?)",
                    ("pagination probe %02d" % i, "com", 50.0 - i, 0.1,
                     json.dumps([{"asin": "B%011d" % i, "title": "Probe %d" % i,
                                  "price": 9.99, "stars": 4.0, "reviews": 10,
                                  "url": "https://www.amazon.com/dp/B%011d" % i}])))
        cls.expected_total = 43 + 60
        cls.port = 8794
        env = dict(os.environ,
                   PSTORE_DB=cls.db, PSTORE_ADMIN_EMAIL="t@example.com",
                   PSTORE_ADMIN_PASSWORD="testpass123", PSTORE_PORT=str(cls.port),
                   PSTORE_SKIP_WORKERS="1")
        cls._saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        import importlib
        importlib.reload(server)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()
        cls._login()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _login(cls):
        data = urllib.parse.urlencode(
            {"email": "t@example.com", "password": "testpass123"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/admin/login" % cls.port, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            cls.cookie = r.headers.get("Set-Cookie", "").split(";")[0]

    def _get(self, query=""):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/niches%s" % (self.port, query))
        req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def test_total_is_reported(self):
        d = self._get("")
        self.assertIn("total", d)
        self.assertEqual(d["total"], self.expected_total)
        self.assertGreater(d["total"], 50,
                           "catalogue must exceed one page for this test to mean anything")
        self.assertEqual(d["limit"], 50)
        self.assertEqual(d["offset"], 0)
        self.assertTrue(d["has_more"])

    def test_offset_advances_instead_of_repeating(self):
        """The core bug: offset=50 used to return page 1 again."""
        p1 = self._get("")
        p2 = self._get("?offset=50")
        ids1 = [n["id"] for n in p1["niches"]]
        ids2 = [n["id"] for n in p2["niches"]]
        self.assertEqual(len(ids1), 50)
        self.assertTrue(ids2, "second page must not be empty")
        self.assertFalse(set(ids1) & set(ids2),
                         "pages overlap: offset was ignored")

    def test_pages_cover_every_row_exactly_once(self):
        seen, offset = [], 0
        while True:
            d = self._get("?limit=17&offset=%d" % offset)
            seen += [n["id"] for n in d["niches"]]
            if not d["has_more"]:
                break
            offset += 17
            self.assertLess(offset, d["total"] + 100, "pagination did not terminate")
        self.assertEqual(len(seen), len(set(seen)), "duplicate ids across pages")
        self.assertEqual(len(seen), d["total"])

    def test_limit_is_respected_and_bounded(self):
        self.assertEqual(len(self._get("?limit=3")["niches"]), 3)
        self.assertEqual(self._get("?limit=3")["limit"], 3)
        self.assertEqual(self._get("?limit=0")["limit"], 1)
        self.assertEqual(self._get("?limit=-9")["limit"], 1)
        self.assertEqual(self._get("?limit=100000")["limit"], 500)

    def test_garbage_params_do_not_500(self):
        for q in ("?limit=abc", "?offset=xyz", "?limit=&offset=",
                  "?limit=1e9", "?offset=-1", "?limit=%%%"):
            d = self._get(q)
            self.assertEqual(d["offset"], 0 if "offset=-1" not in q else 0)
            self.assertIn("niches", d)

    def test_offset_past_end_is_empty_not_error(self):
        d = self._get("?offset=999999")
        self.assertEqual(d["niches"], [])
        self.assertFalse(d["has_more"])

    def test_keyword_filter(self):
        d = self._get("?q=keto&limit=100")
        self.assertTrue(d["niches"])
        for n in d["niches"]:
            self.assertIn("keto", n["keyword"].lower())
        self.assertLessEqual(d["total"], d["limit"] + d["niches"].__len__())

    def test_rows_still_carry_slug_and_products(self):
        n = self._get("?limit=1")["niches"][0]
        self.assertTrue(n["slug"])
        self.assertIsInstance(n["products"], list)


if __name__ == "__main__":
    unittest.main()
