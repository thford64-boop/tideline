"""Proxy tests: making proxies with FFmpeg, the job queue, saving them with
the project, and which file the preview and the export read."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

import numpy as np

from tideline import proxies
from tideline.edits import Editor
from tideline.engine import media as mediamod
from tideline.engine.builder import Builder
from tideline.engine.mltlib import mlt
from tideline.model import MediaItem, Project, Sequence

TMP = None
W, H = 1280, 720


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="tideline-proxy-test-")
    proxies.CACHE_DIR = os.path.join(TMP, "cache")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=0x00c000:size={W}x{H}:rate=30:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=48000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    f"{TMP}/big.mp4"], check=True)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def big_media():
    return mediamod.probe(f"{TMP}/big.mp4")


def frame(graph, pos, w=320, h=180):
    graph.tractor.seek(pos)
    f = graph.tractor.get_frame()
    data = mlt.frame_get_image(f, mlt.mlt_image_rgb, w, h)
    return np.frombuffer(data, np.uint8).reshape(h, w, 3).astype(int)


class Helpers(unittest.TestCase):
    def test_eligible_and_worth_it(self):
        video = MediaItem(name="v", kind="video", path="/v.mp4", has_video=True, width=3840, height=2160)
        self.assertTrue(proxies.eligible(video))
        self.assertTrue(proxies.worth_it(video, 1080))
        self.assertFalse(proxies.worth_it(video, 2160))
        portrait = MediaItem(name="p", kind="video", path="/p.mp4", has_video=True, width=1080, height=1920)
        self.assertFalse(proxies.worth_it(portrait, 1080))      # the short side counts
        self.assertTrue(proxies.worth_it(portrait, 720))
        for kind in ("audio", "image", "title", "sequence"):
            self.assertFalse(proxies.eligible(MediaItem(name="x", kind=kind, path="/x", has_video=True)))

    def test_commands(self):
        land = MediaItem(name="v", kind="video", path="/v.mp4", has_video=True, width=1920, height=1080)
        cmd = proxies.ffmpeg_command("/v.mp4", "/out.mov", land, 540, "mjpeg")
        self.assertIn("scale=-2:540:flags=bicubic:out_color_matrix=bt709", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "mjpeg")
        self.assertEqual(cmd[-1], "/out.mov")
        tall = MediaItem(name="t", kind="video", path="/t.mp4", has_video=True, width=1080, height=1920)
        cmd = proxies.ffmpeg_command("/t.mp4", "/out.mov", tall, 720, "h264")
        self.assertIn("scale=720:-2:flags=bicubic:out_color_matrix=bt709", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")

    def test_folder(self):
        p = Project()
        m = MediaItem(name="v", kind="video", path=f"{TMP}/big.mp4", has_video=True)
        self.assertEqual(proxies.folder_for(p, "project"), proxies.CACHE_DIR)    # unsaved: cache
        p.path = "/work/film/film.tideline"
        self.assertEqual(proxies.folder_for(p, "project"), "/work/film/Proxies")
        self.assertEqual(proxies.folder_for(p, "cache"), proxies.CACHE_DIR)
        path = proxies.target_path(p, m, "project")
        self.assertTrue(path.startswith("/work/film/Proxies/big-") and path.endswith(proxies.SUFFIX))


class Making(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p-", dir=TMP)

    def test_job_writes_a_matching_proxy(self):
        media = big_media()
        dest = os.path.join(self.dir, "big" + proxies.SUFFIX)
        job = proxies.Job(media, dest, size=360, codec="mjpeg")
        seen = []
        self.assertTrue(job.run(lambda j: seen.append(j.progress)), job.error)
        self.assertTrue(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(job.progress, 1.0)
        info = mediamod.probe(dest)
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration, media.duration, delta=0.1)
        self.assertAlmostEqual(info.fps, media.fps, delta=0.01)

    def test_h264(self):
        media = big_media()
        dest = os.path.join(self.dir, "h" + proxies.SUFFIX)
        job = proxies.Job(media, dest, size=360, codec="h264")
        self.assertTrue(job.run(), job.error)
        self.assertEqual(mediamod.probe(dest).video_codec, "h264")

    def test_failure_cleans_up(self):
        media = big_media()
        media.path = os.path.join(self.dir, "missing.mp4")
        dest = os.path.join(self.dir, "m" + proxies.SUFFIX)
        job = proxies.Job(media, dest, size=360)
        self.assertFalse(job.run())
        self.assertTrue(job.error)
        self.assertFalse(os.path.exists(dest) or os.path.exists(dest + ".part"))

    def test_queue(self):
        media = big_media()
        done, progress = [], []
        finished = threading.Event()

        def on_done(job, ok):
            done.append((job.media_id, ok))
            if len(done) == 2:
                finished.set()

        q = proxies.Queue(on_progress=lambda j, i, n: progress.append((i, n)), on_done=on_done)
        other = big_media()
        jobs = [proxies.Job(media, os.path.join(self.dir, "a" + proxies.SUFFIX), 360),
                proxies.Job(other, os.path.join(self.dir, "b" + proxies.SUFFIX), 360)]
        self.assertEqual(q.submit(jobs), 2)
        self.assertEqual(q.submit(jobs[:1]), 0)          # already queued
        self.assertIn(q.state(media.id), ("creating", "queued"))
        self.assertTrue(finished.wait(60))
        q.wait(5)
        self.assertEqual([ok for _, ok in done], [True, True])
        self.assertEqual(progress[0], (1, 2))
        self.assertEqual(progress[-1][0], 2)
        self.assertFalse(q.busy())
        self.assertIsNone(q.state(media.id))

    def test_cancel(self):
        media = big_media()
        q = proxies.Queue()
        job = proxies.Job(media, os.path.join(self.dir, "c" + proxies.SUFFIX), 360)
        q.submit([job])
        q.cancel()
        q.wait(30)
        self.assertFalse(q.busy())
        self.assertFalse(os.path.exists(job.dest + ".part"))


class InProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="proj-", dir=TMP)
        # Make the "proxy" visibly different (blue) so frames show which file played.
        cls.proxy = os.path.join(cls.dir, "blue" + proxies.SUFFIX)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=0x0000c0:size=640x360:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=48000",
                        "-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p", "-c:a", "pcm_s16le", "-f", "mov",
                        cls.proxy], check=True)

    def setUp(self):
        self.project = Project()
        self.seq = Sequence.create("S", 320, 180, 30, 1)
        self.project.add_sequence(self.seq)
        self.media = self.project.add_media(big_media())
        Editor(self.project, self.seq).place(self.media, 0, src_out=60)
        proxies.attach(self.project, self.media, self.proxy)

    def test_preview_reads_proxy_export_reads_original(self):
        preview = Builder(self.project, proxies=True)
        self.assertEqual(preview.source_path(self.media), (self.proxy, True))
        img = frame(preview.build(self.seq), 10)
        self.assertGreater(img[90, 160][2], 150)               # blue: the proxy
        plain = Builder(self.project)
        self.assertEqual(plain.source_path(self.media), (self.media.path, False))
        img = frame(plain.build(self.seq), 10)
        self.assertGreater(img[90, 160][1], 150)               # green: the original
        xml = os.path.join(self.dir, "export.mlt")
        plain.write_xml(self.seq, xml)
        with open(xml) as fh:
            text = fh.read()
        self.assertIn("big.mp4", text)
        self.assertNotIn(proxies.SUFFIX, text)

    def test_toggle_reuses_builder(self):
        b = Builder(self.project, proxies=True)
        self.assertGreater(frame(b.build(self.seq), 5)[90, 160][2], 150)
        b.proxies = False
        self.assertGreater(frame(b.build(self.seq), 5)[90, 160][1], 150)

    def test_source_monitor_uses_proxy(self):
        profile, producer, length, refs = Builder(self.project, proxies=True).build_media(self.media, self.seq)
        self.assertEqual(producer.get("resource"), self.proxy)

    def test_offline_original_plays_proxy(self):
        self.media.offline = True
        img = frame(Builder(self.project, proxies=True).build(self.seq), 10)
        self.assertGreater(img[90, 160][2], 150)
        self.media.offline = False

    def test_save_load_and_undo(self):
        folder = tempfile.mkdtemp(prefix="saved-", dir=TMP)
        path = os.path.join(folder, "film.tideline")
        self.project.save(path)
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["proxies"][self.media.id]["path"], self.proxy)
        self.assertIn("relpath", data["proxies"][self.media.id])
        self.assertNotIn("proxies", self.project.to_dict())    # undo history never holds them
        loaded = Project.load(path)
        self.assertEqual(proxies.find(loaded, loaded.media[self.media.id]), self.proxy)
        # Restoring an undo snapshot keeps the proxy.
        loaded.load_dict(loaded.to_dict())
        self.assertEqual(proxies.find(loaded, loaded.media[self.media.id]), self.proxy)

    def test_moved_project_finds_proxies_by_relative_path(self):
        folder = tempfile.mkdtemp(prefix="move-", dir=TMP)
        os.makedirs(os.path.join(folder, "film", "Proxies"))
        shutil.copy(f"{TMP}/big.mp4", os.path.join(folder, "film", "big.mp4"))
        moved_proxy = os.path.join(folder, "film", "Proxies", "big" + proxies.SUFFIX)
        shutil.copy(self.proxy, moved_proxy)
        p = Project()
        m = p.add_media(mediamod.probe(os.path.join(folder, "film", "big.mp4")))
        proxies.attach(p, m, moved_proxy)
        p.save(os.path.join(folder, "film", "film.tideline"))
        shutil.move(os.path.join(folder, "film"), os.path.join(folder, "film2"))
        loaded = Project.load(os.path.join(folder, "film2", "film.tideline"))
        self.assertEqual(proxies.find(loaded, loaded.media[m.id]),
                         os.path.join(folder, "film2", "Proxies", "big" + proxies.SUFFIX))

    def test_changed_original_makes_proxy_stale(self):
        copy = os.path.join(tempfile.mkdtemp(prefix="stale-", dir=TMP), "big.mp4")
        shutil.copy(f"{TMP}/big.mp4", copy)
        m = self.project.add_media(mediamod.probe(copy))
        proxies.attach(self.project, m, self.proxy)
        self.assertEqual(proxies.find(self.project, m), self.proxy)
        future = time.time() + 100
        os.utime(copy, (future, future))
        self.assertIsNone(proxies.find(self.project, m))

    def test_detach(self):
        own = os.path.join(self.dir, "own" + proxies.SUFFIX)
        shutil.copy(self.proxy, own)
        proxies.attach(self.project, self.media, own)
        self.assertTrue(proxies.detach(self.project, self.media, delete_file=True))
        self.assertFalse(os.path.exists(own))
        self.assertIsNone(proxies.find(self.project, self.media))
        self.assertFalse(proxies.detach(self.project, self.media))


if __name__ == "__main__":
    unittest.main()
