# -*- coding: utf-8 -*-
"""Template & style for onepagers: a preset look, advanced style overrides
(custom CSS / accent / font / radius) and a global page feature — the
announcement banner — applied to every /n/ niche page (and its /n/<parent>/<term>
long-tails), with fine targeting: apply to all niches, or only/except a specific
set of keywords or slugs. Deterministic, stdlib-only, offline; every value lives
in the settings table (tpl.*) editable on /admin/template. `for_page()` returns
the injection pack (empty = base look untouched), so pages render unchanged
until the operator opts in."""

import os
import re
import sqlite3

import seo

DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pstore.db")


def _db():
    conn = sqlite3.connect(os.environ.get("PSTORE_DB", DB_DEFAULT))
    conn.row_factory = sqlite3.Row
    return conn


def _setting(key, default=""):
    try:
        conn = _db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()
    except Exception:
        return default


def _set(key, value):
    try:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value or ""))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


PRESETS = {
    "base": {},
    "ocean": {"--accent": "#0a7ea4", "--accent2": "#1359d6",
              "--grad-from": "#0a7ea4", "--grad-to": "#3bb7e0",
              "--ring": "0 0 0 4px rgba(10,126,164,.15)",
              "--ring-soft": "0 0 0 4px rgba(10,126,164,.08)"},
    "forest": {"--accent": "#1f7a4d", "--accent2": "#0f5132",
               "--grad-from": "#1f7a4d", "--grad-to": "#46b56f",
               "--warm": "#d97706",
               "--ring": "0 0 0 4px rgba(31,122,77,.15)",
               "--ring-soft": "0 0 0 4px rgba(31,122,77,.08)"},
    "coral": {"--accent": "#e05d3f", "--accent2": "#b23a48",
              "--grad-from": "#e05d3f", "--grad-to": "#f2855d",
              "--pink": "#e0538f",
              "--ring": "0 0 0 4px rgba(224,93,63,.15)",
              "--ring-soft": "0 0 0 4px rgba(224,93,63,.08)"},
    "violet": {"--accent": "#7c3aed", "--accent2": "#4f46e5",
               "--grad-from": "#7c3aed", "--grad-to": "#a78bfa",
               "--ring": "0 0 0 4px rgba(124,58,237,.15)",
               "--ring-soft": "0 0 0 4px rgba(124,58,237,.08)"},
    "mono": {"--bg": "#fafafa", "--card": "#ffffff", "--text": "#17181c",
             "--muted": "#5b5f6b", "--border": "#e5e6ea", "--line": "#edeef1",
             "--accent": "#17181c", "--accent2": "#4b4f58",
             "--grad-from": "#17181c", "--grad-to": "#565b66",
             "--ring": "0 0 0 4px rgba(23,24,28,.12)",
             "--ring-soft": "0 0 0 4px rgba(23,24,28,.08)"},
}

FONTS = {
    "system": 'font-family:"Segoe UI",system-ui,-apple-system,Arial,sans-serif',
    "serif": 'font-family:Georgia,"Times New Roman",serif',
    "rounded": 'font-family:"Trebuchet MS","Segoe UI",Verdana,sans-serif',
    "sans": 'font-family:Arial,Helvetica,sans-serif',
    "mono": 'font-family:ui-monospace,Menlo,Consolas,monospace',
}

_RADIUS_RE = re.compile(r"^\d+(\.\d+)?(px|em|rem|%)$")


def config():
    return {
        "preset": _setting("tpl.preset", "base") or "base",
        "mode": _setting("tpl.mode", "all") or "all",
        "targets": [t.strip().lower()
                    for t in (_setting("tpl.targets") or "").split(",")
                    if t.strip()],
        "css": _setting("tpl.css"),
        "banner": (_setting("tpl.banner") or "").strip()
        in ("1", "on", "true", "yes"),
        "banner_text": _setting("tpl.banner_text"),
        "banner_link": _setting("tpl.banner_link"),
        "accent": _setting("tpl.accent"),
        "font": _setting("tpl.font"),
        "radius": _setting("tpl.radius"),
    }


def save(**kw):
    mapping = {"preset": "tpl.preset", "mode": "tpl.mode", "targets": "tpl.targets",
               "css": "tpl.css", "banner": "tpl.banner",
               "banner_text": "tpl.banner_text", "banner_link": "tpl.banner_link",
               "accent": "tpl.accent", "font": "tpl.font", "radius": "tpl.radius"}
    for short, key in mapping.items():
        if short not in kw:
            continue
        val = kw[short]
        if short == "targets" and isinstance(val, (list, tuple)):
            val = ",".join(str(v).strip() for v in val)
        if short == "banner":
            val = "1" if str(val).strip().lower() in ("1", "true", "on", "yes") else ""
        _set(key, "" if val is None else str(val))
    return config()


def _applies(cfg, keyword, slug):
    mode = (cfg.get("mode") or "all").lower()
    if mode not in ("only", "except"):
        return True
    targets = [t for t in (cfg.get("targets") or []) if t]
    kw = (keyword or "").strip().lower()
    slug = (slug or "").lower()
    hit = False
    for t in targets:
        if not t:
            continue
        if t == kw:
            hit = True
            break
        try:
            t_slug = seo._slugify(t)
        except Exception:
            t_slug = t
        if t == slug or t_slug == slug:
            hit = True
            break
    return hit if mode == "only" else not hit


def _build_style(cfg):
    over = dict(PRESETS.get(cfg.get("preset") or "base", {}))
    accent = (cfg.get("accent") or "").strip()
    if accent:
        over["--accent"] = accent
    parts = []
    if over:
        parts.append(":root{" + ";".join("%s:%s" % kv for kv in over.items()) + "}")
    font = (cfg.get("font") or "").strip()
    if font in FONTS:
        parts.append("body," + FONTS[font] + ";")
    radius = (cfg.get("radius") or "").strip()
    if radius and _RADIUS_RE.match(radius):
        parts.append(".card,.badge,.pill,.sticky-cta .cta,.key{border-radius:%s}" % radius)
    custom = (cfg.get("css") or "").strip()
    if custom:
        parts.append(custom)
    if cfg.get("banner"):
        parts.append(".tpl-banner{background:linear-gradient(90deg,"
                     "var(--accent,#e8710a),var(--accent2,var(--accent,#e8710a)));"
                     "color:#fff;padding:12px 20px;text-align:center;"
                     "font-weight:600;font-size:15px;letter-spacing:.2px;"
                     "font-family:var(--font-family)}"
                     ".tpl-banner a{color:#fff;text-decoration:underline}")
    if not parts:
        return ""
    return "<style>\n" + "\n".join(parts) + "\n</style>"


def _banner_html(cfg):
    if not cfg.get("banner"):
        return ""
    text = (cfg.get("banner_text") or "").strip() \
        or "Today's top pick, ranked live from Amazon data."
    link = (cfg.get("banner_link") or "").strip()
    inner = seo._clean(text)
    if link and (link.startswith("/") or link.startswith("http")):
        inner = '<a href="%s">%s</a>' % (seo._clean(link), inner)
    return '<aside class="tpl-banner">%s</aside>' % inner


def for_page(keyword, slug=None):
    """Injection pack for one onepager: {"css": <style>…</style>,
    "banner": <aside>…</aside>}. Empty dict when the niche isn't targeted or
    nothing is customized, so base pages keep rendering byte-identical."""
    cfg = config()
    if not _applies(cfg, keyword, slug or seo._slugify(keyword)):
        return {}
    css = _build_style(cfg)
    banner = _banner_html(cfg)
    if not css and not banner:
        return {}
    return {"css": css, "banner": banner}


def target_hits(keywords):
    """Given saved niche keywords, the subset the active targeting selects —
    for the admin page's live "applies to" summary."""
    cfg = config()
    kept = []
    for kw in keywords or []:
        if _applies(cfg, kw, seo._slugify(kw)):
            kept.append(kw)
    return kept