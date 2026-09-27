"""Regression tests for CMS/AI copy hygiene.

Two defects shipped to production before these tests existed:

1. ``cms_render._esc`` was a bare ``html.escape``, so LLM-authored copy that
   ignored the "plain text, no markdown" prompt rendered literal ``**`` in the
   live <h1> and body copy (observed on /lp/keto).
2. ``ai._clean`` stripped quotes before markdown, so ``**"Headline**`` was
   stored with a dangling leading quote, and long headlines were cut mid-word
   ("...Without Starving—Try T").

Fixing the AI prompt alone would not help: rows already in the DB carry the bad
markup, so the strip has to happen at the render boundary too.
"""
import unittest

import ai
import cms_render as cr


class TestStripMarkdown(unittest.TestCase):
    def test_bold_and_italic(self):
        self.assertEqual(cr.strip_markdown("**bold** and *em*"), "bold and em")
        self.assertEqual(cr.strip_markdown("__underbold__"), "underbold")
        self.assertEqual(cr.strip_markdown("***triple***"), "triple")

    def test_live_broken_headline_is_repaired(self):
        """The exact string served live before the fix."""
        broken = '**"Keto Hacks That Actually Work: Lose Fat Without Starving—Try T'
        out = cr._esc(broken)
        self.assertNotIn("**", out)
        self.assertTrue(out.startswith("&quot;Keto Hacks"))

    def test_links_and_images(self):
        self.assertEqual(cr.strip_markdown("[Buy now](https://amzn.to/x)"), "Buy now")
        self.assertEqual(cr.strip_markdown("![alt](http://x/y.png)"), "alt")

    def test_block_markers(self):
        self.assertEqual(cr.strip_markdown("# Heading"), "Heading")
        self.assertEqual(cr.strip_markdown("- a\n- b"), "a\nb")
        self.assertEqual(cr.strip_markdown("1. one\n2. two"), "one\ntwo")
        self.assertEqual(cr.strip_markdown("> quoted"), "quoted")
        self.assertEqual(cr.strip_markdown("text\n---\nmore"), "text\n\nmore")

    def test_code_ticks(self):
        self.assertEqual(cr.strip_markdown("`code` here"), "code here")

    def test_plain_text_untouched(self):
        for s in ("Normal copy.", "5 stars, $12.99", "café & crème",
                  "Q1/Q2 50% off", "a — b – c"):
            self.assertEqual(cr.strip_markdown(s), s)

    def test_empty_and_none(self):
        self.assertEqual(cr.strip_markdown(""), "")
        self.assertEqual(cr.strip_markdown(None), "")

    def test_xss_still_blocked_after_strip(self):
        """Markdown normalisation must not weaken escaping."""
        for bad in ("<script>alert(1)</script>",
                    '"><img src=x onerror=alert(1)>',
                    "**<script>alert(1)</script>**"):
            out = cr._esc(bad)
            self.assertNotIn("<", out, bad)
            self.assertNotIn(">", out, bad)
            self.assertNotIn('"', out, bad)

    def test_underscores_in_identifiers_survive(self):
        """_MD_ITAL must not eat snake_case or mid-word underscores."""
        self.assertEqual(cr.strip_markdown("field_name here"), "field_name here")
        self.assertEqual(cr.strip_markdown("snake_case and_more"), "snake_case and_more")


class TestClipWords(unittest.TestCase):
    LONG = ("Keto Hacks That Actually Work: Lose Fat Without Starving "
            "When You Are Tired Of Counting Macros")

    def test_never_cuts_mid_word(self):
        for lim in (30, 40, 55, 70, 90):
            out = cr.clip_words(self.LONG, lim)
            self.assertLessEqual(len(out), lim)
            # last token must be a whole word from the source
            self.assertIn(out, self.LONG)

    def test_short_text_untouched(self):
        self.assertEqual(cr.clip_words("Short title", 110), "Short title")

    def test_strips_trailing_punctuation(self):
        self.assertFalse(cr.clip_words(self.LONG, 12).endswith((",", ".", " ")))

    def test_zero_and_negative(self):
        self.assertEqual(cr.clip_words("abc", 0), "abc")

    def test_h1_uses_clipped_headline(self):
        """The rendered hero H1 must be length-capped, not sliced raw."""
        long_head = ("Keto Hacks That Actually Work: Lose Fat Without "
                     "Starving When You Are Tired Of Counting Macros Daily")
        html = cr._section_html(
            {"_type": "hero", "headline": long_head, "subheadline": "sub"},
            {"pick": {}, "style": {}})
        h1 = html.split("<h1>", 1)[1].split("</h1>", 1)[0]
        self.assertLessEqual(len(h1), 115)
        self.assertNotIn("**", h1)


class TestAiClean(unittest.TestCase):
    def test_strips_markdown_and_quotes_in_right_order(self):
        self.assertEqual(
            ai._clean('**"Keto Hacks That Actually Work**'),
            ["Keto Hacks That Actually Work"])

    def test_multiline(self):
        self.assertEqual(ai._clean('*em*\n"quoted"\n### Head'),
                         ["em", "quoted", "Head"])

    def test_drops_empty_lines(self):
        self.assertEqual(ai._clean("a\n\n\nb"), ["a", "b"])

    def test_empty_input(self):
        self.assertEqual(ai._clean(""), [])
        self.assertEqual(ai._clean(None), [])


if __name__ == "__main__":
    unittest.main()
