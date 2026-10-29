"""The RAM frame cache and the optional decoders. Needs ffmpeg and MLT; the
cached-playback test plays through MLT's sdl2_audio consumer on SDL's dummy
audio driver."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np  # noqa: E402
from gi.repository import GLib  # noqa: E402

from tideline.edits import Editor  # noqa: E402
from tideline.engine import decoders  # noqa: E402
from tideline.engine import framecache  # noqa: E402
from tideline.engine import media as mediamod  # noqa: E402
from tideline.engine.builder import Builder  # noqa: E402
from tideline.engine.mltlib import RENDER_RESCALE  # noqa: E402
from tideline.engine.player import Player  # noqa: E402
from tideline.model import Effect, Project, Sequence  # noqa: E402

TMP = None
W, H = 320, 180


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="tideline-cache-test-")
    mediamod.CACHE_DIR = os.path.join(TMP, "cache")
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:duration=4",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=48000",
                         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                         "-shortest", f"{TMP}/clip.mp4"], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0xc00000:size=200x100", "-frames:v", "1",
                         f"{TMP}/red.png"], check=True)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def pump(seconds, until=None):
    ctx = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end and not (until and until()):
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)


class Store(unittest.TestCase):
    def test_budget_drops_the_oldest_frames(self):
        c = framecache.FrameCache(0)
        c.put("s", 0, b"x" * 10, 1, 1)
        self.assertIsNone(c.get("s", 0))                     # off: nothing is kept
        c.budget = 100
        for i in range(12):
            c.put("s", i, b"x" * 10, 1, 1)
        self.assertEqual((c.used, c.get("s", 0), c.get("s", 1)), (100, None, None))
        self.assertEqual(c.ranges("s"), [(2, 12)])
        c.get("s", 2)                                        # used again: kept over 3
        c.put("t", 0, b"y" * 10, 1, 1)
        self.assertTrue(c.has("s", 2) and not c.has("s", 3))
        self.assertEqual(c.ranges("s"), [(2, 3), (4, 12)])
        self.assertEqual((c.run_ahead("s", 4, 100), c.run_ahead("s", 3, 100)), (8, 0))
        c.set_budget(0)
        self.assertEqual((c.used, c.ranges("s")), (0, []))

    def test_signature_follows_the_picture(self):
        p = Project()
        seq = Sequence.create("S", W, H, 30, 1)
        p.add_sequence(seq)
        clip = Editor(p, seq).place(p.add_media(mediamod.probe(f"{TMP}/clip.mp4")), 0)[0]
        before = framecache.signature(p, seq)
        self.assertEqual(before, framecache.signature(p, seq))
        clip.effect("opacity").params["opacity"] = 50.0
        changed = framecache.signature(p, seq)
        self.assertNotEqual(before, changed)
        del clip.effect("opacity").params["opacity"]           # undone: the old frames are good again
        self.assertEqual(before, framecache.signature(p, seq))
        self.assertNotEqual(before, framecache.signature(p, seq, (True,)))      # proxies on
        seq.playhead, seq.in_point, seq.px_per_frame = 40, 5, 3.0       # the view and marks are not the picture
        clip.name = "renamed"
        seq.audio_tracks[0].volume = -6.0
        self.assertEqual(before, framecache.signature(p, seq))


class Rendering(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.seq = Sequence.create("S", W, H, 30, 1)
        self.p.add_sequence(self.seq)
        ed = Editor(self.p, self.seq)
        clip = self.p.add_media(mediamod.probe(f"{TMP}/clip.mp4"))
        red = self.p.add_media(mediamod.probe(f"{TMP}/red.png"))
        ed.place(clip, 0, src_out=60)
        c = ed.place(red, 10, v_track=self.seq.video_tracks[1])[0]
        c.effect("motion").params.update(scale=40.0, position=[W * 0.3, H * 0.4], rotation=10.0)
        c.effects.insert(0, Effect("text", params={"text": "Cached", "size": 30, "stroke_width": 2}))
        c.effects.append(Effect("drop_shadow"))
        self.cache = framecache.FrameCache(256)
        self.sig = (framecache.signature(self.p, self.seq), (W, H))

    def render_all(self, first, last):
        graph = Builder(self.p).build(self.seq)
        job = framecache.Renderer(graph, self.cache, self.sig, first, last, (W, H), RENDER_RESCALE)
        job.render(first)
        job.start().wait(60)
        return job

    def test_pre_rendered_frames_are_the_export_frames(self):
        job = self.render_all(0, 39)
        self.assertEqual((job.done, self.cache.ranges(self.sig)), (40, [(0, 40)]))
        g = Builder(self.p).build(self.seq)
        from tideline.engine.mltlib import mlt
        for pos in (0, 9, 10, 25, 39):
            g.tractor.seek(pos)
            f = g.tractor.get_frame()
            f.set("consumer.rescale", RENDER_RESCALE)
            want = np.frombuffer(mlt.frame_get_image(f, mlt.mlt_image_rgb, W, H), np.uint8)
            got = np.frombuffer(self.cache.get(self.sig, pos)[0], np.uint8)
            self.assertLessEqual(int(np.abs(got.astype(int) - want.astype(int)).max()), 1, f"frame {pos}")

    def test_cached_stretch_plays_from_ram_like_the_live_render(self):
        graph = Builder(self.p).build(self.seq)
        player = Player("test")
        player.cache = framecache.FrameCache(256)      # a separate cache for what playback renders itself
        player.configure(scale=1.0, threads=1, drop_frames=False)
        shown = {}
        player.on_frame = lambda pixels, w, h, pos: shown.setdefault(pos, pixels)
        player.set_producer(graph.tractor, graph.profile, graph.length, refs=graph)
        player.set_cache_sig(self.sig[0])
        self.assertEqual(player.cache_key, self.sig)
        try:
            player.play()
            pump(2.0, until=lambda: max(shown, default=0) >= 45)
            player.pause()
            pump(0.3)
            live = dict(shown)
            self.assertFalse(player.cached_mode)
            self.assertGreater(len(player.cache.ranges(self.sig)), 0)     # what was shown was kept
            # Now the frames are all pre-rendered: playback stops rendering pictures.
            self.cache = player.cache
            self.render_all(0, 59)
            shown.clear()
            player.seek(0)
            pump(0.3)
            hits = player.cache_hits
            player.play()
            self.assertTrue(player.cached_mode)
            pump(2.0, until=lambda: max(shown, default=0) >= 45)
            player.pause()
            pump(0.3)
            self.assertGreater(player.cache_hits - hits, 30)
            for pos in sorted(set(live) & set(shown) - {0})[:40]:
                a, b = np.frombuffer(live[pos], np.uint8), np.frombuffer(shown[pos], np.uint8)
                self.assertLessEqual(int(np.abs(a.astype(int) - b.astype(int)).max()), 1, f"frame {pos}")
            # Past the cached frames it renders again.
            player.seek(58)
            pump(0.2)
            player.play()
            pump(1.5, until=lambda: max(shown, default=0) >= 80)
            player.pause()
            pump(0.3)
            self.assertFalse(player.cached_mode)
            self.assertGreaterEqual(max(shown), 70)
            # A changed graph is a different signature: nothing stale is shown.
            player.set_cache_sig("other")
            self.assertEqual(player.cache.run_ahead(player.cache_key, 0, 10), 0)
        finally:
            player.close()


class Decoders(unittest.TestCase):
    def tearDown(self):
        decoders.set_backend("ffmpeg")

    def test_png_encoder(self):
        img = np.zeros((3, 4, 3), np.uint8)
        img[1, 2] = (10, 200, 30)
        png = decoders.encode_png(img)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "png_pipe", "-i", "-", "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], input=png, capture_output=True, check=True).stdout
        self.assertEqual(np.frombuffer(raw, np.uint8).reshape(3, 4, 3).tolist(), img.tolist())

    def test_unknown_or_missing_decoders_fall_back_to_ffmpeg(self):
        decoders.set_backend("nope")
        self.assertEqual(decoders.current, "ffmpeg")
        decoders.set_backend("opencv")
        self.assertEqual((decoders.active("probe"), decoders.active("audio")), ("ffmpeg", "ffmpeg"))
        missing = [k for k, _ in decoders.BACKENDS if not decoders.available(k)]
        for key in missing:
            decoders.set_backend(key)
            self.assertEqual(decoders.active("thumbnail"), "ffmpeg")

    def test_every_installed_decoder_reads_the_same_media(self):
        path = f"{TMP}/clip.mp4"
        ref = decoders._IMPL["ffmpeg"]
        info = ref["probe"](path)
        thumb = ref["thumbnail"](path, 1.0, 90, False)
        sound = ref["audio"](path, 8000, None)
        tried = []
        for key, _ in decoders.BACKENDS:
            if not decoders.available(key):
                continue
            tried.append(key)
            impl = decoders._IMPL[key]
            if "probe" in impl:
                got = impl["probe"](path)
                v = [s for s in got["streams"] if s["codec_type"] == "video"][0]
                a = [s for s in got["streams"] if s["codec_type"] == "audio"][0]
                want = [s for s in info["streams"] if s["codec_type"] == "video"][0]
                self.assertEqual((v["width"], v["height"], v["avg_frame_rate"]),
                                 (want["width"], want["height"], want["avg_frame_rate"]), key)
                self.assertEqual((int(a["channels"]), int(a["sample_rate"])), (1, 48000), key)
                self.assertAlmostEqual(float(got["format"]["duration"]), 4.0, delta=0.1)
            png = impl["thumbnail"](path, 1.0, 90, False)
            self.assertEqual(png[:4], b"\x89PNG", key)
            self.assertEqual(png[16:24], thumb[16:24], key)                 # same width and height
            if "audio" in impl:
                got = impl["audio"](path, 8000, None)
                self.assertAlmostEqual(len(got), len(sound), delta=200, msg=key)
                self.assertAlmostEqual(int(np.abs(got).max()), int(np.abs(sound).max()), delta=400, msg=key)
                self.assertEqual(len(impl["audio"](path, 8000, 1.5)), 12000, key)
            # Through media.py, the way the editor asks.
            decoders.set_backend(key)
            item = mediamod.probe(path)
            self.assertEqual((item.kind, item.width, item.height, item.has_audio), ("video", W, H, True))
            img = mediamod.probe(f"{TMP}/red.png")
            self.assertEqual((img.kind, img.width), ("image", 200))
            self.assertTrue(decoders.thumbnail_png(f"{TMP}/red.png", 0, 50, True)[:4] == b"\x89PNG")
        self.assertIn("ffmpeg", tried)


if __name__ == "__main__":
    unittest.main()
