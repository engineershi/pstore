# -*- coding: utf-8 -*-
"""Offline tests for the end-to-end traffic doctor (doctor.py). No network: the
transport is injected, so every branch (clean site, noindex, missing consoles,
dead IndexNow key file, no social channel, total network failure) is exercised
hermetically."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doctor

BASE = "https://shop.example.com"
KEY = "a" * 32


def _page(path, extra=""):
    html = ('<!DOCTYPE html><html><head><title>Title for %s</title>'
            '<link rel="canonical" href="%s%s">%s</head><body>%s</body></html>'
            % (path, BASE, path, extra, "x" * 2000))
    return (200, {"content-type": "text/html"}, html.encode())


def _sitemap():
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset>'
            '<url><loc>%s/n/a</loc></url>'
            '<url><loc>%s/n/b</loc></url>'
            '<url><loc>%s/lp/c</loc></url></urlset>' % (BASE, BASE, BASE)).encode()


def _fetch_factory(overrides=None, default=(404, {}, b"not found")):
    routes = {
        BASE + "/robots.txt": (200, {}, b"User-agent: *\nDisallow: /admin\n"
                                      b"Sitemap: " + (BASE + "/sitemap.xml").encode() + b"\n"),
        BASE + "/sitemap.xml": (200, {}, _sitemap()),
        BASE + "/": _page("/"),
        BASE + "/n/a": _page("/n/a"),
        BASE + "/n/b": _page("/n/b"),
        BASE + "/" + KEY + ".txt": (200, {}, KEY.encode()),
    }
    if overrides:
        routes.update(overrides)

    def fetch(url, timeout=15):
        return routes.get(url, default)
    return fetch


GOOD_ENGINES = [{"engine": "gsc", "state": "ready"},
                {"engine": "bing", "state": "ready"},
                {"engine": "yandex", "state": "ready"}]
GOOD_SOCIAL = {"webhook": True, "webhook_stats": {"ok": 2, "fail": 0},
               "native": [], "counts": {"published": 5}}


def _statuses(report, cid):
    return [c["status"] for c in report["checks"] if c["id"] == cid]


class TestDoctorUnit(unittest.TestCase):

    def test_clean_site_verdict_ok(self):
        r = doctor.run(BASE, sample=2, fetch=_fetch_factory(), engines=GOOD_ENGINES,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertEqual(r["verdict"], "ok", r)
        self.assertEqual(r["blockers"], 0)
        self.assertEqual(r["sitemap_size"], 3)
        self.assertEqual(_statuses(r, "sitemap"), [doctor.STATUS_OK])

    def test_noindex_sample_is_blocker(self):
        f = _fetch_factory({BASE + "/n/a": _page(
            "/n/a", '<meta name="robots" content="noindex,nofollow">')})
        r = doctor.run(BASE, sample=2, fetch=f, engines=GOOD_ENGINES,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertEqual(r["verdict"], "fail")
        self.assertEqual(_statuses(r, "page:/n/a"), [doctor.STATUS_FAIL])

    def test_google_not_connected_is_blocker_with_indexnow_truth(self):
        engines = [{"engine": "gsc", "state": "needs-client"},
                   {"engine": "bing", "state": "ready"}]
        r = doctor.run(BASE, sample=1, fetch=_fetch_factory(), engines=engines,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertEqual(_statuses(r, "gsc"), [doctor.STATUS_FAIL])
        fix = [c["fix"] for c in r["checks"] if c["id"] == "gsc"][0]
        self.assertIn("IndexNow does NOT reach Google", fix)

    def test_missing_indexnow_keyfile_is_blocker(self):
        f = _fetch_factory({BASE + "/" + KEY + ".txt": (404, {}, b"not found")})
        r = doctor.run(BASE, sample=1, fetch=f, engines=GOOD_ENGINES,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertEqual(_statuses(r, "indexnow"), [doctor.STATUS_FAIL])

    def test_no_social_channel_is_blocker(self):
        r = doctor.run(BASE, sample=1, fetch=_fetch_factory(), engines=GOOD_ENGINES,
                       indexnow_key=KEY, social={})
        self.assertEqual(_statuses(r, "social-channel"), [doctor.STATUS_FAIL])

    def test_webhook_failing_only_reported_without_success(self):
        r = doctor.run(BASE, sample=1, fetch=_fetch_factory(), engines=GOOD_ENGINES,
                       indexnow_key=KEY,
                       social={"webhook": True,
                               "webhook_stats": {"ok": 0, "fail": 3, "err": "502"},
                               "counts": {}})
        self.assertEqual(_statuses(r, "social-webhook"), [doctor.STATUS_FAIL])

    def test_network_failure_never_raises(self):
        def dead(url, timeout=15):
            return None, {}, b"error: URLError: boom"
        r = doctor.run(BASE, sample=2, fetch=dead, engines=GOOD_ENGINES,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "fail")
        self.assertTrue(r["checks"])

    def test_robots_without_sitemap_line_warns(self):
        f = _fetch_factory({BASE + "/robots.txt": (200, {}, b"User-agent: *\n")})
        r = doctor.run(BASE, sample=1, fetch=f, engines=GOOD_ENGINES,
                       indexnow_key=KEY, social=GOOD_SOCIAL)
        self.assertEqual(_statuses(r, "robots"), [doctor.STATUS_WARN])

    def test_free_host_domain_warning(self):
        free = "https://site.onrender.com"
        routes = {
            free + "/robots.txt": (200, {},
                                   b"Disallow: /admin\nSitemap: x\n"),
            free + "/sitemap.xml": (200, {}, b"<urlset><loc>%s/</loc></urlset>"
                                    % free.encode()),
            free + "/": _page("/"),
        }
        r = doctor.run(free, sample=0, fetch=lambda u, timeout=15: routes.get(
            u, (404, {}, b"nf")), engines=GOOD_ENGINES, indexnow_key=KEY,
            social=GOOD_SOCIAL)
        self.assertEqual(_statuses(r, "domain"), [doctor.STATUS_WARN])

    def test_social_platform_status_lists_ready_and_missing(self):
        r = doctor.run(BASE, sample=0, fetch=_fetch_factory(), engines=GOOD_ENGINES,
                       indexnow_key=KEY,
                       social={"webhook": False, "native": ["Telegram"],
                               "platforms": {"Telegram": True, "Facebook": False},
                               "counts": {}})
        c = [x for x in r["checks"] if x["id"] == "social-platforms"][0]
        self.assertEqual(c["status"], doctor.STATUS_WARN)
        self.assertIn("Telegram", c["detail"])
        self.assertIn("Facebook", c["detail"])

    def test_social_all_platforms_missing_is_fail(self):
        r = doctor.run(BASE, sample=0, fetch=_fetch_factory(), engines=GOOD_ENGINES,
                       indexnow_key=KEY,
                       social={"webhook": False, "native": [],
                               "platforms": {"Telegram": False, "Facebook": False},
                               "counts": {}})
        c = [x for x in r["checks"] if x["id"] == "social-platforms"][0]
        self.assertEqual(c["status"], doctor.STATUS_FAIL)

    def test_fixes_come_before_ok_checks(self):
        r = doctor.run(BASE, sample=1, fetch=_fetch_factory(),
                       engines=[{"engine": "gsc", "state": "needs-client"}],
                       indexnow_key=KEY, social={})
        statuses = [c["status"] for c in r["checks"]]
        self.assertEqual(statuses[0], doctor.STATUS_FAIL)
        self.assertEqual(statuses[-1], doctor.STATUS_OK)


if __name__ == "__main__":
    unittest.main()
