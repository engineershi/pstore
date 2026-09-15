#!/usr/bin/env python3
"""Atomic telegram wiring splice for pstore app/server.py.

server.py (current on-disk state) = git HEAD + telegram_subs schema table.
This applies ALL remaining wiring via module-level seams + dispatch anchors,
then promotes the result ONLY if it py_compiles.

Architecture (module-attach, avoids class-body handler splice entirely):
  - import telegram, import telegram_admin
  - seams: telegram._STORE_GET/_STORE_SET = ... ; telegram._set_transport used
    inside telegram_admin? no — telegram.py owns transport seam. We wire
    telegram._STORE_GET/_STORE_SET here (module seams — same pattern webmasters).
    Transport is wired in tmp as telegram._set_transport export? Actually the
    DRAFT module seams: webmasters._STORE_GET... The telegram module exposes
    _STORE_GET/_STORE_SET/_post seams. In tmp we did:
        telegram._STORE_GET = _get_setting
        telegram._STORE_SET = _set_setting
        telegram._set_transport(publish._post)   <- in tmp transport splice
    ERROR TRAP: if an intermediate server.py already contains ANY of these
    strings, abort with a clear message (we never splice twice).
  - handlers: `Handle._admin_telegram = telegram_admin.admin_telegram` etc.

Dispatch:
  - GET /admin/telegram -> self._admin_telegram(q)   (in GET zone, after /admin/social)
  - PUBLIC POST /api/telegram/hook -> self._telegram_webhook() (BEFORE /api/ auth gate)
  - authed POST /api/telegram/state|config|broadcast -> API handlers (RBAC zone)
"""

import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "server.py")

src = open(SRC).read()
orig_len = len(src)


def C(k, s=None):
    return (s or src).count(k)


def splice(anchor, add, after=True, label=""):
    """assert 1-ocracy; insert `add` after (or before) the single anchor."""
    global src
    n = C(anchor)
    if n == 0:
        raise AssertionError("[%s] anchor NOT FOUND: %r" % (label, anchor))
    if n > 1:
        raise AssertionError("[%s] anchor count %d (wanted 1): %r" % (label, n, anchor))
    i = src.index(anchor) + (len(anchor) if after else 0)
    src = src[:i] + add + src[i:]


# ---------------- always-clean gate: refuse to re-splice ----------------
already = [
    "import telegram\n",
    "telegram._STORE_GET = _get_setting\n",
    "telegram._STORE_SET = _set_setting\n",
    'def _admin_telegram(self, q):\n',
    'if path == "/admin/telegram":\n',
    'if parsed.path == "/api/telegram/hook":\n',
]
for k in already:
    if C(k) > 0:
        raise AssertionError("refusing to re-splice; %r already present (%d)" % (k, C(k)))

# ================ 1. imports ================
splice("import publish\n", "import telegram\nimport telegram_admin\n", label="imports")

# ================ 2. store seams (module-level, after webmasters) ================
seam_s = "webmasters._STORE_GET = _get_setting\nwebmasters._STORE_SET = _set_setting\n"
if C(seam_s) != 1:
    # fallback: maybe only one of the two exists adjacent; assert we found the block adjacent.
    raise AssertionError("webmasters seam block not unique: %d" % C(seam_s))
splice(seam_s,
       "telegram._STORE_GET = _get_setting\ntelegram._STORE_SET = _set_setting\n",
       after=True, label="store-seams")

# ================ 3. RBAC: FUNCTIONS chip (after social) ================
splice('    ("social", "Social publisher"),\n',
       '    ("telegram", "Telegram broadcast"),\n', label="rbac-functions")

# ================ 4. RBAC: FUNCTION_PATHS (after social) ================
splice('    "social": ("/admin/social", "/api/social"),\n',
       '    "telegram": ("/admin/telegram", "/api/telegram/hook", "/api/telegram/state", "/api/telegram/config", "/api/telegram/broadcast"),\n',
       label="rbac-fnpaths")

# ================ 5. NAV_FN chip (after social) ================
splice('    "social": "social", "variants": "marketing", "segments": "marketing",\n',
       '    "telegram": "telegram", "variants": "marketing", "segments": "marketing",\n',
       label="nav-fn")

# ================ 6. GET /admin/telegram dispatch (after /admin/social) ================
splice('            if path == "/admin/social":\n'
       '                return self._admin_social(q)\n',
       '            if path == "/admin/telegram":\n'
       '                return self._admin_telegram(q)\n', label="get-admin-dispatch")

# ================ 7. PUBLIC POST /api/telegram/hook (before /api/ auth gate) ================
splice('            if parsed.path == "/api/social/webhook":\n'
       '                return self._social_webhook()\n',
       '            if parsed.path == "/api/telegram/hook":\n'
       '                return self._telegram_webhook()\n', label="public-hook-dispatch")

# ================ 8. authed POST API dispatches (RBAC POST zone) ================
# anchor: social broadcast API line in the authed POST dispatch zone
splice('            if parsed.path == "/api/social/topics":\n'
       '                return self._social_topics()\n',
       '            if parsed.path == "/api/telegram/state":\n'
       '                return self._telegram_state_api()\n'
       '            if parsed.path == "/api/telegram/config":\n'
       '                return self._telegram_config_api()\n'
       '            if parsed.path == "/api/telegram/broadcast":\n'
       '                return self._telegram_broadcast_api()\n',
       label="api-post-dispatch")

# ================ 9. handler attach (after class, before def main) ================
attach_lines = (
    "Handler._admin_telegram = telegram_admin.admin_telegram\n"
    "Handler._telegram_webhook = telegram_admin.telegram_webhook\n"
    "Handler._telegram_state_api = telegram_admin.telegram_state_api\n"
    "Handler._telegram_config_api = telegram_admin.telegram_config_api\n"
    "Handler._telegram_broadcast_api = telegram_admin.telegram_broadcast_api\n"
    "Handler._tg_upsert_sub = telegram_admin.tg_upsert_sub\n"
    "Handler._tg_subs = telegram_admin.tg_subs\n"
    "Handler._tg_config_blob = telegram_admin.tg_config_blob\n"
    "Handler._tg_config_save = telegram_admin.tg_config_save\n"
)
splice("\ndef main():\n", "\n" + attach_lines + "\n", after=False, label="handler-attach")

# ================ write + compile gate ================
out = SRC + ".new"
open(out, "w").write(src)

import py_compile
try:
    py_compile.compile(out, doraise=True)
except py_compile.PyCompileError as e:
    os.unlink(out)
    raise AssertionError("post-splice compile FAILED: %s" % e)

if len(src) < orig_len:
    os.unlink(out)
    raise AssertionError("shrank unexpectedly")

os.replace(out, SRC)
print("TELEGRAM_SPLICED_OK bytes=%d" % len(src))
