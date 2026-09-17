# -*- coding: utf-8 -*-
"""Hermetic tests for the multi-scene Shorts plan + renderer (no ffmpeg, no net)."""
import struct
import unittest

import shorts
import social


PNG_SIG = b"\x89PNG\r\n\x1a\n"


class ScenePlan(unittest.TestCase):
    def test_build_scenes_returns_four_named_scenes(self):
        scenes = shorts.build_scenes(
            "keto snacks",
            "Top keto snacks ranked by 4,200 reviews. Crunchy ones win. "
            "Granola bars are the surprise hit. Check prices now.",
            "#Keto #Snacks", "https://pstore.test/lp/keto-snacks")
        self.assertEqual([s["scene"] for s in scenes],
                         ["hook", "picks", "proof", "cta"])
        self.assertEqual([s["tag"] for s in scenes], list(shorts.SCENE_TAGS))

    def test_build_scenes_is_deterministic(self):
        a = shorts.build_scenes("js", "one. two. three. four.", "#X")
        b = shorts.build_scenes("js", "one. two. three. four.", "#X")
        self.assertEqual(a, b)

    def test_picks_scene_has_three_picks(self):
        scenes = shorts.build_scenes(
            "js", "one. two. three. four. five.", "#X")
        picks = scenes[1]["picks"]
        self.assertEqual(len(picks), 3)

    def test_picks_fall_back_on_short_body(self):
        scenes = shorts.build_scenes("js", "just one sentence.", "#X")
        self.assertEqual(len(scenes[1]["picks"]), 3)

    def test_cta_carries_hashtags_and_link(self):
        scenes = shorts.build_scenes(
            "js", "See https://pstore.test/lp/js for the list.", "#JS #Best")
        cta = scenes[3]
        self.assertIn("#JS", cta["hashtags"])
        self.assertEqual(cta["link"], "https://pstore.test/lp/js")

    def test_empty_kit_still_builds_scenes(self):
        scenes = shorts.build_scenes("", "", "")
        self.assertEqual(len(scenes), 4)


class SceneRender(unittest.TestCase):
    def setUp(self):
        self._orig_glow = social._glow
        self._orig_vignette = social._vignette
        social._glow = lambda img, W, H, cx, cy, R, rgb, amp, step=2: None
        social._vignette = lambda img, W, H, strength=0.13: None

    def tearDown(self):
        social._glow = self._orig_glow
        social._vignette = self._orig_vignette

    def test_render_scenes_is_1080x1920_png(self):
        pngs = shorts.render_scenes(
            "keto snacks",
            "Top keto snacks ranked by 4,200 reviews. Crunchy ones win.",
            "#Keto #BestPicks")
        self.assertEqual(len(pngs), 4)
        for png in pngs:
            self.assertTrue(png.startswith(PNG_SIG))
            w, h = struct.unpack(">II", png[16:24])
            self.assertEqual((w, h), (1080, 1920))

    def test_render_scenes_handles_empty_and_long_body(self):
        self.assertTrue(shorts.render_scenes("", "", ""))
        self.assertTrue(shorts.render_scenes("a", "word " * 200, "#X"))

    def test_scene_png_formats_are_distinct(self):
        scenes = shorts.build_scenes(
            "js", "one. two. three. four.", "#JS")
        frames = [shorts.scene_png(s) for s in scenes]
        self.assertEqual(len(set(frames)), 4)

    def test_scene_png_unknown_scene_is_none(self):
        self.assertIsNone(shorts.scene_png({"scene": "nope"}))


class ConcatPlan(unittest.TestCase):
    def test_total_and_offsets(self):
        plan = shorts.concat_plan(4, seconds=1.8, transition=0.4, fps=25)
        self.assertEqual(plan["clips"], 4)
        self.assertEqual(plan["offsets"], [1.4, 2.8, 4.2])
        self.assertAlmostEqual(plan["total"], 6.0, places=3)
        self.assertEqual(plan["last"], "v4")

    def test_single_clip_short_circuits(self):
        plan = shorts.concat_plan(1, seconds=2)
        self.assertEqual(plan["offsets"], [])
        self.assertEqual(plan["last"], "v1")
        self.assertEqual(plan["filter"], "[0:v]null[v1]")

    def test_filter_chains_every_input(self):
        plan = shorts.concat_plan(3)
        for i in (1, 2):
            self.assertIn("[%d:v]xfade" % i, plan["filter"])
        self.assertEqual(plan["filter"].count("xfade"), 2)

    def test_plan_is_deterministic(self):
        self.assertEqual(shorts.concat_plan(4), shorts.concat_plan(4))

    def test_bad_inputs_are_clamped(self):
        plan = shorts.concat_plan(0, seconds=1, transition=5)
        self.assertEqual(plan["clips"], 1)


if __name__ == "__main__":
    unittest.main()
