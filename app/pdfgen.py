# -*- coding: utf-8 -*-
"""Book-profile PDF writer — pure stdlib, everything drawn by hand.

No reportlab, no pillow. A PDF is just a text format, so we emit exactly the
objects we need: a trade-book page profile (6x9 in by default), calming paper
with gradient frames and footers, vector diagrams (rounded panels, gradients,
chevrons, arrows, circles, bar charts), clickable links (external URI + internal
GoTo for the table of contents) and tickable AcroForm checkboxes.

Coordinates are bottom-up (like a PDF page): y grows toward the top, text
baselines are at y, and the page content ends at the footer near y=34.

Layout uses the real Adobe Helvetica advance widths (the same numbers an AFM
carries), not an average-character-width guess, so wrapping and right/centre
alignment land where the glyphs actually end.

`latin-1` output keeps bytes small and deterministic for tests.
"""
import unicodedata

_SAFE = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
         "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2192": "->",
         "\u00b7": ".", "\u2022": "*", "\u2605": "*", "\u2b50": "*"}

# Helvetica / Helvetica-Bold advance widths for ASCII 32..126, in 1/1000 em.
# Source: the standard Adobe AFM metrics (Helvetica.afm, Helvetica-Bold.afm).
_W_HELV = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
    278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
    584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
    500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
    278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)
_W_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333,
    278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333,
    584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
    556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556,
    333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
    333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584)


def _char_width(ch, size, bold=False):
    o = ord(ch)
    if 32 <= o <= 126:
        return (_W_BOLD if bold else _W_HELV)[o - 32] * size / 1000.0
    base = unicodedata.normalize("NFKD", ch)[:1]
    if base and 32 <= ord(base) <= 126:
        return _char_width(base, size, bold)
    return 0.556 * size


def text_width(s, size, bold=False, italic=False):
    """Rendered width in points. Oblique shares Helvetica's advances."""
    return sum(_char_width(c, size, bold) for c in _safe(s))


def _split_long(word, width, size, bold):
    """Break a single word that cannot fit, so it never bleeds past the margin."""
    out, cur = [], ""
    for ch in word:
        if not cur or text_width(cur + ch, size, bold) <= width:
            cur += ch
        else:
            out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


def _safe(t):
    out = []
    for ch in (t or "").replace("\r", " "):
        if ch in _SAFE:
            out.append(_SAFE[ch])
        elif ord(ch) < 256:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def _esc(s):
    return _safe(s).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _n(*nums):
    return " ".join("%0.2f" % n for n in nums)


def _wrap(text, width, size, bold=False, italic=False):
    """Word-wrap using real glyph advances.

    Honours explicit newlines, breaks over-long words instead of letting them
    run past the margin, and measures with the same font that text() draws.
    """
    width = max(4.0, float(width))
    lines = []
    for para in _safe(text).split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            pieces = ([word] if text_width(word, size, bold, italic) <= width
                      else _split_long(word, width, size, bold))
            for piece in pieces:
                cand = piece if not cur else cur + " " + piece
                if not cur or text_width(cand, size, bold, italic) <= width:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = piece
        if cur:
            lines.append(cur)
    return lines


def _mix(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


class Pdf:
    """One page at a time. cover(), heading(), chapter(), paragraph(),
    bullets(), pullquote(), spacer(), diagrams + links, then save() -> PDF."""

    def __init__(self, page_w=432, page_h=648, accent=(255, 107, 44), bg=(255, 253, 247),
                 ink=(45, 40, 52), muted=(150, 140, 155),
                 footer="PSTORE \u00b7 USER GUIDE"):
        """6x9 in book profile by default; pass A4 (595x842) for letter/A4."""
        self.w, self.h = page_w, page_h
        self.accent, self.bg = accent, bg
        self.ink, self.muted = ink, muted
        self.footer = footer
        self.margin_x = 48
        self.body_w = self.w - 96
        self.margin_y = 42
        self._pages = []       # finalized content streams (draw order)
        self._links = []       # finalised annotations, one list per page
        self._content = []
        self._cur_links = []
        self._plain = False    # True on the cover (no auto frame/footer)
        self._framed = False
        self._fields = []      # AcroForm field names
        self.y = self.h - 96

    # ------------------------------------------------------------- internals
    def _c(self, s):
        self._content.append(s)

    def _rgb(self, c):
        return "%0.3f %0.3f %0.3f" % (c[0] / 255, c[1] / 255, c[2] / 255)

    def _sync(self):
        """Frame the current page before anything is drawn on it.

        A cover is a one-shot page: the first element after it starts a fresh
        page automatically. Without this, cover() and chapter 1 were painted on
        the same sheet and the two text blocks interleaved.
        """
        if self._plain and self._content:
            self.page_break()
        if not self._content and not self._framed and not self._plain:
            self._frame()
            self._framed = True

    def _frame(self):
        """Calm, professional page furniture: cream paper, soft top wash,
        accent hairline, a corner dot motif, and a footer rule + running head."""
        self.rect(0, 0, self.w, self.h, self.bg)
        self.grad(0, self.h - 72, self.w, 72, (255, 236, 226), self.bg)
        self.rect(0, self.h - 75, self.w, 3, self.accent)
        self.rect(0, self.h - 78.5, self.w, 0.6, (238, 226, 220))
        self.circle(self.w - 54, self.h - 40, 3, self.accent)
        self.circle(self.w - 42, self.h - 40, 1.6, (214, 186, 172))

    def _flush(self, force=False, footer=True):
        content = self._content or ([] if not force else [])
        if force and footer:
            self.line(self.margin_x, 34, self.w - self.margin_x, 34, (224, 214, 222), 0.6)
            self.text(_esc(self.footer), self.margin_x, 27, 7.5, self.muted)
            self.text(str(len(self._pages) + 1), self.w - self.margin_x, 27, 8,
                      self.accent, bold=True, align="right")
        self._pages.append("\n".join(content) if content else "")
        self._links.append(list(self._cur_links))
        self._content = []
        self._cur_links = []
        self._framed = False
        self._plain = False
        self.y = self.h - 96

    def ensure(self, need):
        if self.y - need < self.margin_y:
            self.page_break()
        self._sync()

    def new_page(self):
        self.page_break()

    def page_break(self, decorated=True):
        if self._content:
            self._flush(force=True, footer=not self._plain)

    def current_page(self):
        """0-based index of the page currently being drawn."""
        return len(self._pages)

    # -------------------------------------------------------------- cover
    def cover(self, title, subtitle, kicker=None, owner=None, site=None):
        """A warm, calm book cover: sunset gradient wedge, floating dot motif,
        brand chip, title, subtitle, the four-phase strip and founder credit —
        each pinned so nothing ever overlaps."""
        self._plain = True
        w, h = self.w, self.h
        self.rect(0, 0, w, h, self.bg)
        self.grad(0, h - 215, w, 215, (92, 48, 42), self.bg)
        self.rect(0, h - 215, w, 4, self.accent)
        self.circle(w - 92, h - 100, 30, (250, 208, 190))
        self.ring(w - 92, h - 100, 38, (236, 196, 180), 1.6)
        self.circle(w - 56, h - 152, 7, (210, 170, 154))
        self.circle(58, h - 112, 9, (244, 198, 180))
        self.circle(40, h - 170, 5, (228, 182, 166))
        self.chip(48, h - 254, 92, 22, self.accent, label="PSTORE", size=9)
        # Measured text block: the subtitle's last baseline is anchored just
        # above the four-phase strip (126..  - the strip's top), so title and
        # subtitle can have ANY number of lines and still never collide.
        tsize, tadv = 30, 41
        ssize, slead = 12, 19
        t_w, s_w = int(self.body_w * 0.94), int(self.body_w * 0.9)
        nT = len(_wrap(title or "", t_w, tsize, bold=True))
        nS = len(_wrap(subtitle or "", s_w, ssize))
        sub_last = 170  # baseline of the subtitle's final line
        top = sub_last + (nS - 1) * slead + 4 + nT * tadv + (22 if kicker else 0)
        if top > 380:  # very long copy: step down sizes once
            tsize, tadv, ssize, slead = 28, 37, 11, 17
            nT = len(_wrap(title or "", t_w, tsize, bold=True))
            nS = len(_wrap(subtitle or "", s_w, ssize))
            top = sub_last + (nS - 1) * slead + 4 + nT * tadv + (22 if kicker else 0)
        if top > 385:  # still tall: let the strip sit a little lower instead
            sub_last = 144
            top = sub_last + (nS - 1) * slead + 4 + nT * tadv + (22 if kicker else 0)
        # first title baseline sits `top`; draw downward from there
        y = top
        if kicker:
            self.text(_esc(kicker).upper(), 48, y + 22, 10, (150, 96, 74), bold=True)
        self.rect(48, y + 6, 34, 2.4, self.accent)
        for i, line in enumerate(_wrap(title or "", t_w, tsize, bold=True)):
            self.text(line, 48, y, tsize, self.ink, bold=True)
            y -= tadv
        y -= 4
        for line in _wrap(subtitle or "", s_w, ssize):
            self.text(line, 48, y, ssize, (130, 100, 90))
            y -= slead
        # the four-phase strip: sits a fixed clearance below the subtitle
        cw = (self.body_w - 12) / 4
        strip_y = sub_last - 26 if sub_last <= 152 else sub_last - 38
        for i, label in enumerate(("1 ATTRACT", "2 CONVERT", "3 DELIVER", "4 MULTIPLY")):
            self.chip(48 + i * (cw + 4), strip_y, cw, 26, (120, 150, 135),
                      label=label, size=7.5)
        # founder credit, pinned near the bottom
        self.hline(72, 48, w - 48, (224, 214, 222), 0.8)
        self.text("BUILT FROM SCRATCH BY", 48, 56, 8, (150, 140, 155), bold=True)
        self.text(owner or "The founder and engineer", 48, 40, 13, self.ink, bold=True)
        self.text(site or "A free guide from pstore", 48, 26, 8.5, self.muted)

    def _toc_row(self, num, label, page_no, target_idx):
        """One clickable contents row (label, dot leader, page number)."""
        x = self.margin_x
        width = text_width(label, 12)
        label_y = self.y - 10
        self.text(label, x, label_y, 12, self.ink)
        if width < self.body_w - 60:
            self.graddot(x + width + 10, self.w - self.margin_x - 30, label_y + 2)
        self.text(str(page_no), self.w - self.margin_x, label_y, 12, self.accent,
                  bold=True, align="right")
        self.goto(x - 6, self.y - 15, self.body_w + 12, 16, target_idx, self.h - 30)
        self.y -= 19

    # ---------------------------------------------------------- primitives
    def rect(self, x, y, w, h, rgb, r=0):
        path = self._round(x, y, w, h, r) if r else "%s re" % _n(x, y, w, h)
        self._c("q %s rg %s f Q" % (self._rgb(rgb), path))

    def rect_o(self, x, y, w, h, rgb, r=0, width=1, fill=None):
        path = self._round(x, y, w, h, r) if r else "%s re" % _n(x, y, w, h)
        pre = "q %s rg %s f " % (self._rgb(fill), path) if fill else ""
        self._c("%sq %s RG %0.2f w %s S Q" % (pre, self._rgb(rgb), width, path))

    @staticmethod
    def _round(x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        if r <= 0:
            return "%s re" % _n(x, y, w, h)
        k = 0.5523 * r
        return " ".join([
            "%s m" % _n(x + r, y),
            "%s l" % _n(x + w - r, y),
            "%s c" % _n(x + w - r + k, y, x + w, y + r - k, x + w, y + r),
            "%s l" % _n(x + w, y + h - r),
            "%s c" % _n(x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h),
            "%s l" % _n(x + r, y + h),
            "%s c" % _n(x + r - k, y + h, x, y + h - r + k, x, y + h - r),
            "%s l" % _n(x, y + r),
            "%s c" % _n(x, y + r - k, x + r - k, y, x + r, y),
        ])

    @staticmethod
    def _arc(cx, cy, r):
        k = 0.5522847498307936 * r
        return "%s m %s c %s c %s c %s c" % (
            _n(cx + r, cy),
            _n(cx + r, cy + k, cx + k, cy + r, cx, cy + r),
            _n(cx - k, cy + r, cx - r, cy + k, cx - r, cy),
            _n(cx - r, cy - k, cx - k, cy - r, cx, cy - r),
            _n(cx + k, cy - r, cx + r, cy - k, cx + r, cy))

    def circle(self, cx, cy, r, rgb):
        self._c("q %s rg %s h f Q" % (self._rgb(rgb), self._arc(cx, cy, r)))

    def ring(self, cx, cy, r, rgb, width=1.4):
        self._c("q %s RG %0.2f w %s h S Q" % (self._rgb(rgb), width, self._arc(cx, cy, r)))

    def line(self, x1, y1, x2, y2, rgb, width=1):
        self._c("q %s RG %0.2f w %s m %s l S Q" % (self._rgb(rgb), width, _n(x1, y1), _n(x2, y2)))

    def hline(self, y, x1, x2, rgb, width=1):
        self.line(x1, y, x2, y, rgb, width)

    def vline(self, x, y1, y2, rgb, width=1):
        self.line(x, y1, x, y2, rgb, width)

    def dash_line(self, x1, y, x2, color=(214, 186, 172), width=0.8):
        self._c("q %s RG %0.2f w [2.4 3] 0 d %s m %s l S Q" % (self._rgb(color), width, _n(x1, y), _n(x2, y)))

    def graddot(self, x1, x2, y, color=(214, 186, 172), step=7, r=0.55):
        d = x2 - x1
        n = max(1, int(d / step))
        for i in range(n):
            self.circle(x1 + d * (i + 0.5) / n, y, r, color)

    def grad(self, x, y, w, h, c1, c2, steps=None, horizontal=False):
        n = steps or max(2, int((h if not horizontal else w) / 3))
        for i in range(n):
            t = i / (n - 1)
            c = _mix(c1, c2, t)
            if horizontal:
                sw = w / n
                self.rect(x + i * sw, y, sw + 0.6, h, c)
            else:
                sh = h / n
                self.rect(x, y + i * sh, w, sh + 0.6, c)

    def chevron(self, x, y, w, h, rgb, flip=False):
        """A right-pointing '>' chevron (or mirrored). Filled polygon."""
        m = min(w * 0.42, h * 0.6)
        pts = [(x, y), (x + w - m, y), (x + w, y + h / 2),
               (x + w - m, y + h), (x, y + h)]
        if flip:
            pts = [(x + w - px, py) for px, py in pts]
        parts = ["%s m" % _n(*pts[0])]
        parts.extend("%s l" % _n(*p) for p in pts[1:])
        self._c("q %s rg %s h f Q" % (self._rgb(rgb), " ".join(parts)))

    def arrow(self, x1, y, x2, rgb, width=2, head=8):
        self.line(x1, y, x2 - head, y, rgb, width)
        hh = head * 0.42
        self._c("q %s rg %s m %s l %s l h f Q" % (
            self._rgb(rgb), _n(x2, y), _n(x2 - head, y - hh), _n(x2 - head, y + hh)))

    def panel(self, x, y, w, h, rgb, r=10):
        self.rect(x, y, w, h, rgb, r=r)

    def chip(self, x, y, w, h, rgb, label=None, label_color=(255, 255, 255), size=8):
        self.rect(x, y, w, h, rgb, r=min(6, h / 2))
        if label:
            self.text(_esc(label), x + w / 2, y + h / 2 - size * 0.34, size,
                      label_color, bold=True, align="center", cx=x + w / 2)

    # ------------------------------------------------------------- text
    def text(self, s, x, y, size, color, bold=False, align="left", italic=False, cx=None):
        family = "/F2" if bold else ("/F3" if italic else "/F1")
        if align in ("right", "center"):
            width = text_width(s, size, bold, italic)
            if align == "right":
                x = self.w - self.margin_x - width
            else:
                x = (cx if cx is not None else self.margin_x + self.body_w / 2) - width / 2
        s = _esc(s)
        self._c("BT %s %d Tf %s rg %s Td (%s) Tj ET"
                % (family, size, self._rgb(color), _n(x, y), s))

    # ---------------------------------------------------------- elements
    def kicker(self, text, y=None):
        self._sync()
        self.text(_esc(text).upper(), self.margin_x, y or self.y, 8.5, self.accent, bold=True)
        self.rect(self.margin_x, (y or self.y) - 6, 34, 2.2, self.accent, r=1)

    def heading(self, title, size=21):
        lines = _wrap((title or "").upper(), self.body_w - 16, size, bold=True)
        leading = size + 3
        self.ensure(max(44, len(lines) * leading + 12))
        # Draw inside the cursor block (never above it) so a heading that lands
        # at the top of a page cannot paint into the header furniture.
        self.rect(self.margin_x, self.y - size - 2, 6, size + 2, self.accent, r=3)
        ty = self.y - size
        for line in lines:
            self.text(line, self.margin_x + 16, ty, size, self.ink, bold=True)
            ty -= leading
        self.y -= (len(lines) - 1) * leading + size + 16

    def chapter(self, num, title, blurb=None, size=20):
        lines = _wrap((title or "").upper(), self.body_w - 44, size, bold=True)
        leading = size + 3
        block = max(38, (len(lines) - 1) * leading + size + 14)
        self.ensure(block + 16)
        top = self.y
        # Everything descends from the cursor; the badge sits flush with the
        # first title line instead of protruding into the page header.
        self.rect(self.margin_x, top - 34, 34, 34, self.accent, r=9)
        self.text(str(num), self.margin_x + 17, top - 30, 15,
                  (255, 255, 255), bold=True)
        ty = top - size * 0.75
        for line in lines:
            self.text(line, self.margin_x + 44, ty, size, self.ink, bold=True)
            ty -= leading
        self.y = top - block
        if blurb:
            self.paragraph(blurb, size=11.5, leading=16, color=self.muted)
        self.hline(self.y + 4, self.margin_x, self.w - self.margin_x,
                   (224, 214, 222), 0.8)
        self.y -= 12

    def paragraph(self, text, size=12, leading=17, color=None):
        color = color or self.ink
        self.ensure(leading * 2)
        for line in _wrap(text, self.body_w, size):
            self.ensure(leading)
            self.text(line, self.margin_x, self.y, size, color)
            self.y -= leading
        self.y -= 4

    def bullets(self, items, size=12, leading=17, color=None):
        color = color or self.ink
        for it in (items or []):
            self.ensure(leading * 2)
            self.circle(self.margin_x + 4, self.y - 0.5 * size + 3.5, 1.7, self.accent)
            for line in _wrap(" " + str(it), self.body_w - 20, size):
                self.ensure(leading)
                self.text(line, self.margin_x + 15, self.y, size, color)
                self.y -= leading
            self.y -= 3

    def pullquote(self, text, size=15, color=(120, 70, 44), bg=(255, 247, 240)):
        qw = self.body_w - 20
        lines = _wrap(text, qw, size, italic=True)
        line_h = size + 8
        box_h = max(1, len(lines)) * line_h + 22
        # Break first, then draw: a quote taller than the page must never be
        # pinned off the bottom edge. The box opens at the cursor and descends.
        self.ensure(box_h + 10)
        self.rect(self.margin_x, self.y - box_h, self.body_w, box_h, bg, r=14)
        self.rect(self.margin_x, self.y - box_h + 8, 5, box_h - 16, self.accent, r=3)
        ty = self.y - 18
        for line in lines:
            self.text(line, self.margin_x + 26, ty, size, color, italic=True)
            ty -= line_h
        self.y -= box_h + 10

    def two_col(self, rows, size=11, leading=15, gap=18):
        col_w = (self.body_w - gap) / 2
        for left, right in rows:
            ll = _wrap(str(left), col_w, size, bold=True)
            rl = _wrap(str(right), col_w, size)
            n = max(len(ll), len(rl), 1)
            self.ensure(n * leading + 4)
            y0 = self.y
            for i, line in enumerate(ll):
                self.text(line, self.margin_x, y0 - i * leading, size,
                          self.ink, bold=True)
            for i, line in enumerate(rl):
                self.text(line, self.margin_x + col_w + gap,
                          y0 - i * leading, size, self.ink)
            self.y -= n * leading
        self.y -= 6

    def spacer(self, n=10):
        self.y -= n

    # ------------------------------------------------------- diagrams
    def flow_chart(self, boxes, y, gap=8):
        """boxes = [(label, rgb)] drawn left to right with arrows."""
        self.ensure(64)
        slot = self.body_w / len(boxes)
        bw = min(slot - gap, 62)
        top = y
        bottom = y - 34
        for i, (label, rgb) in enumerate(boxes):
            x = self.margin_x + (slot - bw) / 2 + i * slot
            self.rect(x, bottom, bw, 34, rgb, r=9)
            lines = _wrap(label, bw - 8, 8.5, bold=True)[:2]
            lh = 9.5
            ty = bottom + (34 - (len(lines) - 1) * lh) / 2 - 3
            for line in lines:
                self.text(line, x + bw / 2, ty, 8.5, (255, 255, 255),
                          bold=True, align="center", cx=x + bw / 2)
                ty -= lh
            if i < len(boxes) - 1:
                self.arrow(x + bw + 2, bottom + 17, x + slot - 4, self.muted,
                           width=1.6, head=5)
        self.y = bottom - 12

    def bar_chart(self, x, y, w, h, values, labels, colors, title=None):
        maxv = max(values) or 1
        n = len(values)
        slot = w / n
        bw = slot * 0.56
        label_lines = [_wrap(str(lab), slot - 4, 7.5) for lab in labels]
        label_max = max((len(ls) for ls in label_lines), default=0)
        self.ensure(h + 20 + label_max * 9)
        base = y + h
        self.hline(base - 1, x, x + w, (210, 200, 210), 0.8)
        for i, v in enumerate(values):
            bh = max(3, (v / maxv) * (h - 8))
            cx = x + slot * i + (slot - bw) / 2
            self.rect(cx, base - bh, bw, bh, colors[i], r=2.5)
            self.text("%0.1f" % v, cx + bw / 2, base + 3, 8, self.muted, align="center", cx=cx + bw / 2)
            ly = base - bh - 11
            for line in label_lines[i]:
                self.text(line, cx + bw / 2, ly, 7.5, self.ink,
                          align="center", cx=cx + bw / 2)
                ly -= 9
        self.y = y - 11 - label_max * 9 - 6
        if title:
            for line in _wrap(title, w, 9.5, italic=True):
                self.text(line, x, self.y, 9.5, self.ink, italic=True)
                self.y -= 12
            self.y -= 2

    # -------------------------------------------------- links & checkboxes
    def uri(self, x, y, w, h, url):
        self._sync()
        self._cur_links.append(("uri", (x, y, w, h), _safe(url)))

    def goto(self, x, y, w, h, page_index, top_y):
        self._sync()
        self._cur_links.append(("goto", (x, y, w, h), (page_index, top_y)))

    def checkbox(self, name, label, x=None, size=11, checked=False, size_pt=9.5):
        """A tickable form checkbox on its own row (the cursor advances by itself)."""
        lines = _wrap(label, self.body_w - (size + 7), size_pt)
        extra = max(0, len(lines) - 1) * (size_pt + 2)
        self.ensure(size + 8 + extra)
        x = self.margin_x + 4 if x is None else x
        y = self.y - 12
        self.rect(x, y, size, size, (255, 255, 255), r=3)
        self.rect_o(x, y, size, size, self.accent, r=3, width=1)
        if checked:
            self.line(x + 2.4, y + size / 2, x + 4.6, y + size - 3.4, self.accent, 1.6)
            self.line(x + 4.6, y + size - 3.4, x + size - 1.6, y + 2.6, self.accent, 1.6)
        ty = y + size * 0.36
        for line in lines:
            self.text(line, x + size + 7, ty, size_pt, self.ink)
            ty -= size_pt + 2
        self._cur_links.append(("check", (x, y, size, size), _safe(name), bool(checked)))
        self._fields.append(_safe(name))
        self.y -= size + 7 + extra

    # -------------------------------------------------------------- assembly
    def save(self):
        if self._content:
            self._flush(force=True)
        n = len(self._pages)
        f1, f2, f3 = 3 + n, 4 + n, 5 + n
        stream_base = 6 + n
        page_annots = [a for a in self._links]          # annots per page
        total_annots = sum(len(a) for a in page_annots)
        ap_off = 6 + 2 * n + total_annots
        ap_on = ap_off + 1

        # annotation bodies + object numbers (assigned in draw order)
        ann_bodies, ann_pages, field_refs = [], [], []
        next_ann = 6 + 2 * n
        for pi, page_links in enumerate(page_annots):
            refs = []
            for item in page_links:
                if item[0] == "uri":
                    (x, y, w, h), url = item[1], item[2]
                    body = (b"<< /Type /Annot /Subtype /Link /Border [ 0 0 0 ] "
                            b"/Rect [ %s ] /A << /S /URI /URI (%s) >> >>"
                            % (_n(x, y, x + w, y + h).encode("latin-1"),
                               url.encode("latin-1", "replace")))
                elif item[0] == "goto":
                    (x, y, w, h), (pidx, top_y) = item[1], item[2]
                    body = (b"<< /Type /Annot /Subtype /Link /Border [ 0 0 0 ] "
                            b"/Rect [ %s ] /Dest [ %d 0 R /XYZ null %0.2f null ] >>"
                            % (_n(x, y, x + w, y + h).encode("latin-1"),
                               3 + pidx, top_y))
                else:  # checkbox widget
                    (x, y, w, h), name, checked = item[1], item[2], item[3]
                    state = "/Yes" if checked else "/Off"
                    body = (b"<< /Type /Annot /Subtype /Widget /FT /Btn "
                            b"/T (%s) /Rect [ %s ] /AS %s /V %s /DV %s "
                            b"/BS <</S /S /W 1>> /MK <</BG [ 1 1 1 ] /BC [ 1 1 1 ]>> "
                            b"/AP <</N <</Off %d 0 R /Yes %d 0 R>>>> >>"
                            % (name.encode("latin-1", "replace"),
                               _n(x, y, x + w, y + h).encode("latin-1"),
                               state.encode("latin-1"), state.encode("latin-1"),
                               state.encode("latin-1"), ap_off, ap_on))
                    field_refs.append(next_ann)
                ann_bodies.append(body)
                refs.append(next_ann)
                next_ann += 1
            ann_pages.append(refs)

        # appearance streams for checked / unchecked checkboxes (11x11 cell)
        accent_rgb = self._rgb(self.accent)
        off_app = ("1 1 1 rg\n0 0 11 11 re f\n%s RG\n0.7 w\n"
                   "0.6 0.6 m 10.4 0.6 l 10.4 10.4 l 0.6 10.4 l h S" % accent_rgb)
        on_app = ("%s rg\n0 0 11 11 re f\n1 1 1 rg\n%s RG\n2 w\n"
                  "2.4 6.2 m 4.8 8.8 l 8.8 3.2 l S" % (accent_rgb, accent_rgb))

        def xo(ops):
            data = b"q\n" + ops.encode("latin-1") + b"\nQ"
            return (b"<< /Type /XObject /Subtype /Form /BBox [ 0 0 11 11 ] "
                    b"/Resources << >> /Length %d >>stream\n%s\nendstream"
                    % (len(data), data))

        objs = []
        acro = ""
        if field_refs:
            fields = " ".join("%d 0 R" % r for r in field_refs)
            acro = (" /AcroForm <</Fields [ %s ] /DR << /F1 %d 0 R >> "
                    "/DA (/F1 9 Tf 0.20 0.20 0.20 rg)>>" % (fields, f1))
        objs.append(b"<< /Type /Catalog /Pages 2 0 R%s /ViewerPreferences "
                    b"<< /DisplayDocTitle true >> >>" % acro.encode("latin-1"))              # 1
        kids = " ".join("%d 0 R" % (3 + i) for i in range(n))
        objs.append(b"<< /Type /Pages /Kids [ %s ] /Count %d >>"
                    % (kids.encode("latin-1"), n))                          # 2
        for i in range(n):                                                  # pages
            st = stream_base + i
            annots = (b" /Annots [ %s ]" % (" ".join("%d 0 R" % r for r in ann_pages[i])).encode("latin-1")
                      if ann_pages[i] else b"")
            objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [ 0 0 %d %d ] "
                        b"/Contents %d 0 R /Resources << /Font << /F1 %d 0 R "
                        b"/F2 %d 0 R /F3 %d 0 R >> >>%s >>"
                        % (self.w, self.h, st, f1, f2, f3, annots))
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")          # F1
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")     # F2
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>")  # F3
        for page in self._pages:                                            # streams
            data = page.encode("latin-1", "replace")
            objs.append(b"<< /Length %d >>stream\n%s\nendstream" % (len(data), data))
        objs.extend(ann_bodies)                                             # annotations
        objs.append(xo(off_app))                                            # Off appearance
        objs.append(xo(on_app))                                             # Yes appearance

        out = bytearray()
        out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        offsets = [0]
        for idx, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % idx
            out += o
            out += b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n" % (len(objs) + 1)
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += b"%010d 00000 n \n" % off
        out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, xref))
        return bytes(out)