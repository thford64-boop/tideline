"""Caption files, caption layout and the auto-caption pipeline. The pipeline
test renders real sequence audio with melt and runs a stand-in for
whisper.cpp's ``whisper-cli``, so no speech model is needed."""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from tideline import captions as cap
from tideline.edits import Editor
from tideline.engine import media as mediamod
from tideline.engine.builder import Builder
from tideline.model import Project, Sequence

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello <i>there</i>.

2
00:00:03,000 --> 00:00:04,000
Second line
continues here
"""

VTT = """WEBVTT

NOTE a comment

intro
00:01.000 --> 00:02.500 align:center
<v Speaker>Hello there.</v>

00:00:03.000 --> 00:00:04.000
Second
"""

FAKE_WHISPER = textwrap.dedent('''\
    #!{python}
    import sys
    args = sys.argv[1:]
    out = args[args.index("-of") + 1]
    assert args[args.index("-m") + 1].endswith("ggml-base.bin")
    assert args[args.index("-ml") + 1] == "32"
    for p in (10, 50, 100):
        print(f"whisper_print_progress_callback: progress = {{p}}%", file=sys.stderr, flush=True)
    with open(out + ".srt", "w") as fh:
        fh.write("1\\n00:00:00,200 --> 00:00:01,000\\nThis is a test\\n\\n"
                 "2\\n00:00:01,100 --> 00:00:01,900\\nof auto captions.\\n\\n"
                 "3\\n00:00:02,000 --> 00:00:02,600\\n[MUSIC]\\n")
    ''')


class Files(unittest.TestCase):
    def test_parse_srt(self):
        cues = cap.parse(SRT)
        self.assertEqual([(c.start, c.end) for c in cues], [(1.0, 2.5), (3.0, 4.0)])
        self.assertEqual(cues[0].text, "Hello there.")
        self.assertEqual(cues[1].text, "Second line\ncontinues here")

    def test_parse_vtt(self):
        cues = cap.parse(VTT)
        self.assertEqual(len(cues), 2)
        self.assertEqual((cues[0].start, cues[0].end, cues[0].text), (1.0, 2.5, "Hello there."))

    def test_round_trip(self):
        cues = cap.parse(SRT)
        self.assertEqual([(c.start, c.end, c.text) for c in cap.parse(cap.to_srt(cues))],
                         [(c.start, c.end, c.text) for c in cues])
        vtt = cap.to_vtt(cues)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:01.000 --> 00:00:02.500", vtt)
        self.assertEqual([c.text for c in cap.parse(vtt)], [c.text for c in cues])

    def test_load_rejects_other_files(self):
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False) as fh:
            fh.write("just some words")
        try:
            with self.assertRaises(cap.CaptionError):
                cap.load(fh.name)
        finally:
            os.remove(fh.name)


class Layout(unittest.TestCase):
    def test_short_segments_join_and_wrap(self):
        cues = [cap.Cue(0.0, 0.8, "so what we"), cap.Cue(0.85, 1.6, "found was"), cap.Cue(1.65, 2.4, "remarkable.")]
        out = cap.layout(cues, 20, 2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text.replace("\n", " "), "so what we found was remarkable.")
        self.assertTrue(all(len(line) <= 20 for line in out[0].text.split("\n")))
        self.assertEqual((out[0].start, out[0].end), (0.0, 2.4))

    def test_sentence_end_and_pause_split(self):
        cues = [cap.Cue(0.0, 1.0, "Done."), cap.Cue(1.1, 2.0, "Next one"), cap.Cue(4.0, 5.0, "later")]
        self.assertEqual([c.text for c in cap.layout(cues, 42, 2)], ["Done.", "Next one", "later"])

    def test_long_segment_is_split_in_time(self):
        words = " ".join(f"word{i}" for i in range(30))
        out = cap.layout([cap.Cue(10.0, 20.0, words)], 20, 2)
        self.assertGreater(len(out), 3)
        self.assertEqual(out[0].start, 10.0)
        self.assertAlmostEqual(out[-1].end, 20.0)
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a.end, b.start + 1e-9)
        for c in out:
            self.assertLessEqual(len(c.text.split("\n")), 2)

    def test_two_lines_are_balanced(self):
        (c,) = cap.layout([cap.Cue(0, 3, "one two three four five six seven eight")], 32, 2)
        a, b = c.text.split("\n")
        self.assertLessEqual(abs(len(a) - len(b)), 6)


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tideline-captions-test-")
        mediamod.CACHE_DIR = os.path.join(cls.tmp, "cache")
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=4:sample_rate=48000", "-ac", "2",
                        f"{cls.tmp}/speech.wav"], check=True)
        bin_dir = os.path.join(cls.tmp, "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "whisper-cli")
        with open(fake, "w") as fh:
            fh.write(FAKE_WHISPER.format(python=sys.executable))
        os.chmod(fake, 0o755)
        cls.env = {k: os.environ.get(k) for k in ("PATH", "XDG_DATA_HOME")}
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["XDG_DATA_HOME"] = os.path.join(cls.tmp, "data")
        os.makedirs(cap.models_dir())
        open(os.path.join(cap.models_dir(), "ggml-base.bin"), "wb").close()

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_transcribe_sequence(self):
        p = Project()
        seq = Sequence.create("S", 320, 180, 25, 1)
        p.add_sequence(seq)
        Editor(p, seq).place(p.add_media(mediamod.probe(f"{self.tmp}/speech.wav")), 0)
        xml = os.path.join(self.tmp, "seq.mlt")
        Builder(p).write_xml(seq, xml)
        engine = cap.engines()[0]
        self.assertEqual(engine.key, "cpp")
        self.assertTrue(engine.model_ready("base"))
        req = cap.Request(xml_path=xml, range_in=25, range_out=seq.end(), fps=seq.fps, engine=engine,
                          max_chars=32, max_lines=2)
        done, stages = threading.Event(), []
        job = cap.TranscribeJob(req, on_progress=lambda s, f: stages.append((s, f)), on_done=lambda j: done.set())
        job.start()
        self.assertTrue(done.wait(60))
        self.assertEqual(job.error, "")
        self.assertEqual([c.text.replace("\n", " ") for c in job.cues], ["This is a test of auto captions."])
        self.assertEqual((job.cues[0].start, job.cues[0].end), (0.2, 1.9))
        fractions = [f for _, f in stages]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
