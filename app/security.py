# -*- coding: utf-8 -*-
"""pstore security helpers (stdlib only).

Rate limiting (per client), a concurrency cap so heavy load fails fast instead
of exhausting threads, request-size caps, and HMAC-signed one-time tokens used
for OAuth state + CSRF defense. None of this is a substitute for TLS/ops policy,
but it raises the practical bar against brute force, flooding and CSRF.
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import time

# Signing secrets. Prefer durable values from the environment so HMAC-signed
# links/tokens (unsubscribe, PDF gate, OAuth state) survive restarts and
# redeploys; fall back to fresh random values only when unconfigured. Never
# expose these via API/logs.
_OAUTH_SECRET = os.environ.get("PSTORE_OAUTH_SECRET") or secrets.token_hex(32)
HASH_SECRET = os.environ.get("PSTORE_HASH_SECRET") or secrets.token_hex(32)

MAX_BODY = 64 * 1024        # reject bodies larger than this (413)
MAX_URL = 4096              # reject request targets longer than this (414)

# One lightweight semaphore bounds real concurrency so DDoS-style request
# floods degrade to quick 503s instead of thread exhaustion.
CONCURRENCY = threading.BoundedSemaphore(64)


class RateLimiter:
    """Sliding-window limit, keyed by (namespace, key). Thread-safe."""

    def __init__(self, limit, window_sec):
        self.limit = limit
        self.window = window_sec
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, key):
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(bucket) < self.limit:
                bucket.append(now)
                self._hits[key] = bucket
                return True
            self._hits[key] = bucket
            return False

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


# Per-client budgets (a client = peer socket, or forwarded chain when present).
LOGIN_LIMITER = RateLimiter(limit=5, window_sec=15 * 60)     # 5 attempts / 15 min
API_LIMITER = RateLimiter(limit=240, window_sec=60)          # /api flood guard
HTTP_LIMITER = RateLimiter(limit=720, window_sec=60)         # global per-client cap
SUBSCRIBE_LIMITER = RateLimiter(limit=6, window_sec=10 * 60)  # opt-in abuse guard
TRACK_LIMITER = RateLimiter(limit=180, window_sec=60)        # click beacons
PAGEVIEW_LIMITER = RateLimiter(limit=120, window_sec=60)     # pageview/event beacons
REGISTER_LIMITER = RateLimiter(limit=6, window_sec=10 * 60)   # account signups per client
RESEND_LIMITER = RateLimiter(limit=5, window_sec=15 * 60)     # "resend verify" emails
FORGOT_LIMITER = RateLimiter(limit=5, window_sec=15 * 60)     # "forgot password" emails
RESET_LIMITER = RateLimiter(limit=8, window_sec=15 * 60)      # password set attempts


def client_key(headers, peer_ip):
    """One stable-ish key per client: prefer the proxy's forwarded chain, but
    pin to the peer address so a spoofed X-Forwarded-For can't reset a ban."""
    xff = (headers.get("X-Forwarded-For") or "").strip()
    chain = [x.strip() for x in xff.split(",") if x.strip()]
    try:
        first = str(ipaddress.ip_address(chain[0]))  # reject junk -> ValueError
    except Exception:
        first = ""
    base = first or (peer_ip or "unknown")
    return "%s|%s" % (base, peer_ip or "unknown")


def make_token(scope, ttl_sec):
    """Signed one-time token: scope:expiry:nonce:sig. Verifying populates the
    same fields; signature proves this server issued it."""
    exp = int(time.time()) + ttl_sec
    nonce = secrets.token_hex(16)
    raw = "%s:%d:%s" % (scope, exp, nonce)
    sig = hmac.new(HASH_SECRET.encode("utf-8"), raw.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return "%s:%s" % (raw, sig)


def verify_token(token):
    """Returns the scope on success, None when missing/expired/forged."""
    if not token:
        return None
    try:
        head, sig = token.rsplit(":", 1)
        scope, exp, nonce = head.rsplit(":", 2)
        exp = int(exp)
    except Exception:
        return None
    expected = hmac.new(HASH_SECRET.encode("utf-8"), head.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if exp < time.time():
        return None
    return scope


def ip_token(ip):
    """One-way, salted fingerprint of a client IP for analytics — records who
    clicked without ever storing a raw address."""
    if not ip:
        return ""
    return hashlib.sha256((ip + "|" + HASH_SECRET).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ passwords
_PBKDF2_ITER = 210_000


def hash_password(password):
    """PBKDF2-HMAC-SHA256 with a random 16-byte salt. Format:
    pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex> (self-describing, so the
    iteration count can be raised later without breaking old hashes)."""
    salt = secrets.token_bytes(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), _PBKDF2_ITER).hex()
    return "pbkdf2_sha256$%d$%s$%s" % (_PBKDF2_ITER, salt, h)


def verify_password(password, stored):
    """Constant-time check of a stored hash string; False on junk/None."""
    if not stored or "$" not in stored:
        return False
    try:
        _alg, iters, salt, expect = stored.split("$")
        iters = int(iters)
        if iters < 100_000:  # never accept weakened hashes
            return False
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), iters).hex()
    return hmac.compare_digest(h, expect)


# ------------------------------------------------------------ input validation
# Same rules the client mirrors in server._AUTH_VALIDATE_JS; keep in sync.
_NAME_RE = re.compile(r"^[^<>]{2,120}$")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", re.I)

# Small denylist of the most common weak passwords (lowercase comparison).
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "123456", "12345678", "123456789",
    "1234567890", "qwerty", "qwerty123", "abc123", "123abc", "111111",
    "666666", "letmein", "iloveyou", "admin", "admin123", "welcome",
    "monkey", "dragon", "sunshine", "princess", "football", "baseball",
    "trustno1", "master", "shadow", "passw0rd", "1qaz2wsx", "zaq12wsx",
    "qazwsx", "superman", "login", "prime", "default",
})


def validate_name(name):
    """Return an error string for an unacceptable display name, else None."""
    if not name:
        return "Enter your name."
    if len(name) < 2:
        return "Name must be at least 2 characters."
    if len(name) > 120:
        return "Name must be 120 characters or fewer."
    if not any(c.isalpha() for c in name):
        return "Name must contain at least one letter."
    if any(ord(c) < 32 or c in "<>&" for c in name):
        return "Name contains unsupported characters."
    return None


def validate_email(email):
    """Return an error string for an unacceptable address, else None. Accepts
    subdomains and the `+tag` / hyphen / underscore forms without accepting
    mangled or obviously spammed input."""
    if not email:
        return "Enter your email address."
    e = email.strip()
    if e != email or len(e) > 160:
        return "Enter a valid email address."
    if not _EMAIL_RE.match(e):
        return "Enter a valid email address."
    local, _, domain = e.rpartition("@")
    if len(local) > 64 or ".." in local or ".." in domain or "@" in local:
        return "Enter a valid email address."
    if not local or local[0] in "._-" or local[-1] in "._-":
        return "Enter a valid email address."
    return None


def password_policy_errors(pw, email="", name=""):
    """Return a list of unmet password rules ([] = passes). Checks length,
    character classes, variety, common-password denylist, and that the
    password doesn't echo the name or email."""
    if not pw:
        return ["a password is required"]
    if len(pw) > 128:
        return ["at most 128 characters"]
    low = pw.lower()
    unmet = []
    if len(pw) < 8:
        unmet.append("at least 8 characters")
    if not any(c.islower() for c in pw):
        unmet.append("a lowercase letter")
    if not any(c.isupper() for c in pw):
        unmet.append("an uppercase letter")
    if not any(c.isdigit() for c in pw):
        unmet.append("a number")
    if not any(not c.isalnum() for c in pw):
        unmet.append("a symbol (e.g. !, @, #, -)")
    if low in _COMMON_PASSWORDS:
        unmet.append("a less common password")
    if len(set(pw)) < 4:
        unmet.append("at least 4 different characters")
    base = (email or "").strip().lower().rpartition("@")[0]
    for bad in (base, name):
        b = (bad or "").strip().lower()
        if len(b) >= 4 and b in low:
            unmet.append("not containing your name or email")
            break
    return unmet