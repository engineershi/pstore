# -*- coding: utf-8 -*-
"""Offline tests for the multi-user RBAC flow:
self-registration + email verification, owner-granted role matrix (custom
function permissions, users may hold several roles), password hashing, and
function-level gating of admin pages + APIs with an owner-only users hub."""

import http.client
import importlib
import json
import os
import re
import shutil
import sqlite3
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.sys.path:
    os.sys.path.insert(0, sys_path)

import mailer
import security


class TestPasswordHashing(unittest.TestCase):
    def test_hash_roundtrip(self):
        stored = security.hash_password("s3cret-word")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(security.verify_password("s3cret-word", stored))
        self.assertFalse(security.verify_password("wrong", stored))

    def test_hash_is_salted(self):
        self.assertNotEqual(security.hash_password("same"), security.hash_password("same"))

    def test_verify_rejects_junk(self):
        self.assertFalse(security.verify_password("x", ""))
        self.assertFalse(security.verify_password("x", None))
        self.assertFalse(security.verify_password("x", "garbage"))
        self.assertFalse(security.verify_password("x", "pbkdf2_sha256$9$00$00"))


class TestUsersServer(unittest.TestCase):
    """End-to-end register -> verify -> roles -> gated access."""

    IPKEY = "127.0.0.1|127.0.0.1"

    def setUp(self):
        security.REGISTER_LIMITER.clear("reg|" + self.IPKEY)
        security.RESEND_LIMITER.clear("resend|" + self.IPKEY)
        security.LOGIN_LIMITER.clear("login|" + self.IPKEY)

    @classmethod
    def setUpClass(cls):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.db = "/tmp/pstore_test_users_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(base, "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "owner-pass-123"
        os.environ["PSTORE_HASH_SECRET"] = "test-hash-secret"
        os.environ["PSTORE_OAUTH_SECRET"] = "test-oauth-secret"
        os.environ["PSTORE_URL"] = "https://pstore-test.example"
        os.environ["SMTP_HOST"] = "smtp.test.example"
        os.environ["SMTP_PORT"] = "587"
        os.environ["SMTP_USER"] = "smtp-user"
        os.environ["SMTP_PASSWORD"] = "smtp-pass"
        importlib.reload(security)
        importlib.reload(mailer)
        import server
        importlib.reload(server)
        cls.server = server
        # deterministic roles: create tables, purge any leftovers, seed builtins
        _conn = server._db()
        _conn.close()
        conn = sqlite3.connect(cls.db)
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM roles")
        for slug, label, funcs in server.BUILTIN_ROLES:
            conn.execute(
                "INSERT INTO roles (slug, label, functions, builtin) VALUES (?,?,?,1)",
                (slug, label, json.dumps(list(funcs))))
        conn.commit()
        conn.close()
        cls.sent = []
        mailer._send = lambda subject, body, to, attachments=None, pixel_url="": (
            cls.sent.append((subject, body, to)) or True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.owner_cookie, _ = cls._login("owner@test.example", "owner-pass-123")

    @classmethod
    def tearDownClass(cls):
        mailer._send = None
        security.REGISTER_LIMITER.clear("reg|" + cls.IPKEY)
        security.RESEND_LIMITER.clear("resend|" + cls.IPKEY)
        security.LOGIN_LIMITER.clear("login|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        for k in ("PSTORE_HASH_SECRET", "PSTORE_OAUTH_SECRET", "PSTORE_URL",
                  "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"):
            os.environ.pop(k, None)
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    @classmethod
    def _login(cls, email, pw):
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=5)
        conn.request("POST", "/admin/login",
                     body="email=%s&password=%s" % (email, pw),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        status = resp.status
        conn.close()
        return (cookie.split(";")[0] if cookie else ""), status

    def _raw(self, method, path, body=None, cookie=None, ctype=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        status, loc = resp.status, resp.getheader("Location")
        conn.close()
        return status, loc, data

    def _register(self, email, pw="teampass-123", name="Team Member"):
        return self._raw("POST", "/admin/register",
                         body=json.dumps({"name": name, "email": email,
                                          "password": pw}),
                         ctype="application/json")

    def _last_verify_link(self, email):
        for subject, body, to in reversed(self.sent):
            if to == email and "Confirm your" in subject:
                m = re.search(r"https://pstore-test\.example(/admin/verify\?t=[^\s]+)",
                              body)
                if m:
                    return m.group(1)
        return None

    def test_register_creates_unverified_and_emails(self):
        email = "alice@team.example"
        st, _, body = self._register(email)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertTrue(d["mail"])
        self.assertIn("unverified", self._user_row(email)["status"])
        link = self._last_verify_link(email)
        self.assertTrue(link and link.startswith("/admin/verify?t="))

    def test_login_blocked_until_verified(self):
        email = "bob@team.example"
        self._register(email)
        cookie, status = self._login(email, "teampass-123")
        self.assertEqual(status, 401)
        self.assertEqual(cookie, "")

    def test_verify_link_activates_and_redirects(self):
        email = "carol@team.example"
        self._register(email)
        link = self._last_verify_link(email)
        st, loc, _ = self._raw("GET", link)
        self.assertEqual(st, 302)
        self.assertIn("verified=1", loc)
        self.assertEqual(self._user_row(email)["status"], "verified")
        cookie, status = self._login(email, "teampass-123")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("pstore_admin="))

    def test_team_login_no_roles_lands_on_pending(self):
        email = "carol2@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        cookie, status = self._login(email, "teampass-123")
        st, _, body = self._raw("GET", "/admin/pending", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("No tool access yet", body.decode("utf-8"))
        # un-granted admin pages are rejected
        st, _, _ = self._raw("GET", "/dashboard", cookie=cookie)
        self.assertEqual(st, 403)
        st, _, _ = self._raw("GET", "/api/settings", cookie=cookie)
        self.assertEqual(st, 403)

    def test_owner_assign_role_grants_function_access(self):
        email = "dave@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        # owner gives dave the "emailer" role => only Email Studio
        st, _, body = self._raw("POST", "/api/users",
                                body=json.dumps({"action": "set_roles", "id": uid,
                                                 "roles": ["emailer"]}),
                                cookie=self.owner_cookie, ctype="application/json")
        self.assertEqual(st, 200)
        cookie, status = self._login(email, "teampass-123")
        self.assertEqual(status, 200)
        st, _, body = self._raw("GET", "/admin/emails", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("Email Studio", body.decode("utf-8"))
        st, _, body = self._raw("GET", "/admin/social", cookie=cookie)
        self.assertEqual(st, 403)
        # API gate matches the page gate
        st, _, body = self._raw("POST", "/api/social/publish",
                                body=b"{}", cookie=cookie,
                                ctype="application/json")
        j = json.loads(body)
        self.assertEqual(st, 403)
        self.assertIn(j.get("error", "forbidden"), "forbidden")

    def test_multi_role_union(self):
        email = "erin@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_roles", "id": uid,
                                   "roles": ["social", "seo"]}),
                  cookie=self.owner_cookie, ctype="application/json")
        cookie, _ = self._login(email, "teampass-123")
        st, _, _ = self._raw("GET", "/admin/social", cookie=cookie)
        self.assertEqual(st, 200)
        st, _, _ = self._raw("GET", "/admin/seoengines", cookie=cookie)
        self.assertEqual(st, 200)
        st, _, _ = self._raw("GET", "/admin/emails", cookie=cookie)
        self.assertEqual(st, 403)
        st, _, body = self._raw("GET", "/admin/pending", cookie=cookie)
        html = body.decode("utf-8")
        self.assertIn("Social publisher", html)
        self.assertIn("SEO &amp; consoles", html)

    def test_disabled_user_loses_access_and_session(self):
        email = "finn@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_roles", "id": uid,
                                   "roles": ["full"]}),
                  cookie=self.owner_cookie, ctype="application/json")
        cookie, _ = self._login(email, "teampass-123")
        st, _, _ = self._raw("GET", "/dashboard", cookie=cookie)
        self.assertEqual(st, 200)
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_status", "id": uid,
                                   "status": "disabled"}),
                  cookie=self.owner_cookie, ctype="application/json")
        st, _, body = self._raw("GET", "/api/niches", cookie=cookie)
        self.assertEqual(st, 401)
        _, status = self._login(email, "teampass-123")
        self.assertEqual(status, 401)

    def test_reset_password(self):
        email = "grace@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "reset_password", "id": uid,
                                   "password": "newpass-456"}),
                  cookie=self.owner_cookie, ctype="application/json")
        _, status = self._login(email, "teampass-123")
        self.assertEqual(status, 401)
        cookie, status = self._login(email, "newpass-456")
        self.assertEqual(status, 200)  # verified, no roles yet -> pending
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_roles", "id": uid,
                                   "roles": ["full"]}),
                  cookie=self.owner_cookie, ctype="application/json")
        st, _, _ = self._raw("GET", "/dashboard", cookie=cookie)
        self.assertEqual(st, 200)

    def test_resend_verification(self):
        email = "hana@team.example"
        self._register(email)
        n_before = len(self.sent)
        st, _, body = self._raw("POST", "/admin/resend",
                                body=json.dumps({"email": email}),
                                ctype="application/json")
        j = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertGreater(len(self.sent), n_before)
        # already verified => no resend
        self._raw("GET", self._last_verify_link(email))
        st, _, body = self._raw("POST", "/admin/resend",
                                body=json.dumps({"email": email}),
                                ctype="application/json")
        self.assertEqual(st, 400)

    def test_register_duplicate_and_short_password(self):
        email = "ida@team.example"
        self._register(email)
        st, _, _ = self._register(email)
        self.assertEqual(st, 409)
        st, _, body = self._register("ida2@team.example", pw="short")
        self.assertEqual(st, 400)
        self.assertIn("8 characters", json.loads(body)["error"])

    def test_owner_cannot_register_own_email(self):
        st, _, body = self._register("owner@test.example")
        self.assertEqual(st, 400)

    def test_users_api_is_owner_only(self):
        email = "joe@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        cookie, _ = self._login(email, "teampass-123")
        st, _, body = self._raw("GET", "/api/users", cookie=cookie)
        self.assertEqual(st, 403)
        st, _, body = self._raw("GET", "/admin/users", cookie=cookie)
        self.assertEqual(st, 403)
        st, _, body = self._raw("POST", "/api/users",
                                body=json.dumps({"action": "save_role",
                                                 "slug": "x", "label": "X",
                                                 "functions": []}),
                                cookie=cookie, ctype="application/json")
        self.assertEqual(st, 403)

    def test_users_api_lists_safely(self):
        st, _, body = self._raw("GET", "/api/users", cookie=self.owner_cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertIn("users", d)
        self.assertIn("roles", d)
        self.assertEqual(len(d["functions"]), 8)
        slugs = {r["slug"] for r in d["roles"]}
        self.assertIn("full", slugs)
        self.assertIn("emailer", slugs)
        for u in d["users"]:
            self.assertNotIn("pass_hash", u)

    def test_role_crud_and_delete_unassigns(self):
        email = "kim@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        st, _, _ = self._raw("POST", "/api/users",
                             body=json.dumps({"action": "save_role",
                                              "slug": "writer", "label": "Writer",
                                              "functions": ["content"]}),
                             cookie=self.owner_cookie, ctype="application/json")
        self.assertEqual(st, 200)
        st, _, body = self._raw("GET", "/api/users", cookie=self.owner_cookie)
        self.assertIn("Writer", body.decode("utf-8"))
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_roles", "id": uid,
                                   "roles": ["writer", "full"]}),
                  cookie=self.owner_cookie, ctype="application/json")
        st, _, _ = self._raw("POST", "/api/users",
                             body=json.dumps({"action": "delete_role",
                                              "slug": "writer"}),
                             cookie=self.owner_cookie, ctype="application/json")
        self.assertEqual(st, 200)
        st, _, body = self._raw("GET", "/api/users", cookie=self.owner_cookie)
        self.assertNotIn("Writer", body.decode("utf-8"))
        st, _, body = self._raw("GET", "/api/users", cookie=self.owner_cookie)
        for u in json.loads(body)["users"]:
            if u["id"] == uid:
                self.assertNotIn("writer", u["roles"])

    def test_builtin_role_protected(self):
        st, _, body = self._raw("POST", "/api/users",
                                body=json.dumps({"action": "delete_role",
                                                 "slug": "full"}),
                                cookie=self.owner_cookie, ctype="application/json")
        self.assertEqual(st, 400)

    def test_register_page_and_login_hints(self):
        st, _, body = self._raw("GET", "/admin/register")
        self.assertEqual(st, 200)
        self.assertIn("Request a", body.decode("utf-8"))
        st, _, body = self._raw("GET", "/admin/login")
        html = body.decode("utf-8")
        self.assertIn("resend my confirmation", html)
        self.assertIn("/admin/register", html)

    def test_team_nav_filters_by_function(self):
        email = "leo@team.example"
        self._register(email)
        self._raw("GET", self._last_verify_link(email))
        uid = self._user_row(email)["id"]
        self._raw("POST", "/api/users",
                  body=json.dumps({"action": "set_roles", "id": uid,
                                   "roles": ["emailer"]}),
                  cookie=self.owner_cookie, ctype="application/json")
        cookie, _ = self._login(email, "teampass-123")
        st, _, body = self._raw("GET", "/admin/emails", cookie=cookie)
        html = body.decode("utf-8")
        self.assertIn("Email Studio", html)
        self.assertNotIn(">📣 Social<", html)
        self.assertNotIn("Users &amp; roles", html)

    def test_password_reset_on_register_via_owner_allows_arbitrary_role(self):
        # owner-created users are verified immediately (no email needed)
        email = "mia@team.example"
        st, _, body = self._raw("POST", "/api/users",
                                body=json.dumps({"action": "create_user",
                                                 "name": "Mia", "email": email,
                                                 "password": "ownermade-123",
                                                 "roles": []}),
                                cookie=self.owner_cookie, ctype="application/json")
        self.assertEqual(st, 200)
        self.assertEqual(self._user_row(email)["status"], "verified")
        cookie, status = self._login(email, "ownermade-123")
        self.assertEqual(status, 200)

    def _user_row(self, email):
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute("SELECT id, email, name, status, pass_hash FROM users "
                               "WHERE email=?", (email,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()