"""Regressions for the rate limiter's bucket sweep and for the boot-scoped
ownership of long-running background jobs.

Both bugs shared a shape: state that only ever grows (or only ever clears), so
the failure is invisible until memory climbs or a button stops working.
"""
import json
import os
import shutil
import sys
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import security


class TestRateLimiterSweep(unittest.TestCase):
    def test_window_still_enforced(self):
        rl = security.RateLimiter(3, 5.0)
        self.assertEqual([rl.hit("k") for _ in range(5)],
                         [True, True, True, False, False])

    def test_hits_expire_after_the_window(self):
        rl = security.RateLimiter(2, 0.3)
        self.assertEqual([rl.hit("z") for _ in range(4)],
                         [True, True, False, False])
        time.sleep(0.4)
        self.assertEqual([rl.hit("z") for _ in range(3)],
                         [True, True, False])

    def test_rotating_client_keys_do_not_grow_the_map_without_bound(self):
        """A client rotating X-Forwarded-For mints one key per request. Those
        buckets must expire; the old `max(64, len(self._hits))` sweep gate could
        never fire because the counter and the map grew at the same rate, so
        every dead bucket was retained for the life of the process."""
        rl = security.RateLimiter(5, 0.5)
        for i in range(5000):
            rl.hit("10.0.%d.%d" % (i // 250, i % 250))
        # all 5000 are live inside the window, so they are legitimately held
        self.assertEqual(len(rl._hits), 5000)
        time.sleep(0.6)
        for i in range(300):
            rl.hit("10.9.9.%d" % i)
        self.assertLess(len(rl._hits), 400,
                        "expired buckets were never reclaimed")

    def test_sweep_cadence_fires_on_the_stride(self):
        """The stride must actually fire. A sweep cannot drop a bucket that is
        still inside its window, so this checks the cadence itself: after
        _SWEEP_EVERY requests the counter resets, proving the gate no longer
        chases len(self._hits)."""
        rl = security.RateLimiter(5, 3600.0)
        rl.hit("warmup")  # the first call always sweeps an empty map
        for i in range(security.RateLimiter._SWEEP_EVERY - 1):
            rl.hit("k%d" % i)
        self.assertEqual(rl._sweeps, security.RateLimiter._SWEEP_EVERY - 1)
        rl.hit("k-last")
        self.assertEqual(rl._sweeps, 0, "stride never triggered a sweep")

    def test_sweep_does_not_reset_a_live_bucket(self):
        """Reclaiming is per-key: a key still inside its window must keep its
        history, so the limit is not silently refreshed by a sweep."""
        rl = security.RateLimiter(3, 5.0)
        self.assertTrue(rl.hit("live"))
        self.assertTrue(rl.hit("live"))
        self.assertTrue(rl.hit("live"))
        for i in range(2000):
            rl.hit("noise%d" % i)
        self.assertFalse(rl.hit("live"), "sweep reset the bucket and unblocked it")
        self.assertFalse(rl.hit("live"))

        rl = security.RateLimiter(3, 5.0)
        self.assertTrue(rl.hit("live"))
        self.assertTrue(rl.hit("live"))
        self.assertTrue(rl.hit("live"))
        for i in range(2000):
            rl.hit("noise%d" % i)
        self.assertFalse(rl.hit("live"), "sweep reset the bucket and unblocked it")
        self.assertFalse(rl.hit("live"))

    def test_quiet_limiter_still_reclaims_on_the_next_request(self):
        """Time-based fallback: after traffic stops, the first later request
        must still drop expired buckets instead of holding them forever."""
        rl = security.RateLimiter(5, 0.4)
        for i in range(1000):
            rl.hit("q%d" % i)
        self.assertGreater(len(rl._hits), 900)
        time.sleep(0.5)
        rl.hit("trigger")
        self.assertEqual(len(rl._hits), 1)


class TestBackgroundRunOwnership(unittest.TestCase):
    """A run flagged `running` in sqlite but owned by a dead process would make
    every later POST answer `{"started": false}` forever, permanently disabling
    the Price-drop, Drop-push and Publish-all buttons."""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.db = "/tmp/pstore_test_orphan_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        cls._env = {k: os.environ.get(k) for k in
                    ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD")}
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        import server
        importlib.reload(server)
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db + suffix)
            except OSError:
                pass

    def setUp(self):
        for key in self.server._ORPHANED_RUN_KEYS:
            self.server._set_setting(key, "{}")

    def _handler(self):
        return self.server.Handler.__new__(self.server.Handler)

    def test_run_state_trusts_only_the_current_boot(self):
        live = json.dumps({"running": True, "owner": self.server._BOOT_ID,
                           "status": "sending"})
        self.assertTrue(self.server._run_state(live)["running"])
        dead = json.dumps({"running": True, "owner": "some-previous-boot",
                           "status": "sending"})
        parsed = self.server._run_state(dead)
        self.assertFalse(parsed["running"])
        self.assertEqual(parsed["status"], "interrupted")

    def test_run_state_tolerates_garbage(self):
        for raw in ("", "   ", "not json", "null", "[]"):
            self.assertIsInstance(self.server._run_state(raw), dict)

    def test_state_readers_report_an_orphan_as_not_running(self):
        self.server._set_setting(self.server._PRICEDROP_SEND_KEY, json.dumps(
            {"running": True, "owner": "dead", "status": "sending"}))
        h = self._handler()
        self.assertFalse(h._pricedrop_send_state()["running"])
        self.assertEqual(h._pricedrop_send_state()["status"], "interrupted")

    def test_startup_sweep_clears_every_orphan_key(self):
        for key in self.server._ORPHANED_RUN_KEYS:
            self.server._set_setting(key, json.dumps(
                {"running": True, "owner": "dead", "status": "scanning",
                 "checked": 3, "total": 9}))
        cleared = self.server._clear_orphaned_runs()
        self.assertEqual(sorted(cleared), sorted(self.server._ORPHANED_RUN_KEYS))
        for key in self.server._ORPHANED_RUN_KEYS:
            stored = json.loads(self.server._get_setting(key, "{}"))
            self.assertFalse(stored["running"])
            self.assertEqual(stored["status"], "interrupted")
            self.assertEqual(stored["checked"], 3,
                             "progress must survive so the UI can report it")

    def test_startup_sweep_is_idempotent_and_never_touches_a_live_run(self):
        self.server._set_setting(self.server._PRICEDROP_SEND_KEY, json.dumps(
            {"running": True, "owner": "dead", "status": "sending"}))
        self.assertEqual(len(self.server._clear_orphaned_runs()), 1)
        self.assertEqual(self.server._clear_orphaned_runs(), [])
        self.server._set_setting(self.server._PRICEDROP_SEND_KEY, json.dumps(
            {"running": True, "owner": self.server._BOOT_ID, "status": "sending"}))
        self.assertEqual(self.server._clear_orphaned_runs(), [],
                         "must not cancel this process's own in-flight run")
        self.assertTrue(self._handler()._pricedrop_send_state()["running"])


if __name__ == "__main__":
    unittest.main()
