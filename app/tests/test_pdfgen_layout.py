# -*- coding: utf-8 -*-
"""Regression tests for the hand-rolled PDF layout engine.

The bugs these pin were real: the cover and chapter 1 were painted on the same
page, and every width decision used a 0.52*size guess instead of the font's
advance widths, so long headings and two-column rows collided.
"""
import re
import unittest

import pdfgen


class TestMetrics(unittest.TestCase):
    def test_table_covers_printable_ascii(self):
        self.assertEqual(95, len(pdfgen._W_HELV))
        self.assertEqual(95, len(pdfgen._W_BOLD))

    def test_known_helvetica_advances(self):
        # A=667, V=667 per 1000 em -> 13.34pt at 10pt.
        self.assertAlmostEqual(13.34, pdfgen.text_width("AV", 10), places=2)
        # space=278, so "a a" = 556+278+556 = 1390/1000*10.
        self.assertAlmostEqual(13.90, pdfgen.text_width("a a", 10), places=2)

    def test_bold_is_wider_than_regular(self):
        self.assertGreater(pdfgen.text_width("Hello", 12, bold=True),
                           pdfgen.text_width("Hello", 12))

    def test_accented_letters_use_base_glyph(self):
        self.assertAlmostEqual(pdfgen.text_width("caf\u00e9", 10),
                               pdfgen.text_width("cafe", 10), places=4)


class TestWrap(unittest.TestCase):
    def assert_fits(self, lines, width, size, bold=False):
        for line in lines:
            self.assertLessEqual(pdfgen.text_width(line, size, bold),
                                 width + 1e-6, "line overflows: %r" % line)

    def test_long_heading_wraps_inside_width(self):
        text = ("NICHE VALIDATION REPORT FOR KITCHEN APPLIANCES AND SMALL "
                "ELECTRONICS")
        lines = pdfgen._wrap(text, 200, 20, bold=True)
        self.assertGreater(len(lines), 1)
        self.assert_fits(lines, 200, 20, bold=True)

    def test_overlong_word_is_split_not_overflowed(self):
        lines = pdfgen._wrap("A" * 200, 100, 12)
        self.assertGreater(len(lines), 1)
        self.assert_fits(lines, 100, 12)

    def test_explicit_newlines_are_preserved(self):
        self.assertEqual(["a", "b"], pdfgen._wrap("a\nb", 100, 10))

    def test_empty_text_is_safe(self):
        self.assertEqual([""], pdfgen._wrap("", 100, 10))


class TestPagination(unittest.TestCase):
    def test_cover_never_shares_a_page(self):
        doc = pdfgen.Pdf()
        doc.cover("Niche Validation: Kitchen Appliances", "A decision.")
        doc.heading("Chapter one")
        doc.save()
        self.assertEqual(2, len(doc._pages))
        self.assertNotIn("CHAPTER ONE", doc._pages[0])
        self.assertIn("CHAPTER ONE", doc._pages[1])

    def test_chapter_title_wraps_and_advances_cursor(self):
        doc = pdfgen.Pdf()
        before = doc.y
        doc.chapter(1, "A very long chapter title " * 4)
        self.assertLess(doc.y, before - 40)

    def test_heading_does_not_paint_past_the_right_margin(self):
        doc = pdfgen.Pdf()
        doc.heading("A" * 120, size=21)
        for op in doc._content:
            m = re.search(r"\((.*)\) Tj ET", op)
            if not m:
                continue
            self.assertLessEqual(pdfgen.text_width(m.group(1), 21, bold=True),
                                 doc.body_w - 16 + 1e-6)


class TestPreciseAlignment(unittest.TestCase):
    def _x_of_last_text(self, doc):
        m = re.search(r"([0-9.]+) ([0-9.]+) Td", doc._content[-1])
        return float(m.group(1))

    def test_right_alignment_uses_real_advance_width(self):
        doc = pdfgen.Pdf()
        doc.text("HELLO", 0, 100, 20, doc.ink, bold=True, align="right")
        expected = doc.w - doc.margin_x - pdfgen.text_width("HELLO", 20, True)
        self.assertAlmostEqual(expected, self._x_of_last_text(doc), places=2)

    def test_centre_alignment_uses_real_advance_width(self):
        doc = pdfgen.Pdf()
        doc.text("HELLO", 0, 100, 20, doc.ink, bold=True, align="center",
                 cx=200)
        expected = 200 - pdfgen.text_width("HELLO", 20, True) / 2
        self.assertAlmostEqual(expected, self._x_of_last_text(doc), places=2)

    def test_two_col_wraps_both_columns(self):
        doc = pdfgen.Pdf()
        before = doc.y
        doc.two_col([("A label long enough to need a second line in its column",
                      "A value long enough to need a second line in its column")])
        self.assertLessEqual(doc.y, before - 30)

    def test_checkbox_label_wraps_without_overflow(self):
        doc = pdfgen.Pdf()
        before = doc.y
        doc.checkbox("agree", "word " * 40)
        self.assertLess(doc.y, before - 20)


if __name__ == "__main__":
    unittest.main()
