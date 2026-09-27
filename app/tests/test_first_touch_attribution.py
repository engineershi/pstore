"""First-touch attribution must survive internal navigation.

`courier.js` read `utm_source` from `location.search` on every page view. A
Pinterest visitor who landed on /n/x?utm_source=pinterest and then followed an
internal link to /n/y lost their source mid-session, so the click beacon fell
through to `baseSource` and `_click_channel` classified the click as "organic".

The visible effect in production: 27 of 28 lifetime clicks attributed to
"organic" and 0 to "social", while the search-console view showed 208 referral
views. Every channel decision -- is Pinterest working? is email working? -- is
based on that number, so it has to be trustworthy before spending on traffic.

These drive the real script in a minimal browser stand-in and assert on the
beacon payload, because the payload is the contract the server records. The
scenario is one browser context walking several pages, which is the case that
was broken.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from shutil import which

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COURIER = os.path.join(REPO, "static", "courier.js")

# Stand-in for the browser globals courier.js touches. The click handler is
# captured so a test can fire a synthetic Amazon-link click and read the
# resulting beacon payload.
PRELUDE = r"""
var __store = {};
var sessionStorage = {
  getItem: function (k) { return k in __store ? __store[k] : null; },
  setItem: function (k, v) { __store[k] = String(v); },
  removeItem: function (k) { delete __store[k]; }
};
var localStorage = sessionStorage;
var location = { search: "" };
var __handlers = {};
var __beacons = [];
var __main = {
  getAttribute: function (a) {
    if (a === "data-niche") return "griddle-pan";
    if (a === "data-source") return "niche";
    if (a === "data-variant") return "0";
    return null;
  },
  dataset: { source: "niche", opted: "" }
};
var document = {
  querySelector: function (sel) {
    if (sel && sel.indexOf("main") === 0) return __main;
    return null;
  },
  querySelectorAll: function () { return []; },
  addEventListener: function (t, fn) { (__handlers[t] = __handlers[t] || []).push(fn); },
  body: { setAttribute: function () {}, getAttribute: function () { return null; },
          removeAttribute: function () {}, appendChild: function () {} },
  createElement: function () { return { style: {}, setAttribute: function () {},
                                        appendChild: function () {}, addEventListener: function () {} }; },
  head: { appendChild: function () {} },
  readyState: "complete"
};
var window = { location: location, addEventListener: function () {} };
function Blob(parts) { this.__s = (parts || []).join(""); }
var navigator = {
  sendBeacon: function (url, blob) { __beacons.push({ url: url, payload: JSON.parse(blob.__s) }); return true; }
};
var fetch = function () { return Promise.resolve({ json: function () { return Promise.resolve({}); } }); };
function __URLSearchParams(q) {
  var out = {};
  String(q || "").replace(/^\?/, "").split("&").forEach(function (kv) {
    if (!kv) return;
    var i = kv.indexOf("=");
    out[decodeURIComponent(i < 0 ? kv : kv.slice(0, i))] =
      decodeURIComponent(i < 0 ? "" : kv.slice(i + 1).replace(/\+/g, " "));
  });
  this.get = function (k) { return k in out ? out[k] : null; };
}
function __fireAmazonClick() {
  var anchor = {
    getAttribute: function (a) {
      if (a === "href") return "https://www.amazon.com/dp/B0TESTTEST?tag=pstore2006-20";
      if (a === "data-beacon") return "";
      if (a === "data-asin") return "B0TESTTEST";
      return null;
    }
  };
  var before = __beacons.length;
  /* courier.js registers several click handlers -- the analytics beacon plus
     UI handlers (watch modal, nudge) that expect real DOM nodes. Fire them one
     at a time and keep the one that emits a /api/track beacon; handlers that
     throw on the synthetic DOM are irrelevant to what is under test. */
  var ev = { target: { closest: function () { return anchor; } },
             preventDefault: function () {}, stopPropagation: function () {} };
  var list = (__handlers["click"] || []).slice();
  for (var i = 0; i < list.length; i++) {
    var mark = __beacons.length;
    try { list[i](ev); } catch (e) { continue; }
    for (var j = __beacons.length - 1; j >= mark; j--) {
      if (__beacons[j].url === "/api/track") return __beacons[j];
    }
  }
  throw new Error("no click handler emitted a /api/track beacon (" +
                  list.length + " handlers)");
  /* courier.js also beacons pageviews, and ordering between them is not
     guaranteed, so select the click beacon by endpoint rather than popping the
     last entry. */
  for (var i = __beacons.length - 1; i >= 0; i--) {
    if (__beacons[i].url === "/api/track") return __beacons[i];
  }
}
function __visit(search) { location.search = search; }
"""


def _node(script, timeout=40):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            raise AssertionError("node failed: %s" % r.stderr[-600:])
        return json.loads(r.stdout.strip())
    finally:
        os.unlink(path)


def _walk(visits):
    """Load courier.js once per page in one shared context, fire an Amazon
    click on each, and return the beacon payload for each page."""
    with open(COURIER) as fh:
        src = fh.read()
    js = PRELUDE + "\n"
    for v in visits:
        js += "__visit(%s);\n" % json.dumps(v)
        js += src + "\n"
        js += "__results.push(__fireAmazonClick());\n"
    js = "var __results = [];\n" + js
    js += "console.log(JSON.stringify(__results));\n"
    js += "process.exit(0);\n"
    return _node(js)


class TestFirstTouchAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not which("node"):
            raise unittest.SkipTest("node not available")

    # ------------------------------------------------------------- the bug
    def test_attribution_survives_internal_navigation(self):
        """Page 2 of a Pinterest session carries no utm params. It used to
        report source "niche" -> channel organic, mislabelling social traffic
        as search traffic."""
        r = _walk(["?utm_source=pinterest&utm_content=pin-42", "/n/other"])
        self.assertEqual(r[0]["payload"]["source"], "pinterest")
        self.assertEqual(r[1]["payload"]["source"], "pinterest",
                         "source lost on internal navigation: %r" % r)

    def test_utm_content_survives_navigation_too(self):
        r = _walk(["?utm_source=pinterest&utm_content=pin-42", "/n/other"])
        self.assertEqual(r[1]["payload"]["content"], "pin-42")

    def test_every_social_platform_survives(self):
        for src in ("pinterest", "twitter", "facebook", "reddit", "email"):
            r = _walk(["?utm_source=%s" % src, "/n/other"])
            self.assertEqual(r[1]["payload"]["source"], src, src)

    def test_slug_and_asin_are_still_reported(self):
        """The fix must not disturb the rest of the payload."""
        r = _walk(["?utm_source=pinterest", "/n/other"])
        for p in r:
            self.assertEqual(p["payload"]["slug"], "griddle-pan")
            self.assertEqual(p["payload"]["asin"], "B0TESTTEST")

    # ------------------------------------------------------- first-touch wins
    def test_first_touch_wins_over_later_utm(self):
        r = _walk(["?utm_source=pinterest&utm_content=pin-1",
                   "?utm_source=twitter&utm_content=other"])
        self.assertEqual(r[1]["payload"]["source"], "pinterest")
        self.assertEqual(r[1]["payload"]["content"], "pin-1")

    def test_long_session_keeps_original_touch(self):
        r = _walk(["?utm_source=pinterest"] + ["/n/x"] * 5)
        self.assertTrue(all(x["payload"]["source"] == "pinterest" for x in r), r)

    # ------------------------------------------------------------- no utm
    def test_no_utm_falls_back_to_page_source(self):
        r = _walk(["/n/anything", "/n/other"])
        self.assertEqual(r[0]["payload"]["source"], "niche")

    def test_unrelated_params_are_not_a_source(self):
        r = _walk(["?ref=abc&page=2"])
        self.assertEqual(r[0]["payload"]["source"], "niche")

    def test_fresh_untagged_visit_is_not_polluted(self):
        r = _walk(["/n/fresh"])
        self.assertEqual(r[0]["payload"]["source"], "niche")

    def test_beacon_still_reports_every_page(self):
        r = _walk(["/n/a", "/n/b", "/n/c"])
        self.assertEqual(len(r), 3)

    # ------------------------------------------------- the server agrees
    def test_recorded_source_maps_to_social_channel(self):
        """End of the chain: the payload source must classify as 'social', which
        is the whole point of the fix."""
        import server
        self.assertIn("pinterest", server._SOCIAL_SOURCE_KEYS)
        self.assertEqual(server._click_channel("pinterest"), "social")

    def test_click_beacon_prefers_persisted_source(self):
        with open(COURIER) as fh:
            src = fh.read()
        self.assertRegex(src, r"var source = [^;]*utmSource[^;]*;")

    def test_first_touch_key_is_session_scoped(self):
        with open(COURIER) as fh:
            src = fh.read()
        m = re.search(r'var _FT_KEY = "([^"]+)"', src)
        self.assertIsNotNone(m)
        self.assertIn("sessionStorage", src[m.start():m.start() + 900])


if __name__ == "__main__":
    unittest.main()
