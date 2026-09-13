# -*- coding: utf-8 -*-
"""pstore email sender: sends the 5-email buyer sequence over SMTP (stdlib).

Config via env (never commit secrets):
  SMTP_HOST        e.g. smtp.gmail.com
  SMTP_PORT        587 (STARTTLS) or 465 (SSL)
  SMTP_USER        full account, e.g. your@gmail.com
  SMTP_PASSWORD    app password / API token, NOT the account login password
  SMTP_FROM        optional display address; defaults to SMTP_USER
  SMTP_STARTTLS    1 (default) to upgrade with STARTTLS, 0 for implicit SSL
  PSTORE_URL       site origin used to build per-subscriber unsubscribe links

When SMTP is not configured the admin page says so and sends are refused —
everything else (opt-in capture, unsubscribe, analytics) keeps working.
"""
import html as _html
import imaplib
import os
import re
import smtplib
import threading
import urllib.parse
from email import message_from_bytes
from email.header import Header, decode_header as _decode_header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

import market_engine
import security

STORE_NAME = os.environ.get("PSTORE_NAME", "pstore").strip() or "pstore"
REPLY_TO = (os.environ.get("SMTP_REPLY_TO", "") or "").strip()
EMAIL_SEND_DELAY = float(os.environ.get("SMTP_SEND_DELAY", "0") or "0")  # seconds between sends

SMTP_HOST = os.environ.get("SMTP_HOST", "")
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
except (TypeError, ValueError):
    SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or (SMTP_USER or "noreply@localhost")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") == "1"
MAX_EMAILS_PER_RUN = int(os.environ.get("SMTP_MAX_PER_RUN", "50") or "50")
SEQUENCE_LENGTH = 5

# --- threaded replies ------------------------------------------------------
# Every sequence/studio email gets a unique Reply-To like
#   pstore+<subscriber-id>@yourdomain
# so a customer reply maps straight back to the exact subscriber + is captured
# by the Studio Inbox. Set PSTORE_REPLY_DOMAIN (or SMTP_REPLY_TO, whose domain
# is reused) to enable; without it sends keep the previous reply behavior.
REPLY_PREFIX = (os.environ.get("PSTORE_REPLY_PREFIX", "pstore").strip() or "pstore")
REPLY_DOMAIN = os.environ.get("PSTORE_REPLY_DOMAIN", "").strip()

# --- inbound (inbox) -------------------------------------------------------
# Poll an IMAP mailbox that receives the tagged Reply-To addresses (or have
# your forwarding service POST to /api/cron/inbox with the EMAIL_CRON_SECRET).
IMAP_HOST = os.environ.get("IMAP_HOST", "").strip()
try:
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
except (TypeError, ValueError):
    IMAP_PORT = 993
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")


def inbound_configured():
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)


def _reply_domain():
    """Replies are routed to `REPLY_PREFIX+<tag>@domain`. The domain comes from
    PSTORE_REPLY_DOMAIN when set (a bare domain or an address), else from
    SMTP_REPLY_TO's domain. Empty = no threading."""
    for src in (REPLY_DOMAIN, REPLY_TO):
        if not src:
            continue
        if "@" in src:
            return src.rsplit("@", 1)[-1].lower()
        return src.strip().lower()
    return ""


def thread_reply_to(tag):
    """The per-subscriber tagged Reply-To address, or '' when threading is off."""
    dom = _reply_domain()
    if not dom or not tag:
        return ""
    return "%s+%s@%s" % (REPLY_PREFIX, str(tag), dom)


def parse_thread_tag(addresses, prefix=None):
    """Pull the subscriber id out of a 'To' header with our tagged address."""
    prefix = prefix or REPLY_PREFIX
    m = re.search(r"%s\+(\d+)@[^\s;,>\"']+" % re.escape(prefix), addresses or "")
    return int(m.group(1)) if m else None


def configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


# Per-request site origin (set by the server from the Host header) so email
# links stay absolute on deployments that never set PSTORE_URL. Pure env-based
# (PSTORE_URL always wins) and thread-local, so tests and non-HTTP callers keep
# today's relative-link behaviour.
_site_base = threading.local()


def set_site_base(base):
    _site_base.value = (base or "").rstrip("/")


def site_base():
    env = os.environ.get("PSTORE_URL", "").rstrip("/")
    if env:
        return env
    return getattr(_site_base, "value", "") or ""


def unsubscribe_url(email):
    token = security.make_token("unsub:" + email.lower(), 30 * 24 * 3600)
    return "%s/unsubscribe?e=%s&t=%s" % (site_base(), urllib.parse.quote(email),
                                         urllib.parse.quote(token))


def _footer(email):
    return ("\n\n— %s\n\nYou're getting this because you opted in on a %s page.\n"
            "Change your mind any time: %s"
            % (STORE_NAME, STORE_NAME, unsubscribe_url(email)))


def _guess_first_name(email, stored="", fallback="there"):
    """Best first-name to greet the reader with, from most to least specific:
    a stored name we captured, else one derived from the email local-part
    (e.g. jane.doe@example.com -> "Jane"), else the plain fallback."""
    if stored and stored.strip():
        return stored.strip()
    if email and "@" in email:
        local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
        if local:
            return " ".join(w[:1].upper() + w[1:] for w in local.split() if w)[:80]
    return fallback


def render_body(mail, to_name="there", site_name=STORE_NAME, email="", tracked_link=""):
    """Turn one mail dict from market_engine.build_email_sequence into a
    sendable plain-text body (drop the redundant Subject: line, fill the
    {{placeholders}}, append a permission-reminder + unsubscribe footer).

    {{first_name}} uses a captured name, else a name derived from the reader's
    email local-part, so every send stays personal even without a stored name.
    {{your_name}} is the store/sender's signature (configurable via PSTORE_NAME).

    `tracked_link` (optional) rewrites the raw affiliate URLs inside the body to
    a click-tracked link so email outbound clicks are attributed."""
    body = mail.get("body") or ""
    if body.startswith("Subject:"):
        body = body.split("\n\n", 1)[-1]
    greeted = _guess_first_name(email, to_name, fallback="there")
    body = body.replace("{{first_name}}", greeted).replace("{first_name}", greeted) \
               .replace("{{your_name}}", site_name or STORE_NAME) \
               .replace("{your_name}", site_name or STORE_NAME)
    if tracked_link:
        body = _wrap_links(body, tracked_link)
    if email:
        body += _footer(email)
    return body


def _fill_mail(mail, to_name="there", site_name=STORE_NAME, email=""):
    """Fill the {{placeholders}} shared by the plain-text and HTML renderers."""
    body = mail.get("body") or ""
    if body.startswith("Subject:"):
        body = body.split("\n\n", 1)[-1]
    greeted = _guess_first_name(email, to_name, fallback="there")
    body = body.replace("{{first_name}}", greeted).replace("{first_name}", greeted) \
               .replace("{{your_name}}", site_name or STORE_NAME) \
               .replace("{your_name}", site_name or STORE_NAME)
    return body


def _price_text(item):
    try:
        import amazon
        return "%s%0.2f" % (amazon.currency_symbol((item or {}).get("currency") or "USD"),
                            (item or {}).get("price"))
    except Exception:
        return None


def _email_sections(body, tracked_link=""):
    """Convert a plain-text marketing body into well-organised HTML: short lines
    become bold sub-headings, bullet lists stay lists, every URL is clickable
    (and rewritten to the tracked link when one is provided)."""
    e = _html.escape
    blocks = [b for b in (body or "").split("\n\n") if (b or "").strip()]
    out = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if (ln or "").strip()]
        joined = block.strip()
        bullets = [ln for ln in lines if re.match(r"^\s*(?:[•\-*]|\d{1,2}[\.\)])\s", ln)]
        interactive = len(bullets) == len(lines) and len(lines) > 1 and bullets
        label = re.match(r"^([^.:?!\n]{2,42}):", joined)
        is_heading = (len(joined) <= 64 and "://" not in joined
                      and (joined[-1:] in (":", "?") or joined[-1:] == ".")
                      and not joined.lower().startswith(("hi ", "subject:", "dear ")))
        def _link(m):
            href = tracked_link or m.group(0)
            return '<a href="%s" style="color:#e8600c;font-weight:700;text-decoration:none">%s</a>' % (
                e(href), e((m.group(0)[m.group(0).find("://") + 3:])[:64]))
        if label and not label.group(0).lower().startswith(("http", "hi ")):
            lead, rest = joined.split(":", 1)
            rest_html = re.sub(r"https?://\S+", _link, e(rest.strip()))
            out.append('<h3 style="margin:20px 0 6px;font-size:16px;letter-spacing:-.2px;'
                       'color:#191b26;font-weight:800">%s</h3>'
                       '<p style="margin:0 0 14px;font-size:15px;line-height:1.65;color:#3a3f4b">%s</p>'
                       % (e(lead.strip()), rest_html))
        elif interactive:
            items = "".join(
                '<li style="margin:5px 0 0">%s</li>' %
                re.sub(r"https?://\S+", _link, e(ln))
                for ln in bullets)
            out.append('<ul style="margin:8px 0;padding-left:20px;list-style:square">%s</ul>' % items)
        elif is_heading:
            out.append('<h3 style="margin:22px 0 8px;font-size:16px;letter-spacing:-.2px;'
                       'color:#191b26;font-weight:800">%s</h3>' %
                       re.sub(r"https?://\S+", _link, e(joined)))
        else:
            out.append('<p style="margin:0 0 14px;font-size:15px;line-height:1.65;color:#3a3f4b">%s</p>' %
                       re.sub(r"https?://\S+", _link, e(joined)))
    return "\n".join(out) if out else '<p style="margin:0;color:#3a3f4b">…</p>'


def product_card_html(item, link_url="", image_url="", badge="", stars=0, reviews=0):
    """One Amazon-style product card for email bodies: clean image on top, bold
    title, ⭐ rating line, orange price, and a direct affiliate CTA. All inline
    styles so Gmail/Outlook/Apple Mail render it the same."""
    e = _html.escape
    it = item or {}
    title = market_engine._clip(it.get("title") or "", 96)
    price = _price_text(it)
    rating = ""
    if stars or reviews:
        sst = "" if not reviews else "s" if reviews != 1 else ""
        rating = ('<div style="margin:7px 0 0;font-size:13px;color:#5c6b7a">'
                  '★ <strong style="color:#191b26">%s</strong> · %s rating%s</div>' %
                  (e(str(stars)), e("{:,}".format(reviews) if reviews else "—"), sst))
    badge_html = ('<div style="margin:0 0 8px;font-size:11px;font-weight:800;letter-spacing:.12em;'
                  'color:#e8600c;text-transform:uppercase">%s</div>' % e(badge)) if badge else ""
    cta = ('<div style="margin:14px 0 0"><a href="%s" rel="nofollow sponsored noopener" '
           'style="display:inline-block;background:#f0a41a;background-image:linear-gradient(180deg,#ffd75e,#f0a41a);'
           'color:#111;text-decoration:none;font-weight:800;font-size:14px;'
           'padding:10px 26px;border-radius:999px;border:1px solid #e6a700">See it on Amazon →</a></div>'
           % e(link_url)) if link_url else ""
    return """<div style="margin:20px 0;background:#ffffff;border:1px solid #ececf1;border-radius:16px;overflow:hidden">%s
  <div style="padding:18px 18px 20px;text-align:left">
    %s
    <div style="font-size:15.5px;line-height:1.4;font-weight:700;color:#191b26;letter-spacing:-.15px">%s</div>
    %s
    <div style="margin-top:9px;font-size:22px;font-weight:800;color:#b12704;letter-spacing:-.4px">%s</div>
    %s
  </div>
</div>""" % (
        (('<div style="background:#fbfbfd;padding:14px;border-bottom:1px solid #f0f0f4">'
          '<img src="%s" alt="%s" width="280" style="max-width:100%%;height:auto;border:0;display:block;margin:0 auto">'
          '</div>' % (e(image_url), e(title))) if image_url else ""),
        badge_html, e(title), rating,
        (e(price) if price else '<span style="font-size:15px;color:#5c6b7a">price on Amazon</span>'),
        cta)


def _runner_rows(items, tracked_link=""):
    """Compact runner-up list shown under the hero card when alternatives exist."""
    alt = market_engine._alternate_lines(items, (market_engine.pick_for_buyers(items) or {}).get("asin") or "")
    if not alt:
        return ""
    row_html = []
    for line in alt:
        if not (line or "").strip():
            continue
        row_html.append('<div style="margin:6px 0"><span style="font-size:14px;color:#3a3f4b">%s</span></div>'
                        % re.sub(r"https?://\S+",
                                 lambda m: '<a href="%s" style="color:#e8600c;font-weight:700;text-decoration:none">view on Amazon</a>'
                                           % _html.escape(tracked_link or m.group(0)),
                                 _html.escape(line)))
    return ('<div style="margin:6px 0 0;padding:14px 18px;background:#fafafc;border-radius:12px">'
            '<div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:#9aa0ad;'
            'text-transform:uppercase;margin-bottom:6px">Also matched</div>%s</div>'
            % "".join(row_html))


def render_email_html(mail, to_name="there", site_name=STORE_NAME, email="",
                      keyword="", items=None, base_url="", tracked_link=""):
    """Full branded HTML body for a marketing sequence/studio mail. Bold H1 from
    the subject, bold sub-headings + paragraphs from the body, an Amazon-style
    hero product card with the niche share-card image, runner-ups, an orange CTA
    and an unsubscribe footer. Returns '' when nothing sensibly renderable so a
    sender can fall back to plain text (never breaks mail)."""
    try:
        body = _fill_mail(mail, to_name, site_name, email)
        subject = (mail.get("subject") or "").strip()
        if not (body or "").strip():
            return ""
        inner = _email_sections(body, tracked_link)
        pick = market_engine.pick_for_buyers(items) if items else None
        if pick and keyword:
            base = (base_url or site_base()).rstrip("/")
            image = "%s/og/%s.png?v=navy3" % (base, _slug(keyword))
            link = tracked_link or (base + "/lp/" + _slug(keyword))
            stars = pick.get("stars") or 0
            reviews = pick.get("reviews") or 0
            inner += product_card_html(pick, link, image, badge="Top pick for “%s”" % keyword,
                                       stars=stars, reviews=reviews)
            inner += _runner_rows(items, tracked_link)
        if tracked_link:
            inner += ('<div style="text-align:center;margin:26px 0 4px">'
                      '<a href="%s" style="display:inline-block;background:#e8600c;color:#ffffff;'
                      'text-decoration:none;font-weight:700;font-size:15px;padding:13px 30px;'
                      'border-radius:999px">Check price &amp; reviews →</a></div>'
                      % _html.escape(tracked_link))
        subject = subject or ("Your curated %s picks" % keyword if keyword else "From " + site_name)
        return _brand_shell(subject, subject, inner,
                            _footer_html(email))
    except Exception:
        return ""


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s or "")).lower().strip("-")


def _footer_html(email=""):
    unsub = unsubscribe_url(email) if email else site_base()
    return ('You&rsquo;re receiving this because you opted in on a %s page and asked to hear '
            'about our curated picks. Change your mind any time: '
            '<a href="%s" style="color:#c96a12;font-weight:700;text-decoration:none">Unsubscribe</a>.'
            % (_html.escape(STORE_NAME), _html.escape(unsub)))


# Test hook: tests assign _send(subject, body, to, attachments=None) -> True/False.
# Keeping the network path behind one function means sending is fully stub-able offline.
_send = None


def track_token(external, scope="e", ttl=30 * 24 * 3600):
    """Signed token whose scope carries the tracked-link payload (default 30-day
    click window). `external` is a `|`-joined string (niche slug, asin, sub id,
    email index) the recipient handler decodes back off the scope after verify."""
    return security.make_token("%s:%s" % (scope, external), ttl)


def decode_track_token(token, scope="e"):
    """Verify a tracked-link/open token and return the stored `external` payload
    as a tuple, or None when forged/expired. Handles URL-encoded tokens."""
    import urllib.parse as _up
    sc = security.verify_token(_up.unquote(token))
    if not sc or not sc.startswith(scope + ":"):
        return None
    return tuple(sc[len(scope) + 1:].split("|"))


def tracked_url(keyword, asin, sid=None, idx=0):
    """Click-tracked affiliate link for an email. Wraps the niche + ASIN + who +
    which email index so the redirect records a click attributed to the email."""
    token = track_token("%s|%s|%s|%s" % (keyword, asin, sid or "", idx or 0))
    return "%s/e/%s" % (site_base(), urllib.parse.quote(token, safe=""))


def open_pixel_url(keyword, asin, sid=None, idx=0):
    """1x1 open-tracking pixel URL for an email."""
    token = track_token("%s|%s|%s|%s" % (keyword, asin, sid or "", idx or 0), scope="o")
    return "%s/e/o/%s" % (site_base(), urllib.parse.quote(token, safe=""))


def _wrap_links(body, link_url, pid=""):
    """Rewrite the plain affiliate links inside a sequence body to the tracked
    URL so email clicks are attributed. Falls back to the raw link when the
    tracked URL is unavailable."""
    def _link_replace(m):
        return link_url or m.group(0)
    import re as _re
    urls = _re.findall(r"https?://[^\s)\]]+", body)
    for u in urls:
        if link_url:
            body = body.replace(u, link_url)
    return body


def _build_message(subject, body, to, from_addr, attachments=None, pixel_url="",
                   html="", reply_to="", in_reply_to=""):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((STORE_NAME, from_addr))
    msg["To"] = to
    rt = reply_to or REPLY_TO
    if rt:
        msg["Reply-To"] = rt
        msg["Return-Path"] = rt
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["List-Unsubscribe"] = "<mailto:%s?subject=unsubscribe>" % from_addr
    if pixel_url:
        body = "%s\n\n<img src=\"%s\" width=\"1\" height=\"1\" alt=\"\" border=\"0\">" % (body, pixel_url)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html:
        if pixel_url:
            html = html.replace("</body>",
                                '<img src="%s" width="1" height="1" alt="" border="0" '
                                'style="display:none"></body>' % _html.escape(pixel_url))
            if "</body>" not in html:
                html += '<img src="%s" width="1" height="1" alt="" border="0" ' \
                        'style="display:none">' % _html.escape(pixel_url)
        msg.attach(MIMEText(html, "html", "utf-8"))
    for name, data in (attachments or []):
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)
    return msg


def _smtp_send(subject, body, to, from_addr=None, attachments=None, pixel_url="",
               html="", reply_to="", in_reply_to=""):
    from_addr = from_addr or SMTP_FROM
    msg = _build_message(subject, body, to, from_addr, attachments, pixel_url, html,
                         reply_to, in_reply_to)
    try:
        if SMTP_STARTTLS:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
        else:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
        return True
    except Exception:
        return False


def send(subject, body, to, attachments=None, pixel_url="", html="",
         reply_to="", in_reply_to=""):
    if not configured():
        return False
    if _send is not None:
        return bool(_send(subject, body, to, attachments, pixel_url,
                          reply_to=reply_to, in_reply_to=in_reply_to))
    return _smtp_send(subject, body, to, attachments=attachments, pixel_url=pixel_url,
                      html=html, reply_to=reply_to, in_reply_to=in_reply_to)


# ------------------------------------------------------------------ inbound (inbox)

# Test hook: tests assign _imap_fetch(limit) -> list of inbound message dicts.
_imap_fetch = None


def _decode_mime(value):
    if not value:
        return ""
    out = []
    for txt, enc in _decode_header(str(value)):
        if isinstance(txt, bytes):
            try:
                txt = txt.decode(enc or "utf-8", "replace")
            except (LookupError, UnicodeDecodeError):
                txt = txt.decode("utf-8", "replace")
        out.append(str(txt))
    return "".join(out)


def _parse_inbound(raw):
    """Parse one raw RFC822 message into a flat inbound dict for the Studio
    Inbox. Never raises on malformed mail; returns best-effort fields."""
    msg = message_from_bytes(raw)
    try:
        from_addr, from_name = parseaddr(msg.get("From", ""))
    except Exception:
        from_addr, from_name = "", ""
    to_hdrs = [h for h in (msg.get("To", ""), msg.get("Cc", ""),
                           msg.get("Delivered-To", ""), msg.get("X-Original-To", "")) if h]
    text = html = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type() or ""
                if ctype == "text/plain" and not text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = _decode_mime(payload)
                elif ctype == "text/html" and not html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = _decode_mime(payload)
        else:
            ctype = msg.get_content_type() or ""
            payload = msg.get_payload(decode=True) or b""
            if ctype == "text/plain":
                text = _decode_mime(payload)
            elif ctype == "text/html":
                html = _decode_mime(payload)
    except Exception:
        pass
    return {"message_id": (_decode_mime(msg.get("Message-ID", "")) or "").strip(),
            "in_reply_to": (_decode_mime(msg.get("In-Reply-To", "")) or "").strip(),
            "to_header": " ".join(_decode_mime(h) for h in to_hdrs),
            "from_addr": (str(from_addr or "").strip().lower()),
            "from_name": _decode_mime(from_name or ""),
            "subject": _decode_mime(msg.get("Subject", "")),
            "date": _decode_mime(msg.get("Date", "")),
            "text": text or "",
            "html": html or ""}


def fetch_inbound(limit=25):
    """Fetch the newest messages from the configured IMAP mailbox and return
    parsed inbound dicts (newest first? oldest-first here). Returns the raw
    parsed list; the server dedups by Message-ID, so re-fetching is safe."""
    if _imap_fetch is not None:
        return _imap_fetch(limit) or []
    if not inbound_configured():
        return []
    msgs = []
    try:
        if IMAP_PORT == 993:
            box = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
        else:
            box = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
            if os.environ.get("IMAP_STARTTLS", "1") == "1":
                box.starttls()
        try:
            box.login(IMAP_USER, IMAP_PASSWORD)
            typ, _ = box.select(IMAP_FOLDER or "INBOX")
            if typ == "OK":
                _, ids = box.search(None, "ALL")
                nums = (ids[0] or b"").split()
                for num in nums[-limit:]:
                    typ, data = box.fetch(num, "(RFC822)")
                    if typ == "OK" and data and data[0]:
                        msgs.append(_parse_inbound(data[0][1]))
        finally:
            try:
                box.logout()
            except Exception:
                pass
    except Exception:
        return msgs
    return msgs


# ------------------------------------------------------------------ transactional HTML

def _esc(value):
    return _html.escape(str(value or ""), quote=True)


def transactional_html(preheader, heading, paragraphs, cta_url, cta_label, footnote=""):
    """Branded, light-rendered HTML mail (works in Gmail/Outlook/Apple Mail):
    gradient header wordmark, one big rounded CTA button, a plain-text fallback
    link, and a discreet footnote showing expiry / "didn't request this"."""
    paras = "\n".join('<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#3a3f4b">%s</p>' % p
                       for p in paragraphs)
    fallback = ('<p style="margin:20px 0 0;font-size:13px;line-height:1.5;color:#7a8191">'
                'Button not working? Copy and paste this link into your browser:<br>'
                '<a href="%s" style="color:#a453ff;word-break:break-all">%s</a></p>'
                % (_esc(cta_url), _esc(cta_url)))
    note = ('<p style="margin:18px 0 0;padding-top:16px;border-top:1px solid #ececf1;'
            'font-size:12px;line-height:1.6;color:#9aa0ad">%s</p>' % footnote) if footnote else ""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>%(store)s</title></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif">
<span style="display:none;max-height:0;overflow:hidden">%(pre)s</span>
<div style="max-width:560px;margin:24px auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 10px 34px rgba(20,20,40,.10)">
  <div style="background:linear-gradient(120deg,#ff7a18 0%%,#ff4e9e 55%%,#a453ff 100%%);padding:26px 34px">
    <div style="font-size:19px;font-weight:800;color:#ffffff;letter-spacing:.2px">%(store)s</div>
    <div style="font-size:12px;color:rgba(255,255,255,.82);margin-top:2px">America-verified product picks</div>
  </div>
  <div style="padding:34px 34px 28px">
    <h1 style="margin:0 0 14px;font-size:21px;letter-spacing:-.2px;color:#191b26">%(head)s</h1>
    %(paras)s
    <div style="text-align:center;margin:26px 0 4px">
      <a href="%(url)s" style="display:inline-block;background:#ff7a18;color:#ffffff;text-decoration:none;
        font-weight:700;font-size:15px;padding:13px 30px;border-radius:999px">%(label)s</a>
    </div>
    %(fallback)s
    %(note)s
  </div>
  <div style="background:#fafafc;padding:18px 34px;border-top:1px solid #ececf1">
    <p style="margin:0;font-size:12px;line-height:1.6;color:#9aa0ad">You're receiving this because an account was
    created (or a password change was requested) on %(store)s with this email address.
    If this wasn't you, you can ignore this message — nothing changes unless you use the link above.</p>
  </div>
</div></body></html>""" % {
        "store": _esc(STORE_NAME), "pre": _esc(preheader), "head": _esc(heading),
        "paras": paras, "url": _esc(cta_url), "label": _esc(cta_label),
        "fallback": fallback, "note": note}


def _brand_shell(preheader, heading, inner_html, footnote_html=""):
    """Shared clean email shell (same header family as transactional mail) so
    marketing, sequence and price-drop emails all render consistently."""
    note = ('<div style="background:#fafafc;padding:18px 34px;border-top:1px solid #ececf1">'
            '<p style="margin:0;font-size:12px;line-height:1.6;color:#9aa0ad">%s</p></div>'
            % footnote_html) if footnote_html else ""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>%(store)s</title></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,&#39;Segoe UI&#39;,Roboto,Arial,Helvetica,sans-serif">
<span style="display:none;max-height:0;overflow:hidden">%(pre)s</span>
<div style="max-width:560px;margin:24px auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 10px 34px rgba(20,20,40,.10)">
  <div style="background:linear-gradient(120deg,#ff7a18 0%%,#ff4e9e 55%%,#a453ff 100%%);padding:26px 34px">
    <div style="font-size:19px;font-weight:800;color:#ffffff;letter-spacing:.2px">%(store)s</div>
    <div style="font-size:12px;color:rgba(255,255,255,.82);margin-top:2px">America-verified product picks</div>
  </div>
  <div style="padding:34px 34px 28px">
    <h1 style="margin:0 0 16px;font-size:21px;letter-spacing:-.2px;color:#191b26;font-weight:800">%(head)s</h1>
    %(inner)s
  </div>
  %(note)s
</div></body></html>""" % {
        "store": _esc(STORE_NAME), "pre": _esc(preheader), "head": _esc(heading),
        "inner": inner_html, "note": note}


def verify_email_html(confirm_url, name):
    return transactional_html(
        "Confirm your email address to activate your pstore account",
        "Confirm your email address",
        ["Hi %s,\n\nThanks for signing up for pstore. To activate your account and get "
         "started, confirm that this email address is yours." % _esc(name),
         "This link is private to you and expires in <b>72 hours</b>."],
        confirm_url, "Activate my account",
        footnote="Your confirmation link expires in 72 hours. If it expires, you can "
                 "request a new one from the sign-in page.")


def reset_email_html(reset_url, name):
    return transactional_html(
        "Reset your pstore password — link valid for 1 hour",
        "Reset your password",
        ["Hi there,\n\nWe received a request to reset the password for your pstore account. "
         "If that was you, use the button below to choose a new password.",
         "This link is private to you and expires in <b>1 hour</b>. If you didn't ask to "
         "reset your password, you can safely ignore this email — your password stays the same."],
        reset_url, "Choose a new password",
        footnote="This password reset link expires in 1 hour and can only be used once.")


# ------------------------------------------------------------------ sequence

def next_email(keyword, items, index):
    """Return the mail dict at 1-based `index` in the 5-email sequence, or None."""
    seq = market_engine.build_email_sequence(keyword, items)
    if not seq or index < 1 or index > len(seq):
        return None
    return seq[index - 1]