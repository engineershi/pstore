"""Keep-alive regression tests.

The site served HTTP/1.0 semantics (BaseHTTPRequestHandler's default), so every
response carried `Connection: close` and every subresource paid a fresh TCP+TLS
handshake. These tests pin the HTTP/1.1 contract and, critically, that every
response is properly delimited so a client can actually reuse the socket.
"""
import contextlib
import http.client
import io
import os
import shutil
import socket
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import security
import server


class TestKeepAlive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_keepalive_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        cls._env = {k: os.environ.get(k) for k in
                    ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD")}
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
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

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=30)

    def test_protocol_version_is_http_1_1(self):
        self.assertEqual(server.Handler.protocol_version, "HTTP/1.1")

    def test_html_response_is_keep_alive_delimited(self):
        c = self._conn()
        c.request("GET", "/")
        r = c.getresponse()
        body = r.read()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.version, 11, "must answer HTTP/1.1")
        self.assertIsNotNone(r.getheader("Content-Length"),
                             "no Content-Length means the client must read "
                             "until close, which defeats keep-alive")
        self.assertEqual(int(r.getheader("Content-Length")), len(body))
        self.assertFalse(r.will_close, "connection must stay open")
        c.close()

    def test_many_requests_reuse_one_socket(self):
        """The whole point: N requests, one connection, no close in between."""
        c = self._conn()
        paths = ["/", "/robots.txt", "/style.css", "/app.js", "/", "/about"]
        for i, p in enumerate(paths):
            c.request("GET", p)
            r = c.getresponse()
            body = r.read()
            self.assertEqual(r.status, 200, p)
            self.assertFalse(r.will_close,
                             "connection closed after request %d (%s)" % (i, p))
            self.assertIsNotNone(r.getheader("Content-Length"), p)
        c.close()

    def test_redirect_carries_zero_content_length(self):
        c = self._conn()
        # /admin/analytics is gated -> 302 to the login page
        c.request("GET", "/admin/analytics")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Content-Length"), "0")
        self.assertFalse(r.will_close)
        c.close()

    def test_trailing_slash_redirect_stays_on_one_connection(self):
        c = self._conn()
        c.request("GET", "/about/")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 301)
        self.assertEqual(r.getheader("Content-Length"), "0")
        self.assertFalse(r.will_close)
        # and the connection is immediately reusable
        c.request("GET", "/about")
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 200)
        c.close()

    def test_404_is_keep_alive_delimited(self):
        c = self._conn()
        c.request("GET", "/definitely-not-a-page-xyz")
        r = c.getresponse()
        body = r.read()
        self.assertEqual(r.status, 404)
        self.assertIsNotNone(r.getheader("Content-Length"))
        self.assertFalse(r.will_close)
        c.close()

    def test_post_then_get_on_same_connection(self):
        c = self._conn()
        c.request("POST", "/api/track",
                  body=b'{"type":"click","asin":"B0KEEPALIVE01"}',
                  headers={"Content-Type": "application/json",
                           "Host": "127.0.0.1"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertFalse(r.will_close)
        c.request("GET", "/robots.txt")
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 200)
        c.close()

    def test_json_error_is_keep_alive_delimited(self):
        c = self._conn()
        c.request("GET", "/api/mine")
        r = c.getresponse()
        body = r.read()
        self.assertIn(r.status, (401, 403))
        self.assertIsNotNone(r.getheader("Content-Length"))
        self.assertFalse(r.will_close)
        c.close()

    def test_redirect_that_sets_a_cookie_is_delimited(self):
        """Regression: the auto-login / OAuth-callback redirects call
        _set_cookie() and then emit a bare 302. Missing Content-Length there
        made every verification link hang the browser for the full socket
        timeout instead of redirecting."""
        c = self._conn()
        # Invalid link -> 302 to the login page.
        c.request("GET", "/admin/verify?t=garbage&e=nobody%40test.example")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Content-Length"), "0")
        self.assertFalse(r.will_close)
        c.request("GET", "/admin/login")
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 200)
        c.close()

    def test_pipelined_head_then_get_are_both_framed(self):
        """One socket, two requests sent back to back. The second must not
        inherit the first response's state."""
        s = socket.create_connection(("127.0.0.1", self.PORT), timeout=10)
        try:
            s.sendall(b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n"
                      b"GET /robots.txt HTTP/1.1\r\nHost: x\r\n\r\n")
            buf = b""
            s.settimeout(5)
            try:
                while buf.count(b"HTTP/1.1 ") < 2 or buf.count(b"Content-Length") < 2:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            self.assertEqual(buf.count(b"HTTP/1.1 "), 2,
                             "expected two framed responses, got %r" % buf[:200])
        finally:
            s.close()

    def test_logout_redirect_sets_cookie_and_stays_alive(self):
        c = self._conn()
        c.request("GET", "/admin/logout")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Content-Length"), "0")
        c.close()

    def test_safety_net_frames_a_bodyless_response(self):
        """A response that forgets Content-Length entirely must still be
        delimited by the guard, not hang the client. See TestFramingGuard for
        the deliberately-broken handler that exercises this."""
        c = self._conn()
        c.request("GET", "/__ka_probe/empty")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404, "real handler should 404 unknown probes")
        self.assertIsNotNone(r.getheader("Content-Length"))
        self.assertFalse(r.will_close)
        c.close()


class TestFramingGuard(unittest.TestCase):
    """The framing guard, exercised with deliberately-broken handlers. These
    routes never exist in production; they only exist to prove the guard frames
    a response that forgot its length instead of hanging the client.

    The handler is built lazily inside setUpClass because other test modules
    reload `server`, which replaces the Handler class: a subclass captured at
    import time would keep serving the old code against a new module."""

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_guard_%s.db" % uuid.uuid4().hex[:6]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        cls._env = {k: os.environ.get(k) for k in
                    ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD")}
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        import importlib
        importlib.reload(server)
        base = server.Handler

        class _Probe(base):
            def do_GET(self):
                if self.path == "/__ka_probe/empty":
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.path == "/__ka_probe/unframed":
                    # Body written with no declared length: the guard cannot
                    # know a body is coming, so it frames the response as empty
                    # and logs loudly.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"undelimited-probe")
                    return
                return base.do_GET(self)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
        cls.PORT = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
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

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()

    def test_bodyless_response_gets_zero_length(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r, body = self._get("/__ka_probe/empty")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Length"), "0")
        self.assertEqual(body, b"")
        self.assertFalse(r.will_close, "must not hang waiting for a close")
        self.assertIn("no Content-Length", buf.getvalue())

    def test_undeclared_body_is_framed_and_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r, body = self._get("/__ka_probe/unframed")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Length"), "0")
        self.assertIn("no Content-Length", buf.getvalue(),
                      "a real mistake must be visible in the logs")

    def test_normal_route_never_triggers_the_guard(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r, body = self._get("/robots.txt")
        self.assertEqual(r.status, 200)
        self.assertFalse(r.will_close)
        self.assertTrue(body)
        self.assertEqual(buf.getvalue(), "",
                         "well-formed responses must not warn")

    def test_head_sends_length_without_a_body(self):
        c = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        c.request("HEAD", "/")
        r = c.getresponse()
        body = r.read()
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(r.getheader("Content-Length")), 0,
                           "HEAD must report the real length, not 0")
        c.close()


class TestMalformedRequests(unittest.TestCase):
    """A request line the stdlib cannot parse routes through send_error(), which
    calls our end_headers() *before* self.headers exists. The unguarded
    X-Forwarded-Proto lookup raised AttributeError, so the client got no
    response at all and the process logged a traceback for every bad probe."""

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_malformed_%s.db" % uuid.uuid4().hex[:6]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        cls._env = {k: os.environ.get(k) for k in
                    ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD")}
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
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

    def _raw(self, lines, wait=3.0):
        s = socket.create_connection(("127.0.0.1", self.PORT), timeout=10)
        try:
            s.sendall(("\r\n".join(lines + ["", ""])).encode())
            buf = b""
            s.settimeout(wait)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            return buf
        finally:
            s.close()

    def test_unparseable_request_line_still_gets_a_response(self):
        out = self._raw(["GARBAGE", "Host: x"])
        self.assertTrue(out, "client received nothing at all")
        self.assertIn(b"Error", out)

    def test_bad_version_still_gets_a_response(self):
        out = self._raw(["GET / HTTP/9.9", "Host: x"])
        self.assertTrue(out)

    def test_overlong_request_line_is_rejected_cleanly(self):
        out = self._raw(["GET /" + ("a" * 70000) + " HTTP/1.1", "Host: x"])
        self.assertIn(b"414", out)
        self.assertIn(b"Content-Length", out)

    def test_unsupported_method_is_rejected_cleanly(self):
        out = self._raw(["BREW / HTTP/1.1", "Host: x"])
        self.assertIn(b"501", out)
        self.assertIn(b"Content-Length", out)

    def test_server_survives_malformed_input(self):
        for lines in (["GARBAGE"], ["GET / HTTP/9.9"], ["BREW / HTTP/1.1"],
                      ["GET /" + ("a" * 70000) + " HTTP/1.1"]):
            self._raw(lines, wait=1.0)
        c = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        c.request("GET", "/robots.txt")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        r.read()
        c.close()


if __name__ == "__main__":
    unittest.main()
