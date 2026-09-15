"""telegram_admin.py — Telegram subscriber capture + admin broadcast page.

Attached to the Handle class by server.py via module-level seams:

    Handle._admin_telegram = telegram_admin.admin_telegram
    Handle._telegram_hook = telegram_admin.telegram_hook
    Handle._telegram_state_api = telegram_admin.telegram_state_api
    Handle._telegram_config_api = telegram_admin.telegram_config_api
    Handle._telegram_broadcast_api = telegram_admin.telegram_broadcast_api

Handlers call self.* (dispatch surface identical to server's own handlers)
and telegram module wrappers once their seams are wired in server.py.
"""

import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import telegram as _tg

_STORE_GET = None
_STORE_SET = None
_SET = None


def _store_get(key, default=None):
    return _STORE_GET(key, default)


def _store_set(key, val):
    return _STORE_SET(key, val)


def _config_blob(self):
    cfg = {
        "token": _tg.token(),
        "secret": _tg.secret(),
        "botname": _tg.botname(),
        "on_page": _tg.on_page(),
    }
    return cfg


def _config_save(self, blob):
    try:
        j = json.loads(blob) if isinstance(blob, str) else blob
    except Exception:
        raise ValueError("invalid Telegram config JSON")
    pairs = [
        ("telegram.token", str(j.get("token", "")).strip()),
        ("telegram.secret", str(j.get("secret", "")).strip()),
        ("telegram.botname", str(j.get("botname", "")).strip()),
        ("telegram.on_page", "1" if j.get("on_page") else "0"),
    ]
    for k, v in pairs:
        _store_set(k, v)
    return pairs


def _tg_upsert_sub(self, chat_id, first_name="", username="", source="site"):
    db = _get_db()
    try:
        with db:
            db.execute(
                """INSERT INTO telegram_subs (chat_id, first_name, username, source)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username,
                       last_seen = datetime('now')""",
                (str(chat_id), str(first_name), str(username), str(source)),
            )
    finally:
        _close_db(db)


def _tg_subs(self):
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT chat_id, first_name, username, source, created_at, last_seen "
            "FROM telegram_subs ORDER BY last_seen DESC"
        ).fetchall()
        subs = [dict(r) for r in rows]
        return subs if subs else []
    finally:
        _close_db(db)


def admin_telegram(self, q):
    """GET /admin/telegram — admin broadcast + subscriber management page."""
    authed = self._authed()
    if not authed:
        return self._render(q, "Telegram broadcast", self._admin_login_html(q))
    subs = _tg_subs(self)
    cfg = _config_blob(self)
    rows_html = "\n".join(
        """<tr><td>{first}</td><td>{username}</td><td>{chat}</td><td>{source}</td>
        <td>{last}</td></tr>""".format_map(r)
        for r in subs
    )
    html = """<!doctype html><html><head><meta charset="utf-8">
<title>Telegram broadcast — pstore</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#fff;color:#111}
main{max-width:1000px;margin:2em auto;padding:0 1em}
h1{font-size:1.4em}
.chip{display:inline-block;background:#eef3fb;color:#1d4f91;border-radius:4px;
padding:3px 10px;margin:2px;font-size:.85em}
form{background:#f7f9fc;border:1px solid #dfe6f0;border-radius:8px;padding:1.2em;margin:1.2em 0}
label{display:block;font-weight:600;margin:.6em 0 .2em}
input[type=text],input[type=password],textarea{width:100%;box-sizing:border-box;
padding:.55em;border:1px solid #ccd6e4;border-radius:6px}
button{background:#1d4f91;color:#fff;border:0;border-radius:6px;padding:.6em 1.4em;
cursor:pointer;margin:.3em .3em 0 0;font-size:.95em}
.small{font-size:.85em;color:#555}.ok{color:#0a6}.
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #e3e9f1;padding:.5em;text-align:left;font-size:.9em}
</style></head><body>
<main>
<div class="chip" onclick="location.href='/admin'">Back</div>
<div class="chip" onclick="location.href='/admin/telegram'">Telegram</div>
<h1>📣 Telegram broadcast</h1>
<p class="small">Webhook: <b>__HOOK_URL__</b></p>

<form id="cfg">
<label>Bot token</label><input type="password" id="token" placeholder="123:ABC…">
<label>Secret (webhook verification)</label><input type="text" id="secret">
<label>Bot username</label><input type="text" id="botname" placeholder="@MyPstoreBot">
<label><input type="checkbox" id="on_page"> Show join button on every page</label><br>
<button type="button" onclick="tgSave()">Save</button>
<button type="button" onclick="tgMe()">Test connection</button>
<span id="res"></span>
</form>

<form id="bc">
<h3>Broadcast to all subscribers</h3>
<label>Text</label><textarea id="msg" rows="3"></textarea>
<label>Image URL (optional)</label><input type="text" id="img">
<button type="button" onclick="tgBroadcast()">Send broadcast</button>
<span id="bres"></span>
</form>

<h3>Subscribers (<span id="cnt">__CNT__</span>)</h3>
<table><thead><tr><th>Name</th><th>Username</th><th>Chat</th><th>Source</th>
<th>Last seen</th></tr></thead><tbody id="rows">__ROWS__</tbody></table>
</main>
<script>
const T = () => document.getElementById("token").value;
const S = () => document.getElementById("secret").value;
const B = () => document.getElementById("botname").value;
async function api(p, opts) {
  const r = await fetch(p, {method: "POST", ...(
    {headers: {"Content-Type": "application/json"}, body: JSON.stringify(opts)})});
  return r.json();
}
async function tgSave(){const r=await api("/api/telegram/config",{token:T(),secret:S(),
  botname:B(),on_page:document.getElementById("on_page").checked});
  document.getElementById("res").textContent = r.ok ? "Saved ✓" : "Error: "+r.error;}
async function tgMe(){const r=await api("/api/telegram/state",{action:"me"});
  document.getElementById("res").textContent = r.ok ? "✅ "+r.result : "Error: "+r.error;}
async function tgBroadcast(){const r=await api("/api/telegram/broadcast",
  {text:document.getElementById("msg").value,image:document.getElementById("img").value});
  document.getElementById("bres").textContent = r.ok ? "Broadcast started" : "Error: "+r.error;}
function fill(){fetch("/api/telegram/state").then(r=>r.json()).then(j=>{
  if(j.ok){document.getElementById("token").value=j.token||"";document.getElementById("secret").value=j.secret||"";
    document.getElementById("botname").value=j.botname||"";
    document.getElementById("on_page").checked=!!j.on_page;}});}
fill();
</script></body></html>""".replace("__HOOK_URL__",
        _tg.site_url().rstrip("/") + "/api/telegram/hook"
    ).replace("__ROWS__", rows_html).replace("__CNT__", str(len(subs)))
    return self._send(q, html)


def telegram_state_api(self):
    """POST /api/telegram/state — settings editor fill + getMe."""
    q = self._body_json()
    cfg = _config_blob(self)
    if q and q.get("action") == "me":
        try:
            info = _tg.get_me(_tg.token())
            return self._json(q, {"ok": True, "result": info.get("result", info)})
        except Exception as ex:
            return self._json(q, {"ok": False, "error": str(ex)}, status=400)
    return self._json(q, {"ok": True,
        "token": cfg["token"], "secret": cfg["secret"],
        "botname": cfg["botname"], "on_page": cfg["on_page"]})


def telegram_config_api(self):
    q = self._body_json()
    try:
        _config_save(self, q)
        return self._json(q, {"ok": True})
    except Exception as ex:
        return self._json(q, {"ok": False, "error": str(ex)}, status=400)


def telegram_broadcast_api(self):
    q = self._body_json()
    text = (q or {}).get("text", "")
    image = (q or {}).get("image", "")
    subs = _tg_subs(self)
    if not text.strip():
        return self._json(q, {"ok": False, "error": "empty message"}, status=400)
    if not subs:
        return self._json(q, {"ok": False, "error": "no subscribers yet"}, status=400)

    def worker():
        tok = _tg.token()
        try:
            _tg._set_transport_default()
        except Exception:
            pass
        for sub in subs:
            try:
                _tg.send_message(tok, sub["chat_id"], text.strip(), image)
                time.sleep(0.3)
            except Exception:
                time.sleep(0.1)
                continue

    threading.Thread(target=worker, daemon=True).start()
    return self._json(q, {"ok": True, "sent": len(subs)})


def telegram_hook(self):
    """PUBLIC POST /api/telegram/hook (no auth) — Telegram servers call this."""
    try:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        update = json.loads(raw.decode("utf-8", "replace"))
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id", "")).strip()
        if not cid:
            return self._json(update, {"ok": False, "error": "no chat"}, status=400)
        first = (msg.get("from") or {}).get("first_name", "") or chat.get("first_name", "")
        username = (msg.get("from") or {}).get("username", "") or chat.get("username", "")
        _tg_upsert_sub(self, cid, first, username, "site")
        text = msg.get("text") or ""
        args = text.split()
        cmd = args[0].split("@")[0] if args else ""
        reply = ""
        if cmd in ("/start", "/join", "/subscribe", "💌"):
            reply = "👋 Welcome! You're subscribed to pstore price drops."
        elif cmd == "/stop":
            db = _get_db()
            try:
                with db:
                    db.execute("DELETE FROM telegram_subs WHERE chat_id = ?", (cid,))
            finally:
                _close_db(db)
            reply = "You're unsubscribed. Sad to see you go!"
        if reply:
            try:
                _tg.send_message(_tg.token(), cid, reply)
            except Exception:
                pass
        return self._json(update, {"ok": True})
    except Exception as ex:
        return self._json({}, {"ok": False, "error": str(ex)}, status=400)


def _get_db():
    try:
        from server import _db_connect
        return _db_connect()
    except Exception:
        from server import db_path
        return sqlite3.connect(db_path())


def _close_db(db):
    try:
        db.close()
    except Exception:
        pass
