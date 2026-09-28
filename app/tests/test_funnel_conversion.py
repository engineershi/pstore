"""The public funnel must actually capture the leads it offers.

Six leaks found by auditing the rendered HTML of every public page type against
what the promise on the page says would happen. None of them threw an error;
each one silently cost signups, so nothing in the logs flagged them:

  1. The masthead button on all 491 /n/ pages and their long-tail children
     read "Get the free guide" but pointed at #courier -- the "Notify me when
     picks change" opt-in, a different promise with a different button.
  2. The /lp PDF gate form had no action/method, so with JavaScript off the
     submit button did nothing at all. courier.js intercepts submit, so this
     only bit the no-JS visitor -- and the gate is the offer.
  3. courier.js sent `source: main.dataset.source` and ignored each form's own
     hidden source, so all 1,852 sub-topics recorded as "topic", all 491 gates
     recorded as "niche": no way to tell which page type converts.
  4. The /blog and /stories indexes were the only crawlable hubs with no
     capture form, and their masthead CTA sent people elsewhere to sign up.
  5. Topic pages linked out to exactly one URL, making 1,852 of 3,333
     indexable URLs crawl dead ends.
  6. _send_welcome_email replaced an unmatched keyword with the largest niche
     on the site, so a lead who asked for one guide was emailed another's --
     and got a sub_interests row for a niche they never named.

Rendered HTML and the real courier.js are the contract here, so these assert
on the bytes the browser gets, not on internal helpers.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import unittest
from shutil import which

import cms_render
import seo
import server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COURIER = os.path.join(REPO, "static", "courier.js")

NICE = {"keyword": "yoga mats", "created_at": "2026-01-02 03:04:05",
        "products": [{"asin": "B00000001", "title": "A thick mat", "price": 24.5,
                      "rating": 4.6, "reviews": 812, "img": "/i/1.png",
                      "brand": "Acme", "url": "u", "aff": "/go/B00000001"}]}
OTHER = {"keyword": "griddle pan", "created_at": "2026-01-01 00:00:00",
         "products": [{"asin": "B00000002", "title": "A square pan", "price": 39.0,
                       "rating": 4.4, "reviews": 300, "img": "/i/2.png",
                       "brand": "Acme", "url": "u", "aff": "/go/B00000002"}]}


def _masthead_cta(html):
    """The masthead CTA href/label pair, which is what a reader clicks.

    Matched on the class _masthead() renders, not on a bare `class="cta"`,
    which several unrelated blocks also use.
    """
    m = re.search(r'<a class="btn mast-cta" href="([^"]*)">(.*?)</a>', html, re.S)
    return (m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()) if m else (None, None)


# ------------------------------------------------------------------ 1. the CTA
class TestMastheadCtaTargetsTheGate(unittest.TestCase):
    """'Get the free guide' must land on the form that gives the guide."""

    def test_niche_page_cta_points_at_the_pdf_gate(self):
        html = seo.render_niche("yoga mats", NICE, [NICE]).decode()
        self.assertEqual(_masthead_cta(html)[0], "#gate")

    def test_topic_page_cta_points_at_the_pdf_gate(self):
        html = seo.render_topic("thick mat", "yoga mats", NICE, "yoga-mats",
                                saved_niches=[NICE]).decode()
        self.assertEqual(_masthead_cta(html)[0], "#gate")

    def test_priceband_page_cta_points_at_the_gate(self):
        items = [dict(NICE["products"][0], price=12.0),
                 dict(NICE["products"][0], asin="B00000009", price=95.0)]
        html = seo.render_priceband(20, "yoga mats", "yoga-mats", items).decode()
        self.assertEqual(_masthead_cta(html)[0], "#gate")

    def test_niche_without_products_falls_back_to_the_optin(self):
        """A dead #gate would be worse than the old mismatch, so a page that
        renders no gate keeps pointing at the form it does render."""
        empty = {"keyword": "empty niche", "products": []}
        html = seo.render_niche("empty niche", empty, [empty]).decode()
        self.assertNotIn('id="gate"', html)
        self.assertEqual(_masthead_cta(html)[0], "#courier")

    def test_cta_label_still_promises_the_guide(self):
        html = seo.render_niche("yoga mats", NICE, [NICE]).decode()
        self.assertEqual(_masthead_cta(html)[1], "Get the free guide")


# ------------------------------------------------------ 2. the gate works no-JS
class TestGateFormSubmitsWithoutJavaScript(unittest.TestCase):
    def _landing_gate(self, **settings):
        """The /lp PDF gate, rendered through the public section renderer."""
        on = {"pdf_gated": True, "email_gate_enabled": True}
        on.update(settings)
        return cms_render._section_html(
            {"_type": "email_gate", "headline": "Free guide",
             "button_text": "Send it"},
            {"keyword": NICE["keyword"], "settings": on})

    def test_landing_gate_posts_to_subscribe(self):
        html = self._landing_gate()
        self.assertIn('action="/subscribe"', html)
        self.assertIn('method="post"', html)

    def test_landing_gate_explains_the_no_js_path(self):
        self.assertIn("<noscript>", self._landing_gate())

    def test_seo_gate_keeps_a_native_fallback(self):
        html = seo.lead_gate_html("yoga mats", "niche")
        self.assertIn('action="/subscribe"', html)
        self.assertIn('method="post"', html)


# ------------------------------------------------- 3. the form's own source wins
class TestFormSourceBeatsPageSource(unittest.TestCase):
    """courier.js must send the form's hidden source, not <main data-source>.

    With one value for the whole page, every gate, opt-in and nudge on it
    looked identical to the subscriber table, so there was no way to learn
    which page type converts.
    """

    PRELUDE = r"""
var __posted = [];
var __handlers = {};
var sessionStorage = { getItem: function () { return null; },
                       setItem: function () {}, removeItem: function () {} };
var localStorage = sessionStorage;
var location = { search: "" };
function __field(name, value) { return { name: name, value: value }; }
var __form = {
  querySelector: function (sel) {
    if (sel === "[name=email]") return __field("email", "lead@example.com");
    if (sel === "[name=first_name]") return null;
    if (sel === "[name=keyword]") return __field("keyword", "yoga mats");
    if (sel === "[name=source]") return __field("source", %s);
    if (sel === ".courier-msg") return { textContent: "", style: {} };
    return null;
  },
  querySelectorAll: function () { return []; },
  classList: { contains: function (c) { return c === "courier"; } }
};
var __main = { getAttribute: function () { return null; },
               dataset: { source: %s, opted: "" } };
var document = {
  querySelector: function (sel) {
    if (sel && sel.indexOf("main") === 0) return __main;
    if (sel && sel.indexOf("form.courier") === 0) return __form;
    return null;
  },
  querySelectorAll: function () { return []; },
  classList: { contains: function (c) { return c === "courier"; } },
  addEventListener: function (t, fn) { __handlers[t] = (__handlers[t] || []).concat(fn); },
  body: { setAttribute: function () {}, getAttribute: function () { return null; },
          removeAttribute: function () {}, appendChild: function () {} },
  createElement: function () { return { style: {}, setAttribute: function () {},
                                        appendChild: function () {}, addEventListener: function () {} }; },
  head: { appendChild: function () {} },
  readyState: "complete"
};
/* courier.js binds its signup POST to a document-level 'submit' listener at
   load time, so the form is submitted the way a browser would: the event target
   is the form itself. */
function __submit() {
  var list = (__handlers["submit"] || []).slice();
  var errs = [];
  for (var i = 0; i < list.length; i++) {
    try { list[i]({ target: __form, preventDefault: function () {} }); }
    catch (e) { errs.push(i + ": " + e); }
  }
  console.log(JSON.stringify({ posts: __posted, errs: errs }));
  process.exit(0);
}
var window = { location: location, addEventListener: function () {} };
function fetch(url, opts) { __posted.push(JSON.parse(opts.body)); return Promise.resolve({ json: function () { return Promise.resolve({}); } }); }
var navigator = { sendBeacon: function () { return true; } };
function Blob(p) { this.__s = (p || []).join(""); }
"""

    @classmethod
    def setUpClass(cls):
        if not which("node"):
            raise unittest.SkipTest("node not available")

    def _post(self, form_source, page_source='"niche"'):
        with open(COURIER) as fh:
            js = (self.PRELUDE % (json.dumps(form_source), page_source)
                  + fh.read() + "\n__submit();\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True,
                               timeout=40)
            self.assertEqual(r.returncode, 0, r.stderr[-600:])
            out = json.loads(r.stdout.strip())
            self.assertEqual(out["errs"], [], "courier.js handler raised")
            return out["posts"]
        finally:
            os.unlink(path)

    def test_hidden_form_source_is_sent_not_the_page_source(self):
        posted = self._post("niche-gate")
        self.assertTrue(posted, "courier.js did not POST the signup")
        self.assertEqual(posted[0]["source"], "niche-gate")

    def test_topic_gate_is_distinguishable_from_its_parent_hub(self):
        """1,852 sub-topics used to record as 'topic', same as the 491 hubs."""
        self.assertEqual(self._post("topic-gate")[0]["source"], "topic-gate")

    def test_falls_back_to_page_source_when_the_form_has_none(self):
        self.assertEqual(self._post(None)[0]["source"], "niche")


# ------------------------------------------------- 4. /blog and /stories capture
class TestIndexPagesOfferTheGuide(unittest.TestCase):
    def test_blog_index_has_a_gate(self):
        html = seo.render_blog([NICE, OTHER]).decode()
        self.assertIn('id="gate"', html)
        self.assertIn('name="source" value="blog-gate"', html)

    def test_stories_index_has_a_gate(self):
        html = seo.render_stories_gallery([NICE]).decode()
        self.assertIn('id="gate"', html)
        self.assertIn('name="source" value="stories-gate"', html)

    def test_index_cta_no_longer_bounces_to_the_homepage(self):
        """The old CTA sent readers to /#top-picks to sign up somewhere else.
        The footer's browse link still goes there, so scope this to the header.
        """
        for html in (seo.render_blog([NICE]).decode(),
                     seo.render_stories_gallery([NICE]).decode()):
            self.assertEqual(_masthead_cta(html)[0], "#gate")
            header = html[html.index("<header"):html.index("</header>")]
            self.assertNotIn("/#top-picks", header)

    def test_empty_indexes_still_offer_a_form(self):
        self.assertIn('id="gate"', seo.render_blog([]).decode())
        self.assertIn('id="gate"', seo.render_stories_gallery([]).decode())


# --------------------------------------------------------- 5. topic pages link out
class TestTopicPagesAreNotDeadEnds(unittest.TestCase):
    def test_topic_page_links_to_related_niches(self):
        html = seo.render_topic("thick mat", "yoga mats", NICE, "yoga-mats",
                                saved_niches=[NICE, OTHER]).decode()
        self.assertIn("/n/griddle-pan", html)

    def test_related_block_appears_without_saved_niches(self):
        """The argument is optional, so a caller that forgets it must not 500."""
        html = seo.render_topic("thick mat", "yoga mats", NICE, "yoga-mats").decode()
        self.assertIn("</html>", html)

    def test_optin_html_carries_an_explicit_source(self):
        """Otherwise courier.js has nothing to preserve but the page's value."""
        self.assertIn('name="source" value="niche"',
                      seo.optin_html("yoga mats", "niche"))


# ------------------------------------------------ 6. no foreign guide in email #1
class TestWelcomeEmailNeverInventsAnInterest(unittest.TestCase):
    """A lead must never be emailed a guide for a niche they didn't ask for."""

    @classmethod
    def setUpClass(cls):
        # Other modules reload `server` with PSTORE_DB pointed at their own temp
        # copy, so the module-level DB and schema this test writes to depend on
        # execution order. Build a private, definitely-schema'd database rather
        # than inheriting whatever the previous module left behind.
        cls.db = "/tmp/pstore_test_funnel_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        cls.prev = os.environ.get("PSTORE_DB")
        os.environ["PSTORE_DB"] = cls.db
        import importlib
        globals()["server"] = importlib.reload(server)
        server._db().close()  # opens the file and creates the schema

    @classmethod
    def tearDownClass(cls):
        import importlib
        if cls.prev is None:
            os.environ.pop("PSTORE_DB", None)
        else:
            os.environ["PSTORE_DB"] = cls.prev
        globals()["server"] = importlib.reload(server)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.db + suffix)
            except OSError:
                pass

    def test_generic_optin_keywords_are_recognised(self):
        for kw in ("picks", "blog", "stories"):
            self.assertIn(kw, server._GENERIC_OPTIN_KEYWORDS)
        for kw in ("yoga mats", "thick mat"):
            self.assertNotIn(kw, server._GENERIC_OPTIN_KEYWORDS)

    def test_a_blank_keyword_is_rejected_before_the_fallback(self):
        """_send_welcome_email returns early on an empty keyword, so the
        generic set never has to carry "". Pin that both guards agree."""
        self.assertNotIn("", server._GENERIC_OPTIN_KEYWORDS)

    def test_specific_keyword_gets_no_fallback_guide(self):
        """'spare niche' is saved with zero products, so the fallback branch is
        the one under test. A lead who asked for it must not be mailed the
        largest niche's guide."""
        sent = []
        stubs = {"configured": lambda: True,
                 "next_email": lambda kw, items, i: (sent.append(kw) or
                                                    {"subject": "s", "html": "h",
                                                     "text": "t"}),
                 "send": lambda *a, **k: False,
                 "tracked_url": lambda *a, **k: "",
                 "open_pixel_url": lambda *a, **k: "",
                 "render_body": lambda *a, **k: "body",
                 "thread_reply_to": lambda *a, **k: "r@mazon.com"}
        saved = {k: getattr(server.mailer, k) for k in stubs}
        with server._db() as conn:
            conn.execute("INSERT INTO niches (keyword, market, products) "
                         "VALUES ('spare niche', ?, '[]')", (server.amazon.MARKET,))
            conn.commit()
        for k, v in stubs.items():
            setattr(server.mailer, k, v)
        try:
            server._send_welcome_email(1, "spare niche")
        finally:
            for k, v in saved.items():
                setattr(server.mailer, k, v)
        self.assertEqual(sent, [], "sent a guide for an unrelated niche: %r" % sent)

    def test_generic_optin_still_gets_the_fallback_guide(self):
        """The fix must not silence the homepage/blog opt-in, which has no
        niche of its own and genuinely wants the default track."""
        sent = []
        stubs = {"configured": lambda: True,
                 "next_email": lambda kw, items, i: (sent.append(kw) or
                                                    {"subject": "s", "html": "h",
                                                     "text": "t"}),
                 "send": lambda *a, **k: False,
                 "tracked_url": lambda *a, **k: "",
                 "open_pixel_url": lambda *a, **k: "",
                 "render_body": lambda *a, **k: "body",
                 "thread_reply_to": lambda *a, **k: "r@mazon.com"}
        saved = {k: getattr(server.mailer, k) for k in stubs}
        # _default_niche_items() only returns a track if some niche is
        # productized, which the test DB otherwise has no reason to contain.
        stocked = [{"asin": "B0000000%d" % i, "title": "p%d" % i, "price": 10.0 + i,
                    "rating": 4.0, "reviews": 10, "img": "/i.png", "url": "u",
                    "aff": "/go/B"} for i in (1, 2, 3)]
        with server._db() as conn:
            conn.execute("INSERT INTO niches (keyword, market, products) "
                         "VALUES ('stocked niche', ?, ?)",
                         (server.amazon.MARKET, json.dumps(stocked)))
            # The welcome is only sent to a confirmed, unsubscribed lead that
            # has not already had email #1.
            conn.execute("INSERT OR REPLACE INTO subscribers "
                         "(id,email,confirmed,unsubscribed,sent_index) "
                         "VALUES (1,'a@b.com',1,0,0)")
            conn.commit()
        for k, v in stubs.items():
            setattr(server.mailer, k, v)
        try:
            server._send_welcome_email(1, "picks")
        finally:
            for k, v in saved.items():
                setattr(server.mailer, k, v)
        self.assertTrue(sent, "the generic opt-in must still be served")


if __name__ == "__main__":
    unittest.main()
