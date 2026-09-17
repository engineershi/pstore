# -*- coding: utf-8 -*-
"""Offline tests for the pstore link-authority engine: deterministic per-niche
target generation, the outreach table CRUD, and the /admin/linkauthority page +
/api/linkauthority/* endpoints (auth-gated)."""

import json
import os
import shutil
import sqlite3
import sys
import threading
import unittest
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkauthority
import security
import server


class TestLinkAuthorityModule(unittest.TestCase):
    """Pure module: generation is deterministic, deduped, and escape-safe."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(linkauthority.SCHEMA)
        self.conn.execute(linkauthority.SCHEMA_INDEX)

    def tearDown(self):
        self.conn.close()

    def test_build_target_has_all_fields(self):
        t = linkauthority.build_target("keto snacks", "guestpost")
        self.assertEqual(t["keyword"], "keto snacks")
        self.assertEqual(t["slug"], "keto-snacks")
        self.assertEqual(t["tactic"], "guestpost")
        self.assertTrue(t["angle"])
        self.assertTrue(t["pitch"])
        self.assertIn("keto snacks", t["pitch"])
        self.assertEqual(len(t["searches"]), 4)
        for urls in t["searches"]:
            for eng in ("google", "bing", "duckduckgo"):
                self.assertTrue(urls[eng].startswith("http"))

    def test_build_target_is_deterministic(self):
        a = linkauthority.build_target("keto snacks", "haro")
        b = linkauthority.build_target("keto snacks", "haro")
        self.assertEqual(a["angle"], b["angle"])
        self.assertEqual(a["pitch"], b["pitch"])

    def test_generate_inserts_one_per_tactic_and_dedupes(self):
        res = linkauthority.generate(self.conn, ["keto snacks", "golf gifts"])
        self.assertEqual(res["added"], 2 * len(linkauthority.TACTICS))
        again = linkauthority.generate(self.conn, ["keto snacks", "golf gifts"])
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["total"], res["total"])

    def test_generate_respects_limit(self):
        res = linkauthority.generate(self.conn, ["keto snacks"], limit=2)
        self.assertEqual(res["added"], 2)

    def test_generate_uses_dlp_url_in_pitch(self):
        linkauthority.generate(self.conn, ["keto snacks"], limit=1,
                               dlp_url="https://example.test/n/keto-snacks")
        row = self.conn.execute("SELECT pitch FROM backlink_outreach").fetchone()
        self.assertIn("https://example.test/n/keto-snacks", row["pitch"])

    def test_update_delete_and_stats(self):
        linkauthority.generate(self.conn, ["keto snacks"], limit=1)
        row_id = self.conn.execute(
            "SELECT id FROM backlink_outreach").fetchone()["id"]
        upd = linkauthority.update(self.conn, row_id, status="pitched",
                                   source_url="https://blog.test/list",
                                   target_url="https://pstore.test/n/keto-snacks",
                                   note="emailed editor")
        self.assertEqual(upd["status"], "pitched")
        self.assertEqual(upd["source_url"], "https://blog.test/list")
        self.assertEqual(upd["note"], "emailed editor")
        self.assertEqual(linkauthority.stats(self.conn)["pitched"], 1)
        self.assertEqual(linkauthority.delete(self.conn, row_id), 1)
        self.assertEqual(linkauthority.stats(self.conn)["total"], 0)

    def test_update_rejects_unknown_id(self):
        self.assertIsNone(linkauthority.update(self.conn, 999, status="live"))

    def test_list_rows_filters(self):
        linkauthority.generate(self.conn, ["keto snacks"], limit=len(linkauthority.TACTICS))
        linkauthority.generate(self.conn, ["golf gifts"], limit=len(linkauthority.TACTICS))
        self.assertEqual(len(linkauthority.list_rows(self.conn, tactic="haro")), 2)
        self.assertEqual(len(linkauthority.list_rows(self.conn, keyword="golf")),
                         len(linkauthority.TACTICS))

    def test_search_urls_are_real_engines(self):
        u = linkauthority.search_urls('keto "write for us"')
        self.assertIn("google.com/search", u["google"])
        self.assertIn("bing.com/search", u["bing"])
        self.assertIn("duckduckgo.com", u["duckduckgo"])
        self.assertIn("%22write%20for%20us%22", u["google"])

    def test_all_tactics_have_content(self):
        for key, meta in linkauthority.TACTICS.items():
            self.assertTrue(meta["label"] and meta["goal"] and meta["platforms"])
            self.assertIn(key, linkauthority._SEARCHES)
            self.assertIn(key, linkauthority._PITCH)
            self.assertIn(key, linkauthority._ANGLES)


class TestLinkAuthorityHTTP(unittest.TestCase):
    """Boots a real HTTP server against a copied DB, like the rest of the suite."""

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_linkauth_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    @classmethod
    def _login(cls):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=5)
        conn.request("POST", "/admin/login",
                     body=b"email=owner@test.example&password=test-pass-123",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        sc = resp.getheader("Set-Cookie")
        resp.read()
        conn.close()
        return sc.split(";")[0]

    @classmethod
    def _raw(cls, path, method="GET", body=None, cookie=None, timeout=8):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=timeout)
        hdrs = {}
        if cookie:
            hdrs["Cookie"] = cookie
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, resp.getheader("Location"), resp.read())
        conn.close()
        return out

    def setUp(self):
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM backlink_outreach")
            conn.commit()
            conn.close()

    def test_admin_page_requires_login(self):
        st, location, _ = self._raw("/admin/linkauthority")
        self.assertEqual(st, 302)
        self.assertTrue(location.startswith("/admin/login"))

    def test_api_requires_auth(self):
        st, _, data = self._raw("/api/linkauthority/list", "POST", body=b"{}")
        self.assertEqual(st, 401)

    def test_generate_then_list_then_update_then_delete(self):
        st, _, data = self._raw(
            "/api/linkauthority/generate", "POST",
            body=json.dumps({"keywords": "keto snacks", "limit": 5}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertEqual(res["added"], 5)
        self.assertEqual(res["keywords"], 1)

        st, _, data = self._raw("/api/linkauthority/list", "POST", body=b"{}",
                                cookie=self.cookie)
        self.assertEqual(st, 200)
        rows = json.loads(data)["rows"]
        self.assertEqual(len(rows), 5)
        row_id = rows[0]["id"]

        st, _, data = self._raw(
            "/api/linkauthority/update", "POST",
            body=json.dumps({"id": row_id, "status": "live",
                             "target_url": "https://blog.test/roundup",
                             "note": "done"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["row"]["status"], "live")

        st, _, data = self._raw(
            "/api/linkauthority/delete", "POST",
            body=json.dumps({"id": row_id}), cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["removed"], 1)

    def test_update_rejects_bad_status(self):
        self._raw("/api/linkauthority/generate", "POST",
                  body=json.dumps({"keywords": "keto snacks", "limit": 1}),
                  cookie=self.cookie)
        st, _, data = self._raw("/api/linkauthority/list", "POST", body=b"{}",
                                cookie=self.cookie)
        row_id = json.loads(data)["rows"][0]["id"]
        st, _, data = self._raw(
            "/api/linkauthority/update", "POST",
            body=json.dumps({"id": row_id, "status": "bogus"}),
            cookie=self.cookie)
        self.assertEqual(st, 400)

    def test_admin_page_renders_pipeline(self):
        st, _, data = self._raw("/admin/linkauthority", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = data.decode("utf-8", "replace")
        self.assertIn("Link authority", html)
        self.assertIn("Generate targets", html)
        self.assertIn("/api/linkauthority/generate", html)
        for meta in linkauthority.TACTICS.values():
            self.assertIn(meta["label"], html)

    def test_generate_uses_stocked_niches_when_none_given(self):
        st, _, data = self._raw("/api/linkauthority/generate", "POST",
                                body=b"{}", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertGreater(json.loads(data)["added"], 0)


if __name__ == "__main__":
    unittest.main()