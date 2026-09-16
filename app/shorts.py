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