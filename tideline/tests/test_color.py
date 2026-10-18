"""Colour tools without MLT or a display: curves maths (checked against
FFmpeg's own curves filter), scope analysis, and curves in presets."""

import shutil
import subprocess
import unittest

import numpy as np

from tideline import curves as cv
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


if __name__ == "__main__":
    unittest.main()
