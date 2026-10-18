"""Colour tools without MLT or a display: curves maths (checked against
FFmpeg's own curves filter), scope analysis, curves in presets, and the
grading maths (colour wheels, hue curves, secondary) with their LUT files,
whose layout is checked against FFmpeg's lut3d filter."""

import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

from tideline import curves as cv
from tideline import grading as gr
from tideline import presets
from tideline import scopes as sc


def solid(rgb, w=64, h=36):
    return bytes(np.tile(np.array(rgb, dtype=np.uint8), (h, w, 1)).tobytes()), w, h


class Curves(unittest.TestCase):
    def test_clean_sorts_clamps_and_thins(self):
        self.assertEqual(cv.clean([[1.2, 0.5], [0.5, -1], [0.0, 0.0], [0.505, 0.9]]),
                         [[0.0, 0.0], [0.505, 0.9], [1.0, 0.5]])
        self.assertEqual(cv.clean([[0.4, 0.6]]), [[0.0, 0.6], [1.0, 0.6]])
        self.assertEqual(cv.clean([]), cv.IDENTITY)
        for bad in ("0/0 1/1", [[0, "x"]], [[0, 0, 0]], [[True, 0]], [[float("nan"), 0]]):
            with self.assertRaises(ValueError):
                cv.clean(bad)

    def test_clean_set_fills_missing_channels(self):
        value = cv.clean_set({"red": [[0, 0.1], [1, 0.9]], "blue": "broken"})
        self.assertEqual(set(value), {"master", "red", "green", "blue"})
        self.assertEqual(value["red"], [[0.0, 0.1], [1.0, 0.9]])
        self.assertEqual(value["blue"], cv.IDENTITY)
        self.assertEqual(cv.clean_set(None), cv.default())

    def test_identity_and_flat_ends(self):
        self.assertTrue(cv.is_identity(cv.IDENTITY))
        self.assertFalse(cv.is_identity([[0, 0], [0.5, 0.6], [1, 1]]))
        pts = [[0.2, 0.3], [0.8, 0.7]]
        self.assertAlmostEqual(cv.evaluate(pts, 0.0), 0.3)
        self.assertAlmostEqual(cv.evaluate(pts, 1.0), 0.7)

    def test_monotone_between_points(self):
        table = cv.table([[0, 0], [0.2, 0.05], [0.25, 0.9], [1, 1]], 512)
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(table, table[1:])))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in table))

    def test_point_editing(self):
        pts, i = cv.add_point(cv.IDENTITY, 0.5, 0.7)
        self.assertEqual((pts, i), ([[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]], 1))
        self.assertEqual(cv.add_point(pts, 0.505, 0.1)[1], -1)          # too close to a point
        moved = cv.move_point(pts, 1, 2.0, 0.2)
        self.assertLess(moved[1][0], 1.0)                               # stays left of its neighbour
        self.assertEqual(cv.remove_point(cv.remove_point(pts, 1), 0), cv.IDENTITY)   # keeps two

    def test_ffmpeg_string(self):
        self.assertEqual(cv.to_ffmpeg([[0, 0], [0.5, 0.625], [1, 1]]), "0/0 0.5/0.625 1/1")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_matches_ffmpeg_pchip(self):
        for pts in ([[0, 0], [0.3, 0.2], [0.7, 0.85], [1, 1]], [[0.1, 0.05], [0.5, 0.7], [0.9, 0.95]],
                    [[0, 0.2], [0.25, 0.9], [0.5, 0.1], [1, 1]]):
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "nullsrc=s=256x1:d=0.04,format=gbrp,geq=r='X':g='X':b='X'",
                 "-vf", f"curves=master='{cv.to_ffmpeg(pts)}':interp=pchip", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
            theirs = np.frombuffer(out, np.uint8)[0::3].astype(int)
            ours = np.array([round(v * 255) for v in cv.table(pts)])
            self.assertLessEqual(int(np.abs(theirs - ours).max()), 1, pts)

    def test_presets_keep_curves(self):
        from tideline.engine.effects import EFFECTS
        pdef = EFFECTS["curves"].param("curves")
        value = presets._value(pdef, {"master": [[0, 0], [0.5, 0.6], [1, 1]]})
        self.assertEqual(value["master"], [[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]])
        self.assertEqual(value["red"], cv.IDENTITY)
        with self.assertRaises(ValueError):
            presets._value(pdef, [[0, 0], [1, 1]])
        self.assertEqual(EFFECTS["curves"].default("curves"), cv.default())


class Scopes(unittest.TestCase):
    def test_frame_is_sampled_down(self):
        img = sc.frame_array(*solid((10, 20, 30), 1920, 1080))
        self.assertLessEqual(img.shape[1], sc.MAX_WIDTH)
        self.assertLessEqual(img.shape[0], sc.MAX_HEIGHT)
        self.assertEqual(tuple(img[0, 0]), (10, 20, 30))

    def test_luma_waveform_of_a_flat_grey(self):
        img = sc.frame_array(*solid((128, 128, 128)))
        (density, _colour), = sc.waveform(img, "luma")
        self.assertEqual(density.shape, (sc.LEVELS, img.shape[1]))
        row = sc.LEVELS - 1 - 128
        self.assertTrue(np.all(density[row] == img.shape[0]))
        self.assertEqual(density.sum(), img.shape[0] * img.shape[1])

    def test_rec709_luma_and_parade(self):
        img = sc.frame_array(*solid((255, 0, 0)))
        (density, _), = sc.waveform(img, "luma")
        self.assertEqual(int(np.argmax(density[:, 0])), sc.LEVELS - 1 - round(255 * sc.KR))
        red, green, blue = (d for d, _ in sc.parade(img))
        self.assertEqual(red[0].sum(), img.shape[0] * img.shape[1])        # all at the top: 255
        self.assertEqual(green[-1].sum(), img.shape[0] * img.shape[1])     # all at the bottom: 0
        self.assertEqual(len(sc.waveform(img, "rgb")), 3)
        self.assertEqual(len(sc.waveform(img, "ycbcr")), 3)

    def test_vectorscope_puts_colours_at_their_angle(self):
        size = 128
        for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)):
            count, colour = sc.vectorscope(sc.frame_array(*solid(rgb)), size)
            y, x = np.unravel_index(np.argmax(count), count.shape)
            cb, cr = sc.rgb_to_cbcr(*(c / 255 for c in rgb))
            self.assertLessEqual(abs(x - (cb + 0.5) * (size - 1)), 1, rgb)
            self.assertLessEqual(abs(y - (0.5 - cr) * (size - 1)), 1, rgb)
            self.assertTrue(np.allclose(colour[y, x], rgb))
        grey, _ = sc.vectorscope(sc.frame_array(*solid((90, 90, 90))), size)
        y, x = np.unravel_index(np.argmax(grey), grey.shape)
        self.assertLessEqual(abs(x - (size - 1) / 2), 0.5)
        self.assertLessEqual(abs(y - (size - 1) / 2), 0.5)

    def test_skin_line_sits_between_red_and_yellow(self):
        angle = lambda rgb: np.degrees(np.arctan2(*reversed(sc.rgb_to_cbcr(*rgb)))) % 360
        self.assertLess(angle((0.75, 0, 0)), sc.SKIN_ANGLE)          # red at about 103°
        self.assertLess(sc.SKIN_ANGLE, angle((0.75, 0.75, 0)))       # yellow at about 173°

    def test_histogram_and_clipping(self):
        img = sc.frame_array(*solid((0, 128, 255)))
        h = sc.histogram(img)
        n = img.shape[0] * img.shape[1]
        self.assertEqual((h["red"][0], h["green"][128], h["blue"][255]), (n, n, n))
        self.assertEqual(h["luma"].sum(), n)
        self.assertEqual(sc.clipping(img), (1.0, 1.0))
        self.assertEqual(sc.clipping(sc.frame_array(*solid((60, 60, 60)))), (0.0, 0.0))

    def test_argb_is_premultiplied_in_cairo_order(self):
        out = sc.argb_from_rgb(np.array([[[1.0, 0.5, 0.0]]]))
        b, g, r, a = out[0, 0] if np.little_endian else out[0, 0][::-1]
        self.assertEqual((r, g, b, a), (255, 128, 0, 255))
        out = sc.argb_from_rgb(np.array([[[1.0, 1.0, 1.0]]]), alpha=np.array([[0.5]]))
        self.assertTrue(np.all(out[0, 0] == 128))
        self.assertEqual(sc.intensity(np.array([0.0]))[0], 0.0)
        self.assertEqual(sc.intensity(np.array([1000.0]), full=10)[0], 1.0)
        spread = sc.spread(np.array([[0.0], [1.0], [0.0]]))
        self.assertTrue(np.allclose(spread[:, 0], [0.6, 1.0, 0.6]))



def rgb(*c):
    return np.array([c], dtype=np.float64)


class Grading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tideline-luts-")
        gr.CACHE_DIR = self.tmp

    def tearDown(self):
        gr.CACHE_DIR = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_neutral_grades_need_no_filter(self):
        for kind in ("wheels", "hue_curves", "secondary"):
            self.assertIsNone(gr.canonical(kind, {}), kind)
            self.assertIsNone(gr.lut_file(kind, {}), kind)
        self.assertIsNone(gr.canonical("hue_curves", {"curves": {"hue_sat": [[0.3, 0.5], [0.6, 0.5]]}}))
        self.assertIsNotNone(gr.canonical("secondary", {"view": "mask"}))

    def test_wheel_pushes_follow_the_vectorscope(self):
        # A push straight up the wheel is +Cr: towards red, with luma unchanged.
        push = gr.push_rgb(0.0, 0.1)
        self.assertGreater(push[0], 0)
        self.assertLess(push[2], push[0])
        self.assertAlmostEqual(float(gr.KR * push[0] + gr.KG * push[1] + gr.KB * push[2]), 0.0, places=9)
        y, cb, cr = gr.to_ycc(rgb(0.4, 0.4, 0.4) + push)
        self.assertAlmostEqual(float(cr[0]), 0.1, places=6)
        self.assertAlmostEqual(float(cb[0]), 0.0, places=6)
        self.assertEqual(gr.clean_wheel([3.0, 4.0, 9.0]), [0.6, 0.8, 1.0])
        self.assertEqual(gr.clean_wheel("junk"), [0.0, 0.0, 0.0])

    def test_lift_gamma_gain_offset(self):
        grey = rgb(0.5, 0.5, 0.5)
        self.assertTrue(np.allclose(gr.wheels(grey), grey))
        black, white = rgb(0, 0, 0), rgb(1, 1, 1)
        lifted = lambda c: gr.wheels(c, lift=[0, 0, 0.4])
        self.assertAlmostEqual(float(lifted(black)[0, 0]), 0.2)              # lift raises black…
        self.assertAlmostEqual(float(lifted(white)[0, 0]), 1.0)              # …and leaves white
        self.assertAlmostEqual(float(gr.wheels(grey, gain=[0, 0, 0.5])[0, 0]), 0.75)
        self.assertAlmostEqual(float(gr.wheels(black, gain=[0, 0, 0.5])[0, 0]), 0.0)
        self.assertGreater(float(gr.wheels(grey, gamma=[0, 0, 0.5])[0, 0]), 0.55)
        self.assertAlmostEqual(float(gr.wheels(black, offset=[0, 0, 0.2])[0, 0]), 0.1)
        warm = gr.wheels(grey, gain=[0, 1, 0])
        self.assertGreater(warm[0, 0], warm[0, 2])                           # gain pushed up: warmer
        flat = gr.wheels(rgb(0.9, 0.2, 0.2), saturation=0)
        self.assertTrue(np.allclose(flat, flat[0, 0]))

    def test_hue_curves_wrap_and_spare_greys(self):
        pts = [[0.1, 0.8], [0.5, 0.2]]
        self.assertAlmostEqual(float(gr.evaluate_curve(pts, 0.0, True)), float(gr.evaluate_curve(pts, 1.0, True)))
        self.assertAlmostEqual(float(gr.evaluate_curve(pts, 0.1, True)), 0.8)
        self.assertEqual(float(gr.evaluate_curve([], 0.3, True)), gr.FLAT)
        self.assertEqual(gr.clean_hue_curve([[1.0, 0.7]], True), [[0.0, 0.7]])
        greens_off = {"hue_sat": [[0.2, 0.5], [1 / 3, 0.0], [0.46, 0.5]]}
        green, red, grey = rgb(0.2, 0.8, 0.2), rgb(0.8, 0.2, 0.2), rgb(0.5, 0.5, 0.5)
        out = gr.hue_curves(np.vstack([green, red, grey]), greens_off)
        self.assertLess(np.ptp(out[0]), 0.02)                                # green lost its colour
        self.assertTrue(np.allclose(out[1], red[0], atol=1e-6))              # red untouched
        self.assertTrue(np.allclose(out[2], grey[0], atol=1e-6))
        y0 = gr.to_ycc(green)[0]
        self.assertAlmostEqual(float(gr.to_ycc(out[:1])[0][0]), float(y0[0]), places=6)   # brightness kept
        shifted = gr.hue_curves(red, {"hue_hue": [[0.0, 0.5 + 60 / 360]]})
        self.assertTrue(35 < float(gr.hue_of(shifted)[0]) * 360 < 70)       # red turned towards yellow
        dark = gr.hue_curves(np.vstack([red, grey]), {"hue_luma": [[0.0, 0.2]]})
        self.assertLess(gr.to_ycc(dark[:1])[0][0], gr.to_ycc(red)[0][0])
        self.assertTrue(np.allclose(dark[1], grey[0]))

    def test_secondary_corrects_only_its_selection(self):
        params = {"hue": 120.0, "hue_width": 40.0, "hue_soft": 10.0, "saturation": 0.0}
        green, red = rgb(0.2, 0.8, 0.2), rgb(0.8, 0.2, 0.2)
        out = gr.secondary(np.vstack([green, red]), params)
        self.assertLess(np.ptp(out[0]), 0.02)
        self.assertTrue(np.allclose(out[1], red[0]))
        mask = gr.secondary(np.vstack([green, red]), dict(params, view="mask"))
        self.assertTrue(np.allclose(mask, [[1, 1, 1], [0, 0, 0]]))
        inverted = gr.secondary(np.vstack([green, red]), dict(params, view="mask", invert=True))
        self.assertTrue(np.allclose(inverted, [[0, 0, 0], [1, 1, 1]]))
        grey = gr.secondary(rgb(0.5, 0.5, 0.5), dict(params, view="mask", sat_low=0.0))
        self.assertTrue(np.allclose(grey, 0))                                # greys have no hue to match
        pick = gr.sample_qualifier((0.8, 0.2, 0.2))
        self.assertAlmostEqual(pick["hue"], 0.0, places=1)
        self.assertTrue(pick["use_hue"] and pick["sat_high"] > pick["sat_low"])
        self.assertFalse(gr.sample_qualifier((0.5, 0.5, 0.5))["use_hue"])

    def test_lut_files_are_cached_and_match_the_maths(self):
        params = {"gain": [0.3, -0.2, 0.2], "contrast": 120.0}
        path = gr.lut_file("wheels", params)
        self.assertTrue(path.endswith(".cube") and os.path.exists(path))
        self.assertEqual(gr.lut_file("wheels", dict(params)), path)
        self.assertNotEqual(gr.lut_file("wheels", dict(params, contrast=110.0)), path)
        with open(path) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln and ln[0].isdigit()]
        self.assertEqual(len(lines), gr.LUT_SIZE ** 3)
        table = np.array([[float(v) for v in ln.split()] for ln in lines])
        self.assertTrue(np.allclose(table, gr.apply("wheels", params, gr.lattice()), atol=1e-5))
        self.assertTrue(np.allclose(gr.lattice()[1], [1 / (gr.LUT_SIZE - 1), 0, 0]))     # red changes fastest

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_ffmpeg_applies_the_lut_as_drawn(self):
        params = {"hue": 0.0, "hue_width": 60.0, "hue_shift": 120.0}
        path = gr.lut_file("secondary", params)
        colours = [(200, 40, 40), (40, 160, 40), (90, 90, 220)]
        out = []
        for c in colours:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x%02x%02x%02x:s=16x16" % c,
                                  "-frames:v", "1", "-vf", f"format=rgb24,lut3d=file={path}", "-f", "rawvideo",
                                  "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
            out.append(np.frombuffer(raw, np.uint8).reshape(16, 16, 3)[8, 8].astype(float))
        want = gr.apply("secondary", params, np.array(colours) / 255.0) * 255.0
        self.assertTrue(np.allclose(np.array(out), want, atol=4), (out, want))

    def test_presets_keep_grades(self):
        from tideline.engine.effects import EFFECTS
        self.assertEqual(presets._value(EFFECTS["wheels"].param("lift"), [0.3, 0.4, 2]), [0.3, 0.4, 1.0])
        with self.assertRaises(ValueError):
            presets._value(EFFECTS["wheels"].param("lift"), [1, 2])
        value = presets._value(EFFECTS["hue_curves"].param("curves"), {"hue_sat": [[0.2, 0.9]], "bogus": 1})
        self.assertEqual(value["hue_sat"], [[0.0, 0.9]])                     # one point: the same height all round
        self.assertEqual(value["sat_sat"], [])


if __name__ == "__main__":
    unittest.main()
