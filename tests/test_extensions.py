"""Extension tests: manifests, Python extensions, installing, and extension
effects and transitions rendered through real MLT graphs."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

import numpy as np

from tideline import extensions, presets
from tideline.edits import Editor
from tideline.engine import export
from tideline.engine.builder import Builder
from tideline.engine.effects import EFFECTS, TRANSITIONS, categories, transition_categories
from tideline.engine.mltlib import mlt
from tideline.model import Effect, MediaItem, Project, Sequence

W, H = 320, 180
TMP = None


def setUpModule():
    global TMP
    TMP = tempfile.mkdtemp(prefix="tideline-ext-test-")
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "10"]
    for name, colour in (("green", "0x00c000"), ("blue", "0x0000c0")):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"color=c={colour}:size={W}x{H}:rate=30:duration=4",
                             *x264, f"{TMP}/{name}.mp4"], check=True)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def write_ext(base, folder, manifest, files=None):
    path = os.path.join(base, folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "extension.json"), "w") as fh:
        json.dump(manifest, fh)
    for name, content in (files or {}).items():
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)
    return path


def gradient_pgm(w=64, h=36):
    """Left-to-right black-to-white ramp: a wipe that starts on the left."""
    row = bytes(int(255 * x / (w - 1)) for x in range(w))
    return f"P5\n{w} {h}\n255\n".encode() + row * h


DECLARATIVE = {
    "id": "test.declarative",
    "name": "Declarative Pack",
    "version": "1.2.0",
    "author": "Tests",
    "description": "Everything a manifest alone can add.",
    "tideline": "0.0.3",
    "effects": [
        {"key": "dim", "label": "Dim", "category": "Color", "services": ["brightness"],
         "params": [{"name": "level", "label": "Level", "type": "float", "default": 100, "min": 0,
                     "max": 200, "unit": "%", "mlt": "level", "scale": 0.01}]},
        {"key": "soft_vignette", "label": "Soft Vignette", "category": "Looks", "services": ["frei0r.vignette"],
         "params": [{"name": "aspect", "label": "Aspect", "default": 50, "min": 0, "max": 100,
                     "mlt": "aspect", "scale": 0.01},
                    {"name": "clear", "label": "Clear Centre", "default": 0, "min": 0, "max": 100,
                     "mlt": "clearCenter", "scale": 0.01}]},
        {"key": "phaser", "label": "Phaser", "kind": "audio", "category": "Time & Pitch",
         "services": ["avfilter.aphaser"],
         "params": [{"name": "speed", "label": "Speed", "default": 0.5, "min": 0.1, "max": 2,
                     "unit": "Hz", "step": 0.1, "mlt": "av.speed", "animate": "hold"}]},
    ],
    "transitions": [
        {"key": "ramp_wipe", "label": "Ramp Wipe", "category": "Wipe", "wipe": "wipes/ramp.pgm"},
        {"key": "dip_red", "label": "Dip to Red", "category": "Dissolve", "dip": "#c00000ff"},
    ],
    "export_formats": [
        {"key": "mjpeg", "label": "Motion JPEG (MOV)", "ext": ".mov",
         "video": {"f": "mov", "vcodec": "mjpeg", "pix_fmt": "yuvj422p"},
         "audio": {"acodec": "pcm_s16le"}, "quality": [2, 5, 10], "quality_key": "qscale"},
    ],
    "presets": "presets",
}

PRESET = {"tideline_preset": 1, "name": "Pack Fade", "kinds": ["video"], "effects": [
    {"type": "opacity", "params": {}, "keyframes": {"opacity": [[0, 0.0, "linear"], [15, 100.0, "linear"]]}}]}

PY_MAIN = '''
import helper

calls = []

def register(tl):
    def apply(filters, effect, ctx):
        level = tl.mlt_value(effect, tl_level)
        filters[0].set("level", level)

    tl_level = tl.number("amount", "Amount", 50, 0, 100, unit="%", scale=0.01)
    tl.effect("half", "Half Brightness", ["brightness"], [tl_level], category="Color", apply=apply)
    tl.command("hello", "Say Hello", lambda app: calls.append(helper.GREETING), accel="<Ctrl><Alt>F9")
    tl.on("project_saved", lambda path: calls.append(("saved", path)))
    tl.settings["runs"] = tl.settings.get("runs", 0) + 1
    tl.save_settings()
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="exts-", dir=TMP)
        self.config = tempfile.mkdtemp(prefix="config-", dir=TMP)
        self._env = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.config
        self.reg = extensions.Registry()

    def tearDown(self):
        self.reg.unload_all()
        if self._env is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._env


class Manifest(Base):
    def load_declarative(self):
        write_ext(self.dir, "decl", DECLARATIVE, {"wipes/ramp.pgm": gradient_pgm(),
                                                 "presets/Pack Fade.tlpreset": json.dumps(PRESET)})
        exts = self.reg.load([self.dir])
        self.assertEqual(len(exts), 1)
        return exts[0]

    def test_everything_registers(self):
        ext = self.load_declarative()
        self.assertTrue(ext.loaded, ext.error)
        self.assertEqual(ext.name, "Declarative Pack")
        self.assertEqual(ext.summary(), "3 effects, 2 transitions, 1 export format, 1 preset folder")
        self.assertIn("test.declarative/dim", EFFECTS)
        self.assertEqual(EFFECTS["test.declarative/dim"].category, "Color")
        self.assertIn("Looks", categories("video"))             # a new category is listed after the built-ins
        self.assertEqual(categories("video")[-1], "Looks")
        self.assertEqual(EFFECTS["test.declarative/phaser"].kind, "audio")
        self.assertTrue(EFFECTS["test.declarative/phaser"].param("speed").hold)
        self.assertEqual(TRANSITIONS["test.declarative/ramp_wipe"].mode, "luma")
        self.assertTrue(TRANSITIONS["test.declarative/ramp_wipe"].resource.endswith("ramp.pgm"))
        self.assertEqual(TRANSITIONS["test.declarative/dip_red"].mode, "dip")
        self.assertIn("Wipe", transition_categories("video"))
        fmt = export.FORMAT_BY_KEY["test.declarative/mjpeg"]
        self.assertIn(fmt, export.FORMATS)
        self.assertEqual(fmt.crf, (2, 5, 10))
        names = [p["name"] for path, p, builtin in presets.library()]
        self.assertIn("Pack Fade", names)
        path = next(path for path, p, b in presets.library() if p["name"] == "Pack Fade")
        self.assertTrue(presets.is_builtin(path))

    def test_unload_removes_everything(self):
        self.load_declarative()
        self.reg.unload_all()
        self.assertNotIn("test.declarative/dim", EFFECTS)
        self.assertNotIn("test.declarative/ramp_wipe", TRANSITIONS)
        self.assertNotIn("test.declarative/mjpeg", export.FORMAT_BY_KEY)
        self.assertEqual(presets.EXTRA_DIRS, [])

    def test_export_command_uses_format(self):
        self.load_declarative()
        p = Project()
        seq = Sequence.create("S", W, H, 30, 1)
        p.add_sequence(seq)
        job = export.ExportJob(p, seq, export.ExportSettings(format="test.declarative/mjpeg", path="/tmp/x.mov",
                                                             quality="medium"))
        cmd = job.command("/tmp/x.mlt", 30)
        self.assertIn("vcodec=mjpeg", cmd)
        self.assertIn("qscale=5", cmd)
        self.assertIn("acodec=pcm_s16le", cmd)

    def test_errors_are_reported_and_rolled_back(self):
        write_ext(self.dir, "bad_json", {})
        with open(os.path.join(self.dir, "bad_json", "extension.json"), "w") as fh:
            fh.write("{not json")
        write_ext(self.dir, "bad_id", {"id": "Bad Id!"})
        write_ext(self.dir, "too_new", {"id": "test.new", "tideline": "99.0"})
        write_ext(self.dir, "half_done", {"id": "test.half", "effects": [
            {"key": "ok", "services": ["brightness"]},
            {"key": "broken", "services": ["brightness"], "params": [{"name": "x", "type": "vector"}]}]})
        write_ext(self.dir, "no_main", {"id": "test.nomain", "main": "missing.py"})
        write_ext(self.dir, "raises", {"id": "test.raises", "main": "main.py"},
                  {"main.py": "def register(tl):\n    tl.command('x', 'X', print)\n    raise RuntimeError('boom')\n"})
        with contextlib.redirect_stderr(io.StringIO()):         # the traceback of "raises"
            exts = {os.path.basename(e.path): e for e in self.reg.load([self.dir])}
        self.assertIn("not valid JSON", exts["bad_json"].error)
        self.assertIn("\"id\"", exts["bad_id"].error)
        self.assertIn("needs Tideline 99.0", exts["too_new"].error)
        self.assertIn("type must be one of", exts["half_done"].error)
        self.assertNotIn("test.half/ok", EFFECTS)                 # the part that worked was taken back
        self.assertIn("missing.py not found", exts["no_main"].error)
        self.assertEqual(exts["raises"].error, "boom")
        self.assertEqual(self.reg.commands, [])
        self.assertFalse(any(e.loaded for e in exts.values()))

    def test_duplicate_id_and_disabled(self):
        write_ext(self.dir, "a", {"id": "test.same", "effects": [{"key": "one", "services": ["brightness"]}]})
        write_ext(self.dir, "b", {"id": "test.same"})
        write_ext(self.dir, "c", {"id": "test.off", "effects": [{"key": "two", "services": ["brightness"]}]})
        exts = {os.path.basename(e.path): e for e in self.reg.load([self.dir], disabled={"test.off"})}
        self.assertTrue(exts["a"].loaded)
        self.assertIn("already loaded", exts["b"].error)
        self.assertFalse(exts["c"].enabled)
        self.assertNotIn("test.off/two", EFFECTS)

    def test_bad_keys(self):
        write_ext(self.dir, "k", {"id": "test.keys", "effects": [{"key": "Has Space", "services": ["brightness"]}]})
        ext = self.reg.load([self.dir])[0]
        self.assertIn("key", ext.error)


class Python(Base):
    def test_register_command_hook_settings(self):
        write_ext(self.dir, "py", {"id": "test.python", "name": "Py", "main": "main.py"},
                  {"main.py": PY_MAIN, "helper.py": "GREETING = 'hi'\n"})
        ext = self.reg.load([self.dir])[0]
        self.assertTrue(ext.loaded, ext.error)
        self.assertEqual(ext.summary(), "1 effect, 1 command, 1 event hook")
        cmd = self.reg.commands[0]
        self.assertEqual(cmd.action, "ext-test-python-hello")
        self.assertIs(self.reg.command("ext-test-python-hello"), cmd)
        cmd.callback(None)
        self.assertEqual(ext.module.calls, ["hi"])

        class FakeApp:
            def __init__(self):
                self.listeners, self.commands = [], []

            def add_extension_command(self, c):
                self.commands.append(c)

            def remove_extension_command(self, c):
                self.commands.remove(c)

            def on(self, event, fn):
                self.listeners.append((event, fn))

            def off(self, event, fn):
                self.listeners.remove((event, fn))

        app = FakeApp()
        self.reg.attach(app)
        self.assertEqual(app.commands, [cmd])
        event, fn = app.listeners[0]
        self.assertEqual(event, "project_saved")
        fn("/p.tideline")
        self.assertEqual(ext.module.calls[-1], ("saved", "/p.tideline"))
        with open(os.path.join(self.config, "tideline", "extension-data", "test.python", "settings.json")) as fh:
            self.assertEqual(json.load(fh), {"runs": 1})
        self.reg.unload_all()
        self.assertEqual(app.listeners, [])

    def test_hook_errors_do_not_escape(self):
        write_ext(self.dir, "py", {"id": "test.hookerr", "main": "main.py"},
                  {"main.py": "def register(tl):\n    tl.on('media', lambda: 1 / 0)\n"})
        self.reg.load([self.dir])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.reg.hooks[0][1]()              # reports the error, raises nothing
        self.assertIn("ZeroDivisionError", err.getvalue())

    def test_unknown_event(self):
        write_ext(self.dir, "py", {"id": "test.event", "main": "main.py"},
                  {"main.py": "def register(tl):\n    tl.on('lunch', print)\n"})
        ext = self.reg.load([self.dir])[0]
        self.assertIn("unknown event", ext.error)


class Install(Base):
    def test_zip_with_top_folder(self):
        src = write_ext(self.dir, "pack", {"id": "test.zipped"}, {"main.py": "def register(tl): pass\n"})
        archive = os.path.join(self.dir, "pack.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            for name in ("extension.json", "main.py"):
                zf.write(os.path.join(src, name), f"pack-1.0/{name}")
        dest = os.path.join(self.dir, "installed")
        ext_id, folder = extensions.install(archive, dest)
        self.assertEqual(ext_id, "test.zipped")
        self.assertEqual(folder, os.path.join(dest, "test.zipped"))
        self.assertTrue(os.path.exists(os.path.join(folder, "main.py")))
        # Installing again replaces the old copy.
        extensions.install(src, dest)
        self.assertEqual(sorted(os.listdir(dest)), ["test.zipped"])

    def test_unsafe_zip(self):
        archive = os.path.join(self.dir, "evil.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("extension.json", json.dumps({"id": "test.evil"}))
            zf.writestr("../outside.txt", "x")
        with self.assertRaises(extensions.ExtensionError):
            extensions.install(archive, os.path.join(self.dir, "installed"))

    def test_uninstall_only_user_folder(self):
        os.makedirs(extensions.user_dir())
        write_ext(extensions.user_dir(), "mine", {"id": "test.mine"})
        ext = self.reg.load([extensions.user_dir()])[0]
        self.assertTrue(ext.user_installed)
        extensions.uninstall(ext)
        self.assertFalse(os.path.exists(ext.path))
        other = extensions.Extension(self.dir, {"id": "test.other"})
        with self.assertRaises(extensions.ExtensionError):
            extensions.uninstall(other)

    def test_builtin_extensions_load_and_give_way_to_a_user_copy(self):
        exts = {e.id: e for e in self.reg.load([extensions.builtin_dir()])}
        self.assertEqual(set(exts), {"frito.cutout", "frito.deepvoice"})
        for ext in exts.values():
            self.assertTrue(ext.loaded, ext.error)
            self.assertTrue(ext.builtin)
            self.assertFalse(ext.user_installed)
            with self.assertRaises(extensions.ExtensionError):
                extensions.uninstall(ext)
        labels = [c.label for c in self.reg.commands]
        self.assertIn("Remove Background…", labels)
        self.assertIn("Isolate Voice", labels)
        self.assertEqual(extensions.search_dirs()[-1], extensions.builtin_dir())
        self.reg.unload_all()
        # A copy in the user folder (an update, say) is loaded instead, and the
        # built-in one is not listed as a clash.
        os.makedirs(extensions.user_dir())
        write_ext(extensions.user_dir(), "cutout", {"id": "frito.cutout", "name": "My Cutout"})
        loaded = self.reg.load([extensions.user_dir(), extensions.builtin_dir()])
        self.assertEqual(sorted(e.id for e in loaded), ["frito.cutout", "frito.deepvoice"])
        mine = self.reg.get("frito.cutout")
        self.assertEqual(mine.name, "My Cutout")
        self.assertTrue(mine.user_installed)
        self.assertEqual([e.error for e in loaded], ["", ""])


def frame(graph, pos):
    graph.tractor.seek(pos)
    f = graph.tractor.get_frame()
    data = mlt.frame_get_image(f, mlt.mlt_image_rgb, W, H)
    return np.frombuffer(data, np.uint8).reshape(H, W, 3).astype(int)


class Render(Base):
    def setUp(self):
        super().setUp()
        write_ext(self.dir, "decl", DECLARATIVE, {"wipes/ramp.pgm": gradient_pgm(),
                                                 "presets/Pack Fade.tlpreset": json.dumps(PRESET)})
        write_ext(self.dir, "py", {"id": "test.python", "main": "main.py"},
                  {"main.py": PY_MAIN, "helper.py": "GREETING = 'hi'\n"})
        for ext in self.reg.load([self.dir]):
            self.assertTrue(ext.loaded, ext.error)
        p = self.project = Project()
        self.seq = Sequence.create("S", W, H, 30, 1)
        p.add_sequence(self.seq)
        self.green = p.add_media(MediaItem(name="g", kind="video", path=f"{TMP}/green.mp4", duration=4.0,
                                           has_video=True, width=W, height=H, fps=30))
        self.blue = p.add_media(MediaItem(name="b", kind="video", path=f"{TMP}/blue.mp4", duration=4.0,
                                          has_video=True, width=W, height=H, fps=30))

    def test_declared_effect_maps_parameters(self):
        ed = Editor(self.project, self.seq)
        clip = ed.place(self.green, 0, src_out=60)[0]
        clip.effects.append(Effect("test.declarative/dim", params={"level": 0.0}))
        img = frame(Builder(self.project).build(self.seq), 10)
        self.assertLess(img[H // 2, W // 2].max(), 20)
        clip.effects[-1].params["level"] = 100.0
        img = frame(Builder(self.project).build(self.seq), 10)
        self.assertGreater(img[H // 2, W // 2][1], 150)

    def test_custom_apply_with_keyframes(self):
        ed = Editor(self.project, self.seq)
        clip = ed.place(self.green, 0, src_out=60)[0]
        clip.effects.append(Effect("test.python/half", keyframes={"amount": [[0, 0.0, "linear"],
                                                                             [30, 100.0, "linear"]]}))
        graph = Builder(self.project).build(self.seq)
        self.assertLess(frame(graph, 0)[H // 2, W // 2].max(), 20)
        self.assertGreater(frame(graph, 40)[H // 2, W // 2][1], 150)

    def test_wipe_transition_from_image(self):
        ed = Editor(self.project, self.seq)
        a = ed.place(self.green, 0, src_out=60)[0]
        b = ed.place(self.blue, 60, src_in=30, src_out=90)[0]
        ed.add_transition(b.id, "start", "test.declarative/ramp_wipe", 30)
        graph = Builder(self.project).build(self.seq)
        img = frame(graph, 60)                   # halfway: the left (dark) half already shows B
        left, right = img[H // 2, W // 8], img[H // 2, W - W // 8]
        self.assertGreater(left[2], 150)
        self.assertGreater(right[1], 150)
        self.assertIsNotNone(a)


if __name__ == "__main__":
    unittest.main()


DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "EXTENSIONS.md")


class DocsExamples(Base):
    """Every example file in docs/EXTENSIONS.md loads as written."""

    def test_examples_load(self):
        import re
        with open(DOCS, encoding="utf-8") as fh:
            text = fh.read()
        files = re.findall(r"\*\*`([\w.-]+/[\w./-]+)`\*\*\n\n```\w*\n(.*?)```", text, re.S)
        self.assertGreaterEqual(len(files), 7)
        for rel, content in files:
            path = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        # The look pack's wipe image is made as the guide describes.
        os.makedirs(os.path.join(self.dir, "look-pack", "wipes"))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "gradients=s=256x256:c0=black:c1=white:x0=0:y0=0:x1=255:y1=255", "-frames:v", "1",
                        os.path.join(self.dir, "look-pack", "wipes", "diagonal.png")], check=True)
        exts = self.reg.load([self.dir])
        self.assertEqual(len(exts), len({rel.split("/")[0] for rel, _ in files}))
        for ext in exts:
            self.assertTrue(ext.loaded, f"{ext.id}: {ext.error}")
            for key in ext.added["effects"]:
                self.assertTrue(EFFECTS[key].available(), key)
        self.assertEqual(self.reg.get("example.look-pack").summary(), "3 effects, 2 transitions, 1 export format")
        self.assertEqual(self.reg.commands[0].label, "Add Marker Every 5 Seconds")

    def test_examples_render(self):
        self.test_examples_load()
        p = Project()
        seq = Sequence.create("S", W, H, 30, 1)
        p.add_sequence(seq)
        green = p.add_media(MediaItem(name="g", kind="video", path=f"{TMP}/green.mp4", duration=4.0,
                                      has_video=True, width=W, height=H, fps=30))
        blue = p.add_media(MediaItem(name="b", kind="video", path=f"{TMP}/blue.mp4", duration=4.0,
                                     has_video=True, width=W, height=H, fps=30))
        ed = Editor(p, seq)
        a = ed.place(green, 0, src_out=60)[0]
        b = ed.place(blue, 60, src_in=30, src_out=90)[0]
        for key in ("example.look-pack/soft_vignette", "example.look-pack/glow", "example.look-pack/warm_hue"):
            a.effects.append(Effect(key))
        b.effects.append(Effect("example.darken/darken", params={"amount": 100.0}))
        ed.add_transition(b.id, "start", "example.look-pack/diagonal", 20)
        graph = Builder(p).build(seq)
        self.assertFalse(graph.missing)
        self.assertGreater(frame(graph, 20)[H // 2, W // 2][1], 100)     # still green through the looks
        self.assertLess(frame(graph, 100)[H // 2, W // 2].max(), 20)     # darkened to black

    def test_readme_snippets_load(self):
        import re
        with open(os.path.join(os.path.dirname(DOCS), "..", "README.md"), encoding="utf-8") as fh:
            text = fh.read().split("## Writing extensions", 1)[1].split("## Project files", 1)[0]
        manifest = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
        code = re.search(r"```python\n(.*?)```", text, re.S).group(1)
        manifest["main"] = "main.py"
        write_ext(self.dir, "readme", manifest, {"main.py": code, "wipes/diagonal.png": gradient_pgm(),
                                                 "presets/Pack Fade.tlpreset": json.dumps(PRESET)})
        ext = self.reg.load([self.dir])[0]
        self.assertTrue(ext.loaded, ext.error)
        self.assertEqual(ext.summary(), "1 effect, 1 transition, 1 export format, 1 preset folder, "
                                        "1 command, 1 event hook")
