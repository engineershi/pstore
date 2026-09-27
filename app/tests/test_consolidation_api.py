"""End-to-end test for the consolidation settings write path.

`_consolidation_holds()` existed and the pages honoured `hold=`, but
`POST /api/settings` had no `consolidation` branch, so the planner's
`{"consolidation": {"holds": [...]}}` payload was accepted with 200 and
silently discarded. Nothing read the value back either, so an operator had no
way to confirm what was actually applied.

These tests drive the real HTTP endpoint, because the bug lived precisely in
the gap between the helper and the route.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

import server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLD = "best-griddle-pan"
KEEP = "best-solar-lights"


class TestConsolidationSettingsAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="consol_api_")
        cls.db = os.path.join(cls.tmp, "t.db")
        shutil.copy(os.path.join(REPO, "pstore.db"), cls.db)
        cls.port = 8796
        cls._saved = {k: os.environ.get(k) for k in
                      ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD",
                       "PSTORE_PORT", "PSTORE_SKIP_WORKERS")}
        os.environ.update(
            PSTORE_DB=cls.db, PSTORE_ADMIN_EMAIL="t@example.com",
            PSTORE_ADMIN_PASSWORD="testpass123", PSTORE_PORT=str(cls.port),
            PSTORE_SKIP_WORKERS="1")
        import importlib
        importlib.reload(server)
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})
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

    def _post(self, payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/settings" % self.port,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())

    def _get(self):
        req = urllib.request.Request("http://127.0.0.1:%d/api/settings" % self.port)
        req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def setUp(self):
        self._post({"consolidation": {"holds": []}})

    # ------------------------------------------------------------------ write
    def test_holds_are_actually_persisted(self):
        """The regression: this used to return 200 and store nothing."""
        st, body = self._post({"consolidation": {"holds": [HOLD]}})
        self.assertEqual(st, 200)
        self.assertEqual(body["consolidation"]["holds"], [HOLD])
        self.assertEqual(self._get()["consolidation"]["holds"], [HOLD])
        self.assertEqual(server._consolidation_holds(), frozenset({HOLD}))

    def test_multiple_holds_round_trip_sorted(self):
        holds = ["zeta-niche", "alpha-niche", HOLD]
        self._post({"consolidation": {"holds": holds}})
        self.assertEqual(self._get()["consolidation"]["holds"],
                         sorted(holds))

    def test_empty_list_clears(self):
        self._post({"consolidation": {"holds": [HOLD]}})
        self.assertEqual(self._get()["consolidation"]["holds"], [HOLD])
        st, body = self._post({"consolidation": {"holds": []}})
        self.assertEqual(st, 200)
        self.assertEqual(body["consolidation"]["holds"], [])
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_null_clears(self):
        self._post({"consolidation": {"holds": [HOLD]}})
        self._post({"consolidation": {"holds": None}})
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_comma_string_accepted(self):
        """The planner may post a CSV; accept it rather than 500."""
        st, body = self._post({"consolidation": {"holds": "a-niche, b-niche ,,"}})
        self.assertEqual(st, 200)
        self.assertEqual(body["consolidation"]["holds"], ["a-niche", "b-niche"])

    def test_rollback_by_clearing(self):
        before = self._get()["consolidation"]["holds"]
        self._post({"consolidation": {"holds": [HOLD]}})
        self._post({"consolidation": {"holds": before}})
        self.assertEqual(self._get()["consolidation"]["holds"], before)

    # ------------------------------------------------------------------ shape
    def test_absent_key_leaves_state_untouched(self):
        self._post({"consolidation": {"holds": [HOLD]}})
        self._post({"market": "com"})          # no consolidation key
        self.assertEqual(self._get()["consolidation"]["holds"], [HOLD])

    def test_holds_key_absent_but_group_present(self):
        self._post({"consolidation": {"holds": [HOLD]}})
        self._post({"consolidation": {}})
        self.assertEqual(self._get()["consolidation"]["holds"], [HOLD])

    def test_junk_types_do_not_500(self):
        for junk in (123, True, {"a": 1}):
            st, _ = self._post({"consolidation": {"holds": junk}})
            self.assertEqual(st, 200, "junk %r rejected" % (junk,))

    def test_read_shape_is_always_a_list(self):
        d = self._get()
        self.assertIn("consolidation", d)
        self.assertIsInstance(d["consolidation"]["holds"], list)
        self.assertEqual(d["consolidation"]["holds"],
                         sorted(d["consolidation"]["holds"]))

    def test_other_settings_survive_a_consolidation_post(self):
        """A consolidation write must not blank the affiliate tag or PA-API."""
        _, before = self._post({"market": "com"})
        self._post({"consolidation": {"holds": [HOLD]}})
        after = self._get()
        self.assertEqual(after["affiliate_tag"], before["affiliate_tag"])
        self.assertIn("paapi", after)


if __name__ == "__main__":
    unittest.main()
