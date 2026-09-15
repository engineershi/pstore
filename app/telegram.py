# -*- coding: utf-8 -*-
"""Telegram opt-in capture + broadcast backend (stdlib-only, zero budget).

The money path: every public page carries a "Join on Telegram" button pointing
at ``https://t.me/<bot>?start=site``; the reader hits Start and Telegram POSTs
the update to our webhook, which captures their chat id as an organic Telegram
subscriber. The admin broadcast then pushes tracked-link deal posts to every
captured chat — the Telegram analog of Email Studio's batches.

Scope discipline: pure protocol + tiny settings store (via the server's
settings-table hooks, like webmasters.py). No DB here — the server owns the
``telegram_subs`` table and calls back into these helpers.

Credentials (settings table, settable on /admin/telegram):
  telegram.token     bot token (env PSTORE_TELEGRAM_TOKEN wins)
  telegram.secret    webhook secret_token (validated on every inbound hook)
  telegram.botname   bot @username (fetched from getMe)
  telegram.on_page   "1" enables the join button on the public pages
"""

import json
import os
import urllib.request

API = "https://api.telegram.org/bot"

_STORE_GET = None   # callable(key, default="") -> str
_STORE_SET = None   # callable(key, value) -> None
_post = None        # seam: (url, payload, headers) -> (status, parsed_json)


def _set_transport(fn):
    global _post
    _post = fn


def _default_post(url, payload, headers=None, timeout=20):
    """POST JSON to ``url``. Test seam (like publish._post). Returns
    (status_or_0, parsed_json). Never raises."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers or {},
                     **{"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "null")
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw.strip() else {})
        except ValueError:
            return e.code, raw
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)[:160]}
    except OSError as exc:
        return 0, {"error": str(exc)[:160]}


_post = _default_post


def store_get(key, default=""):
    return _STORE_GET(key, default) if _STORE_GET else default


def store_set(key, value):
    if _STORE_SET:
        _STORE_SET(key, value)


def _json_get(key, default=None):
    raw = store_get(key, "")
    try:
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default


def _json_set(key, value):
    store_set(key, json.dumps(value))


# ------------------------------------------------------------------ config
def token():
    """Active bot token: env wins, else the /admin/telegram setting."""
    return (os.environ.get("PSTORE_TELEGRAM_TOKEN") or
            store_get("telegram.token", "") or "").strip()


def secret():
    """Webhook secret_token used to sign every inbound update."""
    return store_get("telegram.secret", "").strip()


def botname():
    """Bot @username (without the @) shown on the opt-in button."""
    return (store_get("telegram.botname", "") or "").strip().lstrip("@").lower()


def on_page():
    """Whether public pages render the "Join on Telegram" button."""
    return store_get("telegram.on_page", "0") == "1"


def _call(method, payload, tok=None):
    """One Bot API call; never raises. Returns (status, json)."""
    tok = tok or token()
    if not tok:
        return 0, {"error": "no bot token set"}
    return _post(API + tok + "/" + method, payload,
                 {"Content-Type": "application/json"})


def get_me(tok=None):
    """Bot identity (getMe) — also validates the token. Returns
    (ok, {"username", "name"} or {"error"})."""
    st, data = _call("getMe", {}, tok)
    if st != 200 or not isinstance(data, dict) or not data.get("ok"):
        return False, {"error": str(data)[:160] if not isinstance(data, dict)
                       else (data.get("description") or str(data))[:160]}
    info = data.get("result") or {}
    return True, {"username": (info.get("username") or "").lstrip("@").lower(),
                  "name": info.get("first_name") or info.get("username") or "bot"}


def set_webhook(tok, url, secret_token, tok_param=None):
    """Register this server's hook as the bot's update delivery point, signed
    with secret_token (the inbound handler validates the header). Returns
    (ok, description)."""
    st, data = _call("setWebhook", {
        "url": url, "secret_token": secret_token,
        "allowed_updates": ["message"], "drop_pending_updates": True,
    }, tok)
    if st != 200 or not isinstance(data, dict) or not data.get("ok"):
        err = (data or {}).get("description") if isinstance(data, dict) else str(data)
        return False, str(err)[:160]
    return True, "webhook set"


def send_message(tok, chat_id, text, image=None):
    """Deliver a broadcast/welcome message to one chat. Falls back from the
    photo form (share-card image) to a plain message when no image. Returns
    {"ok": bool, "msg": message_id or err}."""
    text = (text or "").strip()
    if not str(chat_id).strip():
        return {"ok": False, "msg": "no chat_id"}
    payload = {"chat_id": str(chat_id), "text": text[:4000]}
    method = "sendMessage"
    if image:
        method = "sendPhoto"
        payload = {"chat_id": str(chat_id), "photo": image,
                   "caption": text[:1024]}
    st, data = _post(API + tok + "/" + method, payload,
                     {"Content-Type": "application/json"})
    if st != 200 or not isinstance(data, dict) or not data.get("ok"):
        err = (data or {}).get("description") if isinstance(data, dict) else str(data)
        return {"ok": False, "msg": str(err)[:160] or "telegram error"}
    try:
        msg_id = str((data.get("result") or {}).get("message_id") or "")
    except Exception:
        msg_id = ""
    return {"ok": True, "msg": msg_id or "sent"}


# ------------------------------------------------------------------ updates
def parse_update(update):
    """Extract a private-chat capture from a webhook update. Returns a dict
    with chat_id/first_name/username/source, or None for anything we ignore
    (channels, groups without from, callback queries, ...). The /start
    payload (any ``t.me/<bot>?start=...`` button) becomes the on-site source;
    bare starts default to "site", other messages to "message"."""
    if not isinstance(update, dict):
        return None
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    if not chat.get("id"):
        return None
    chat_type = chat.get("type") or "private"
    if chat_type != "private" and not (msg.get("text") or "").startswith("/start"):
        return None
    text = str(msg.get("text") or "").strip()
    source = "message"
    if text.startswith("/start"):
        payload = text[len("/start"):].strip().split()[0].strip() if \
            len(text) > len("/start") else ""
        source = (payload[:32] or "site")
    return {"chat_id": str(chat["id"]), "source": source,
            "first_name": str(chat.get("first_name") or "")[:60],
            "username": str(chat.get("username") or "")[:60]}