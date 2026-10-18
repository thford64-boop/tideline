"""Keyboard shortcuts: defaults, reassigning, conflicts, files and key
presses. Uses GTK's accelerator functions, which need no display."""

import json
import os
import tempfile
import unittest

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk  # noqa: E402

from tideline import keymap as km  # noqa: E402
from tideline.commands import COMMANDS  # noqa: E402

CTRL, SHIFT, ALT = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK, Gdk.ModifierType.ALT_MASK


class Keymap(unittest.TestCase):
    def test_normal_form(self):
        self.assertEqual(km.normalize("<Ctrl><Shift>K"), km.normalize("<Shift><Primary>k"))
        self.assertEqual(km.normalize("nonsense key"), "")
        self.assertEqual(km.label("<Ctrl>k"), "Ctrl+K")

    def test_defaults_have_no_conflicts(self):
        k = km.Keymap()
        self.assertEqual(k.conflicts(), [])
        for name, _label, accel, _group in COMMANDS:
            self.assertEqual(k.accel(name), km.normalize(accel), name)
            self.assertFalse(k.changed(name))

    def test_bindings_include_aliases_but_commands_win(self):
        k = km.Keymap()
        b = k.bindings()
        self.assertEqual(b[km.normalize("BackSpace")], "delete")
        self.assertEqual(b[km.normalize("<Ctrl>y")], "redo")
        k.assign("save", "BackSpace")
        self.assertEqual(k.bindings()[km.normalize("BackSpace")], "save")

    def test_assign_takes_the_key_from_its_owner(self):
        k = km.Keymap()
        self.assertEqual(k.owner("<Ctrl>k"), "add_edit")
        taken = k.assign("save", "<Ctrl>k")
        self.assertEqual(taken, "add_edit")
        self.assertEqual(k.accel("save"), km.normalize("<Ctrl>k"))
        self.assertEqual(k.accel("add_edit"), "")
        self.assertTrue(k.changed("save") and k.changed("add_edit"))
        self.assertEqual(k.bindings()[km.normalize("<Ctrl>k")], "save")
        self.assertNotIn(km.normalize("<Ctrl>s"), k.bindings())
        self.assertEqual(k.conflicts(), [])

    def test_reset_and_clear(self):
        k = km.Keymap()
        k.assign("add_edit", "<Ctrl>s")                     # save loses Ctrl+S
        self.assertEqual(k.reset("save"), "add_edit")       # and takes it back
        self.assertEqual(k.accel("save"), km.normalize("<Ctrl>s"))
        self.assertEqual(k.accel("add_edit"), "")
        k.clear("save")
        self.assertEqual(k.accel("save"), "")
        k.assign("save", "<Ctrl>s")                         # the default again: no override kept
        self.assertNotIn("save", k.overrides)
        k.reset_all()
        self.assertEqual(k.overrides, {})

    def test_reserved_and_invalid_keys(self):
        k = km.Keymap()
        with self.assertRaises(ValueError):
            k.assign("save", "Escape")
        with self.assertRaises(ValueError):
            k.assign("save", "")

    def test_overrides_from_settings(self):
        k = km.Keymap({"save": "<Primary>F5", "quit": "", 3: "x"})
        self.assertEqual(k.accel("save"), km.normalize("<Ctrl>F5"))
        self.assertEqual(k.accel("quit"), "")

    def test_extension_commands(self):
        k = km.Keymap(extra=[("ext-a-ping", "Ping", "<Ctrl><Alt>F8", "Extensions: A")])
        self.assertEqual(k.bindings()[km.normalize("<Ctrl><Alt>F8")], "ext-a-ping")
        k.assign("ext-a-ping", "F9")
        self.assertEqual(k.owner("F9"), "ext-a-ping")

    def test_key_presses(self):
        self.assertEqual(km.event_accels(Gdk.KEY_K, SHIFT | CTRL), [km.normalize("<Ctrl><Shift>k")])
        # Shift+1 types "!" on a US keyboard; it is matched as Shift+1, as written in the command table.
        self.assertEqual(km.event_accels(Gdk.KEY_exclam, SHIFT)[0], km.normalize("<Shift>1"))
        self.assertEqual(km.event_accels(Gdk.KEY_Shift_L, SHIFT), [])
        self.assertEqual(km.from_event(Gdk.KEY_Control_L, CTRL), "")
        k = km.Keymap()
        b = k.bindings()
        hit = lambda keyval, state: next((b[a] for a in km.event_accels(keyval, state) if a in b), None)
        self.assertEqual(hit(Gdk.KEY_exclam, SHIFT), "panel_project")
        self.assertEqual(hit(Gdk.KEY_K, SHIFT | CTRL), "add_edit_all")
        self.assertEqual(hit(Gdk.KEY_space, 0), "play_toggle")

    def test_files(self):
        k = km.Keymap()
        k.assign("save", "<Ctrl>k")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mine" + km.KEYMAP_EXT)
            k.dump(path)
            with open(path) as fh:
                data = json.load(fh)
            self.assertEqual(data["tideline_keys"], km.FORMAT)
            other = km.Keymap()
            self.assertEqual(other.load(path), 2)
            self.assertEqual(other.overrides, k.overrides)
            with open(path, "w") as fh:          # a hand-edited file giving one key to two commands
                json.dump({"tideline_keys": 1, "shortcuts": {"save": "F2", "quit": "F2", "undo": "Escape",
                                                             "redo": "not a key"}}, fh)
            other.load(path)
            self.assertEqual(other.owner("F2"), "save")
            self.assertEqual(other.accel("quit"), "")
            self.assertEqual(other.accel("undo"), km.normalize("<Ctrl>z"))
            with open(path, "w") as fh:
                fh.write('{"something": "else"}')
            with self.assertRaises(ValueError):
                other.load(path)
            with self.assertRaises(ValueError):
                other.load(os.path.join(tmp, "missing.tlkeys"))


class HardwareProbe(unittest.TestCase):
    def test_probe_commands(self):
        from tideline.engine import hwenc
        cmd = hwenc.probe_command("h264_vaapi", "vaapi", "/dev/dri/renderD128")
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("format=nv12,hwupload", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_vaapi")
        cmd = hwenc.probe_command("h264_nvenc", "nvenc")
        self.assertNotIn("-vaapi_device", cmd)

    def test_formats_are_declared_for_each_api(self):
        from tideline.engine import export
        hw = export.hardware_formats()
        self.assertEqual({f.hw for f in hw}, {"vaapi", "nvenc", "qsv"})
        for f in hw:
            self.assertTrue(f.encoder.endswith("_" + f.hw), f.key)
            self.assertEqual(len(f.crf), 3)

    def test_vaapi_command_names_the_device(self):
        from tideline.engine import export
        from tideline.model import Project, Sequence
        seq = Sequence.create("S", 640, 360, 30, 1)
        job = export.ExportJob(Project(), seq, export.ExportSettings(format="hevc_vaapi", path="/tmp/x.mp4",
                                                                     device="/dev/dri/renderD129"))
        cmd = job.command("/tmp/x.mlt", 30)
        self.assertIn("vaapi_device=/dev/dri/renderD129", cmd)
        self.assertIn("vcodec=hevc_vaapi", cmd)
        self.assertIn("global_quality=20", cmd)
        self.assertIn("rc_mode=CQP", cmd)
        job = export.ExportJob(Project(), seq, export.ExportSettings(format="h264_nvenc", path="/tmp/x.mp4",
                                                                     quality="low"))
        cmd = job.command("/tmp/x.mlt", 30)
        self.assertIn("cq=29", cmd)
        self.assertFalse(any(c.startswith("vaapi_device") for c in cmd))


if __name__ == "__main__":
    unittest.main()
