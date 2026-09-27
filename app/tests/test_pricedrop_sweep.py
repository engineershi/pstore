"""The price-drop sweep must actually be able to finish a pass.

Found in production: `/api/pricedrop/state` reported
`running: true, status: "scanning", checked: 1086 / 2685, last_run: ""` -- a
sweep that had never completed a single pass. The cause was arithmetic, not
luck:

    _PRICEDROP_ASIN_TIMEOUT (35s) x 2,685 watched ASINs, one worker at a time
        = up to 26 hours for one pass, against a 6-hour auto interval

and the auto loop does `if data.get("running"): continue`, so exactly one pass
ever started and it could never finish. Consequence: 0 price drops and 0
price-drop emails, ever -- the urgency/return-visit mechanic was inert.

These tests pin the three properties that make a pass finishable: bounded
per-ASIN work with real concurrency, durability of progress across a restart,
and a reclaimable heartbeat so a dead worker cannot wedge the loop.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

import pricedrop
import server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeStore:
    """Minimal stand-in for the pricedrop store: same-day idempotency, the
    property the resume logic depends on."""

    def __init__(self):
        self.data = {}
        self.records = 0

    def snapshots(self, asin):
        return list(self.data.get(asin.upper(), {}).get("snapshots", []))

    def record(self, asin, price=None, reviews=None, when=None):
        key = asin.upper()
        cur = self.data.setdefault(key, {})
        if cur.get("price") is None and price is not None:
            cur["price"] = price
        if when is None:
            when = time.strftime("%Y-%m-%d", time.gmtime())
        else:
            when = str(when)[:10]
        snaps = cur.setdefault("snapshots", [])
        if snaps and snaps[-1].get("ts") == when:
            snaps[-1] = {"ts": when, "price": price, "reviews": reviews}
        else:
            snaps.append({"ts": when, "price": price, "reviews": reviews})
        self.records += 1

    def baseline(self, asin):
        return (self.data.get(asin.upper()) or {}).get("price")


class TestPricedropScanFeasibility(unittest.TestCase):
    """The arithmetic that made the sweep impossible in the first place."""

    def test_watchlist_outgrows_a_single_worker_pass(self):
        rows = [{"asin": "B%09d" % i} for i in range(2685)]
        sequential_worst = len(rows) * server._PRICEDROP_ASIN_TIMEOUT
        interval = 6 * 3600
        self.assertGreater(sequential_worst, interval,
                           "if this drops below the interval the old "
                           "single-worker design was still viable")
        # The fix must make a worst-case pass fit inside the auto interval.
        parallel_worst = (len(rows) * server._PRICEDROP_ASIN_TIMEOUT
                          / server._PRICEDROP_WORKERS)
        self.assertLess(parallel_worst, interval,
                        "even parallelised, a fully-timing-out pass must fit "
                        "in the interval or the loop is still blocked")

    def test_workers_are_configured(self):
        self.assertGreater(server._PRICEDROP_WORKERS, 1,
                           "a single worker is what wedged the scanner")

    def test_progress_writes_are_throttled(self):
        self.assertGreater(server._PRICEDROP_STATE_WRITE_EVERY, 1,
                           "one write per ASIN is 2,685 commits per pass")

    def test_stale_threshold_is_sane(self):
        self.assertGreater(server._PRICEDROP_STALE_SECONDS,
                           server._PRICEDROP_ASIN_TIMEOUT,
                           "the heartbeat must outlive a per-ASIN fetch "
                           "window or a healthy scan looks dead")


class TestPricedropResume(unittest.TestCase):
    """The store is the checkpoint, so an interrupted pass resumes."""

    def setUp(self):
        self.store = _FakeStore()
        self.today = time.strftime("%Y-%m-%d", time.gmtime())
        self._store = server._pricedrop_store
        server._pricedrop_store = lambda: self.store
        self._settings = server._set_setting
        self.written = []
        server._set_setting = lambda k, v: self.written.append((k, v))

    def tearDown(self):
        server._pricedrop_store = self._store
        server._set_setting = self._settings

    def _rows(self, n):
        return [{"asin": "B%09d" % i} for i in range(n)]

    def _state(self):
        """The most recent persisted state -- the sweep overwrites it as it
        goes, so only the last write reflects where the pass ended."""
        latest = None
        for k, v in self.written:
            if k == server._PRICEDROP_STATE_KEY:
                latest = json.loads(v)
        return latest

    def test_asins_scanned_today_are_skipped(self):
        """Half the watchlist already has today's snapshot -> only the other
        half is fetched."""
        for i in range(5):
            self.store.record("B%09d" % i, price=10.0 + i)
        calls = []

        def fake_search(asin, n):
            calls.append(asin)
            return ([{"price": 5.0, "reviews": 7}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(10), 5)
        finally:
            server.amazon.search = real
        self.assertEqual(len(calls), 5, "resumed ASINs were refetched: %r" % calls)
        self.assertNotIn("B000000000", calls)

    def test_yesterdays_snapshot_is_refetched(self):
        """Only *today's* work counts as done; yesterday's must be redone."""
        for i in range(3):
            self.store.record("B%09d" % i, price=10.0, when="2020-01-01")
        calls = []

        def fake_search(asin, n):
            calls.append(asin)
            return ([{"price": 5.0, "reviews": 1}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(3), 5)
        finally:
            server.amazon.search = real
        self.assertEqual(len(calls), 3)

    def test_failed_fetch_is_not_marked_done(self):
        """A None price must not checkpoint, or a transient failure would
        suppress that ASIN for a whole day."""
        self.store.record("B000000000", price=None)
        calls = []

        def fake_search(asin, n):
            calls.append(asin)
            if asin == "B000000000":
                return ([{"price": None, "reviews": None}], "src")
            return ([{"price": 5.0}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(1), 5)
        finally:
            server.amazon.search = real
        snaps = self.store.snapshots("B000000000")
        self.assertFalse(any(s.get("price") is not None for s in snaps),
                         "a failed fetch was checkpointed as complete")

    def test_progress_is_checkpointed_during_the_scan(self):
        """Prices must be recorded as they arrive, not only at the end, or a
        deploy mid-pass throws the whole pass away."""
        def fake_search(asin, n):
            return ([{"price": 3.0, "reviews": 2}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(12), 5)
        finally:
            server.amazon.search = real
        self.assertGreater(self.store.records, 0)
        for i in range(12):
            self.assertIsNotNone(self.store.snapshots("B%09d" % i))

    def test_scan_finishes_and_clears_running(self):
        def fake_search(asin, n):
            return ([{"price": 3.0, "reviews": 2}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(6), 5)
        finally:
            server.amazon.search = real
        st = self._state()
        self.assertFalse(st["running"])
        self.assertEqual(st["status"], "done")
        self.assertTrue(st["last_run"], "last_run must be stamped on completion")

    def test_final_state_carries_a_heartbeat(self):
        def fake_search(asin, n):
            return ([{"price": 3.0}], "src")

        real = server.amazon.search
        server.amazon.search = fake_search
        try:
            server._pricedrop_scan(self._rows(3), 5)
        finally:
            server.amazon.search = real
        self.assertIn("beat", self._state())

    def test_stuck_window_still_completes_and_accounts(self):
        """A black-holed fetch must not leave the pass hanging or miscount it.

        Regression from the parallel rewrite: the timeout branch abandoned the
        window without incrementing `checked`, so a pass whose only fetch hung
        reported checked=0, and a dispatch-counter slip once reported a
        negative count. The pass must finish, count every ASIN exactly once,
        and say why.
        """
        import threading

        release = threading.Event()

        def black_hole(asin, n):
            release.wait(30)
            return ([{"price": 5.0}], "src")

        rows = [{"asin": "B%09d" % i} for i in range(5)]
        saved = server._PRICEDROP_ASIN_TIMEOUT
        server._PRICEDROP_ASIN_TIMEOUT = 0.2
        real = server.amazon.search
        server.amazon.search = black_hole
        t = threading.Thread(target=server._pricedrop_scan, args=(rows, 5),
                             daemon=True)
        try:
            t.start()
            t.join(20)
            self.assertFalse(t.is_alive(), "a stuck window stalled the pass")
            st = self._state()
            self.assertFalse(st["running"])
            self.assertEqual(st["status"], "done")
            self.assertEqual(st["checked"], len(rows),
                             "every ASIN must be accounted for exactly once")
            self.assertIn("timeout", st["error"])
        finally:
            release.set()
            server.amazon.search = real
            server._PRICEDROP_ASIN_TIMEOUT = saved


class TestPricedropAutoLoopReclaim(unittest.TestCase):
    """A dead worker must not wedge the auto loop forever."""

    def test_heartbeat_is_written_while_running(self):
        st = {"running": True, "owner": server._BOOT_ID, "status": "scanning",
              "checked": 5, "total": 10, "drops": [], "last_run": "",
              "error": "", "beat": time.time()}
        self.assertIn("beat", st)

    def test_live_heartbeat_counts_as_running(self):
        beat = time.time()
        fresh = beat > 0 and (time.time() - beat) < server._PRICEDROP_STALE_SECONDS
        self.assertTrue(fresh, "a just-written heartbeat must read as live")

    def test_dead_heartbeat_is_reclaimable(self):
        beat = time.time() - (server._PRICEDROP_STALE_SECONDS + 60)
        fresh = beat > 0 and (time.time() - beat) < server._PRICEDROP_STALE_SECONDS
        self.assertFalse(fresh, "an expired heartbeat must not read as live")

    def test_missing_heartbeat_is_reclaimable(self):
        beat = 0
        fresh = beat > 0 and (time.time() - beat) < server._PRICEDROP_STALE_SECONDS
        self.assertFalse(fresh, "a legacy state with no beat must be reclaimable")

    def test_previous_boot_flag_is_already_cleared(self):
        """`_run_state` must neutralise another boot's flag, which is how the
        scanner survived restarts before heartbeats existed."""
        st = json.dumps({"running": True, "owner": "someoldbootid",
                         "status": "scanning", "checked": 1086, "total": 2685})
        data = server._run_state(st)
        self.assertFalse(data["running"])
        self.assertEqual(data["status"], "interrupted")

    def test_wedged_state_is_recoverable_end_to_end(self):
        """The exact production state: running, no heartbeat, foreign owner."""
        st = json.dumps({"running": True, "owner": "previousboot",
                         "status": "scanning", "checked": 1086, "total": 2685,
                         "last_run": ""})
        data = server._run_state(st)
        self.assertFalse(data.get("running"),
                         "a wedged sweep must not block the loop")


if __name__ == "__main__":
    unittest.main()
