"""Multi-camera editing: syncing cameras by sound, In points or first
frames, building the multi-camera sequence, and rendering the camera a clip
shows with the sound its audio mode picks. Needs ffmpeg and MLT."""

import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

from tideline import multicam as mc
from tideline.edits import Editor
from tideline.engine import media as mediamod
from tideline.engine.builder import Builder
from tideline.engine.mltlib import mlt
from tideline.model import Project, Sequence

TMP = None
CLAPS = "if(lt(mod(t*1000\\,1370)\\,30)+lt(abs(t-2.2)\\,0.02)+lt(abs(t-5.1)\\,0.03)\\,sin(2*PI*1000*t)\\,0)"


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="tideline-multicam-")
    mediamod.CACHE_DIR = os.path.join(TMP, "cache")
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    run = lambda args: subprocess.run(ff + args, check=True)
    run(["-f", "lavfi", "-i", f"aevalsrc={CLAPS}:s=48000:d=12", f"{TMP}/claps.wav"])
    # Camera A from the start; camera B 1.5 s later, with background noise;
    # a separate recorder 0.8 s later; a camera with no sound.
    run(["-f", "lavfi", "-i", "color=c=red:size=160x90:rate=30:duration=10", "-i", f"{TMP}/claps.wav",
         "-map", "0:v", "-map", "1:a", "-t", "10", *x264, "-c:a", "aac", f"{TMP}/a.mp4"])
    run(["-f", "lavfi", "-i", "color=c=blue:size=160x90:rate=30:duration=9", "-ss", "1.5", "-i", f"{TMP}/claps.wav",
         "-f", "lavfi", "-i", "anoisesrc=a=0.02:d=12", "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first[a]",
         "-map", "0:v", "-map", "[a]", "-t", "9", *x264, "-c:a", "aac", f"{TMP}/b.mp4"])
    run(["-ss", "0.8", "-i", f"{TMP}/claps.wav", "-t", "9", f"{TMP}/recorder.wav"])
    run(["-f", "lavfi", "-i", "color=c=green:size=160x90:rate=30:duration=6", *x264, f"{TMP}/mute.mp4"])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def picture(graph, pos):
    graph.tractor.seek(pos)
    f = graph.tractor.get_frame()
    img = np.frombuffer(mlt.frame_get_image(f, mlt.mlt_image_rgb, 160, 90), np.uint8).reshape(90, 160, 3)
    return img[45, 80].astype(int)


class Sync(unittest.TestCase):
    def test_offset_from_onsets(self):
        rng = np.random.default_rng(1)
        ref = np.zeros(4000, np.float32)
        ref[rng.integers(0, 4000, 40)] = 1.0
        later = ref[600:].copy()                          # started 600 samples (1.5 s) after ref
        shift, sure = mc.find_offset(ref, later, rate=400)
        self.assertAlmostEqual(shift, 1.5)
        self.assertGreater(sure, 0.5)
        earlier = np.concatenate([np.zeros(200, np.float32), ref])
        self.assertAlmostEqual(mc.find_offset(ref, earlier, rate=400)[0], -0.5)
        self.assertEqual(mc.find_offset(ref, np.zeros(100, np.float32)), (0.0, 0.0))

    def test_cameras_line_up_by_sound(self):
        p = Project()
        items = [p.add_media(mediamod.probe(f"{TMP}/{n}")) for n in ("a.mp4", "b.mp4", "recorder.wav", "mute.mp4")]
        starts, failed = mc.offsets(items, "audio")
        self.assertEqual(failed, ["mute.mp4"])
        self.assertAlmostEqual(starts[0], 0.0, delta=0.01)
        self.assertAlmostEqual(starts[1], 1.5, delta=0.01)
        self.assertAlmostEqual(starts[2], 0.8, delta=0.01)
        self.assertEqual(starts[3], 0.0)

    def test_in_points_and_first_frames(self):
        p = Project()
        a, b = (p.add_media(mediamod.probe(f"{TMP}/{n}")) for n in ("a.mp4", "b.mp4"))
        a.mark_in, b.mark_in = 3.0, 1.0
        self.assertEqual(mc.offsets([a, b], "in")[0], [0.0, 2.0])
        self.assertEqual(mc.offsets([a, b], "start")[0], [0.0, 0.0])


class Cameras(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.items = [self.p.add_media(mediamod.probe(f"{TMP}/{n}")) for n in ("a.mp4", "b.mp4", "recorder.wav")]
        self.mc = mc.build(self.p, self.items, [0.0, 1.5, 0.8], "MC")
        self.item = self.p.add_sequence(self.mc)
        self.seq = Sequence.create("Edit", 160, 90, 30, 1)
        self.p.add_sequence(self.seq)
        self.ed = Editor(self.p, self.seq)

    def test_build(self):
        self.assertTrue(self.mc.multicam)
        self.assertEqual(mc.angle_names(self.mc), ["a.mp4", "b.mp4"])
        self.assertEqual([(t.name, t.angle) for t in self.mc.audio_tracks],
                         [("a.mp4", 0), ("b.mp4", 1), ("recorder.wav", None)])
        self.assertEqual(self.mc.multicam_audio, "2")                       # the recorder's sound by default
        self.assertEqual([t.clips[0].start for t in self.mc.video_tracks], [0, 45])
        self.assertEqual(self.mc.audio_tracks[2].clips[0].start, 24)
        self.assertIs(self.p.multicam_of(self.item), self.mc)
        self.assertEqual([m for m, _ in mc.audio_choices(self.mc)], ["0", "1", "2", "follow", "all"])

    def test_audio_modes(self):
        names = lambda mode, angle=0: [t.name for t in mc.audio_tracks(self.mc, angle, mode)]
        self.assertEqual(names("2"), ["recorder.wav"])
        self.assertEqual(names("follow", 1), ["b.mp4"])
        self.assertEqual(names("all"), ["a.mp4", "b.mp4", "recorder.wav"])
        self.assertEqual(names("9"), [])

    def test_clips_show_their_camera(self):
        v, a = self.ed.place(self.item, 0)
        self.assertEqual((v.angle, a.angle), (0, 0))
        self.assertEqual(self.p.multicam_of(self.p.media[v.media_id]), self.mc)
        builder = Builder(self.p)
        self.assertGreater(picture(builder.build(self.seq), 60)[0], 200)    # camera 1: red
        self.ed.split(60, [v.id])
        right = self.seq.video_tracks[0].clip_at(70)
        self.assertEqual(right.angle, 0)                                    # a cut keeps the camera…
        for c in [right] + [c for _, c in self.ed.partners(right)]:
            c.angle = 1
        graph = builder.build(self.seq)
        self.assertGreater(picture(graph, 30)[0], 200)
        self.assertGreater(picture(graph, 70)[2], 200)                      # …until it is switched: blue
        self.assertEqual(self.seq.video_tracks[0].clip_at(70).to_dict()["angle"], 1)

    def test_camera_grid(self):
        graph = Builder(self.p).build_angle(self.mc, 1, 96)
        self.assertEqual((graph.profile.width(), graph.profile.height()), (96, 54))
        graph.tractor.seek(70)
        f = graph.tractor.get_frame()
        img = np.frombuffer(mlt.frame_get_image(f, mlt.mlt_image_rgb, 96, 54), np.uint8).reshape(54, 96, 3)
        self.assertGreater(img[27, 48, 2], 200)


if __name__ == "__main__":
    unittest.main()
