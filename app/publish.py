# -*- coding: utf-8 -*-
"""pstore publish: native per-platform API posting gateway (stdlib-only).

When the operator pastes real platform keys on /admin/apikeys, scheduled / manual
posts publish natively to that platform instead of only firing the Zapier
webhook. Every platform is optional and gated: if the platform's keys are absent
the gateway posts nothing for it and returns ``skipped``, so the existing
SOCIAL_WEBHOOK / Zapier path stays the safe fallback and posting never blocks.

Credentials are read from a ``key_getter(ns, name)`` callable supplied at build
time (server wires it to its persisted settings KV:
  social.key.twitter, social.key.pinterest, social.key.facebook, social.key.linkedin
The same keys already power the /admin/apikeys UI, so pasting them enables native
posting here with zero extra config.

Networking never raises: every request bottoms out in module-level :func:`_post`,
which tests stub to stay hermetic. On any error a platform reports ``error`` and
the caller (server) decides whether to keep the Zapier webhook path.

Supported native backends today (each posts ``{body, link}``):
  * Twitter / X     — OAuth 1.0a (app + user keys) via api.twitter.com/2/tweets
  * Pinterest       — OAuth 2.0 board app token via api.pinterest.com/v5/pins (POST)
  * Facebook        — Graph API feed POST with a Page access token
  * LinkedIn        — UGC post with an organization access token
  * Instagram       — Graph API photo publish (image_url + caption) with a
                      Business account token + ``social.key.instagram.ig_user_id``
  * YouTube         — renders the kit as a 9:16 Shorts frame (shortslib),
                      encodes a ~6s MP4 with ffmpeg when present, and uploads
                      via Data API v3 ``videos.insert`` (multipart) with an
                      OAuth access token (``social.key.youtube``)
Threads falls back to the webhook (its API needs the same image-video media
endpoints used for the visual caption; kept behind the webhook for now).
"""
import datetime
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

import shorts
import social

_NET_LOCK = threading.Lock()


def _post(url, payload, headers, timeout=15):
    """POST JSON to ``url``. Test seam (module-level) — callers treat any
    exception/HTTP>399 as failure. Returns (http_status, json_dict)."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers, **{"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _read_json(resp)
    except urllib.error.HTTPError as e:
        return e.code, _read_json(e)
    except Exception:
        return 0, {}


def _get(url, headers, timeout=15):
    """GET ``url`` (Pinterest board lookups etc). Test seam like :func:`_post`;
    tests stub it with the same fake and payload arg. Never raises."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _read_json(resp)
    except urllib.error.HTTPError as e:
        return e.code, _read_json(e)
    except Exception:
        return 0, {}


def _read_json(resp):
    try:
        raw = resp.read()
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def _multipart(url, json_section, file_bytes, file_type, headers, timeout=90):
    """POST a JSON metadata part + one binary file part (YouTube
    videos.insert / MediaLibraryService). Test seam like :func:`_post`; never
    raises. Returns (http_status, json_dict)."""
    boundary = "----pstore" + secrets.token_hex(6)
    parts = [
        ("--%s\r\nContent-Type: application/json\r\n\r\n"
         % boundary).encode("ascii"),
        json.dumps(json_section, ensure_ascii=False).encode("utf-8"),
        b"\r\n--%s\r\nContent-Type: %s\r\n\r\n"
        % (boundary.encode("ascii"), file_type.encode("ascii")),
        file_bytes,
        b"\r\n--%s--\r\n" % boundary.encode("ascii"),
    ]
    body = b"".join(parts)
    headers = dict(headers, **{
        "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _read_json(resp)
    except urllib.error.HTTPError as e:
        return e.code, _read_json(e)
    except Exception:
        return 0, {}


def _oauth_nonce():
    return secrets.token_urlsafe(16)


def _oauth_param(key, secret, token, token_secret):
    """OAuth 1.0a header params; empty secrets still sign deterministically so a
    token-only config posts as the user with a consumer key present."""
    from urllib.parse import urlparse
    u = urlparse("https://api.twitter.com/2/tweets")
    ts = str(int(datetime.datetime.now().timestamp()))
    p_enc = urllib.parse.quote("oauth_consumer_key=%s&oauth_nonce=%s"
                               "&oauth_signature_method=HMAC-SHA1&oauth_timestamp=%s"
                               "&oauth_token=%s&oauth_version=1.0"
                               % (urllib.parse.quote(key or "", safe=""),
                                  urllib.parse.quote(_oauth_nonce(), safe=""), ts,
                                  urllib.parse.quote(token or "", safe="")), safe="")
    base = "POST&%s&%s" % (urllib.parse.quote(u.scheme + "://" + u.netloc + u.path, safe=""), p_enc)
    params = {
        "oauth_consumer_key": key or "",
        "oauth_nonce": _oauth_nonce(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts,
        "oauth_token": token or "",
        "oauth_version": "1.0",
    }
    sig = _oauth_sign(key, secret, token_secret, ts, base)
    params["oauth_signature"] = sig
    header = ", ".join('%s="%s"' % (k, urllib.parse.quote(v, safe=""))
                       for k, v in params.items())
    return {"Authorization": "OAuth " + header}


def _oauth_sign(key, secret, token_secret, ts, base):
    import base64
    import hashlib
    import hmac as _hmac
    ckey = urllib.parse.quote(key or "", safe="")
    csec = urllib.parse.quote(secret or "", safe="")
    tsec = urllib.parse.quote(token_secret or "", safe="")
    signing = "&".join([ckey, csec, tsec])
    message = base
    dig = _hmac.new(signing.encode(), message.encode(), hashlib.sha1).digest()
    return base64.b64encode(dig).decode()


# ------------------------------------------------------------------ twitter

def _twt_cred(kv):
    return (kv("twitter", "client_id") or kv("twitter", "app_key") or kv("twitter", "access_key"),
            kv("twitter", "client_secret") or kv("twitter", "app_secret") or kv("twitter", "secret_key"),
            kv("twitter", "token") or kv("twitter", "access_token"),
            kv("twitter", "token_secret") or kv("twitter", "access_token_secret"))


def _pint_cred(kv):
    return (kv("pinterest", "token") or kv("pinterest", "access_token")
            or kv("pinterest", "api_key"),)


def _fb_cred(kv):
    return (kv("facebook", "token") or kv("facebook", "access_token")
            or kv("facebook", "page_token"),)


def _li_cred(kv):
    return (kv("linkedin", "token") or kv("linkedin", "access_token")
            or kv("linkedin", "user_token"),)


# ------------------------------------------------------------------ post app

def _body_for(platform, kit):
    """Body text + link + image + any platform-specific extras the composer produced."""
    body = (kit.get("body") or "")
    link = kit.get("link") or ""
    return {"body": body, "link": link, "image": kit.get("image") or "",
            "image_png": kit.get("image_png") or "",
            "pin_image": kit.get("pin_image") or "",
            "board_id": str(kit.get("board_id") or ""),
            "keyword": kit.get("keyword") or "",
            "hashtags": kit.get("hashtags") or "",
            "name": kit.get("name") or "",
            "utm_content": kit.get("utm_content") or "",
            "platform": platform}


def post_to(platform, kit, key_getter):
    """Post one kit to one platform natively. Returns a dict:
       {"ok": bool, "platform": platform, "via": "native"|"skipped",
        "message": str, "ok" if posted, "skipped" when no creds."""
    profile = {
        "Twitter / X": _post_twitter, "Pinterest": _post_pinterest,
        "Facebook": _post_facebook, "LinkedIn": _post_linkedin,
        "Telegram": _post_telegram, "Instagram": _post_instagram,
        "YouTube": _post_youtube,
    }.get(platform)
    if profile is None:
        return {"ok": False, "platform": platform, "via": "skipped",
                "message": "No native backend for %s — webhook applies." % platform}
    b = _body_for(platform, kit)
    try:
        return profile(b, key_getter)
    except Exception as e:
        return {"ok": False, "platform": platform, "via": "native",
                "message": str(e)}


def _post_twitter(b, kv):
    key, secret, token, token_secret = _twt_cred(kv)
    if not (key and token):
        return {"ok": False, "platform": "Twitter / X", "via": "skipped",
                "message": "No X consumer + access keys configured."}
    text = (b["body"] or "")
    if b["link"] and b["link"] not in text:
        text = text + "\n" + b["link"]
    headers = _oauth_param(key, secret, token, token_secret)
    st, data = _post("https://api.twitter.com/2/tweets", {"text": text}, headers)
    cid = None
    try:
        cid = (data or {})["data"]["id"]
    except Exception:
        cid = None
    return {"ok": 200 <= st < 300, "platform": "Twitter / X", "via": "native",
            "message": ("posted id=" + str(cid)) if cid else json.dumps(data or st)}


_IG_GRAPH = "https://graph.facebook.com/v21.0"


def _post_instagram(b, kv):
    """Instagram Graph API photo publish. Creates an image container from the
    kit's share-card PNG URL (publicly reachable on the site), then publishes
    it. Needs ``social.key.instagram`` (long-lived IG Business user token) and
    ``social.key.instagram.ig_user_id`` (the Business account id)."""
    token = kv("instagram", "access_token") or kv("instagram", "token")
    uid = kv("instagram", "ig_user_id")
    if not token:
        return {"ok": False, "platform": "Instagram", "via": "skipped",
                "message": "No Instagram Graph token configured."}
    if not uid:
        return {"ok": False, "platform": "Instagram", "via": "skipped",
                "message": ("Instagram needs the Business account id "
                            "(social.key.instagram.ig_user_id).")}
    image = b["image_png"] or b["pin_image"] or b["image"]
    if not image:
        return {"ok": False, "platform": "Instagram", "via": "skipped",
                "message": "Instagram needs an image URL in the kit."}
    cap = (b["body"] or "")[:2200]
    st, data = _post("%s/%s/media" % (_IG_GRAPH, uid),
                     {"image_url": image, "caption": cap, "access_token": token},
                     {})
    cid = (data or {}).get("id") if isinstance(data, dict) else None
    if not cid:
        return {"ok": False, "platform": "Instagram", "via": "native",
                "message": "Instagram container failed: %s"
                           % json.dumps(data or st)[:300]}
    pst, pdata = _post("%s/%s/media_publish" % (_IG_GRAPH, uid),
                       {"creation_id": cid, "access_token": token}, {})
    pid = (pdata or {}).get("id") if isinstance(pdata, dict) else None
    return {"ok": 200 <= pst < 300, "platform": "Instagram", "via": "native",
            "message": ("posted id=" + str(pid)) if pid
            else json.dumps(pdata or pst)[:300]}


def _post_youtube(b, kv):
    """YouTube Data API v3 upload. Render the kit as a 9:16 Shorts frame,
    encode a ~6s MP4 with ffmpeg (skipped when ffmpeg is absent, so the
    Zapier/webhook path still fires), then multipart-upload as a *private*
    video. Needs ``social.key.youtube`` (OAuth access token with
    youtube.upload scope)."""
    token = kv("youtube", "access_token") or kv("youtube", "token")
    if not token:
        return {"ok": False, "platform": "YouTube", "via": "skipped",
                "message": "No YouTube Data API token configured."}
    frame = _shorts_frame(b)
    if not frame:
        return {"ok": False, "platform": "YouTube", "via": "skipped",
                "message": "Could not render the Shorts frame — webhook applies."}
    mp4 = _shorts_mp4(frame)
    if not mp4:
        return {"ok": False, "platform": "YouTube", "via": "skipped",
                "message": "ffmpeg unavailable — Shorts upload skipped, webhook applies."}
    title = (b["name"] or _shorts_title(b) or "Best picks, ranked")[:100]
    desc = (b["body"] or "")[:5000]
    if b["link"] and b["link"] not in desc:
        desc = (desc + "\n\n" + b["link"])[:5000]
    tags = _youtube_tags(b)
    snippet = {"title": title, "description": desc, "categoryId": "22",
               "tags": tags, "selfDeclaredMadeForKids": False}
    status = {"privacyStatus": "private"}
    url = ("https://www.googleapis.com/upload/youtube/v3/videos"
           "?uploadType=multipart&part=snippet,status")
    st, data = _multipart(url, {"snippet": snippet, "status": status}, mp4,
                          "video/mp4", {"Authorization": "Bearer " + token})
    vid = (data or {}).get("id") if isinstance(data, dict) else None
    return {"ok": 200 <= st < 300, "platform": "YouTube", "via": "native",
            "message": ("uploaded videoId=" + str(vid)) if vid
            else json.dumps(data or st)[:300]}


def _shorts_title(b):
    from shorts import _title_from
    return _title_from(b["body"] or "")


def _youtube_tags(b):
    used, tags = set(), []
    raw = "%s %s" % ((b["hashtags"] or ""), (b["keyword"] or ""))
    chars = 0
    for w in re.findall(r"#([\w]+)", raw):
        w = w[:30]
        if w.lower() in used:
            continue
        used.add(w.lower())
        if chars + len(w) + 1 > 500:
            break
        tags.append(w)
        chars += len(w) + 1
        if len(tags) >= 24:
            break
    return tags


def _shorts_frame(b):
    try:
        return shorts.frame_png(b.get("keyword") or "", b.get("body") or "",
                                b.get("hashtags") or "")
    except Exception:
        return None


def _shorts_mp4(frame_bytes, seconds=6, fps=25):
    """Encode the single Shorts frame into a ~``seconds`` MP4 (Ken Burns zoom,
    libx264, yuv420p, faststart) when ffmpeg is on PATH; None otherwise."""
    import os
    import shutil
    import subprocess
    import tempfile
    if not frame_bytes:
        return None
    if not shutil.which("ffmpeg"):
        return None
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="pstore_shorts_")
        png = os.path.join(tmpdir, "frame.png")
        out = os.path.join(tmpdir, "out.mp4")
        with open(png, "wb") as f:
            f.write(frame_bytes)
        dur = max(3, min(int(seconds or 6), 15))
        n = dur * fps
        vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,"
              "zoompan=z='min(zoom+0.0006,1.10)':d=%d:s=1080x1920:fps=%d" % (n, fps))
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(dur), "-i", png,
               "-vf", vf, "-c:v", "libx264", "-preset", "medium",
               "-tune", "stillimage", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", "-an", out]
        r = subprocess.run(cmd, capture_output=True, timeout=240)
        if r.returncode != 0:
            return None
        with open(out, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


_PINT_BOARD_CACHE = {}
_PINT_BOARDS_CACHE = {}
_PINT_AUTO_CAP = 15


def _get_user_account(token):
    """GET the authenticated account (v5) — username + follower count for the
    console view. Returns (status, json)."""
    return _get("https://api.pinterest.com/v5/user_account",
                {"Authorization": "Bearer " + token}, timeout=10)


def _get_board_items(token):
    """GET the authenticated account's boards (v5). Returns (status, json)."""
    return _get("https://api.pinterest.com/v5/boards?page_size=100",
                {"Authorization": "Bearer " + token}, timeout=15)


def _pint_boards(token, max_age=60):
    """Cached list of the account's boards (sane dicts) — never rawer than
    ``max_age`` so a blitz / console poll doesn't hammer the boards API."""
    now = time.time()
    hit = _PINT_BOARDS_CACHE.get(token)
    if hit and now - hit[0] < max_age:
        return hit[1]
    st, data = _get_board_items(token)
    items = (data or {}).get("items") if isinstance(data, dict) else None
    boards = [b for b in (items or []) if isinstance(b, dict)]
    _PINT_BOARDS_CACHE[token] = (now, boards)
    return boards


def _pint_create_board(token, board_name):
    """Best-effort create a board (boards:write). Returns the new board id or
    '' on failure (never raises). Invalidates the boards cache on success."""
    name = str(board_name or "").strip()[:60]
    if not name:
        return ""
    st, data = _post("https://api.pinterest.com/v5/boards",
                     {"name": name,
                      "description": "Fresh ranked picks, updated daily."},
                     {"Authorization": "Bearer " + token})
    if not (200 <= st < 300) or not isinstance(data, dict):
        return ""
    bid = str(data.get("id") or "")
    if bid:
        _PINT_BOARDS_CACHE.pop(token, None)
    return bid


def _pint_board_name(keyword):
    """Human per-niche board name (mirrors server._pinterest_board): the
    keyword, title-cased and capped. 'Deals' when there's no keyword."""
    kw = (keyword or "").replace("-", " ").strip()
    name = " ".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", kw) if w)
    return (name or "Deals")[:60]


def _pint_api_error(status, data):
    """Human error string from a Pinterest v5 error response (used by the
    settings test + posting diagnostics). Falls back to the raw status."""
    if isinstance(data, dict):
        msg = (data.get("message") or "")
        if not msg:
            for it in data.get("error") or []:
                msg = (msg + " " + str(it.get("message") or "")).strip()
        if msg:
            return "%s (%s)" % (msg, status)
    return "HTTP %s" % status


def _pint_resolve_board(kv, requested="", auto=True):
    """Best-effort Pinterest board id for the token's account. Resolution order:
    an explicit board NAME from `kv("pinterest", "board")` (settings key
    social.key.pinterest.board) wins; otherwise `requested` (a per-niche board
    name, e.g. the pin's keyword) — and when `auto` is on AND the "auto-create a
    per-niche board" setting is enabled, that board is CREATED on the account
    (boards:write) as long as the account stays under
    `social.key.pinterest.max_boards` total; otherwise the account's "Default"
    board; otherwise the first board in the list. Cached per (token, name) in
    process so a blitz doesn't call the boards API per pin. Returns '' when the
    account has no boards or the lookup fails (the caller then fails the post
    and the webhook fallback takes over)."""
    tok = _pint_cred(kv)[0] or ""
    if not tok:
        return ""
    override = str(kv("pinterest", "board") or "").strip()
    requested = (requested or "").strip()
    wanted = (override or requested).lower()
    wanted_orig = override or requested
    cache_key = tok + "|" + wanted
    if cache_key in _PINT_BOARD_CACHE:
        return _PINT_BOARD_CACHE[cache_key]
    boards = _pint_boards(tok)
    board_id = ""
    for it in boards:
        nm = str(it.get("name") or "").strip().lower()
        if wanted and nm == wanted:
            board_id = str(it.get("id") or "")
            break
        if not wanted and nm == "default":
            board_id = str(it.get("id") or "")
            break
    if not board_id and auto and not override and wanted:
        auto_on = str(kv("pinterest", "auto_board") or "1") not in ("0", "")
        try:
            cap = int(kv("pinterest", "max_boards") or _PINT_AUTO_CAP)
        except (TypeError, ValueError):
            cap = _PINT_AUTO_CAP
        if auto_on and len(boards) < max(1, cap):
            board_id = _pint_create_board(tok, wanted_orig)
    if not board_id:
        for it in boards:
            if it.get("id"):
                board_id = str(it.get("id"))
                break
    if board_id:
        _PINT_BOARD_CACHE[cache_key] = board_id
    return board_id


def _pint_board_id(kv):
    """Compatibility shim: resolve the global board override / default board
    without a per-niche request (auto-create still applies when enabled)."""
    return _pint_resolve_board(kv)


_EMOJI_RE = re.compile(
    u"[\U0001F000-\U0001FAFF\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF"
    u"\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2764\u2E50]"
)


def _strip_emoji(s):
    """Pinterest mangles emoji in renders and duplicate keys hash emoji, so the
    pin title/description carry plain text (ASCII + accented letters only)."""
    s = _EMOJI_RE.sub("", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _pin_title(body, keyword):
    """Keyword-first Pinterest title (<=95 chars, plain text). If the composer
    already smokes the keyword in the first line we keep it — otherwise the
    keyword speaks for itself so the pin is discoverable at a glance."""
    line = _strip_emoji((body or "").split("\n", 1)[0])
    kw = _strip_emoji(keyword or "").strip()
    if kw and kw.lower() not in line.lower():
        line = kw.title()[:70]
    return line[:95] or "Best picks, ranked fresh"


def _pin_desc(body, link, hashtags, keyword):
    """Pinterest-friendly description (<=500 chars): plain body text, the
    keyword fold-in on top when the composer missed it, then discovery tags and
    the tracked link. Emoji stripped; length kept well under Pinterest's cap so
    nothing important gets cut."""
    text = _strip_emoji(body or "")
    kw = _strip_emoji(keyword or "").strip()
    if kw and kw.lower() not in text.lower():
        text = (kw.title() + ": " + text) if text else kw.title()
    head = text[:420]
    tail = ""
    if hashtags and hashtags not in head:
        tail += hashtags[:180]
    if link and link not in text:
        tail += ("\n" if tail else "") + link[:200]
    out = head + (("\n\n" + tail) if tail else "")
    return out[:500] or ""


def _post_pinterest(b, kv):
    tok = _pint_cred(kv)[0] or ""
    if not tok:
        return {"ok": False, "platform": "Pinterest", "via": "skipped",
                "message": "No Pinterest board token configured."}
    board = b.get("board_id") or _pint_resolve_board(
        kv, _pint_board_name(b.get("keyword") or ""),
        auto=bool(b.get("keyword") or ""))
    if not board:
        return {"ok": False, "platform": "Pinterest", "via": "native",
                "message": "Pinterest account has no board to pin to (add one "
                           "or set a board name)."}
    image = (b.get("pin_image") or b.get("image_png") or b.get("image")
             or og_image(b["link"]))
    payload = {
        "title": _pin_title(b["body"], b.get("keyword") or ""),
        "description": _pin_desc(b["body"], b["link"] or "",
                                 b.get("hashtags") or "",
                                 b.get("keyword") or ""),
        "link": b["link"] or "",
        "board_id": board,
        "media_source": {"source_type": "image_url", "url": image},
    }
    st, data = _post("https://api.pinterest.com/v5/pins",
                     payload, {"Authorization": "Bearer " + tok, **pint_ignore()})
    return {"ok": 200 <= st < 300, "platform": "Pinterest", "via": "native",
            "message": ("created " + str((data or {}).get("id") or "")) if data else str(st)}


def _post_facebook(b, kv):
    tok = _fb_cred(kv)[0] or ""
    if not tok:
        return {"ok": False, "platform": "Facebook", "via": "skipped",
                "message": "No Facebook page token configured."}
    text = (b["body"] or "") + ("\n" + b["link"] if b["link"] else "")
    url = ("https://graph.facebook.com/v19.0/me/feed?access_token=%s"
           % urllib.parse.quote(tok, safe=""))
    payload = {"message": text}
    st, data = _post(url, payload, {"User-Agent": "pstore/1.0"})
    return {"ok": 200 <= st < 300, "platform": "Facebook", "via": "native",
            "message": (str(data.get("id") or "") if data else str(st))}


def _post_linkedin(b, kv):
    tok = _li_cred(kv)[0] or ""
    if not tok:
        return {"ok": False, "platform": "LinkedIn", "via": "skipped",
                "message": "No LinkedIn access token configured."}
    subj = (b["body"] or "")[:200]
    text = (b["body"] or "")
    com = (b["body"] or "").replace(subj, "").strip() or subj
    payload = {
        "author": "urn:li:person:" + (kv("linkedin", "urn") or "me"),
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": (com + ("\n" + b["link"] if b["link"] else ""))},
                "shareMediaCategory": "NONE",
            }},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    st, data = _post("https://api.linkedin.com/v2/ugcPosts",
                     payload, {"Authorization": "Bearer " + tok})
    return {"ok": 200 <= st < 300, "platform": "LinkedIn", "via": "native",
            "message": ("created " + str((data or {}).get("id") or "")) if data else str(st)}


def _post_telegram(b, kv):
    """Telegram channel win-loop backend: posts the tracked link (with the share
    card when one exists) to a channel via a free BotFather bot. Credentials:
      * bot token — `social.key.telegram` (one line in /admin/apikeys), or fold
        both into it as `TOKEN|@channel` (or a numeric chat id),
      * optional separate chat id — `social.key.telegram.chat`.
    Zero budget, zero review queue: the loop's winner re-queues posts natively to
    this channel like any other platform."""
    field = (kv("telegram", "token") or "").strip()
    chat = (kv("telegram", "chat") or "").strip()
    token = field
    if "|" in field:
        token, chat = (s.strip() for s in field.split("|", 1))
    elif chat and chat == field:
        chat = ""  # kv() fell back to the token itself — no separate chat set
    if not token:
        return {"ok": False, "platform": "Telegram", "via": "skipped",
                "message": "No Telegram bot token configured."}
    if not chat:
        return {"ok": False, "platform": "Telegram", "via": "native",
                "message": "No Telegram chat id configured (paste TOKEN|@channel or "
                           "set social.key.telegram.chat)."}
    text = (b["body"] or "").strip()
    tail = []
    if b.get("hashtags"):
        tail.append(b["hashtags"])
    if b.get("link") and b["link"] not in text:
        tail.append(b["link"])
    text = (text + ("\n\n" + "\n".join(tail) if tail else "")).strip()
    image = b.get("image_png") or b.get("image") or ""
    if image:
        st, data = _post("https://api.telegram.org/bot%s/sendPhoto" % token,
                         {"chat_id": chat, "photo": image, "caption": text[:1024]},
                         {"Content-Type": "application/json"})
    else:
        st, data = _post("https://api.telegram.org/bot%s/sendMessage" % token,
                         {"chat_id": chat, "text": text[:4096]},
                         {"Content-Type": "application/json"})
    msg_id = ""
    try:
        msg_id = str((data or {}).get("result", {}).get("message_id") or "")
    except Exception:
        msg_id = ""
    return {"ok": 200 <= st < 300, "platform": "Telegram", "via": "native",
            "message": ("posted msg=%s" % msg_id) if msg_id else json.dumps(data or st)}


def og_image(url):
    """Best-effort og:image for the landing URL, so image-first backends
    (Pinterest) can pin something real. Returns '' on any failure."""
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "pstore/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(262144).decode("utf-8", "replace")
        m = __import__("re").search(r'property="og:image"\s+content="([^"]+)"', raw)
        return (m.group(1) or "") if m else ""
    except Exception:
        return ""


def pint_ignore():
    """Extra headers so Pinterest v5 image pins don't require an upload; the API
    accepts a JSON post with an image_url media_source."""
    return {}


# ------------------------------------------------------------------ batch

def publish_batch(kits, key_getter):
    """Post a batch of kits (one per platform) natively. Returns a list of per-
    kit results. Never raises. ``kits`` are the composer kits (dicts)."""
    out = []
    for kit in kits:
        platform = kit.get("platform") or ""
        res = post_to(platform, kit, key_getter)
        res["slug"] = kit.get("slug")
        res["utm_content"] = kit.get("utm_content")
        out.append(res)
    return out