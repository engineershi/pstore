"""Regression tests for the IndexNow key fallback bug.

`set_key("")` used to store an empty string as a *real* value, and `key()`
only tested `is not None`. So the first boot with a missing/empty
`indexnow.key` setting pinned the process to an empty key: `/<key>.txt` 404'd,
`/api/indexnow` reported no key, and every submission went out unauthenticated
-- with the hardcoded build-time default sitting right there unused.

These tests pin the fixed contract: clearing the runtime override means "use
the default", never "become keyless".
"""
import unittest

import indexnow


class TestKeyFallback(unittest.TestCase):
    def setUp(self):
        self.saved = indexnow._RUNTIME_KEY
        self.saved_default = indexnow._DEFAULT_KEY

    def tearDown(self):
        indexnow._RUNTIME_KEY = self.saved
        indexnow._DEFAULT_KEY = self.saved_default

    def test_clear_falls_back_to_default(self):
        """The bug: this returned "" and killed IndexNow for the process."""
        indexnow.set_key("")
        self.assertNotEqual(indexnow.key(), "")
        self.assertEqual(indexnow.key(), self.saved_default)

    def test_none_falls_back_to_default(self):
        indexnow.set_key(None)
        self.assertEqual(indexnow.key(), self.saved_default)

    def test_whitespace_falls_back_to_default(self):
        indexnow.set_key("   ")
        self.assertEqual(indexnow.key(), self.saved_default)

    def test_explicit_key_wins(self):
        indexnow.set_key("a" * 32)
        self.assertEqual(indexnow.key(), "a" * 32)

    def test_key_is_normalised(self):
        indexnow.set_key("  ABCDEF0123456789ABCDEF0123456789  ")
        self.assertEqual(indexnow.key(), "abcdef0123456789abcdef0123456789")

    def test_default_is_used_when_never_set(self):
        indexnow._RUNTIME_KEY = None
        self.assertEqual(indexnow.key(), self.saved_default)

    def test_no_default_still_returns_empty_not_crash(self):
        indexnow._DEFAULT_KEY = ""
        indexnow.set_key("")
        self.assertEqual(indexnow.key(), "")

    def test_clearing_is_idempotent(self):
        indexnow.set_key("b" * 32)
        indexnow.set_key("")
        indexnow.set_key("")
        self.assertEqual(indexnow.key(), self.saved_default)

    def test_key_file_path_is_never_a_bare_dot_txt(self):
        """A 404 on `/.txt` was the visible symptom of the bug."""
        indexnow.set_key("")
        path = indexnow.key_file_path("https://trypstore.com")
        self.assertNotIn("/.txt", path)
        self.assertTrue(path.endswith(".txt"))
        self.assertRegex(path, r"/[0-9a-f]{32}\.txt$")

    def test_key_file_path_still_empty_key_is_dot_txt(self):
        """With no default configured there is nothing better to emit, but
        document the degenerate case so a future default is noticed."""
        indexnow._DEFAULT_KEY = ""
        indexnow._RUNTIME_KEY = None
        self.assertEqual(indexnow.key_file_path("https://trypstore.com"),
                         "https://trypstore.com/.txt")


if __name__ == "__main__":
    unittest.main()
