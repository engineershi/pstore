# -*- coding: utf-8 -*-
"""pstore short-form video: one 9:16 Shorts frame per social kit.

The YouTube backend has no video asset, so it *renders* one: a 1080x1920
frame that composes the kit's copy (keyword label, headline, stars row,
body caption, hashtags, link) over the same calm brand backdrop family as
the share cards, then ffmpeg turns that single frame into a ~6s Ken Burns
Short MP4 (libx264, yuv420p, faststart) for upload to the Data API.

Renders with the stdlib-only raster toolkit shared with the share cards
(social._raster_text / _blend / _png_encode / …) — no PIL, no fonts, no
third-party deps. Pure function, hermetic in tests (no network, no shell).
"""
import re

import social

W, H = 1080, 1920


def _words(s, limit):
    """Word-wrap to ``limit`` chars/line; over-long words hard-split."""
    lines, cur = [], ""
    for w in re.findall(r"\S+|\s+", str(s or "")):
        if w.isspace():
            continue
        if len(w) > limit:
            if cur:
                lines.append(cur)
                cur = ""
            for i in range(0, len(w), limit):
                lines.append(w[i:i + limit])
        elif not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= limit:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _clean(text):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text or ""))


def _title_from(body):
    """The Shorts headline: the kit body's first meaningful sentence, short."""
    t = _clean(body).strip().splitlines()
    t = " ".join(x.strip() for x in t).strip()
    for cut in (". ", "! ", "? ", ".", "!", "?"):
        i = t.find(cut)
        if 8 <= i < 80:
            t = t[:i + (1 if cut.endswith(" ") else 0)].strip()
            break
    if len(t) > 70:
        t = t[:69].rstrip() + "…"
    return t or "Best picks, ranked"


def frame_png(keyword, body, hashtags, variant=0):
    """Render one 1080x1920 Shorts frame for a social kit (PNG bytes) or
    None on any error. `keyword` seeds the accent label, `body` the headline
    + caption block, `hashtags` the bottom handle row."""
    try:
        t0, t1, soft, accent = social._palette_for(variant)
        img = bytearray(W * H * 3)
        for y in range(H):
            t = y / (H - 1)
            img[y * W * 3:(y + 1) * W * 3] = bytes((
                int(t0[0] + (t1[0] - t0[0]) * t),
                int(t0[1] + (t1[1] - t0[1]) * t),
                int(t0[2] + (t1[2] - t0[2]) * t))) * W
        social._glow(img, W, H, 190, 300, 560, accent, 0.14)
        social._glow(img, W, H, 950, 1650, 620, (86, 204, 255), 0.10)
        social._vignette(img, W, H, 0.14)

        def _stamp(pts, rgb, a=1.0):
            for (px, py) in pts:
                if 0 <= px < W and 0 <= py < H:
                    social._blend(img, W, H, px, py, rgb, a)

        # brand row — quiet, top-left
        social._dot(img, W, H, 96, 120, 8, accent)
        _stamp(social._raster_text("PSTORE", 140, 88, 6, gap=2), social._CREAM)
        pill = "UPDATED DAILY"
        pw = social._text_width(pill, 5, gap=1) + 60
        px0, py0, px1, py1 = 984 - pw, 84, 984, 148
        social._fill_round_rect(img, W, H, px0, py0, px1, py1, 26,
                                (255, 255, 255), 0.06)
        social._ring_round_rect(img, W, H, px0, py0, px1, py1, 26,
                                (255, 255, 255), 0.20, 3)
        social._dot(img, W, H, px0 + 26, 116, 6, social._GREEN)
        _stamp(social._raster_text(pill, px0 + 52, 100, 5, gap=1), social._MUTED)

        # keyword — accent label
        kw = (keyword or "").replace("-", " ").upper()
        kw = kw if len(kw) <= 26 else kw[:25] + "..."
        if kw:
            _stamp(social._raster_text(kw, 96, 268, 8, gap=2), accent)

        # headline — big, up to 4 wrapped lines
        head = _title_from(body)
        for i, ln in enumerate(_words(head.upper(), 14)[:4]):
            ln = ln[:14]
            _stamp(social._raster_text(ln, 96, 400 + i * 96, 11, gap=1),
                   social._WHITE)

        # trust row — accent stars + proof, fixed mid-frame
        y_star = 900
        for i in range(3):
            social._star(img, W, H, 150 + i * 76, y_star, 28, accent)
        _stamp(social._raster_text("TOP PICKS . RANKED FRESH", 420, 880, 5, gap=1),
               social._BODY)

        # calm dotted divider
        for x in range(96, 990, 26):
            social._dot(img, W, H, x, 1060, 4, (255, 255, 255), 0.14)

        # caption block — the kit's real copy
        cap = _clean(body)
        cap_lines = _words(cap, 22)[:10]
        cy = 1115
        for ln in cap_lines:
            ln = ln[:22]
            _stamp(social._raster_text(ln, 96, cy, 6, gap=1), social._WHITE)
            cy += 62
        if len(_words(cap, 22)) > 10:
            _stamp(social._raster_text("...", 96, cy, 6, gap=1), social._CREAM)
            cy += 62

        # bottom pinned — hashtags + link
        tags = " ".join(re.findall(r"#[\w]+", _clean(hashtags)))
        if tags:
            tg = tags if len(tags) <= 60 else tags[:59] + "…"
            _stamp(social._raster_text(tg.upper(), 96, 1760, 5, gap=1), accent)
        lnk = re.search(r"https?://\S+", _clean(body))
        lnk = lnk.group(0) if lnk else ""
        if lnk and len(lnk) > 60:
            lnk = lnk[:59] + "…"
        if lnk:
            _stamp(social._raster_text(lnk.upper(), 96, 1825, 4, gap=1),
                   social._CREAM)

        # two faint plus marks frame the composition
        for (px, py) in ((66, 420), (1014, 470)):
            _stamp(social._raster_text("+", px, py, 3, gap=0),
                   (255, 255, 255), 0.14)

        return social._png_encode(W, H, (bytes(img[i:i + W * 3])
                                         for i in range(0, len(img), W * 3)))
    except Exception:
        return None


# ------------------------------------------------------------------ multi-clip
# A YouTube Short is better as a *sequence* than one held frame: hook, picks,
# proof, CTA.  render_scenes() emits the four 1080x1920 scene frames from the
# same kit copy, and concat_plan()/publish.py splice them into one MP4 with
# xfade transitions.  Everything here is pure + deterministic (hermetic tests).

SCENE_TAGS = ("01 · THE HOOK", "02 · THE PICKS", "03 · WHY TRUST",
              "04 · MAKE YOUR MOVE")


def _sentences(text, cap=90):
    out = []
    for p in re.split(r"(?<=[.!?])\s+", _clean(text)):
        p = p.strip()
        if not p:
            continue
        if len(p) > cap:
            p = p[:cap - 1].rstrip() + "…"
        out.append(p)
    return out


def _hashtags(hashtags, keyword, cap=3):
    tags = re.findall(r"#[\w]+", _clean(hashtags))
    if not tags:
        tags = ["#%s" % w for w in re.findall(r"\w+", _clean(keyword or ""))[:cap]]
    return " ".join(tags[:cap * 3])


def _link_from(body, link=""):
    m = re.search(r"https?://\S+", _clean(body))
    return m.group(0) if m else (link or "")


def build_scenes(keyword, body, hashtags, link=""):
    """Deterministic scene plan (pure copy, no rendering): one dict per clip."""
    sents = _sentences(body)
    title = _title_from(body)
    if len(sents) >= 4:
        picks = sents[1:4]
    elif sents:
        picks = list(sents) + [title, title]
        picks = picks[:3]
    else:
        picks = [title, title, title]
    tags = _hashtags(hashtags, keyword)
    return [
        {"scene": "hook", "tag": SCENE_TAGS[0], "keyword": keyword,
         "title": title, "quote": sents[0] if sents else title},
        {"scene": "picks", "tag": SCENE_TAGS[1], "keyword": keyword,
         "picks": [p for p in picks if p.strip()][:3]},
        {"scene": "proof", "tag": SCENE_TAGS[2], "keyword": keyword,
         "quote": sents[0] if sents else title},
        {"scene": "cta", "tag": SCENE_TAGS[3], "keyword": keyword,
         "hashtags": tags, "link": _link_from(body, link)},
    ]


def _canvas(variant):
    t0, t1, _soft, accent = social._palette_for(variant)
    img = bytearray(W * H * 3)
    for y in range(H):
        t = y / (H - 1)
        img[y * W * 3:(y + 1) * W * 3] = bytes((
            int(t0[0] + (t1[0] - t0[0]) * t),
            int(t0[1] + (t1[1] - t0[1]) * t),
            int(t0[2] + (t1[2] - t0[2]) * t))) * W
    social._glow(img, W, H, 190, 300, 560, accent, 0.14)
    social._glow(img, W, H, 950, 1650, 620, (86, 204, 255), 0.10)
    social._vignette(img, W, H, 0.14)
    return img, accent


def _stamp(img, pts, rgb, a=1.0):
    for (px, py) in pts:
        if 0 <= px < W and 0 <= py < H:
            social._blend(img, W, H, px, py, rgb, a)


def _brand_bar(img, accent, scene_label):
    social._dot(img, W, H, 96, 120, 8, accent)
    _stamp(img, social._raster_text("PSTORE", 140, 88, 6, gap=2), social._CREAM)
    pill = scene_label or ""
    pw = social._text_width(pill, 5, gap=1) + 70
    px0, py0, px1, py1 = 984 - pw, 84, 984, 148
    social._fill_round_rect(img, W, H, px0, py0, px1, py1, 26,
                            (255, 255, 255), 0.06)
    social._ring_round_rect(img, W, H, px0, py0, px1, py1, 26,
                            (255, 255, 255), 0.20, 3)
    social._dot(img, W, H, px0 + 26, 116, 6, social._GREEN)
    _stamp(img, social._raster_text(pill, px0 + 52, 100, 5, gap=1), social._MUTED)


def _keyword_label(img, accent, keyword, y=268, scale=8):
    kw = (keyword or "").replace("-", " ").upper()
    kw = kw if len(kw) <= 26 else kw[:25] + "..."
    if kw:
        _stamp(img, social._raster_text(kw, 96, y, scale, gap=2), accent)


def _center(s, scale, gap=1):
    return (W - social._text_width(s, scale, gap)) // 2


def _scene_hook(s, variant):
    img, accent = _canvas(variant)
    _brand_bar(img, accent, s.get("tag") or SCENE_TAGS[0])
    _keyword_label(img, accent, s.get("keyword"))
    head = _title_from(s.get("title") or "")
    for i, ln in enumerate(_words(head.upper(), 14)[:4]):
        _stamp(img, social._raster_text(ln[:14], 96, 520 + i * 96, 11, gap=1),
               social._WHITE)
    y_star = 1200
    for i in range(3):
        social._star(img, W, H, 180 + i * 92, y_star, 30, accent)
    _stamp(img, social._raster_text("TOP PICKS · RANKED FRESH", 96, 1255, 5,
                                    gap=1), social._BODY)
    for x in range(96, 990, 26):
        social._dot(img, W, H, x, 1390, 4, (255, 255, 255), 0.14)
    quote = (s.get("quote") or "").strip()
    if quote:
        qx = _center(quote, 6)
        _stamp(img, social._raster_text(quote, qx, 1460, 6, gap=1),
               social._WHITE)
    _stamp(img, social._raster_text("UPDATED DAILY", 96, 1760, 5, gap=1),
           social._CREAM)
    return img


def _scene_picks(s, variant):
    img, accent = _canvas(variant)
    _brand_bar(img, accent, s.get("tag") or SCENE_TAGS[1])
    _keyword_label(img, accent, s.get("keyword"), y=236, scale=7)
    head = "THE PICKS"
    _stamp(img, social._raster_text(head, _center(head, 9), 400, 9, gap=1),
           social._WHITE)
    picks = (s.get("picks") or [])[:3]
    y = 600
    for i, pick in enumerate(picks, start=1):
        num = "%02d" % i
        _stamp(img, social._raster_text(num, 110, y, 12, gap=1), accent)
        lines = _words(pick, 30)[:3]
        ly = y + 8
        for ln in lines:
            _stamp(img, social._raster_text(ln, 300, ly, 6, gap=1),
                   social._WHITE)
            ly += 62
        y += 360
        if i < len(picks):
            for x in range(110, 980, 22):
                social._dot(img, W, H, x, y - 108, 3, (255, 255, 255), 0.12)
    return img


def _scene_proof(s, variant):
    img, accent = _canvas(variant)
    _brand_bar(img, accent, s.get("tag") or SCENE_TAGS[2])
    _keyword_label(img, accent, s.get("keyword"), y=236, scale=7)
    head = "WHY TRUST THESE?"
    _stamp(img, social._raster_text(head, _center(head, 9), 400, 9, gap=1),
           social._WHITE)
    y_star = 720
    for i in range(3):
        social._star(img, W, H, 210 + i * 110, y_star, 38, accent)
    quote = (s.get("quote") or "").strip()
    qy = 920
    for ln in _words(quote, 34)[:4]:
        _stamp(img, social._raster_text(ln, _center(ln, 7), qy, 7, gap=1),
               social._BODY)
        qy += 64
    for x in range(96, 990, 26):
        social._dot(img, W, H, x, 1350, 4, (255, 255, 255), 0.14)
    pill = "UPDATED DAILY · RANKED FROM LIVE DATA"
    pw = social._text_width(pill, 6, gap=1) + 90
    px0, py0, px1, py1 = (W - pw) // 2, 1450, (W - pw) // 2 + pw, 1540
    social._fill_round_rect(img, W, H, px0, py0, px1, py1, 45,
                            (255, 255, 255), 0.08)
    social._ring_round_rect(img, W, H, px0, py0, px1, py1, 45,
                            accent, 0.55, 3)
    _stamp(img, social._raster_text(pill, px0 + 45, 1474, 6, gap=1), accent)
    return img


def _scene_cta(s, variant):
    img, accent = _canvas(variant)
    _brand_bar(img, accent, s.get("tag") or SCENE_TAGS[3])
    _keyword_label(img, accent, s.get("keyword"), y=236, scale=7)
    head = "YOUR MOVE"
    _stamp(img, social._raster_text(head, _center(head, 10), 400, 10, gap=1),
           social._WHITE)
    tags = (s.get("hashtags") or "").strip()
    ty = 720
    for ln in _words(tags.upper(), 26)[:3]:
        _stamp(img, social._raster_text(ln, _center(ln, 7), ty, 7, gap=1),
               accent)
        ty += 72
    _stamp(img, social._raster_text("SEE THE FULL RANKED LIST", 96, 1560, 6,
                                    gap=1), social._WHITE)
    lnk = (s.get("link") or "").strip()
    if lnk:
        lk = lnk if len(lnk) <= 58 else lnk[:57] + "…"
        _stamp(img, social._raster_text(lk.upper(), _center(lk, 5), 1640, 5,
                                        gap=1), social._CREAM)
    for (px, py) in ((66, 420), (1014, 470)):
        _stamp(img, social._raster_text("+", px, py, 3, gap=0),
               (255, 255, 255), 0.14)
    return img


def scene_png(scene, variant=0):
    """Render one scene dict from build_scenes() to 1080x1920 PNG bytes, or
    None on any error. Pure + stdlib-only, hermetic in tests."""
    try:
        kind = (scene or {}).get("scene")
        img = {"hook": _scene_hook, "picks": _scene_picks,
               "proof": _scene_proof, "cta": _scene_cta}.get(kind)
        if img is None:
            return None
        img = img(scene, int(variant))
        return social._png_encode(W, H, (bytes(img[i:i + W * 3])
                                         for i in range(0, len(img), W * 3)))
    except Exception:
        return None


def render_scenes(keyword, body, hashtags, variant=0, link=""):
    """All four scene frames as a list of PNG bytes (those that render)."""
    out = []
    for s in build_scenes(keyword, body, hashtags, link):
        png = scene_png(s, variant)
        if png:
            out.append(png)
    return out


def concat_plan(n, seconds=1.8, transition=0.4, fps=25):
    """Pure ffmpeg xfade plan for ``n`` equal-duration clips: per-clip duration,
    transition offset per splice, total runtime, the filter_complex string and
    the final output label. Deterministic and testable without ffmpeg."""
    n = max(1, int(n))
    d = max(0.8, float(seconds))
    t = min(0.8, max(0.15, float(transition)))
    fps = int(fps)
    if n == 1:
        return {"clips": 1, "seconds": d, "fps": fps, "offsets": [],
                "total": round(d, 3), "filter": "[0:v]null[v1]", "last": "v1"}
    offsets = [round(i * (d - t), 3) for i in range(1, n)]
    total = round(n * d - (n - 1) * t, 3)
    chain = []
    prev = "[0:v]"
    label = "v1"
    for i, off in enumerate(offsets, start=1):
        chain.append("%s[%d:v]xfade=transition=fade:duration=%.3f:offset=%.3f[%s]"
                     % (prev, i, t, off, label))
        prev = "[%s]" % label
        label = "v%d" % (i + 1)
    return {"clips": n, "seconds": d, "fps": fps, "offsets": offsets,
            "total": total, "filter": ";".join(chain), "last": label}