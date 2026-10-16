"""Engine tests: build real MLT graphs from the model and check rendered
pixels and audio. Needs ffmpeg and MLT; media is generated on the fly."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import wave

import numpy as np

from tideline.edits import Editor
from tideline.engine import media as mediamod
from tideline.engine.builder import Builder
from tideline.engine.effects import EFFECTS, TRANSITIONS
from tideline.engine.export import ExportJob, ExportSettings
from tideline.engine.mltlib import has_service, mlt
from tideline.model import Effect, MediaItem, Project, Sequence

TMP = None
W, H = 640, 360


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="tideline-test-")
    mediamod.CACHE_DIR = os.path.join(TMP, "cache")      # keep test thumbnails out of ~/.cache
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "10"]
    # Solid colours make pixel assertions exact; a ramp clip shows which frame is on screen.
    subprocess.run(ff + ["-f", "lavfi", "-i", f"color=c=0x00c000:size={W}x{H}:rate=30:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=48000",
                         *x264, "-c:a", "aac", "-ac", "2", f"{TMP}/green.mp4"], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", f"color=c=0x0000c0:size={W}x{H}:rate=30:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=880:duration=6:sample_rate=48000",
                         *x264, "-c:a", "aac", "-ac", "2", f"{TMP}/blue.mp4"], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i",
                         f"color=c=black:size={W}x{H}:rate=30:duration=6,geq=lum='min(255,N*2)':cb=128:cr=128",
                         *x264, f"{TMP}/ramp.mp4"], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0xc00000:size=400x400", "-frames:v", "1",
                         f"{TMP}/red.png"], check=True)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def frame(graph, pos, w=W, h=H):
    graph.tractor.seek(pos)
    f = graph.tractor.get_frame()
    f.set("consumer.rescale", "bilinear")
    data = mlt.frame_get_image(f, mlt.mlt_image_rgb, w, h)
    return np.frombuffer(data, np.uint8).reshape(h, w, 3).astype(int)


def px(img, x=None, y=None):
    h, w, _ = img.shape
    x = w // 2 if x is None else x
    y = h // 2 if y is None else y
    return img[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3).mean(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.seq = Sequence.create("S", W, H, 30, 1)
        self.p.add_sequence(self.seq)
        self.green = self.p.add_media(mediamod.probe(f"{TMP}/green.mp4"))
        self.blue = self.p.add_media(mediamod.probe(f"{TMP}/blue.mp4"))
        self.ramp = self.p.add_media(mediamod.probe(f"{TMP}/ramp.mp4"))
        self.red = self.p.add_media(mediamod.probe(f"{TMP}/red.png"))
        self.ed = Editor(self.p, self.seq)
        self.builder = Builder(self.p)
        self.v = self.seq.video_tracks
        self.a = self.seq.audio_tracks

    def build(self):
        return self.builder.build(self.seq)

    def assertColor(self, got, want, tol=12, msg=""):
        self.assertTrue(np.all(np.abs(np.array(got) - np.array(want)) <= tol),
                        f"{msg} got {np.round(got, 1)} want {want}")


GREEN, BLUE, RED, BLACK = (0, 192, 0), (0, 0, 192), (192, 0, 0), (0, 0, 0)


class Probe(Base):
    def test_probe(self):
        self.assertEqual((self.green.kind, self.green.width, self.green.height), ("video", W, H))
        self.assertTrue(self.green.has_audio and self.green.has_video)
        self.assertAlmostEqual(self.green.duration, 6.0, delta=0.1)
        self.assertEqual(self.red.kind, "image")
        self.assertFalse(self.ramp.has_audio)
        self.assertEqual(self.p.media_frames(self.green, self.seq), 180)
        self.assertIsNone(self.p.media_frames(self.red, self.seq))

    def test_thumbnail_and_peaks(self):
        png = mediamod.thumbnail_png(self.green.path, 1.0, 54)
        self.assertTrue(png and png[:4] == b"\x89PNG")
        peaks = mediamod.waveform_peaks(self.green.path)
        self.assertAlmostEqual(len(peaks), 600, delta=10)
        self.assertGreater(int(peaks.max()), 20)


class Graphs(Base):
    def test_cuts_gaps_and_track_order(self):
        self.ed.place(self.green, 0, src_out=60)
        self.ed.place(self.blue, 90, src_out=60)
        self.ed.place(self.red, 30, v_track=self.v[1], src_in=0, src_out=30)
        g = self.build()
        self.assertEqual(g.length, 150)
        self.assertColor(px(frame(g, 10)), GREEN)
        self.assertColor(px(frame(g, 40)), RED, msg="V2 covers V1")
        self.assertColor(px(frame(g, 75)), BLACK, msg="gap")
        self.assertColor(px(frame(g, 100)), BLUE)
        # The square still is fitted to frame height, leaving V1 visible at the sides.
        self.assertColor(px(frame(g, 40), x=20), GREEN)

    def test_source_frames_follow_in_point_and_trim(self):
        self.ed.place(self.ramp, 0, src_in=40, src_out=100)
        g = self.build()
        self.assertAlmostEqual(px(frame(g, 0))[0], 80, delta=8)
        self.assertAlmostEqual(px(frame(g, 30))[0], 140, delta=8)
        self.ed.trim(self.v[0].clips[0].id, "start", 20)
        g = self.build()
        self.assertAlmostEqual(px(frame(g, 20))[0], 120, delta=8)

    def test_hidden_track_and_disabled_clip(self):
        self.ed.place(self.green, 0, src_out=60)
        (c,) = self.ed.place(self.red, 0, v_track=self.v[1], src_in=0, src_out=60)
        self.v[1].hidden = True
        self.assertColor(px(frame(self.build(), 10)), GREEN)
        self.v[1].hidden = False
        c.enabled = False
        self.assertColor(px(frame(self.build(), 10)), GREEN)
        c.enabled = True
        self.assertColor(px(frame(self.build(), 10)), RED)

    def test_opacity_keyframes_and_live_update(self):
        self.ed.place(self.blue, 0, src_out=90)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=90)
        op = c.effect("opacity")
        op.keyframes["opacity"] = [[0, 0.0, "linear"], [60, 100.0, "linear"]]
        g = self.build()
        self.assertColor(px(frame(g, 0)), BLUE)
        self.assertColor(px(frame(g, 30)), (0, 96, 96), tol=16)
        self.assertColor(px(frame(g, 60)), GREEN)
        op.keyframes = {}
        op.params["opacity"] = 25.0
        self.assertTrue(g.update_effect(op))
        self.assertColor(px(frame(g, 30)), (0, 48, 144), tol=16)

    def test_keyframes_stay_with_source_after_trim(self):
        self.ed.place(self.blue, 0, src_out=120)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=120)
        c.effect("opacity").keyframes["opacity"] = [[0, 0.0, "hold"], [60, 100.0, "linear"]]
        self.ed.trim(c.id, "start", 30)
        g = self.build()
        self.assertColor(px(frame(g, 45)), BLUE)
        self.assertColor(px(frame(g, 61)), GREEN)

    def test_colour_is_stable_across_preview_scale_and_transform(self):
        # Regression: BT.601 sources shifted colour when scaled in RGB.
        (c, _) = self.ed.place(self.green, 0, src_out=60)
        g = self.build()
        self.assertColor(px(frame(g, 5)), GREEN)
        self.assertColor(px(frame(g, 5, W // 2, H // 2)), GREEN, msg="half-resolution preview")
        c.effect("motion").params["scale"] = 80.0
        self.assertColor(px(frame(self.build(), 5)), GREEN, msg="scaled clip")

    def test_motion_scale_and_position(self):
        self.ed.place(self.blue, 0, src_out=60)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=60)
        m = c.effect("motion")
        m.params.update(scale=50.0, position=[W * 0.25, H * 0.25])
        img = frame(self.build(), 5)
        self.assertColor(px(img, int(W * 0.25), int(H * 0.25)), GREEN)
        self.assertColor(px(img, int(W * 0.75), int(H * 0.75)), BLUE)
        self.assertColor(px(img, int(W * 0.25), int(H * 0.75)), BLUE)
        m.keyframes["position"] = [[0, [W * 0.25, H * 0.25], "linear"], [40, [W * 0.75, H * 0.75], "linear"]]
        img = frame(self.build(), 40)
        self.assertColor(px(img, int(W * 0.75), int(H * 0.75)), GREEN)
        self.assertColor(px(img, int(W * 0.25), int(H * 0.25)), BLUE)

    def test_crop_and_generic_effects(self):
        self.ed.place(self.blue, 0, src_out=60)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=60)
        c.effects.append(Effect("crop", params={"left": 50.0}))
        img = frame(self.build(), 5)
        self.assertColor(px(img, int(W * 0.25)), BLUE)
        self.assertColor(px(img, int(W * 0.75)), GREEN)
        c.effects[-1] = Effect("invert")
        got = px(frame(self.build(), 5))
        self.assertGreater(got[0], 150)
        self.assertLess(got[1], 110)

    def test_every_registered_effect_builds_and_renders(self):
        (c, a) = self.ed.place(self.green, 0, src_out=30)
        missing = []
        for key, edef in EFFECTS.items():
            if edef.intrinsic:
                continue
            if not edef.available():
                missing.append(key)
                continue
            target = c if edef.kind == "video" else a
            target.effects.append(Effect(key))
        g = self.build()
        self.assertEqual(g.missing, set())
        img = frame(g, 5)
        self.assertEqual(img.shape, (H, W, 3))
        print(f"\n  effects unavailable in this MLT build: {missing or 'none'}", end="")

    def animated(self, key, **params):
        """A green clip over a blue one, with one effect on the green clip."""
        self.ed.place(self.blue, 0, src_out=90)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=90)
        effect = Effect(key, params=params)
        c.effects.append(effect)
        return c, effect

    def test_oscillate_moves_the_picture_with_the_wave(self):
        self.animated("oscillate", wave="square", x=200.0, y=0.0, frequency=1.0)
        g = self.build()
        right, left = frame(g, 5), frame(g, 20)             # first and second half of the cycle
        self.assertColor(px(right, 20), BLUE)
        self.assertColor(px(right, 620), GREEN)
        self.assertColor(px(left, 20), GREEN)
        self.assertColor(px(left, 620), BLUE)

    def test_shake_is_keyframable_and_can_hide_its_edges(self):
        c, shake = self.animated("shake", x=100.0, y=60.0, rotation=5.0)
        g = self.build()
        corners = [(2, 2), (637, 2), (2, 357), (637, 357)]
        for pos in (3, 9, 17, 40):
            img = frame(g, pos)
            for x, y in corners:
                self.assertColor(px(img, x, y), GREEN, msg=f"frame {pos}")
        rect = g.filters[shake.id][0][1][0].get("transition.rect")
        self.assertEqual(len(rect.split(";")), 90)
        self.assertGreater(len(set(rect.split(";")[i].split("=")[1] for i in range(90))), 80)
        shake.params["fit"] = False
        self.assertTrue(g.update_effect(shake))
        uncovered = sum(not np.all(np.abs(px(frame(g, pos), x, y) - GREEN) <= 12)
                        for pos in (3, 9, 17, 40) for x, y in corners)
        self.assertGreater(uncovered, 4)
        shake.keyframes["amount"] = [[0, 0.0, "linear"], [60, 100.0, "linear"]]
        self.assertTrue(g.update_effect(shake))
        for x, y in corners:
            self.assertColor(px(frame(g, 0), x, y), GREEN)

    def test_spin_pulse_and_pan_zoom(self):
        c, spin = self.animated("spin", speed=0.25)
        g = self.build()
        self.assertColor(px(frame(g, 0), 20), GREEN)
        self.assertColor(px(frame(g, 30), 20), BLUE)        # a quarter turn: the frame stands on end
        self.assertColor(px(frame(g, 30), 320, 20), GREEN)
        c.effects[-1] = pulse = Effect("pulse", params={"size": -50.0, "bpm": 60.0})
        g = self.build()
        self.assertColor(px(frame(g, 0), 20), BLUE)         # on the beat
        self.assertColor(px(frame(g, 29), 20), GREEN)       # decayed before the next one
        c.effects[-1] = Effect("pan_zoom", params={"start_scale": 50.0, "end_scale": 100.0, "ease": "linear"})
        g = self.build()
        self.assertColor(px(frame(g, 0), 20), BLUE)
        self.assertColor(px(frame(g, 0)), GREEN)
        self.assertColor(px(frame(g, 89), 20), GREEN)       # ends exactly on the clip's last frame
        self.assertTrue(pulse.id not in g.filters)

    def test_strobe_and_flicker(self):
        c, strobe = self.animated("strobe", frequency=5.0)
        g = self.build()
        self.assertColor(px(frame(g, 0)), GREEN)
        self.assertColor(px(frame(g, 4)), BLUE)
        self.assertColor(px(frame(g, 6)), GREEN)
        c.effects[-1] = Effect("flicker", params={"amount": 100.0})
        g = self.build()
        levels = [px(frame(g, pos))[1] for pos in range(0, 30, 2)]
        self.assertLess(min(levels), 80)
        self.assertGreater(max(levels) - min(levels), 60)
        self.assertTrue(all(v <= 195 for v in levels))

    def test_generated_motion_continues_through_a_transition(self):
        (a, _) = self.ed.place(self.green, 0, src_in=30, src_out=90)
        (b, _) = self.ed.place(self.blue, 60, src_in=30, src_out=90)
        spin = Effect("spin")
        a.effects.append(spin)
        self.ed.add_transition(a.id, "end", "cross_dissolve", 20)
        g = self.build()
        ranges = sorted(ctx["range"] for _, _, ctx in g.filters[spin.id])
        self.assertEqual(ranges, [(30, 89), (30, 99)])      # the dissolve plays the clip's tail handle too
        self.assertTrue(all(ctx["clip"] == (30, 89) for _, _, ctx in g.filters[spin.id]))

    def test_title_clip_renders_text(self):
        title = self.p.add_media(MediaItem(name="Title", kind="title", has_video=True,
                                           params={"text": "HELLO", "size": 200, "fill": "#ffffffff"}))
        self.ed.place(self.blue, 0, src_out=60)
        self.ed.place(title, 0, v_track=self.v[1], src_in=0, src_out=60)
        img = frame(self.build(), 5)
        white = ((img[..., 0] > 200) & (img[..., 1] > 200) & (img[..., 2] > 200)).sum()
        self.assertGreater(white, 2000)
        self.assertColor(px(img, 10, 10), BLUE)

    def title(self, **params):
        title = self.p.add_media(MediaItem(name="Title", kind="title", has_video=True,
                                           params=dict({"text": "HELLO", "size": 100, "fill": "#ffffffff"}, **params)))
        self.ed.place(self.blue, 0, src_out=90)
        (clip,) = self.ed.place(title, 0, v_track=self.v[1], src_in=0, src_out=90)
        return clip.effect("text")

    @staticmethod
    def ink(img):
        return int(((img[..., 0] > 200) & (img[..., 1] > 200) & (img[..., 2] > 200)).sum())

    def test_text_content_and_size_keyframes(self):
        text = self.title()
        text.keyframes = {"text": [[0, "I", "hold"], [30, "WWWWWW", "hold"], [60, "", "hold"]]}
        g = self.build()
        ink = [self.ink(frame(g, t)) for t in (0, 29, 30, 59, 60, 89)]
        self.assertEqual(ink[0], ink[1])
        self.assertGreater(ink[2], ink[1] * 4)                  # the words change exactly on the keyframe
        self.assertEqual(ink[2], ink[3])
        self.assertEqual(ink[4:], [0, 0])
        self.assertEqual(len(g.filters[text.id]), 3)
        self.assertTrue(g.update_effect(text))
        text.keyframes["text"][1][0] = 40                       # a moved step needs a new set of filters
        self.assertFalse(g.update_effect(text))
        text.keyframes = {"size": [[10, 40, "linear"], [40, 200, "ease"]]}
        g = self.build()
        ink = [self.ink(frame(g, t)) for t in (0, 10, 20, 30, 40, 80)]
        self.assertEqual(ink[0], ink[1])
        self.assertTrue(ink[1] < ink[2] < ink[3] < ink[4], ink)  # grows on every sampled frame
        self.assertEqual(ink[4], ink[5])

    def test_colour_keyframes(self):
        text = self.title(text="██████", size=200)
        text.keyframes = {"fill": [[0, "#ff0000ff", "linear"], [60, "#00ff00ff", "linear"]]}
        g = self.build()
        self.assertColor(px(frame(g, 0)), (255, 0, 0), tol=20)
        self.assertColor(px(frame(g, 30)), (128, 128, 0), tol=24)
        self.assertColor(px(frame(g, 75)), (0, 255, 0), tol=20)
        self.assertEqual(len(g.filters[text.id]), 1)            # MLT animates colours itself
        self.ed.place(self.green, 100, src_out=60)
        clip = self.v[0].clip_at(100)
        clip.effects.append(Effect("tint", params={"amount": 100.0, "black": "#000000ff"},
                                   keyframes={"white": [[0, "#ff0000ff", "ease"], [40, "#0000ffff", "linear"]]}))
        g = self.build()
        first, middle, last = px(frame(g, 100)), px(frame(g, 120)), px(frame(g, 150))
        self.assertGreater(first[0] - first[2], 60)              # a generic effect's colour, eased
        self.assertLess(abs(middle[0] - middle[2]), 30)
        self.assertGreater(last[2] - last[0], 60)

    def test_choice_and_switch_keyframes(self):
        self.ed.place(self.blue, 0, src_out=90)
        (c, _) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=90)
        opacity = c.effect("opacity")
        opacity.keyframes = {"blend": [[0, "normal", "hold"], [30, "add", "hold"], [60, "normal", "hold"]]}
        g = self.build()
        self.assertColor(px(frame(g, 29)), GREEN)
        self.assertColor(px(frame(g, 30)), (0, 192, 192))        # green added to blue
        self.assertColor(px(frame(g, 59)), (0, 192, 192))
        self.assertColor(px(frame(g, 60)), GREEN)
        opacity.keyframes["opacity"] = [[30, 100.0, "linear"], [50, 0.0, "linear"]]
        g = self.build()
        self.assertColor(px(frame(g, 20)), GREEN)                # opacity keys keep their time inside a segment
        self.assertColor(px(frame(g, 40)), (0, 96, 192), tol=14)
        self.assertColor(px(frame(g, 55)), BLUE)
        c.effects.append(Effect("threshold", keyframes={"invert": [[0, False, "hold"], [45, True, "hold"]]}))
        c.effect("opacity").keyframes = {}
        g = self.build()
        self.assertNotEqual(tuple(px(frame(g, 44)) > 128), tuple(px(frame(g, 45)) > 128))

    def test_generated_effects_take_keyframes_on_every_setting(self):
        c, osc = self.animated("oscillate", wave="square", x=200.0, y=0.0, frequency=1.0)
        osc.keyframes = {"x": [[0, 200.0, "hold"], [30, 0.0, "hold"]]}
        g = self.build()
        self.assertColor(px(frame(g, 5), 20), BLUE)              # pushed right for the first second
        self.assertColor(px(frame(g, 35), 20), GREEN)            # then the keyframed distance is zero
        self.assertColor(px(frame(g, 50), 20), GREEN)
        c.effects[-1] = spin = Effect("spin", keyframes={"speed": [[0, 0.0, "hold"], [30, 0.25, "hold"]]})
        g = self.build()
        self.assertColor(px(frame(g, 29), 20), GREEN)            # not turning yet
        self.assertColor(px(frame(g, 60), 20), BLUE)             # a quarter turn after one second at speed
        self.assertTrue(g.update_effect(spin))

    def test_every_parameter_renders_with_keyframes(self):
        self.ed.place(self.blue, 0, src_out=30)
        (v, au) = self.ed.place(self.green, 0, v_track=self.v[1], a_track=self.a[1], src_out=30)
        failed = []
        for key, edef in EFFECTS.items():
            clip = v if edef.kind == "video" else au
            if not edef.available():
                continue
            for p in edef.params:
                if not p.key or p.type in ("text", "font", "file"):
                    continue
                if p.type == "bool":
                    a, b = False, True
                elif p.type == "choice":
                    a, b = p.choices[0][0], p.choices[-1][0]
                elif p.type == "color":
                    a, b = "#ff000080", "#0000ffff"
                elif p.type == "point":
                    a, b = [0.0, 0.0], [300.0, 200.0]
                else:
                    lo = p.min if p.min is not None else -10.0
                    hi = p.max if p.max is not None else 10.0
                    a, b = lo + (hi - lo) * 0.2, lo + (hi - lo) * 0.6
                    a, b = (int(a), int(b)) if p.type == "int" else (a, b)
                interp = "hold" if p.hold else "ease"
                effect = Effect(key, keyframes={p.name: [[4, a, interp], [9, b, interp]]})
                saved = clip.effects
                clip.effects = [e for e in saved if e.type != key] + [effect]
                try:
                    g = self.build()
                    frame(g, 6, 160, 90)
                    if not g.filters.get(effect.id):
                        failed.append(f"{key}.{p.name}: no filters")
                except Exception as exc:                          # noqa: BLE001 - report every parameter
                    failed.append(f"{key}.{p.name}: {exc!r}")
                clip.effects = saved
        self.assertEqual(failed, [])

    def test_speed_and_reverse(self):
        (c,) = self.ed.place(self.ramp, 0, src_in=0, src_out=120)
        self.ed.set_speed([c.id], 2.0)
        self.assertEqual(c.duration, 60)
        g = self.build()
        self.assertAlmostEqual(px(frame(g, 30))[0], 120, delta=12)
        self.ed.set_speed([c.id], 1.0, reverse=True)
        g = self.build()
        first, later = px(frame(g, 0))[0], px(frame(g, 40))[0]
        self.assertGreater(first, later + 40)

    def test_nested_sequence(self):
        inner = Sequence.create("Inner", W, H, 30, 1)
        item = self.p.add_sequence(inner)
        Editor(self.p, inner).place(self.green, 0, src_out=60)
        self.ed.place(item, 30)
        g = self.build()
        self.assertColor(px(frame(g, 10)), BLACK)
        self.assertColor(px(frame(g, 50)), GREEN)
        self.assertEqual(g.length, 90)
        # A sequence nested inside itself must not recurse forever.
        Editor(self.p, inner).place(item, 100)
        self.build()

    def test_producers_are_reused_between_builds(self):
        self.ed.place(self.green, 0, src_out=60)
        self.build()
        before = {k: id(v[0]) for k, v in self.builder.cache.items.items()}
        self.ed.split(30)
        self.build()
        after = {k: id(v[0]) for k, v in self.builder.cache.items.items()}
        self.assertTrue(set(before) & set(after))
        for k in set(before) & set(after):
            self.assertEqual(before[k], after[k])


class Transitions(Base):
    def two(self, a_in=30, b_in=30):
        self.ed.place(self.green, 0, src_in=a_in, src_out=a_in + 60)
        self.ed.place(self.blue, 60, src_in=b_in, src_out=b_in + 60)
        return self.v[0].clips

    def test_cross_dissolve(self):
        a, b = self.two()
        self.ed.add_transition(a.id, "end", "cross_dissolve", 20)
        g = self.build()
        self.assertEqual(g.length, 120)
        self.assertColor(px(frame(g, 45)), GREEN)
        self.assertColor(px(frame(g, 60)), (0, 96, 96), tol=20)
        self.assertColor(px(frame(g, 75)), BLUE)

    def test_dip_to_black(self):
        a, b = self.two()
        self.ed.add_transition(a.id, "end", "dip_black", 20)
        g = self.build()
        self.assertColor(px(frame(g, 45)), GREEN)
        self.assertColor(px(frame(g, 60)), BLACK, tol=30)
        self.assertColor(px(frame(g, 75)), BLUE)
        mid_a = px(frame(g, 55))
        self.assertTrue(0 < mid_a[1] < 180 and mid_a[2] < 20, mid_a)

    def test_wipes_and_slides_build(self):
        a, b = self.two()
        for key, tdef in TRANSITIONS.items():
            if tdef.kind != "video":
                continue
            self.ed.add_transition(a.id, "end", key, 20)
            g = self.build()
            self.assertEqual(g.missing, set(), key)
            self.assertColor(px(frame(g, 45)), GREEN, msg=key)
            self.assertColor(px(frame(g, 75)), BLUE, msg=key)
            mid = frame(g, 60)
            left, right = px(mid, 30), px(mid, W - 30)
            if key == "wipe_right":
                self.assertColor(left, BLUE, msg=key)
                self.assertColor(right, GREEN, msg=key)
            if key == "wipe_left":
                self.assertColor(left, GREEN, msg=key)
                self.assertColor(right, BLUE, msg=key)

    def test_missing_handles_shorten_transition(self):
        a, b = self.two(a_in=120, b_in=0)      # A has no tail, B has no head
        self.ed.add_transition(a.id, "end", "cross_dissolve", 20)
        g = self.build()
        self.assertEqual(g.length, 120)
        self.assertColor(px(frame(g, 59)), GREEN)
        self.assertColor(px(frame(g, 60)), BLUE)

    def test_one_sided_fade(self):
        self.ed.place(self.red, 0, src_in=0, src_out=120)
        (c, _) = self.ed.place(self.green, 30, v_track=self.v[1], a_track=self.a[1], src_out=60)
        self.ed.add_transition(c.id, "start", "cross_dissolve", 20)
        self.ed.add_transition(c.id, "end", "cross_dissolve", 20)
        g = self.build()
        self.assertColor(px(frame(g, 30)), RED, tol=20)
        self.assertColor(px(frame(g, 60)), GREEN)
        self.assertColor(px(frame(g, 89)), RED, tol=24)
        mid = px(frame(g, 40))
        self.assertTrue(40 < mid[0] < 160 and 40 < mid[1] < 160, mid)


class Audio(Base):
    def render(self, name="out.wav"):
        g = self.build()
        path = f"{TMP}/{name}"
        c = mlt.Consumer(g.profile, "avformat", path)
        for k, v in (("acodec", "pcm_s16le"), ("vn", 1), ("ar", 48000), ("channels", 2),
                     ("real_time", -1), ("terminate_on_pause", 1)):
            c.set(k, v)
        c.connect(g.tractor)
        g.tractor.seek(0)
        g.tractor.set_speed(1)
        c.start()
        while not c.is_stopped():
            time.sleep(0.01)
        with wave.open(path) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, 2) / 32768.0
        return a

    @staticmethod
    def rms(a, t0, t1, ch=0):
        seg = a[int(t0 * 48000):int(t1 * 48000), ch]
        return float(np.sqrt((seg ** 2).mean())) if len(seg) else 0.0

    def test_volume_pan_mute_solo(self):
        (_, a1) = self.ed.place(self.green, 0, src_out=90)
        ref = self.rms(self.render(), 0.5, 2.5)
        self.assertGreater(ref, 0.02)
        a1.effect("volume").params["level"] = -6.0
        self.assertAlmostEqual(self.rms(self.render(), 0.5, 2.5) / ref, 0.5, delta=0.05)
        a1.effect("volume").params["level"] = 0.0
        a1.effect("pan").params["balance"] = -100.0
        out = self.render()
        self.assertGreater(self.rms(out, 0.5, 2.5, 0), ref * 0.8)
        self.assertLess(self.rms(out, 0.5, 2.5, 1), ref * 0.05)
        a1.effect("pan").params["balance"] = 0.0
        self.a[0].hidden = True
        self.assertLess(self.rms(self.render(), 0.5, 2.5), 0.001)
        self.a[0].hidden = False
        self.ed.place(self.blue, 0, v_track=self.v[1], a_track=self.a[1], src_out=90)
        both = self.rms(self.render(), 0.5, 2.5)
        self.assertGreater(both, ref * 1.2)
        self.a[1].solo = True
        self.assertAlmostEqual(self.rms(self.render(), 0.5, 2.5), ref, delta=ref * 0.1)

    def test_audio_crossfade_and_fades(self):
        self.ed.place(self.green, 0, src_in=30, src_out=90)
        self.ed.place(self.blue, 60, src_in=30, src_out=90)
        a, b = self.a[0].clips
        self.ed.add_transition(a.id, "end", "constant_power", 30)
        self.ed.add_transition(a.id, "start", "constant_gain", 30)
        out = self.render()
        self.assertAlmostEqual(len(out) / 48000, 4.0, delta=0.1)
        full = self.rms(out, 1.2, 1.4)
        self.assertLess(self.rms(out, 0.0, 0.1), full * 0.3)        # fade in
        self.assertGreater(self.rms(out, 1.9, 2.1), full * 0.8)     # crossfade keeps level
        spectrum = np.abs(np.fft.rfft(out[int(2.8 * 48000):int(3.3 * 48000), 0]))
        freq = np.fft.rfftfreq(int(0.5 * 48000), 1 / 48000)[spectrum.argmax()]
        self.assertAlmostEqual(freq, 880, delta=10)


class Export(Base):
    def run_job(self, settings):
        self.ed.place(self.green, 0, src_out=45)
        self.ed.place(self.blue, 45, src_out=45)
        done = []
        job = ExportJob(self.p, self.seq, settings, on_done=done.append)
        job.start()
        t0 = time.time()
        while not done and time.time() - t0 < 60:
            time.sleep(0.05)
        self.assertTrue(done, "export did not finish")
        self.assertEqual(job.error, "")
        return job

    def probe_out(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_name,width,height,nb_frames", "-of", "csv=p=0", path],
                             capture_output=True, text=True).stdout.split()
        return out

    def test_h264_full_sequence(self):
        path = f"{TMP}/export.mp4"
        self.run_job(ExportSettings(format="h264", path=path, quality="low"))
        streams = self.probe_out(path)
        self.assertIn(f"h264,{W},{H},90", streams)
        self.assertTrue(any(s.startswith("aac") for s in streams))

    def test_range_and_scaled(self):
        path = f"{TMP}/export_scaled.mp4"
        self.run_job(ExportSettings(format="h264", path=path, quality="low", width=320, height=180,
                                    range_in=30, range_out=60, audio=False))
        self.assertEqual(self.probe_out(path), ["h264,320,180,30"])

    def test_every_format_preset_encodes(self):
        from tideline.engine.export import FORMATS
        self.ed.place(self.green, 0, src_out=20)
        failed = []
        for fmt in FORMATS:
            path = f"{TMP}/fmt_{fmt.key}{fmt.ext}"
            done = []
            job = ExportJob(self.p, self.seq, ExportSettings(format=fmt.key, path=path, quality="low"),
                            on_done=done.append)
            job.start()
            t0 = time.time()
            while not done and time.time() - t0 < 90:
                time.sleep(0.05)
            out = path if not fmt.image_sequence else f"{TMP}/fmt_{fmt.key}_00001{fmt.ext}"
            if job.error or not os.path.exists(out) or os.path.getsize(out) == 0:
                failed.append((fmt.key, job.error[-200:]))
        self.assertEqual(failed, [])

    def test_audio_only(self):
        path = f"{TMP}/export.flac"
        self.run_job(ExportSettings(format="flac", path=path))
        self.assertEqual(self.probe_out(path)[0].split(",")[0], "flac")


if __name__ == "__main__":
    unittest.main()
