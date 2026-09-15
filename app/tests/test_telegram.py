# -*- coding: utf-8 -*-
"""Offline integration + unit tests for the Telegram broadcast feature set
(telegram.py protocol, telegram_admin.py handlers, server wiring, public
opt-in button)."""

import importlib
import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amazon
import indexnow
import mailer
import security
import server
import telegram
import telegram_admin
import time


class Sink:
    """Fake telegram._post: records calls and returns 200/ok by default."""

    def __init__(self):
        self.calls = []
        self.status = 200

    def __call__(self, url, payload, headers=None, timeout=20):
        self.calls.append((url, payload, headers))
        return self.status, {"ok": True, "result": {"message_id": 42,
                                                    "username": "mybot",
                                                    "first_name": "MyBot"}}


class TestTelegramParseUpdate(unittest.TestCase):
    """Unit tests for telegram.parse_update (deep-link source extraction)."""

    def _upd(self, text="", chat_type="private", chat_id=111):
        msg = {"chat": {"id": chat_id, "type": chat_type,
                        "first_name": "Ann", "username": "ann"},
               "from": {"id": 999, "first_name": "Ann", "username": "ann"},
               "text": text}
        return {"message": msg}

    def test_plain_start_defaults_to_site(self):
        d = telegram.parse_update(self._upd("/start"))
        self.assertEqual(d["source"], "site")
        self.assertEqual(d["chat_id"], "111")
        self.assertEqual(d["username"], "ann")

    def test_payload_becomes_source(self):
        d = telegram.parse_update(self._upd("/start keto"))
        self.assertEqual(d["source"], "keto")

    def test_message_in_private_chat_captures(self):
        d = telegram.parse_update(self._upd("hi there"))
        self.assertEqual(d["source"], "message")

    def test_channel_without_start_ignored(self):
        d = telegram.parse_update(self._upd("sales! 50% off", chat_type="channel"))
        self.assertIsNone(d)

    def test_empty_update_ignored(self):
        self.assertIsNone(telegram.parse_update({}))
        self.assertIsNone(telegram.parse_update({"message": {}}))


class TestTelegramServer(unittest.TestCase):

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_tg_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._saved_send = mailer._send
        cls._saved_indexnow_post = indexnow._post
        mailer._send = lambda *a, **k: True
        indexnow._post = lambda *a, **k: None
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        mailer._send = cls._saved_send
        indexnow._post = cls._saved_indexnow_post
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    @classmethod
    def _login(cls):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=60)
        conn.request("POST", "/admin/login",
                     body=b"email=owner@test.example&password=test-pass-123",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        sc = resp.getheader("Set-Cookie")
        resp.read()
        conn.close()
        assert sc and sc.startswith("pstore_admin="), sc
        return sc.split(";")[0]

    @classmethod
    def _raw(cls, path, method="GET", body=None, cookie=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=60)
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, resp.getheader("Content-Type"), resp.read())
        conn.close()
        return out

    def setUp(self):
        security.API_LIMITER.clear("api|" + self.IPKEY)
        self._sink = Sink()
        self._prev_post = telegram._post
        telegram._post = self._sink
        self._prev_ai_urlopen = None
        conn = server._db()
        conn.execute("DELETE FROM telegram_subs")
        conn.commit()
        conn.close()

    def tearDown(self):
        for k in ("telegram.token", "telegram.secret", "telegram.botname",
                  "telegram.on_page"):
            try:
                server._set_setting(k, "")
            except Exception:
                pass
        telegram._post = self._prev_post

    def _set_cfg(self, **kw):
        blob = dict(token=kw.get("token", "123:TOK"), secret=kw.get("secret", ""),
                    botname=kw.get("botname", "mybot"),
                    on_page=kw.get("on_page", False))
        st, ct, body = self._raw("/api/telegram/config", method="POST",
                                 body=json.dumps(blob).encode(), cookie=self.cookie)
        return st, json.loads(body or b"{}")

    # ------------------------------------------------------------- admin page
    def test_admin_telegram_page_requires_login(self):
        st, ct, body = self._raw("/admin/telegram")
        self.assertEqual(st, 302)

    def test_admin_telegram_page_renders(self):
        st, ct, body = self._raw("/admin/telegram", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Telegram broadcast", body)
        self.assertIn(b"/api/telegram/hook", body)
        self.assertIn(b"tgSave", body)
        # mobile: viewport meta + horizontally scrollable subscriber table
        self.assertIn(b'name="viewport"', body)
        self.assertIn(b'class="table-wrap"', body)

    def test_admin_nav_surfaces_telegram(self):
        st, ct, body = self._raw("/admin", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"/admin/telegram", body)

    # --------------------------------------------------------------- state API
    def test_telegram_state_api_returns_config(self):
        self._set_cfg(token="123:TOK", botname="pstorebot", on_page=True)
        st, ct, body = self._raw("/api/telegram/state", method="POST",
                                 body=b"{}", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["token"], "123:TOK")
        self.assertEqual(d["botname"], "pstorebot")
        self.assertTrue(d["on_page"])

    def test_telegram_state_getme_reports_error_without_token(self):
        st, ct, body = self._raw("/api/telegram/state", method="POST",
                                 body=b'{"action":"me"}', cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertFalse(d["ok"])
        self.assertIn("error", d)

    def test_telegram_state_getme_ok_with_token(self):
        self._set_cfg(token="123:TOK")
        st, ct, body = self._raw("/api/telegram/state", method="POST",
                                 body=b'{"action":"me"}', cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertIn("Bot @mybot", d["result"])

    # -------------------------------------------------------------- config API
    def test_telegram_config_save_persists(self):
        st, d = self._set_cfg(token="123:TOK", secret="shh", botname="pstorebot",
                              on_page=True)
        self.assertTrue(d["ok"])
        self.assertEqual(server._get_setting("telegram.token"), "123:TOK")
        self.assertEqual(server._get_setting("telegram.secret"), "shh")
        self.assertEqual(server._get_setting("telegram.botname"), "pstorebot")
        self.assertEqual(server._get_setting("telegram.on_page"), "1")

    def test_telegram_config_save_rejects_bad_blob(self):
        with self.assertRaises(ValueError):
            telegram_admin._config_save(None, "{not json")

    # ------------------------------------------------------------ broadcast API
    def test_broadcast_requires_subscribers(self):
        st, ct, body = self._raw("/api/telegram/broadcast", method="POST",
                                 body=b'{"text":"hey"}', cookie=self.cookie)
        self.assertEqual(st, 400)
        d = json.loads(body)
        self.assertIn("error", d)

    def test_broadcast_requires_text(self):
        self._seed_sub("111", "Ann")
        st, ct, body = self._raw("/api/telegram/broadcast", method="POST",
                                 body=b'{"text":"  "}', cookie=self.cookie)
        self.assertEqual(st, 400)

    def test_broadcast_fires_per_subscriber(self):
        self._set_cfg(token="123:TOK")
        self._seed_sub("111", "Ann")
        self._seed_sub("222", "Bobby")
        st, ct, body = self._raw("/api/telegram/broadcast", method="POST",
                                 body=b'{"text":"price drop!"}',
                                 cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        deadline = time.time() + 3
        while time.time() < deadline:
            send_calls = [c for c in self._sink.calls
                          if c[0].endswith("/sendMessage")]
            if len(send_calls) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(len(send_calls), 2)
        chats = sorted(c[1]["chat_id"] for c in send_calls)
        self.assertEqual(chats, ["111", "222"])

    # ---------------------------------------------------------------- webhook
    def _hook(self, update, secret=None):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        return self._raw("/api/telegram/hook", method="POST",
                         body=json.dumps(update).encode(), headers=headers)

    def _seed_sub(self, cid, name="Ann"):
        conn = server._db()
        conn.execute(
            "INSERT INTO telegram_subs (chat_id, first_name, username, source) "
            "VALUES (?,?,?,?)", (str(cid), name, "ann", "site"))
        conn.commit()
        conn.close()

    def _sub_rows(self):
        conn = server._db()
        rows = conn.execute(
            "SELECT chat_id, first_name, username, source FROM telegram_subs"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def test_hook_public_without_auth(self):
        upd = {"message": {"chat": {"id": 111, "type": "private",
                                    "first_name": "Ann"},
                           "from": {"first_name": "Ann"},
                           "text": "/start"}}
        st, ct, body = self._hook(upd)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        rows = self._sub_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_id"], "111")
        self.assertEqual(rows[0]["source"], "site")
        # welcome reply attempted
        self.assertTrue(any(c[0].endswith("/sendMessage") for c in self._sink.calls))

    def test_hook_records_start_payload_as_source(self):
        upd = {"message": {"chat": {"id": 222, "type": "private",
                                    "first_name": "Ben"},
                           "text": "/start keto"}}
        st, ct, body = self._hook(upd)
        self.assertEqual(st, 200)
        rows = [r for r in self._sub_rows() if r["chat_id"] == "222"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "keto")

    def test_hook_ignores_channel_posts(self):
        upd = {"message": {"chat": {"id": 333, "type": "channel",
                                    "first_name": ""},
                           "text": "some update"}}
        st, ct, body = self._hook(upd)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ignored"])
        self.assertEqual(self._sub_rows(), [])

    def test_hook_stop_removes_subscriber(self):
        self._seed_sub("111", "Ann")
        upd = {"message": {"chat": {"id": 111, "type": "private",
                                    "first_name": "Ann"},
                           "text": "/stop"}}
        st, ct, body = self._hook(upd)
        self.assertEqual(st, 200)
        self.assertEqual(self._sub_rows(), [])
        self.assertTrue(any(c[0].endswith("/sendMessage") for c in self._sink.calls))

    def test_hook_secret_enforced(self):
        self._set_cfg(secret="shh-secret")
        upd = {"message": {"chat": {"id": 111, "type": "private",
                                    "first_name": "Ann"},
                           "text": "/start"}}
        st, ct, body = self._hook(upd)
        self.assertEqual(st, 403)
        self.assertEqual(self._sub_rows(), [])
        st, ct, body = self._hook(upd, secret="shh-secret")
        self.assertEqual(st, 200)
        self.assertEqual(len(self._sub_rows()), 1)

    # ------------------------------------------------- public opt-in button
    def test_join_button_not_rendered_when_off(self):
        st, ct, body = self._raw("/")
        self.assertEqual(st, 200)
        self.assertNotIn(b"data-pstore-tg-join", body)

    def test_join_button_rendered_on_public_page_when_on(self):
        server._set_setting("telegram.on_page", "1")
        server._set_setting("telegram.botname", "pstorebot")
        st, ct, body = self._raw("/")
        self.assertEqual(st, 200)
        self.assertIn(b"data-pstore-tg-join", body)
        self.assertIn(b"https://t.me/pstorebot?start=site", body)

    def test_join_button_absent_on_admin(self):
        server._set_setting("telegram.on_page", "1")
        server._set_setting("telegram.botname", "pstorebot")
        st, ct, body = self._raw("/admin", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertNotIn(b"data-pstore-tg-join", body)


if __name__ == "__main__":
    unittest.main()