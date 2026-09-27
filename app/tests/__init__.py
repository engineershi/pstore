# -*- coding: utf-8 -*-
"""Package init for the test suite.

This runs before any test module is imported, which is the only point where it
can reliably protect a committed file.

Why this exists: `server.py` resolves its database path once, at import time:

    DB = os.environ.get("PSTORE_DB", os.path.join(ROOT, "pstore.db"))

So a test that imports `server` while `PSTORE_DB` is unset captures
`app/pstore.db` -- a TRACKED, COMMITTED file holding the 43-niche seed -- and
every later `_set_setting` / `_get_setting` call in that module writes straight
into it. Which test does that depends on module import order, so it is not
something an individual test can be trusted to avoid on its own, and the
damage is invisible: the suite still passes, and the corrupted seed is only
noticed when it is committed.

Individual server tests already copy the seed into a temp file and reload the
module, and that is still the right thing for them to do (they need an isolated,
seeded database). This just closes the gap for anything that forgets.

To clean up after a bad run:  git checkout -- app/pstore.db
"""
import atexit
import os
import shutil
import tempfile
import uuid

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(_APP, "pstore.db")

# Respect an explicit override (e.g. running against a real scratch DB).
if not os.environ.get("PSTORE_DB"):
    _tmpdir = tempfile.mkdtemp(prefix="pstore-tests-")
    _scratch = os.path.join(_tmpdir, "suite-%s.db" % uuid.uuid4().hex[:8])
    try:
        shutil.copy(SEED, _scratch)
    except OSError:
        _scratch = os.path.join(_tmpdir, "suite.db")
    os.environ["PSTORE_DB"] = _scratch
    # Any server test that reloads `server` now picks up this path instead of
    # the committed seed, and one that does not reload is still protected.
    os.environ.setdefault("PSTORE_ADMIN_EMAIL", "owner@test.example")
    os.environ.setdefault("PSTORE_ADMIN_PASSWORD", "test-pass-123")

    def _cleanup():
        for path in (_scratch, _scratch + "-wal", _scratch + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(_tmpdir)
        except OSError:
            pass

    atexit.register(_cleanup)
