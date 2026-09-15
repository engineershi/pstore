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


_TOTOP = ('<div class="totop"><a href="#top" aria-label="Back to top">&uarr;</a></div>'
          '<script src="/ui.js" defer></script>')


def admin_telegram(self, q):
    """GET /admin/telegram — admin broadcast + subscriber management page.
    Rendered with the same shared design system as every other admin page
    (/style.css + admin nav + card chrome), subscribers table scrolling
    horizontally on narrow screens."""
    if not self._authed():
        return self._redirect_login("/admin/telegram")
    subs = _tg_subs(self)
    cfg = _config_blob(self)
    rows_html = "\n".join(
        """<tr><td>{first}</td><td>{username}</td><td>{chat}</td><td>{source}</td>
        <td>{last}</td></tr>""".format_map(r)
        for r in subs
    )
    nav = self._admin_nav("telegram") if hasattr(self, "_admin_nav") else ""
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Telegram broadcast — pstore</title>
<link rel="stylesheet" href="/style.css">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Telegram <span>broadcast.</span></h1>
<p class="tagline">Subscribers opt in from a join button on every page, and you send price-drop pushes from here — every tap tracked back to clicks.</p></div>
__NAV__
</header>
<main>
<section class="card"><h2>🤖 Bot config</h2>
<p class="hint" style="overflow-wrap:anywhere">Webhook: <code>__HOOK_URL__</code> — Telegram servers call this whenever someone taps your bot.</p>
<form class="cols-form" id="cfg">
  <label>Bot token <input type="password" id="token" placeholder="123:ABC…" autocomplete="off"></label>
  <label>Secret (webhook verification) <input type="text" id="secret" placeholder="pick a secret, set it in BotFather too"></label>
  <label>Bot username <input type="text" id="botname" placeholder="@MyPstoreBot"></label>
  <label style="flex-direction:row;align-items:center;gap:10px;min-height:46px">
    <input type="checkbox" id="on_page" style="width:auto;height:auto;flex:none"> Show join button on every page</label>
  <div class="row">
    <button type="button" class="btn" onclick="tgSave()">Save config</button>
    <button type="button" class="btn" onclick="tgMe()">Test connection</button>
    <span id="res" class="msg"></span>
  </div>
</form>
</section>
<section class="card"><h2>📨 Broadcast to all subscribers</h2>
<form class="cols-form" id="bc">
  <label>Text <textarea id="msg" rows="3" placeholder="Drop today's price moves…"></textarea></label>
  <label>Image URL (optional) <input type="text" id="img" placeholder="https://…/banner.png"></label>
  <div class="row"><button type="button" class="btn" onclick="tgBroadcast()">Send broadcast</button>
  <span id="bres" class="msg"></span></div>
</form>
</section>
<section class="card"><h2>👥 Subscribers <span class="hint">(</span><span id="cnt" class="hint">__CNT__</span><span class="hint">)</span></h2>
<div class="table-wrap"><table class="plain"><thead><tr><th>Name</th><th>Username</th><th>Chat</th><th>Source</th>
<th>Last seen</th></tr></thead><tbody id="rows">__ROWS__</tbody></table></div>
<p class="hint">On a phone the subscriber table rolls sideways inside its card — swipe to see all columns.</p>
</section>
</main>
__TOTOP__
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
async function fill(){const j=await api("/api/telegram/state",{});
  if(j.ok){document.getElementById("token").value=j.token||"";document.getElementById("secret").value=j.secret||"";
    document.getElementById("botname").value=j.botname||"";
    document.getElementById("on_page").checked=!!j.on_page;}}
fill();
</script></body></html>""".replace("__HOOK_URL__",
        (self._site_base() or "https://YOUR-DOMAIN").rstrip("/") + "/api/telegram/hook"
    ).replace("__ROWS__", rows_html).replace("__CNT__", str(len(subs))
    ).replace("__NAV__", nav).replace("__TOTOP__", _TOTOP)
    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")


def telegram_state_api(self):
    """POST /api/telegram/state — settings editor fill + getMe."""
    q = self._body()
    cfg = _config_blob(self)
    if q and q.get("action") == "me":
        ok, info = _tg.get_me(_tg.token())
        if ok:
            handle = info.get("username") or info.get("name") or "?"
            return self._send(200, {"ok": True, "result": "Bot @" + handle})
        return self._send(200, {"ok": False, "error": info.get("error") or "not connected"})
    return self._send(200, {"ok": True,
        "token": cfg["token"], "secret": cfg["secret"],
        "botname": cfg["botname"], "on_page": cfg["on_page"]})


def telegram_config_api(self):
    q = self._body()
    try:
        _config_save(self, q)
        return self._send(200, {"ok": True})
    except Exception as ex:
        return self._send(400, {"ok": False, "error": str(ex)})


def telegram_broadcast_api(self):
    q = self._body()
    text = (q or {}).get("text", "")
    image = (q or {}).get("image", "")
    subs = _tg_subs(self)
    if not text.strip():
        return self._send(400, {"ok": False, "error": "empty message"})
    if not subs:
        return self._send(400, {"ok": False, "error": "no subscribers yet"})

    def worker():
        tok = _tg.token()
        for sub in subs:
            try:
                _tg.send_message(tok, sub["chat_id"], text.strip(), image)
                time.sleep(0.3)
            except Exception:
                time.sleep(0.1)
                continue

    threading.Thread(target=worker, daemon=True).start()
    return self._send(200, {"ok": True, "sent": len(subs)})


def telegram_hook(self):
    """PUBLIC POST /api/telegram/hook (no auth) — Telegram servers call this.
    When telegram.secret is configured the X-Telegram-Bot-Api-Secret-Token
    header must match, so nobody else can inject fake subscribers."""
    try:
        want = _tg.secret()
        if want:
            got = (self.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
            if got != want:
                return self._send(403, {"ok": False, "error": "bad secret"})
        update = self._body()
        parsed = _tg.parse_update(update)
        if not parsed:
            return self._send(200, {"ok": True, "ignored": True})
        cid = parsed["chat_id"]
        text = str(((update.get("message") or {}).get("text")) or "").strip()
        args = text.split()
        cmd = args[0].split("@")[0] if args else ""
        if cmd == "/stop":
            db = _get_db()
            try:
                with db:
                    db.execute("DELETE FROM telegram_subs WHERE chat_id = ?", (cid,))
            finally:
                _close_db(db)
            reply = "You're unsubscribed. Sad to see you go!"
        else:
            _tg_upsert_sub(self, cid, parsed["first_name"], parsed["username"],
                           parsed["source"])
            reply = ("👋 Welcome! You're subscribed to pstore price drops."
                     if cmd in ("/start", "/join", "/subscribe", "💌") else "")
        if reply:
            try:
                _tg.send_message(_tg.token(), cid, reply)
            except Exception:
                pass
        return self._send(200, {"ok": True})
    except Exception as ex:
        return self._send(400, {"ok": False, "error": str(ex)[:200]})


def _get_db():
    from server import _db
    return _db()


def _close_db(db):
    try:
        db.close()
    except Exception:
        pass
