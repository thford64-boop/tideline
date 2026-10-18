"""Application controller: owns the project, selection, players and undo
history, and implements every command. Panels observe it through events."""

import copy
import os
import threading
from collections import defaultdict

import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, PROJECT_EXT, VERSION  # noqa: E402
from . import captions as captionsmod  # noqa: E402
from . import extensions  # noqa: E402
from . import fonts  # noqa: E402
from . import grading  # noqa: E402
from . import keyframes as kf  # noqa: E402
from . import keymap as keymapmod  # noqa: E402
from . import multicam  # noqa: E402
from . import presets  # noqa: E402
from . import proxies  # noqa: E402
from . import settings as settingsmod  # noqa: E402
from . import timeremap  # noqa: E402
from . import timecode as tc  # noqa: E402
from .commands import BY_NAME, COMMANDS  # noqa: E402
from .edits import AUDIO_TRANSITIONS, EditError, Editor  # noqa: E402
from .engine import media as mediamod  # noqa: E402
from .engine import mltlib  # noqa: E402
from .engine.builder import Builder, _rational_fps  # noqa: E402
from .engine.effects import (DEFAULT_AUDIO_TRANSITION, DEFAULT_VIDEO_TRANSITION, EFFECTS,  # noqa: E402
                             TRANSITIONS)
from .engine.player import Player  # noqa: E402
from .history import History  # noqa: E402
from .model import (LABEL_AUDIO, LABEL_GENERATED, Clip, Effect, Marker, MediaItem, Project,  # noqa: E402
                    Sequence)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)
        fonts.use_private_config()        # before any text is drawn, so added fonts are found
        self.project = Project()
        self.history = History()
        self.builder = None
        self.graph = None
        self.program = None
        self.source = None
        self.media_cache = mediamod.MediaCache(notify=lambda: self.emit("thumbs"))
        self.selection = []
        self.selected_transition = None
        self.selected_gap = None          # (track id, frame)
        self.selected_marker = None
        self.tool = "select"
        self.snap = True
        self.linked = True
        self.source_media = None
        self.clipboard = []
        self.focus = "timeline"
        self.drag_payload = None
        self.window = None
        self.picker = None                # fn(x, y) waiting for a click in the Program monitor
        self.camera_cuts = []             # (frame, angle, track id) switched to while playing
        self.export_job = None
        self._listeners = defaultdict(list)
        self.frames = {}                  # monitor -> (rgb bytes, width, height): what scopes analyse
        self.on("frame", lambda kind, pixels, w, h: self.frames.__setitem__(kind, (pixels, w, h)))
        self._rebuild_id = 0
        self._keymap = {}                 # accelerator -> command, from self.keymap
        self._startup_path = None
        self.exit_status = 0
        self.prefs = settingsmod.Settings()
        if os.environ.get("TIDELINE_DEBUG_SCRIPT"):
            self.prefs.save = lambda: None        # test runs leave the user's settings alone
            self.prefs["shortcuts"] = {}
            self.prefs["dock_layouts"] = {}
        self.keymap = keymapmod.Keymap(self.prefs["shortcuts"])
        self.launcher = None
        self.popout = None
        self._ph_last = 0                 # when "playhead" was last emitted during playback
        self._ph_flush = 0
        self.extensions = extensions.registry
        self._extension_note = None       # load problems, shown once the window is up
        # Proxy jobs report from their worker thread; hop to the main loop.
        self.proxy_queue = proxies.Queue(
            on_progress=lambda job, i, n: GLib.idle_add(self._proxy_progress, job, i, n),
            on_done=lambda job, ok: GLib.idle_add(self._proxy_done, job, ok))
        self._proxy_made = 0

    # -- events ----------------------------------------------------------
    def on(self, event, fn):
        self._listeners[event].append(fn)

    def off(self, event, fn):
        try:
            self._listeners[event].remove(fn)
        except ValueError:
            pass

    def emit(self, event, *args):
        for fn in list(self._listeners[event]):
            fn(*args)

    def status(self, text, warn=False):
        self.emit("status", text, warn)

    # -- lifecycle -------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        from .ui import theme
        theme.install()
        # The icon ships inside the package, so windows have it even when
        # Tideline runs from the source tree without being installed.
        icons = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icons.add_search_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        mltlib.init()
        from . import appimage
        appimage.restore_host_env()
        self._load_extensions()
        self.builder = Builder(self.project, proxies=self.prefs["use_proxies"])
        self.program = Player("program")
        self.source = Player("source")
        self.apply_playback_prefs()
        for name, label, accel, group in COMMANDS:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, n=name: self.do(n))
            self.add_action(action)
        self._rebind()
        label = Gio.SimpleAction.new("set_label", GLib.VariantType.new("i"))
        label.connect("activate", lambda a, p: self.set_label(p.get_int32()))
        self.add_action(label)
        camera = Gio.SimpleAction.new("set_camera", GLib.VariantType.new("i"))
        camera.connect("activate", lambda a, p: self.set_camera(p.get_int32()))
        self.add_action(camera)
        del_track = Gio.SimpleAction.new("delete_track", GLib.VariantType.new("s"))
        del_track.connect("activate", lambda a, p: self.seq and self.seq.track(p.get_string())
                          and self.delete_track(self.seq.track(p.get_string())))
        self.add_action(del_track)
        self.extensions.attach(self)
        self.history.reset(self.project.to_dict(), "New Project")
        GLib.timeout_add_seconds(120, self._autosave)

    def _rebind(self):
        self._keymap = self.keymap.bindings()

    def set_shortcuts(self, overrides=None):
        """The keyboard shortcuts changed (Edit ▸ Keyboard Shortcuts): rebind
        keys, save them and let menus and tooltips show the new ones."""
        if overrides is not None:
            self.keymap.overrides = dict(overrides)
        self.prefs["shortcuts"] = dict(self.keymap.overrides)
        self.prefs.save()
        self._rebind()
        self.emit("keymap")

    def accel_label(self, name):
        """The shortcut of a command as shown to people ("Ctrl+K"), or ""."""
        return keymapmod.label(self.keymap.accel(name))

    def tip(self, text, name):
        """Tooltip text with the command's current shortcut in brackets."""
        key = self.accel_label(name)
        return f"{text} ({key})" if key else text

    def do_activate(self):
        if self.window is None:
            skip = (self._startup_path or os.environ.get("TIDELINE_DEBUG_SCRIPT")
                    or os.environ.get("TIDELINE_NO_LAUNCHER"))
            if not skip and (not self.prefs["setup_done"] or self.prefs["show_launcher"]):
                return self.show_launcher()
            self.open_main_window()
        self.window.present()
        self._open_startup_path()

    def open_main_window(self):
        """Create the editor window (once) and show it."""
        if self.window is None:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
            self.program.on_frame = self._program_frame
            self.program.on_state = self._play_state
            self.source.on_state = lambda playing: self.emit("playstate")
            self.program.on_levels = lambda l, r, playing: self.emit("levels", l, r, playing)
            self.source.on_levels = lambda l, r, playing: self.emit("levels", l, r, playing)
            self.window.present()
            if self._extension_note:
                self.status(self._extension_note, warn=True)
                self._extension_note = None
            from . import devtools
            devtools.maybe_run(self)
        else:
            self.window.present()
        return self.window

    def show_launcher(self):
        """First run: the setup screen. Later: the project picker."""
        from .ui.launcher import Launcher
        if self.launcher is None:
            self.launcher = Launcher(self, setup=not self.prefs["setup_done"])
            self.launcher.connect("destroy", lambda *_: setattr(self, "launcher", None))
        self.launcher.present()

    def launcher_done(self, action):
        """The launcher picked something: ``action()`` runs once the editor is up."""
        self.open_main_window()
        if self.launcher is not None:
            self.launcher.destroy()
        action()

    def _open_startup_path(self):
        if self._startup_path:
            path, self._startup_path = self._startup_path, None
            if path.lower().endswith(presets.PRESET_EXT):
                self.import_presets([path])
            else:
                self.load_project(path)

    def do_open(self, files, n_files, hint):
        if files:
            self._startup_path = files[0].get_path()
        self.activate()

    def do_shutdown(self):
        self.proxy_queue.cancel()
        for p in (self.program, self.source):
            if p is not None:
                p.close()
        Adw.Application.do_shutdown(self)

    def key_command(self, keyval, state):
        for accel in keymapmod.event_accels(keyval, state):
            if accel in self._keymap:
                return self._keymap[accel]
        return None

    def do(self, name):
        ext_cmd = self.extensions.command(name)
        if ext_cmd is not None:
            return self.extensions.guard(ext_cmd.ext, ext_cmd.callback)(self)
        fn = getattr(self, "cmd_" + name, None)
        if fn is None:
            if name.startswith("tool_"):
                return self.set_tool(name[5:])
            if name.startswith("camera_"):
                return self.switch_camera(int(name[7:]) - 1)
            if name.startswith(("workspace_", "panel_")) and self.window:
                return self.window.command(name)
            return self.status(f"{BY_NAME.get(name, (name, name))[1]} is not available yet.", warn=True)
        try:
            return fn()
        except EditError as exc:
            self.status(str(exc), warn=True)

    # -- basics ----------------------------------------------------------
    @property
    def seq(self):
        return self.project.sequence

    def editor(self):
        return Editor(self.project, self.seq, linked=self.linked)

    def ctx(self, seq=None):
        seq = seq or self.seq
        return {"width": seq.width, "height": seq.height, "fps": seq.fps} if seq else \
            {"width": 1920, "height": 1080, "fps": 30.0}

    def fps(self):
        seq = self.seq
        return (seq.fps_num, seq.fps_den) if seq else (30, 1)

    def title(self):
        return f"{self.project.name}{'*' if self.project.dirty else ''} — {APP_NAME}"

    # -- committing edits ------------------------------------------------
    def commit(self, label, rebuild=True):
        seq = self.seq
        if seq is not None:
            Editor(self.project, seq).validate()
            seq._rev = getattr(seq, "_rev", 0) + 1
        self.history.push(label, self.project.to_dict())
        self.project.dirty = True
        self._prune_selection()
        if rebuild:
            self.queue_rebuild()
        self.emit("sequence")
        self.emit("history")

    def edit(self, label, fn):
        """Run an edit on the active sequence; roll back if it fails."""
        seq = self.seq
        if seq is None:
            self.status("No sequence is open. Drag a clip into the timeline to create one.", warn=True)
            return None
        snap = seq.snapshot_tracks()
        markers = [copy.copy(m) for m in seq.markers]
        try:
            result = fn(self.editor())
        except EditError as exc:
            seq.restore_tracks(snap)
            seq.markers = markers
            self.status(str(exc), warn=True)
            self.emit("sequence")
            return None
        if result is None or result is False or result == 0 or result == []:
            return None
        self.commit(label)
        return result

    def queue_rebuild(self):
        if not self._rebuild_id:
            self._rebuild_id = GLib.idle_add(self._rebuild, priority=GLib.PRIORITY_HIGH_IDLE)

    def _rebuild(self):
        self._rebuild_id = 0
        seq = self.seq
        if seq is None:
            self.program.set_producer(None, None)
            self.graph = None
            self.emit("frame", "program", None, 0, 0)
            return False
        self.builder.project = self.project
        graph = self.builder.build(seq)
        pos = min(seq.playhead, graph.length - 1)
        was_source = Player._active is self.source and self.source.playing
        self.program.set_producer(graph.tractor, graph.profile, graph.length, position=pos,
                                  refs=graph, resume=True)
        self.graph = graph
        if not was_source:
            self.program.activate()
            self.program.refresh()
        if graph.missing:
            self.status("Not available in this MLT build: " + ", ".join(sorted(graph.missing)), warn=True)
        return False

    def _prune_selection(self):
        seq = self.seq
        ids = {c.id for _, c in seq.all_clips()} if seq else set()
        before = list(self.selection)
        self.selection = [i for i in self.selection if i in ids]
        if self.selected_transition and seq:
            if Editor(self.project, seq).find_transition(self.selected_transition)[1] is None:
                self.selected_transition = None
        if before != self.selection:
            self.emit("selection")

    def _restore(self, snapshot):
        if snapshot is None:
            return
        view = {s.id: (s.playhead, s.px_per_frame, s.scroll_x, getattr(s, "_rev", 0))
                for s in self.project.sequences.values()}
        active, path, name = self.project.active_sequence_id, self.project.path, self.project.name
        marks = {m.id: (m.mark_in, m.mark_out) for m in self.project.media.values()}
        self.project.load_dict(copy.deepcopy(snapshot))
        self.project.path, self.project.name = path, name
        for s in self.project.sequences.values():
            if s.id in view:
                s.playhead, s.px_per_frame, s.scroll_x, rev = view[s.id]
                s._rev = rev + 1
        for m in self.project.media.values():
            if m.id in marks:
                m.mark_in, m.mark_out = marks[m.id]
            m.offline = bool(m.path) and not os.path.exists(m.path)
        if active in self.project.sequences:
            self.project.active_sequence_id = active
        if self.source_media is not None:
            self.source_media = self.project.media.get(self.source_media.id)
            if self.source_media is None:
                self.source.set_producer(None, None)
                self.emit("frame", "source", None, 0, 0)
                self.emit("source")
        self.project.dirty = True
        self.selected_gap = None
        self._prune_selection()
        self.queue_rebuild()
        for ev in ("media", "sequence", "selection", "history"):
            self.emit(ev)

    def cmd_undo(self):
        label = self.history.undo_label()
        snap = self.history.undo()
        if snap is not None:
            self._restore(snap)
            self.status(f"Undo: {label}")

    def cmd_redo(self):
        label = self.history.redo_label()
        snap = self.history.redo()
        if snap is not None:
            self._restore(snap)
            self.status(f"Redo: {label}")

    def history_jump(self, index):
        self._restore(self.history.jump(index))

    # -- selection -------------------------------------------------------
    def select(self, clip_ids, add=False, toggle=False, expand=True):
        ids = list(clip_ids)
        if expand and self.linked and self.seq is not None:
            ids = self.editor().expand(ids)
        if toggle:
            cur = list(self.selection)
            if ids and all(i in cur for i in ids):
                cur = [i for i in cur if i not in ids]
            else:
                cur += [i for i in ids if i not in cur]
            self.selection = cur
        elif add:
            self.selection = self.selection + [i for i in ids if i not in self.selection]
        else:
            self.selection = ids
        self.selected_transition = None
        self.selected_gap = None
        self.emit("selection")

    def select_transition(self, transition_id):
        self.selection = []
        self.selected_gap = None
        self.selected_transition = transition_id
        self.emit("selection")

    def select_gap(self, track, t):
        self.selection = []
        self.selected_transition = None
        self.selected_gap = (track.id, t)
        self.emit("selection")

    def clear_selection(self):
        if self.selection or self.selected_transition or self.selected_gap:
            self.selection, self.selected_transition, self.selected_gap = [], None, None
            self.emit("selection")

    def selected_clips(self):
        seq = self.seq
        if seq is None:
            return []
        out = []
        for cid in self.selection:
            track, clip = seq.find_clip(cid)
            if clip is not None:
                out.append((track, clip))
        return out

    def cmd_select_all(self):
        if self.seq:
            self.select([c.id for t, c in self.seq.all_clips() if not t.locked], expand=False)

    def cmd_deselect_all(self):
        self.clear_selection()

    def cmd_select_at_playhead(self):
        seq = self.seq
        if seq:
            ids = [t.clip_at(seq.playhead).id for t in seq.tracks()
                   if t.target and not t.locked and t.clip_at(seq.playhead)]
            self.select(ids, expand=False)

    def set_tool(self, name):
        self.tool = name
        self.emit("tool")

    # -- playhead and transport ------------------------------------------
    def set_playhead(self, t, seek=True):
        seq = self.seq
        if seq is None:
            return
        seq.playhead = max(0, int(t))
        if seek and self.program.has_producer:
            self.program.seek(min(seq.playhead, self.program.length - 1))
        self.emit("playhead")

    def _program_frame(self, pixels, w, h, pos):
        seq = self.seq
        if seq is not None and self.program.playing and seq.playhead != pos:
            seq.playhead = pos
            self._playhead_tick()
        self.emit("frame", "program", pixels, w, h)

    def _playhead_tick(self):
        """During playback the picture updates every frame, but the panels
        that follow the playhead (timeline, timecodes, Effect Controls) are
        redrawn at most ``ui_fps`` times a second."""
        now = GLib.get_monotonic_time()
        interval = 1_000_000 // max(1, int(self.prefs["ui_fps"]))
        if now - self._ph_last >= interval:
            self._ph_last = now
            self.emit("playhead")
        elif not self._ph_flush:
            wait = (interval - (now - self._ph_last)) // 1000 + 1
            self._ph_flush = GLib.timeout_add(max(1, wait), self._playhead_flush)

    def _playhead_flush(self):
        self._ph_flush = 0
        self._ph_last = GLib.get_monotonic_time()
        self.emit("playhead")
        return False

    def _play_state(self, playing):
        self.emit("playstate")
        if not playing and self.camera_cuts:
            cuts, self.camera_cuts = self.camera_cuts, []
            self._apply_camera_cuts(cuts)
        if not playing and self.seq is not None:
            # Panels skipped while playing catch up with where it stopped.
            self.emit("playhead")

    # -- playback quality ------------------------------------------------
    def apply_playback_prefs(self, scale_monitors=True):
        p = self.prefs
        threads = settingsmod.render_threads(p["threads"])
        for player in (self.program, self.source):
            if player is not None:
                player.configure(scale=p["preview_scale"] if scale_monitors else None, scaling=p["scaling"],
                                 threads=threads, drop_frames=p["drop_frames"])
        self.emit("playback_prefs")

    def cmd_preferences(self):
        from .ui.launcher import PreferencesDialog
        PreferencesDialog(self).present()

    def cmd_project_manager(self):
        self.show_launcher()

    def active_player(self):
        return self.source if self.focus in ("source",) and self.source.has_producer else self.program

    def _source_focus(self):
        return self.focus == "source" and self.source.has_producer

    def seek_active(self, frame):
        if self._source_focus():
            self.source.seek(frame)
            self.emit("source_pos")
        else:
            self.set_playhead(frame)

    def position_active(self):
        return self.source.position if self._source_focus() else (self.seq.playhead if self.seq else 0)

    def cmd_play_toggle(self):
        p = self.active_player()
        if p is self.program and self.seq is not None and not p.playing:
            p.loop_range = None
            if self.seq.playhead >= p.length - 1:
                self.set_playhead(0)
        p.toggle()

    def cmd_play_in_out(self):
        seq = self.seq
        if seq is None or self._source_focus():
            return self.cmd_play_toggle()
        a = seq.in_point or 0
        b = seq.out_point if seq.out_point is not None else self.program.length
        self.program.loop_range = (a, b)
        was = self.program.loop
        self.program.loop = True if was else False
        self.set_playhead(a)
        self.program.play(1.0)
        if not was:
            self._stop_at = b
            GLib.timeout_add(30, self._watch_out)

    def _watch_out(self):
        b = getattr(self, "_stop_at", None)
        if b is None or not self.program.playing:
            self._stop_at = None
            return False
        if self.program.position >= b - 1:
            self.program.pause()
            self._stop_at = None
            return False
        return True

    def cmd_toggle_loop(self):
        self.program.loop = self.source.loop = not self.program.loop
        self.status("Loop playback " + ("on" if self.program.loop else "off"))
        self.emit("options")

    def cmd_shuttle_back(self):
        self.active_player().shuttle(-1)

    def cmd_shuttle_fwd(self):
        self.active_player().shuttle(1)

    def cmd_shuttle_stop(self):
        self.active_player().pause()

    def _step(self, n):
        p = self.active_player()
        if p.playing:
            p.pause()
        self.seek_active(max(0, self.position_active() + n))

    def cmd_step_back(self):
        self._step(-1)

    def cmd_step_fwd(self):
        self._step(1)

    def cmd_step_back_5(self):
        self._step(-5)

    def cmd_step_fwd_5(self):
        self._step(5)

    def cmd_goto_start(self):
        self.seek_active(0)

    def cmd_goto_end(self):
        if self._source_focus():
            self.source.seek(self.source.length - 1)
            self.emit("source_pos")
        elif self.seq:
            self.set_playhead(self.seq.end())

    def cmd_prev_edit(self):
        seq = self.seq
        if seq:
            pts = [p for p in seq.edit_points() if p < seq.playhead]
            self.set_playhead(pts[-1] if pts else 0)

    def cmd_next_edit(self):
        seq = self.seq
        if seq:
            pts = [p for p in seq.edit_points() if p > seq.playhead]
            self.set_playhead(pts[0] if pts else seq.end())

    # -- marks -----------------------------------------------------------
    def source_fps(self):
        return self.source.fps if self.source.has_producer else 30.0

    def source_marks(self):
        """(in, out exclusive) of the source clip in source-monitor frames."""
        m = self.source_media
        if m is None:
            return 0, 1
        fps, length = self.source_fps(), self.source.length
        a = int(round(m.mark_in * fps)) if m.mark_in is not None else 0
        b = int(round(m.mark_out * fps)) if m.mark_out is not None else length
        a = max(0, min(a, length - 1))
        return a, max(a + 1, min(b, length))

    def _mark(self, which):
        if self._source_focus():
            m, fps, pos = self.source_media, self.source_fps(), self.source.position
            if which == "in":
                m.mark_in = pos / fps
                if m.mark_out is not None and m.mark_out <= m.mark_in:
                    m.mark_out = None
            else:
                m.mark_out = (pos + 1) / fps
                if m.mark_in is not None and m.mark_in >= m.mark_out:
                    m.mark_in = None
            self.emit("source")
            return
        seq = self.seq
        if seq is None:
            return
        if which == "in":
            seq.in_point = seq.playhead
            if seq.out_point is not None and seq.out_point <= seq.in_point:
                seq.out_point = None
        else:
            seq.out_point = seq.playhead + 1
            if seq.in_point is not None and seq.in_point >= seq.out_point:
                seq.in_point = None
        self.emit("marks")

    def cmd_mark_in(self):
        self._mark("in")

    def cmd_mark_out(self):
        self._mark("out")

    def cmd_mark_clip(self):
        seq = self.seq
        if seq is None:
            return
        clips = [c for _, c in self.selected_clips()] or \
            [t.clip_at(seq.playhead) for t in seq.tracks() if t.target and t.clip_at(seq.playhead)]
        if clips:
            seq.in_point, seq.out_point = min(c.start for c in clips), max(c.end for c in clips)
            self.emit("marks")

    def _goto_mark(self, which):
        if self._source_focus():
            a, b = self.source_marks()
            self.source.seek(a if which == "in" else b - 1)
            self.emit("source_pos")
        elif self.seq:
            seq = self.seq
            if which == "in":
                self.set_playhead(seq.in_point or 0)
            else:
                self.set_playhead((seq.out_point - 1) if seq.out_point is not None else seq.end())

    def cmd_goto_in(self):
        self._goto_mark("in")

    def cmd_goto_out(self):
        self._goto_mark("out")

    def _clear_marks(self, a, b):
        if self._source_focus():
            if a:
                self.source_media.mark_in = None
            if b:
                self.source_media.mark_out = None
            self.emit("source")
        elif self.seq:
            if a:
                self.seq.in_point = None
            if b:
                self.seq.out_point = None
            self.emit("marks")

    def cmd_clear_in(self):
        self._clear_marks(True, False)

    def cmd_clear_out(self):
        self._clear_marks(False, True)

    def cmd_clear_in_out(self):
        self._clear_marks(True, True)

    def cmd_add_marker(self):
        seq = self.seq
        if seq is None:
            return
        existing = next((m for m in seq.markers if m.t == seq.playhead), None)
        if existing is not None:
            return self.window.edit_marker(existing)
        seq.markers.append(Marker(t=seq.playhead))
        seq.markers.sort(key=lambda m: m.t)
        self.commit("Add Marker", rebuild=False)

    def _marker_step(self, direction):
        seq = self.seq
        if seq is None:
            return
        ts = sorted(m.t for m in seq.markers)
        cand = [t for t in ts if (t > seq.playhead if direction > 0 else t < seq.playhead)]
        if cand:
            self.set_playhead(cand[0] if direction > 0 else cand[-1])

    def cmd_next_marker(self):
        self._marker_step(1)

    def cmd_prev_marker(self):
        self._marker_step(-1)

    def cmd_clear_marker(self):
        seq = self.seq
        if seq is None:
            return
        target = next((m for m in seq.markers if m.id == self.selected_marker), None) or \
            next((m for m in seq.markers if m.t == seq.playhead), None)
        if target is not None:
            seq.markers.remove(target)
            self.commit("Clear Marker", rebuild=False)

    def cmd_clear_all_markers(self):
        if self.seq and self.seq.markers:
            self.seq.markers = []
            self.commit("Clear All Markers", rebuild=False)

    # -- source monitor --------------------------------------------------
    def open_source(self, media, frame=None):
        if media is None or media.is_bin:
            return
        self.program.pause()
        try:
            profile, producer, length, refs = self.builder.build_media(media, self.seq)
        except Exception as exc:
            return self.status(f"Cannot open {media.name}: {exc}", warn=True)
        self.source_media = media
        self.source.set_producer(producer, profile, length, refs=refs)
        fps = self.source.fps
        if frame is None:
            frame = int(round(media.mark_in * fps)) if media.mark_in is not None else 0
        self.source.activate()
        self.source.seek(frame)
        self.focus = "source"
        self.emit("source")
        if self.window:
            self.window.show_panel("source")

    def open_item(self, media):
        if media.kind == "sequence":
            self.set_active_sequence(media.sequence_id)
        elif not media.is_bin:
            self.open_source(media)

    def set_active_sequence(self, seq_id):
        if seq_id not in self.project.sequences or seq_id == self.project.active_sequence_id:
            if self.window:
                self.window.show_panel("timeline")
            return
        self.program.pause()
        self.project.active_sequence_id = seq_id
        self.selection, self.selected_transition, self.selected_gap = [], None, None
        self.queue_rebuild()
        for ev in ("sequence", "selection", "playhead", "marks"):
            self.emit(ev)
        if self.window:
            self.window.show_panel("timeline")

    def cmd_match_frame(self):
        seq = self.seq
        if seq is None:
            return
        clips = [c for _, c in self.selected_clips() if c.start <= seq.playhead < c.end]
        if not clips:
            for track in reversed(seq.video_tracks):
                c = track.clip_at(seq.playhead)
                if c is not None and track.target:
                    clips = [c]
                    break
        if not clips:
            return self.status("No clip under the playhead to match.", warn=True)
        clip = clips[0]
        media = self.project.media.get(clip.media_id)
        if media is None or media.unlimited:
            return self.open_source(media)
        local = clip.source_frame(seq.playhead)
        remap = timeremap.remap_of(clip) if media.kind in timeremap.MEDIA_KINDS else None
        if remap is not None:
            local = int(round(remap.position(local)))
        total = self.project.media_frames(media, seq, clip.speed) or 1
        if clip.reverse:
            local = total - 1 - local
        self.open_source(media, int(local * abs(clip.speed)))

    # -- insert / overwrite ---------------------------------------------
    def sequence_from_media(self, media):
        if media.kind == "sequence":
            return self.set_active_sequence(media.sequence_id)
        w, h = (media.width, media.height) if media.has_video and media.width else (1920, 1080)
        num, den = _rational_fps(media.fps) if media.fps else (30, 1)
        name = self.project.unique_name(os.path.splitext(media.name)[0])
        seq = Sequence.create(name, w + (w % 2), h + (h % 2), num, den,
                              sample_rate=48000)
        self.project.add_sequence(seq, parent=media.parent)
        self.project.active_sequence_id = seq.id
        self.emit("media")
        return seq

    def _source_edit(self, insert):
        media = self.source_media
        if media is None:
            return self.status("Open a clip in the Source monitor first.", warn=True)
        created = False
        if self.seq is None:
            self.sequence_from_media(media)
            created = True
        seq = self.seq
        fps_ratio = seq.fps / self.source_fps()
        a, b = self.source_marks()
        src_in, src_out = int(round(a * fps_ratio)), int(round(b * fps_ratio))
        if media.unlimited:
            src_in, src_out = 0, src_out - src_in
        t = seq.in_point if seq.in_point is not None else seq.playhead
        if seq.in_point is not None and seq.out_point is not None and media.mark_out is None:
            src_out = src_in + (seq.out_point - seq.in_point)
        self.source.pause()
        clips = self.edit("Insert" if insert else "Overwrite",
                          lambda ed: ed.place(media, t, src_in=src_in, src_out=src_out, insert=insert))
        if clips:
            self.focus = "timeline"
            self.set_playhead(clips[0].end, seek=False)
            if created:
                self.emit("view_fit")
            if self.window:
                self.window.show_panel("timeline")

    def cmd_insert(self):
        self._source_edit(True)

    def cmd_overwrite(self):
        self._source_edit(False)

    def drop_media(self, items, t, v_track=None, a_track=None, insert=False, video=True, audio=True,
                   src_range=None):
        """Place project items on the timeline one after another from ``t``."""
        items = [m for m in items if not m.is_bin]
        if not items:
            return []
        created = False
        if self.seq is None:
            first = next((m for m in items if m.kind != "sequence"), None)
            if first is None:
                return []
            self.sequence_from_media(first)
            t, created = 0, True
        seq = self.seq
        items = [m for m in items if not (m.kind == "sequence" and m.sequence_id == seq.id)]

        def run(ed):
            pos, out = int(t), []
            for m in items:
                a, b = src_range if (src_range and len(items) == 1) else self._marks_in_seq(m, seq)
                clips = ed.place(m, pos, v_track=v_track, a_track=a_track, src_in=a, src_out=b,
                                 insert=insert, video=video, audio=audio)
                out += clips
                pos = clips[0].end
            return out

        clips = self.edit("Insert Clip" if insert else "Overwrite Clip", run)
        if clips:
            self.select([c.id for c in clips], expand=False)
            if created:
                self.emit("view_fit")
        return clips or []

    def _marks_in_seq(self, media, seq):
        if media.mark_in is None and media.mark_out is None:
            return None, None
        if media.unlimited:
            return 0, int(round(((media.mark_out or 5.0) - (media.mark_in or 0.0)) * seq.fps))
        a = int(round(media.mark_in * seq.fps)) if media.mark_in is not None else None
        b = int(round(media.mark_out * seq.fps)) if media.mark_out is not None else None
        return a, b

    # -- editing commands ------------------------------------------------
    def cmd_delete(self):
        if self.focus == "project" and self.window:
            return self.window.project_panel.delete_selected()
        if self.selected_transition:
            tid = self.selected_transition
            if self.edit("Remove Transition", lambda ed: ed.remove_transition(tid)):
                self.selected_transition = None
                self.emit("selection")
            return
        if self.selected_gap and self.seq:
            return self.cmd_ripple_delete()
        ids = list(self.selection)
        if ids:
            self.edit("Clear", lambda ed: ed.delete(ids))
            self.clear_selection()

    def cmd_ripple_delete(self):
        seq = self.seq
        if seq is None:
            return
        if self.selected_gap:
            tid, t = self.selected_gap
            track = seq.track(tid)
            if track is not None and self.edit("Ripple Delete Gap", lambda ed: ed.ripple_delete_gap(track, t)):
                self.selected_gap = None
                self.emit("selection")
            return
        ids = list(self.selection)
        if ids:
            self.edit("Ripple Delete", lambda ed: ed.delete(ids, ripple=True))
            self.clear_selection()

    def cmd_copy(self):
        if self.selection and self.seq:
            self.clipboard = self.editor().copy_items(self.selection)
            self.status(f"Copied {len(self.clipboard)} clip(s)")

    def cmd_cut(self):
        if self.selection:
            self.cmd_copy()
            ids = list(self.selection)
            self.edit("Cut", lambda ed: ed.delete(ids))
            self.clear_selection()

    def _paste(self, insert):
        seq = self.seq
        if seq is None or not self.clipboard:
            return
        items = [it for it in self.clipboard if it["clip"]["media_id"] in self.project.media]
        ids = self.edit("Paste Insert" if insert else "Paste",
                        lambda ed: ed.paste(items, seq.playhead, insert=insert))
        if ids:
            self.select(ids, expand=False)
            end = max(seq.find_clip(i)[1].end for i in ids)
            self.set_playhead(end, seek=False)

    def cmd_paste(self):
        self._paste(False)

    def cmd_paste_insert(self):
        self._paste(True)

    def cmd_duplicate(self):
        seq = self.seq
        pairs = self.selected_clips()
        if not pairs:
            return
        ids = [c.id for _, c in pairs]
        span = max(c.end for _, c in pairs) - min(c.start for _, c in pairs)
        new = self.edit("Duplicate", lambda ed: ed.move(ids, span, duplicate=True))
        if new:
            self.select(new, expand=False)

    def cmd_add_edit(self, all_tracks=False):
        seq = self.seq
        if seq is None:
            return
        ids = [c.id for _, c in self.selected_clips() if c.start < seq.playhead < c.end]
        self.edit("Add Edit", lambda ed: ed.split(seq.playhead, ids or None, all_tracks=all_tracks))

    def cmd_add_edit_all(self):
        self.cmd_add_edit(all_tracks=True)

    def cmd_ripple_trim_prev(self):
        seq = self.seq
        if seq:
            removed = self.edit("Ripple Trim Previous Edit", lambda ed: ed.ripple_trim_to(seq.playhead, "prev"))
            if removed:
                self.set_playhead(removed[0])

    def cmd_ripple_trim_next(self):
        seq = self.seq
        if seq:
            self.edit("Ripple Trim Next Edit", lambda ed: ed.ripple_trim_to(seq.playhead, "next"))

    def _marked_range(self):
        seq = self.seq
        if seq is None or (seq.in_point is None and seq.out_point is None):
            self.status("Mark an In and Out point in the sequence first (I and O).", warn=True)
            return None
        return seq.in_point or 0, seq.out_point if seq.out_point is not None else seq.end()

    def cmd_lift(self):
        r = self._marked_range()
        if r and self.edit("Lift", lambda ed: ed.lift(*r)):
            self.seq.in_point = self.seq.out_point = None
            self.emit("marks")

    def cmd_extract(self):
        r = self._marked_range()
        if r and self.edit("Extract", lambda ed: ed.extract(*r)):
            self.seq.in_point = self.seq.out_point = None
            self.set_playhead(r[0])
            self.emit("marks")

    def _nudge(self, n):
        ids = list(self.selection)
        if ids:
            self.edit("Nudge", lambda ed: ed.move(ed.expand(ids), n))

    def cmd_nudge_left(self):
        self._nudge(-1)

    def cmd_nudge_right(self):
        self._nudge(1)

    def cmd_nudge_left_5(self):
        self._nudge(-5)

    def cmd_nudge_right_5(self):
        self._nudge(5)

    def cmd_toggle_enable(self):
        pairs = self.selected_clips()
        if pairs:
            state = not all(c.enabled for _, c in pairs)
            for _, c in pairs:
                c.enabled = state
            self.commit("Enable Clip" if state else "Disable Clip")

    def cmd_link(self):
        pairs = self.selected_clips()
        if not pairs:
            return
        ids = [c.id for _, c in pairs]
        linked = len({c.link_id for _, c in pairs}) == 1 and pairs[0][1].link_id is not None
        if linked:
            self.edit("Unlink", lambda ed: ed.unlink(ids))
        else:
            self.edit("Link", lambda ed: ed.link(ids))

    def cmd_rename(self):
        if self.focus == "project" and self.window:
            return self.window.project_panel.rename_selected()
        pairs = self.selected_clips()
        if not pairs:
            return
        from .ui.widgets import ask_text

        def apply(text):
            for _, c in pairs:
                c.name = text
            self.commit("Rename Clip", rebuild=False)

        ask_text(self.window, "Rename Clip", "Clip name", pairs[0][1].name, apply)

    def set_label(self, index):
        pairs = self.selected_clips()
        for _, c in pairs:
            c.label = index
        if pairs:
            self.commit("Label", rebuild=False)

    def cmd_speed(self):
        pairs = self.selected_clips()
        if not pairs:
            return self.status("Select a clip to change its speed.", warn=True)
        from .ui.dialogs import SpeedDialog
        SpeedDialog(self, pairs[0][1]).present()

    def apply_speed(self, speed, reverse, ripple):
        ids = list(self.selection)
        self.edit("Speed/Duration", lambda ed: ed.set_speed(ids, speed, reverse, ripple))

    # -- captions --------------------------------------------------------
    def caption_style(self, seq=None):
        """The style new captions get: the sequence's own, over the default."""
        seq = seq or self.seq
        style = captionsmod.default_style(seq.width, seq.height)
        style.update({k: v for k, v in (seq.caption_style or {}).items() if k in captionsmod.STYLE_KEYS})
        return style

    def caption_style_of(self, clip, seq=None):
        """The style a caption (or any title) clip shows, as a style dict."""
        seq = seq or self.seq
        return captionsmod.style_of(clip.effect("text"), clip.effect("drop_shadow"), self.caption_style(seq),
                                    seq.width, seq.height)

    def _styled_effects(self, clip, style, seq):
        """``clip``'s effects with its text restyled (its words kept) and a
        drop shadow added or removed."""
        params = captionsmod.text_params(style, seq.width, seq.height)
        effects = []
        for e in clip.effects:
            if e.type == "text":
                e = Effect("text", id=e.id, enabled=e.enabled, collapsed=e.collapsed,
                           params=dict(e.params, **params),
                           keyframes={k: v for k, v in e.keyframes.items() if k not in params})
            elif e.type == "drop_shadow":
                continue
            effects.append(e)
        if style.get("shadow"):
            at = next((i + 1 for i, e in enumerate(effects) if e.type == "text"), 0)
            effects.insert(at, Effect("drop_shadow", params=captionsmod.shadow_params(style)))
        return effects

    def caption_clips(self, scope="all", seq=None):
        """(track, clip) of the captions a style applies to: every clip on the
        caption tracks, or the selected clips that have text."""
        seq = seq or self.seq
        if seq is None:
            return []
        if scope == "selected":
            return [(t, c) for t, c in self.selected_clips() if c.effect("text") is not None and not t.locked]
        return [(t, c) for t in self.caption_tracks(seq) if not t.locked for c in t.clips if c.effect("text")]

    def apply_caption_style(self, style, scope="all", make_default=True):
        """Restyle captions (``scope`` "all" or "selected"); with
        ``make_default`` new captions in this sequence look the same.
        Returns the number of clips restyled."""
        seq = self.seq
        if seq is None:
            return 0
        style = {k: v for k, v in style.items() if k in captionsmod.STYLE_KEYS}
        pairs = self.caption_clips(scope, seq)
        for _, clip in pairs:
            clip.effects = self._styled_effects(clip, style, seq)
        if make_default:
            seq.caption_style = dict(style)
        if not pairs and not make_default:
            self.status("Select caption clips first, or apply the style to all captions.", warn=True)
            return 0
        self.commit("Caption Style")
        self.emit("selection")
        n = len(pairs)
        self.status(f"Restyled {n} caption{'s' if n != 1 else ''}" if n else "Saved the style for new captions")
        return n

    def cmd_caption_style(self):
        if self.seq is None:
            return self.status("Open a sequence first.", warn=True)
        from .ui.caption_style import CaptionStyleDialog
        CaptionStyleDialog(self).present()

    def caption_tracks(self, seq=None):
        seq = seq or self.seq
        return [t for t in seq.video_tracks if t.captions] if seq is not None else []

    def add_captions(self, cues, start=0, end=None, replace=False, label="Add Captions"):
        """Place ``cues`` (seconds from frame ``start``) as caption clips. With
        ``replace`` they go on the existing caption track, clearing
        ``start``..``end`` first; otherwise on a caption track with nothing
        in that range, or a new one at the top. Returns the number placed."""
        seq = self.seq
        if seq is None or not cues:
            return 0
        fps = seq.fps
        style = self.caption_style(seq)
        params = captionsmod.text_params(style, seq.width, seq.height)
        item = next((m for m in self.project.media.values()
                     if m.kind == "title" and m.name == "Captions" and m.parent is None), None)
        if item is None:
            item = self.project.add_media(MediaItem(name="Captions", kind="title", has_video=True,
                                                    label=LABEL_GENERATED, params=dict(params, text="Caption")))
        stop = end if end is not None else start + int(round(cues[-1].end * fps)) + 1

        def run(ed):
            existing = self.caption_tracks(seq)
            free = [t for t in existing if not t.locked and not any(c.start < stop and c.end > start for c in t.clips)]
            if replace and existing:
                track = existing[-1]
                ed._free(track)
                ed.clear_range(track, start, max(stop, track.end()) if end is None else stop)
            elif free:
                track = free[-1]
            else:
                track = seq.add_track("video")
                n = len(existing) + 1
                track.name = "Captions" if n == 1 else f"Captions {n}"
                track.captions = True
            placed, prev_end = 0, 0
            for cue in cues:
                a = max(start + int(round(cue.start * fps)), prev_end)
                b = start + int(round(cue.end * fps))
                if end is not None:
                    b = min(b, end)
                if b <= a:
                    continue
                track.clips = [c for c in track.clips if c.end <= a or c.start >= b]
                clip = Clip(media_id=item.id, start=a, duration=b - a, name="", effects=[
                    Effect("text", params=dict(params, text=cue.text)), Effect("motion"), Effect("opacity")])
                clip.effects = self._styled_effects(clip, style, seq)
                track.clips.append(clip)
                prev_end = b
                placed += 1
            if not placed:
                raise EditError("None of the captions fall inside the sequence.")
            track.sort()
            return placed

        placed = self.edit(label, run) or 0
        self.emit("media")
        if placed:
            self.status(f"{placed} caption{'s' if placed != 1 else ''} added. Sequence ▸ Captions ▸ Caption Style "
                        "changes how they look; File ▸ Export Captions writes SRT or WebVTT.")
        return placed

    def caption_source(self, clip_ids):
        """A copy of the sequence holding only the given clips and the clips
        linked to them, for captioning just their sound. Returns (sequence,
        first frame, end frame)."""
        seq = self.seq
        ids = set(Editor(self.project, seq, linked=True).expand(list(clip_ids)))
        copy_ = Sequence.from_dict(seq.to_dict())
        copy_.id = seq.id + "-captions"
        for track in copy_.tracks():
            track.clips = [c for c in track.clips if c.id in ids]
            track.transitions = [t for t in track.transitions if t.clip_a in ids or t.clip_b in ids]
        clips = [c for _, c in copy_.all_clips()]
        if not clips:
            return copy_, 0, 0
        return copy_, min(c.start for c in clips), max(c.end for c in clips)

    def caption_cues(self, seq=None):
        """The sequence's captions as cues (seconds from the sequence start)."""
        seq = seq or self.seq
        out = []
        for track in self.caption_tracks(seq):
            if track.hidden:
                continue
            for c in track.clips:
                text_fx = c.effect("text")
                text = (text_fx.value("text", None, "") if text_fx else c.name) or ""
                if c.enabled and text.strip():
                    out.append(captionsmod.Cue(c.start / seq.fps, c.end / seq.fps, text.strip()))
        out.sort(key=lambda q: q.start)
        return out

    def cmd_auto_captions(self):
        seq = self.seq
        if seq is None or seq.end() <= 0:
            return self.status("Add some clips with speech to the timeline first.", warn=True)
        if not any(t.clips for t in seq.audio_tracks):
            return self.status("This sequence has no audio clips to caption.", warn=True)
        from .ui.captions_dialog import CaptionsDialog
        CaptionsDialog(self).present()

    def cmd_import_captions(self):
        if self.seq is None:
            return self.status("Open a sequence to add captions to.", warn=True)
        from .ui.widgets import file_filter, open_files

        def chosen(paths):
            try:
                cues = captionsmod.load(paths[0])
            except captionsmod.CaptionError as exc:
                return self.status(str(exc), warn=True)
            self.add_captions(cues, 0, label="Import Captions")

        open_files(self.window, "Import Captions", chosen,
                   [file_filter("Captions (SRT, WebVTT)", ["*.srt", "*.vtt", "*.SRT", "*.VTT"]),
                    file_filter("All files", ["*"])], multiple=False)

    def cmd_export_captions(self):
        seq = self.seq
        cues = self.caption_cues(seq) if seq is not None else []
        if not cues:
            return self.status("No captions to export: use Sequence ▸ Captions ▸ Auto Captions or Import "
                               "Captions first.", warn=True)
        from .ui.widgets import file_filter, save_file
        folder = os.path.dirname(self.project.path) if self.project.path else None

        def chosen(path):
            if not path.lower().endswith((".srt", ".vtt")):
                path += ".srt"
            try:
                captionsmod.save(cues, path)
            except OSError as exc:
                return self.status(f"Could not write {path}: {exc.strerror}", warn=True)
            self.status(f"Exported {len(cues)} captions to {path}")

        save_file(self.window, "Export Captions", seq.name + ".srt", chosen,
                  [file_filter("SubRip (.srt)", ["*.srt"]), file_filter("WebVTT (.vtt)", ["*.vtt"])],
                  folder=folder)

    # -- multi-camera ----------------------------------------------------
    def cmd_create_multicam(self):
        items = [m for m in (self.window.project_panel.selected_items() if self.window else [])
                 if m.kind in ("video", "audio") and m.path]
        if not [m for m in items if m.has_video]:
            return self.status("Select the camera clips (two or more) in the Project panel first.", warn=True)
        if len(items) < 2:
            return self.status("A multi-camera sequence needs two or more clips: select them in the Project "
                               "panel.", warn=True)
        from .ui.multicam import MulticamDialog
        MulticamDialog(self, items).present()

    def create_multicam(self, items, starts, name, audio=None, edit_sequence=True):
        """Make a multi-camera source sequence from ``items`` lined up at
        ``starts`` (seconds), and, with ``edit_sequence``, a sequence that
        holds it, ready for cutting between cameras."""
        try:
            mc = multicam.build(self.project, items, starts, self.project.unique_name(name), audio)
        except multicam.SyncError as exc:
            return self.status(str(exc), warn=True)
        parent = self.window.project_panel.current_bin if self.window else None
        item = self.project.add_sequence(mc, parent=parent)
        label = "Create Multi-Camera Sequence"
        if edit_sequence:
            seq = Sequence.create(self.project.unique_name(f"{mc.name} Edit"), mc.width, mc.height, mc.fps_num,
                                  mc.fps_den, mc.sample_rate)
            self.project.add_sequence(seq, parent=parent)
            Editor(self.project, seq).place(item, 0)
            self.project.active_sequence_id = seq.id
            self.selection, self.selected_transition, self.selected_gap = [], None, None
            seq.playhead = 0
        self.history.push(label, self.project.to_dict())
        self.project.dirty = True
        self.queue_rebuild()
        for ev in ("media", "sequence", "selection", "playhead", "history"):
            self.emit(ev)
        if edit_sequence:
            self.emit("view_fit")
        if self.window:
            self.window.show_panel("multicam")
        self.status(f"{mc.name}: {len(mc.video_tracks)} cameras. Play the sequence and press 1–"
                    f"{min(9, len(mc.video_tracks))} (or click a camera) to cut between them.")
        return mc

    def multicam_target(self, seq=None):
        """(track, clip, multi-camera sequence) under the playhead: a selected
        multi-camera clip there, else the topmost visible one."""
        seq = seq or self.seq
        if seq is None:
            return None
        found = []
        for track in reversed(seq.video_tracks):
            clip = track.clip_at(seq.playhead)
            inner = self.project.multicam_of(self.project.media.get(clip.media_id)) if clip else None
            if inner is not None and not track.hidden:
                found.append((track, clip, inner))
        selected = [f for f in found if f[1].id in self.selection]
        return (selected or found or [None])[0]

    def switch_camera(self, angle):
        """Cut to camera ``angle`` (0-based) at the playhead. While playing the
        switches are noted and made when playback stops; when stopped, a
        selected multi-camera clip changes camera as a whole, otherwise it is
        cut at the playhead and the rest of it changes camera."""
        seq = self.seq
        target = self.multicam_target()
        if target is None:
            return self.status("No multi-camera clip under the playhead. Select camera clips in the Project panel "
                               "and use Clip ▸ Multi-Camera ▸ Create Multi-Camera Sequence.", warn=True)
        track, clip, inner = target
        if not 0 <= angle < len(inner.video_tracks):
            return self.status(f"{inner.name} has {len(inner.video_tracks)} cameras.", warn=True)
        if self.program.playing:
            self.camera_cuts.append((seq.playhead, angle, track.id))
            self.emit("multicam")
            return
        whole = clip.id in self.selection
        self._apply_camera_cuts([(clip.start if whole else seq.playhead, angle, track.id)])

    def _apply_camera_cuts(self, cuts):
        seq = self.seq
        names = []

        def run(_ed):
            ed = Editor(self.project, seq, linked=True)
            done = 0
            for t, angle, track_id in sorted(cuts):
                track = seq.track(track_id)
                clip = track.clip_at(t) if track is not None else None
                inner = self.project.multicam_of(self.project.media.get(clip.media_id)) if clip else None
                if inner is None or track.locked:
                    continue
                if t > clip.start:
                    ed.split(t, [clip.id])
                    clip = track.clip_at(t)
                for c in [clip] + [p for _, p in ed.partners(clip)]:
                    c.angle = angle
                names.append(multicam.angle_names(inner)[angle] if angle < len(inner.video_tracks) else str(angle))
                done += 1
            return done

        if self.edit("Switch Camera" if len(cuts) == 1 else f"Switch Camera ×{len(cuts)}", run):
            self.status(f"Switched to {names[-1]}" if len(names) == 1 else f"Made {len(names)} camera switches")
        self.emit("multicam")

    def set_camera(self, angle, clip_ids=None):
        """Give the selected multi-camera clips (and their sound) camera ``angle``."""
        seq = self.seq
        ed = Editor(self.project, seq, linked=True)
        pairs = ed.items(ed.expand(clip_ids if clip_ids is not None else self.selection))
        pairs = [(t, c) for t, c in pairs if self.project.multicam_of(self.project.media.get(c.media_id))
                 and not t.locked]
        if not pairs:
            return self.status("Select multi-camera clips to change their camera.", warn=True)
        for _, c in pairs:
            c.angle = angle
        self.commit("Change Camera")
        self.emit("multicam")

    def set_multicam_audio(self, inner, mode):
        if inner.multicam_audio != mode:
            inner.multicam_audio = mode
            inner._rev = getattr(inner, "_rev", 0) + 1
            self.commit("Multi-Camera Audio")
            self.emit("multicam")

    # -- pop-out preview -------------------------------------------------
    def cmd_popout_monitor(self):
        """Open (or bring back) the pop-out preview window."""
        from .ui.popout import PopoutMonitor
        if self.popout is None:
            self.popout = PopoutMonitor(self, "source" if self.focus == "source" else "program")
        self.popout.present()

    # -- time remapping --------------------------------------------------
    def cmd_time_remap(self):
        """Switch time remapping on for the selected clips (and the clips
        linked to them), or off when every one of them already has it."""
        seq = self.seq
        if seq is None:
            return
        ed = Editor(self.project, seq, linked=True)
        pairs = [(t, c) for t, c in ed.items(ed.expand(self.selection)) if not t.locked]
        pairs = [(t, c) for t, c in pairs
                 if getattr(self.project.media.get(c.media_id), "kind", None) in timeremap.MEDIA_KINDS]
        if not pairs:
            return self.status("Select a clip made from a video or audio file to remap its time.", warn=True)
        if all(c.effect(timeremap.EFFECT) is not None for _, c in pairs):
            for _, c in pairs:
                c.effects = [e for e in c.effects if e.type != timeremap.EFFECT]
            self.commit("Remove Time Remapping")
            self.status("Time remapping removed.")
        else:
            for _, c in pairs:
                if c.effect(timeremap.EFFECT) is None:
                    c.effects = [Effect(timeremap.EFFECT, params={"speed": 100.0, "anchor": c.in_point})] + c.effects
            self.commit("Time Remapping")
            self.status("Time remapping is on: animate Speed in Effect Controls (stopwatch, then the curve "
                        "button for its graph). 0 % freezes the frame, below 0 runs backwards.")
            if self.window:
                self.window.show_panel("effect_controls")
        self.emit("selection")

    def _clip_of_effect(self, effect):
        for track, clip in self.seq.all_clips() if self.seq else ():
            if any(e is effect for e in clip.effects):
                return track, clip
        return None, None

    def _remap_partners(self, clip):
        if self.seq is None or not clip.link_id:
            return []
        return Editor(self.project, self.seq).partners(clip)

    def _sync_remap(self, effect):
        """Give the clips linked to the effect's clip the same speed curve,
        so picture and sound stay together."""
        _, clip = self._clip_of_effect(effect)
        if clip is None:
            return
        for _, other in self._remap_partners(clip):
            if getattr(self.project.media.get(other.media_id), "kind", None) not in timeremap.MEDIA_KINDS:
                continue
            shift = other.in_point - clip.in_point
            mirror = Effect(timeremap.EFFECT, enabled=effect.enabled,
                            params=dict(effect.params, anchor=int(effect.params.get("anchor", clip.in_point)) + shift),
                            keyframes={n: kf.scale_times(k, 1.0, shift) for n, k in effect.keyframes.items() if k})
            current = other.effect(timeremap.EFFECT)
            if current is not None:
                mirror.id, mirror.collapsed = current.id, current.collapsed
                other.effects = [mirror if e is current else e for e in other.effects]
            else:
                other.effects = [mirror] + other.effects

    def cmd_remove_effects(self):
        changed = False
        for _, c in self.selected_clips():
            kept = []
            for e in c.effects:
                edef = EFFECTS.get(e.type)
                if edef is not None and (edef.intrinsic or e.type == "text"):
                    kept.append(Effect(e.type, id=e.id) if edef.intrinsic else e)
                else:
                    changed = True
            if len(kept) != len(c.effects) or any(e.params or e.keyframes for e in c.effects
                                                  if getattr(EFFECTS.get(e.type), "intrinsic", False)):
                changed = True
            c.effects = kept
        if changed:
            self.commit("Remove Effects")

    def cmd_reveal(self):
        pairs = self.selected_clips()
        if pairs and self.window:
            self.window.project_panel.reveal(pairs[0][1].media_id)

    # -- transitions -----------------------------------------------------
    def apply_transition(self, key, clip_id=None, edge=None, duration=None):
        seq = self.seq
        if seq is None:
            return
        tdef = TRANSITIONS[key]
        dur = duration or int(round(seq.fps))
        targets = []
        if clip_id is not None:
            targets = [(clip_id, edge or "end")]
        elif self.selection:
            seen = set()
            for track, c in self.selected_clips():
                if track.kind != tdef.kind:
                    continue
                for e in ("start", "end"):
                    at = (track.id, c.start if e == "start" else c.end)
                    if at not in seen:
                        seen.add(at)
                        targets.append((c.id, e))
        else:
            best = None
            for track in seq.tracks_of(tdef.kind):
                if not track.target or track.locked:
                    continue
                for c in track.clips:
                    for e, at in (("start", c.start), ("end", c.end)):
                        d = abs(at - seq.playhead)
                        if best is None or d < best[0]:
                            best = (d, c.id, e)
            if best is not None and best[0] <= seq.fps * 2:
                targets = [(best[1], best[2])]
        if not targets:
            return self.status("Select a clip, or park the playhead near a cut, to apply a transition.", warn=True)

        def run(ed):
            return [ed.add_transition(cid, e, key, dur) for cid, e in targets]

        self.edit(f"Apply {tdef.label}", run)

    def cmd_video_transition(self):
        self.apply_transition(DEFAULT_VIDEO_TRANSITION)

    def cmd_audio_transition(self):
        self.apply_transition(DEFAULT_AUDIO_TRANSITION)

    def cmd_default_transitions(self):
        seq = self.seq
        if seq is None or not self.selection:
            return self.status("Select clips to apply the default transitions.", warn=True)
        dur = int(round(seq.fps))
        pairs = self.selected_clips()

        def run(ed):
            out, seen = [], set()
            for track, c in pairs:
                key = DEFAULT_VIDEO_TRANSITION if track.kind == "video" else DEFAULT_AUDIO_TRANSITION
                for e in ("start", "end"):
                    at = (track.id, c.start if e == "start" else c.end)
                    if at not in seen:
                        seen.add(at)
                        out.append(ed.add_transition(c.id, e, key, dur))
            return out

        self.edit("Apply Default Transitions", run)

    def update_transition(self, tr, final=True, **changes):
        for k, v in changes.items():
            if k in ("type", "duration", "align"):
                setattr(tr, k, v)
            else:
                tr.params = dict(tr.params, **{k: v})
        if final:
            self.commit("Change Transition")
        else:
            self.emit("sequence")

    # -- effects ---------------------------------------------------------
    def apply_effect(self, key, clip_ids=None):
        seq = self.seq
        edef = EFFECTS.get(key)
        if seq is None or edef is None:
            return
        ids = clip_ids if clip_ids is not None else list(self.selection)
        n = 0
        for cid in ids:
            track, clip = seq.find_clip(cid)
            if clip is not None and edef.fits(track.kind) and not track.locked:
                clip.effects = clip.effects + [Effect(key)]
                n += 1
        if n:
            self.commit(f"Apply {edef.label}")
            self.emit("selection")
        else:
            kind = "a video" if edef.kind == "video" else "an audio"
            self.status(f"{edef.label} is {kind} effect: select {kind} clip first.", warn=True)

    def remove_effect(self, clip, effect):
        clip.effects = [e for e in clip.effects if e.id != effect.id]
        if effect.type == timeremap.EFFECT:
            for _, other in self._remap_partners(clip):
                other.effects = [e for e in other.effects if e.type != timeremap.EFFECT]
        self.commit(f"Remove {EFFECTS[effect.type].label}")
        self.emit("selection")

    def move_effect(self, clip, effect, delta):
        effects = list(clip.effects)
        i = effects.index(effect)
        j = i + delta
        if 0 <= j < len(effects) and not getattr(EFFECTS.get(effects[j].type), "intrinsic", False):
            effects[i], effects[j] = effects[j], effects[i]
            clip.effects = effects
            self.commit("Reorder Effects")
            self.emit("selection")

    def effect_changed(self, effect, final=True, label="Change Effect"):
        """An effect's parameters changed: update the live graph, redraw."""
        if effect.type == timeremap.EFFECT:
            self._sync_remap(effect)
        if self.graph is None or not self.graph.update_effect(effect):
            self.queue_rebuild()
        else:
            self.program.activate()
            self.program.refresh()
        if final:
            self.commit(label, rebuild=False)
        self.emit("effects")

    def track_audio_changed(self, final=True, label="Track Mix"):
        """Track volume / pan / mute / solo changed (mixer panel)."""
        seq = self.seq
        if seq is None:
            return
        if self.graph is None or not self.graph.update_audio_tracks(seq):
            self.queue_rebuild()
        else:
            self.program.refresh()
        if final:
            self.commit(label, rebuild=False)
        else:
            self.emit("mixer")

    def clip_time(self, clip):
        """The clip's own frame under the playhead, clamped to the clip."""
        t = self.seq.playhead if self.seq else clip.start
        return clip.source_frame(max(clip.start, min(clip.end - 1, t)))

    def param_value(self, clip, effect, pdef):
        edef = EFFECTS[effect.type]
        v = effect.value(pdef.name, self.clip_time(clip), None)
        return edef.default(pdef.name, self.ctx()) if v is None else v

    def set_param(self, clip, effect, pdef, value, final=True):
        if pdef.key and effect.animated(pdef.name):
            kfs = effect.keyframes[pdef.name]
            effect.keyframes = dict(effect.keyframes, **{pdef.name: kf.set_key(
                kfs, self.clip_time(clip), value, "hold" if pdef.hold else None)})
        else:
            effect.params = dict(effect.params, **{pdef.name: value})
        self.effect_changed(effect, final, f"Change {pdef.label}")

    def toggle_animation(self, clip, effect, pdef, on):
        value = self.param_value(clip, effect, pdef)
        kfs = dict(effect.keyframes)
        if on:
            kfs[pdef.name] = [[self.clip_time(clip), value, "hold" if pdef.hold else "linear"]]
        else:
            kfs.pop(pdef.name, None)
            effect.params = dict(effect.params, **{pdef.name: value})
        effect.keyframes = kfs
        self.effect_changed(effect, True, "Toggle Animation")
        self.emit("selection")

    def toggle_keyframe(self, clip, effect, pdef):
        t = self.clip_time(clip)
        kfs = effect.keyframes.get(pdef.name) or []
        if kf.find(kfs, t) >= 0:
            new = kf.remove_key(kfs, t)
            if not new:
                return self.toggle_animation(clip, effect, pdef, False)
        else:
            new = kf.set_key(kfs, t, self.param_value(clip, effect, pdef), "hold" if pdef.hold else None)
        effect.keyframes = dict(effect.keyframes, **{pdef.name: new})
        self.effect_changed(effect, True, "Keyframe")

    def goto_keyframe(self, clip, effect, pdef, direction):
        prev, nxt = kf.neighbours(effect.keyframes.get(pdef.name), self.clip_time(clip))
        target = nxt if direction > 0 else prev
        if target is not None:
            self.set_playhead(clip.start + (target - clip.in_point))

    def set_interp(self, clip, effect, pdef, interp, t=None):
        """Set the curve of the segment leaving the keyframe at ``t`` (default:
        the playhead). Switching to a custom curve keeps the current shape."""
        t = self.clip_time(clip) if t is None else t
        kfs = [list(k) for k in effect.keyframes.get(pdef.name) or []]
        i = kf.find(kfs, t)
        if i < 0 or pdef.hold:
            return
        kfs[i] = kfs[i][:2] + (["bezier", kf.curve_of(kfs[i])] if interp == "bezier" else [interp])
        self.set_keyframes(effect, pdef, kfs, label="Keyframe Interpolation")

    def set_keyframes(self, effect, pdef, kfs, final=True, label="Keyframe"):
        """Replace a parameter's keyframes (graph editing)."""
        if not kfs:
            return
        effect.keyframes = dict(effect.keyframes, **{pdef.name: kfs})
        self.effect_changed(effect, final, label)

    def remove_keyframe(self, clip, effect, pdef, t):
        kfs = kf.remove_key(effect.keyframes.get(pdef.name), t)
        if not kfs:
            return self.toggle_animation(clip, effect, pdef, False)
        self.set_keyframes(effect, pdef, kfs, label="Remove Keyframe")

    def reset_effect(self, effect):
        keep = {"anchor": effect.params["anchor"]} if "anchor" in effect.params else {}
        effect.params, effect.keyframes = keep, {}
        self.effect_changed(effect, True, "Reset Effect")
        self.emit("selection")

    def toggle_effect(self, effect, enabled):
        effect.enabled = enabled
        self.effect_changed(effect, True, "Toggle Effect")

    # -- colour picking --------------------------------------------------
    def start_pick(self, callback, hint):
        """The next click in the Program monitor goes to ``callback(x, y)``
        (sequence pixels) instead of selecting or moving clips."""
        self.picker = callback
        self.emit("picker")
        self.status(hint)

    def cancel_pick(self):
        if self.picker is not None:
            self.picker = None
            self.emit("picker")

    def picked(self, x, y):
        """A click in the Program monitor while picking."""
        fn, self.picker = self.picker, None
        self.emit("picker")
        if fn is not None:
            fn(x, y)

    def sample_colour(self, x, y, without=None, radius=3):
        """Average RGB (0..1) around sequence pixel (x, y) of the frame under
        the playhead, rendered with the effect ``without`` bypassed."""
        seq = self.seq
        was = without.enabled if without is not None else None
        if without is not None:
            without.enabled = False
        try:
            graph = Builder(self.project, proxies=self.prefs["use_proxies"]).build(seq)
        finally:
            if without is not None:
                without.enabled = was
        w = min(seq.width, 960)
        h = max(2, int(round(seq.height * w / seq.width / 2)) * 2)
        graph.tractor.seek(max(0, min(seq.playhead, graph.length - 1)))
        frame = graph.tractor.get_frame()
        data = mltlib.mlt.frame_get_image(frame, mltlib.IMAGE_RGB, w, h)
        img = np.frombuffer(data, np.uint8).reshape(h, w, 3)
        px, py = int(x * w / seq.width), int(y * h / seq.height)
        patch = img[max(0, py - radius):py + radius + 1, max(0, px - radius):px + radius + 1]
        return tuple(float(c) / 255.0 for c in patch.reshape(-1, 3).mean(axis=0)) if patch.size else (0.0, 0.0, 0.0)

    def pick_qualifier(self, clip, effect):
        """Let a click in the Program monitor set a secondary correction's
        qualifier to the colour under the pointer."""
        def chosen(x, y):
            seq = self.seq
            if seq is None or not (0 <= x < seq.width and 0 <= y < seq.height):
                return self.status("Click inside the picture to pick a colour.", warn=True)
            rgb = self.sample_colour(x, y, without=effect)
            effect.params = dict(effect.params, **grading.sample_qualifier(rgb))
            self.effect_changed(effect, True, "Pick Qualifier Colour")
            self.emit("selection")
            r, g, b = (round(c * 255) for c in rgb)
            self.status(f"Selected colours like RGB {r} {g} {b}. Set Show to Mask to see the selection.")

        chosen.effect = effect
        self.start_pick(chosen, "Click a colour in the Program monitor (Esc cancels).")

    # -- presets ---------------------------------------------------------
    def apply_preset(self, path, clip_ids=None):
        """Apply a ``.tlpreset`` file to the given clips (default: the selection)."""
        seq = self.seq
        if seq is None:
            return 0
        try:
            preset = presets.load(path)
        except presets.PresetError as exc:
            self.status(str(exc), warn=True)
            return 0
        pairs = [seq.find_clip(i) for i in clip_ids] if clip_ids is not None else self.selected_clips()
        n = 0
        for track, clip in pairs:
            if clip is None or track.locked:
                continue
            effects = presets.apply(preset, track, clip, seq)
            if effects is not None:
                clip.effects = effects
                n += 1
        if not n:
            kinds = " or ".join(sorted(presets.kinds(preset)))
            self.status(f"“{preset['name']}” is a preset for {kinds} clips: select one first.", warn=True)
            return 0
        self.commit(f"Apply Preset {preset['name']}")
        self.emit("selection")
        skipped = f" — skipped unknown effects: {', '.join(preset['unknown'])}" if preset["unknown"] else ""
        self.status(f"Applied “{preset['name']}” to {n} clip(s){skipped}", warn=bool(skipped))
        return n

    def import_presets(self, paths):
        """Copy preset files into the library shown in the Effects panel."""
        done, errors = [], []
        for path in paths:
            try:
                done.append(presets.install(path)[1]["name"])
            except (presets.PresetError, OSError) as exc:
                errors.append(str(exc))
        if done:
            self.emit("presets")
            if self.window:
                self.window.show_panel("effects")
        msg = (f"Imported preset(s): {', '.join(done)} — find them under Presets in the Effects panel"
               if done else "No preset imported")
        self.status(msg + (f" — {errors[0]}" if errors else ""), warn=bool(errors))
        return len(done)

    def cmd_import_preset(self):
        from .ui.widgets import file_filter, open_files
        open_files(self.window, "Import Preset", self.import_presets,
                   [file_filter(f"{APP_NAME} presets", ["*" + presets.PRESET_EXT]), file_filter("All files", ["*"])])

    def save_preset(self, path, name=None):
        """Write the selected clip's effects and keyframes to a preset file."""
        pairs = self.selected_clips()
        ids = {c.link_id for _, c in pairs}
        if not pairs or (len(pairs) > 1 and (len(ids) != 1 or None in ids)):
            self.status("Select one clip to save its effects as a preset.", warn=True)
            return False
        if not path.lower().endswith(presets.PRESET_EXT):
            path += presets.PRESET_EXT
        name = name or os.path.splitext(os.path.basename(path))[0]
        try:
            presets.dump(presets.capture(pairs, self.seq, name), path)
        except (presets.PresetError, OSError) as exc:
            self.status(str(exc), warn=True)
            return False
        self.emit("presets")
        self.status(f"Saved preset {os.path.basename(path)}")
        return True

    def cmd_save_preset(self):
        pairs = self.selected_clips()
        if not pairs:
            return self.status("Select one clip to save its effects as a preset.", warn=True)
        from .ui.widgets import file_filter, save_file
        clip = pairs[0][1]
        media = self.project.media.get(clip.media_id)
        os.makedirs(presets.user_dir(), exist_ok=True)
        save_file(self.window, "Save Effects as Preset",
                  presets.file_name(clip.name or (media.name if media else "Preset")), self.save_preset,
                  [file_filter(f"{APP_NAME} presets", ["*" + presets.PRESET_EXT])], folder=presets.user_dir())

    def delete_preset(self, path):
        try:
            os.remove(path)
        except OSError as exc:
            return self.status(str(exc), warn=True)
        self.emit("presets")

    # -- fonts -----------------------------------------------------------
    def add_fonts(self, paths, then=None):
        """Install font files for titles; ``then(family names)`` afterwards."""
        names, live, errors = [], True, []
        for p in (self.program, self.source):
            if p is not None:
                p.pause()
        for path in paths:
            try:
                found, ok = fonts.add(path)
            except fonts.FontError as exc:
                errors.append(str(exc))
                continue
            names += [n for n in found if n not in names]
            live = live and ok
        if names:
            self.queue_rebuild()
            self.emit("fonts")
            msg = f"Added font(s): {', '.join(names)}"
            if not live:
                msg += " — restart Tideline to use them in titles"
            self.status(msg + (f" — {errors[0]}" if errors else ""), warn=bool(errors) or not live)
            if then:
                then(names)
        elif errors:
            self.status(errors[0], warn=True)
        return names

    def choose_fonts(self, then=None):
        from .ui.widgets import file_filter, open_files
        pats = ["*" + e for e in fonts.FONT_EXT] + ["*" + e.upper() for e in fonts.FONT_EXT]
        open_files(self.window, "Add Fonts", lambda paths: self.add_fonts(paths, then),
                   [file_filter("Font files", pats)])

    def cmd_add_fonts(self):
        self.choose_fonts()

    # -- project items ---------------------------------------------------
    def import_paths(self, paths, parent=None, then=None):
        # Presets and fonts dropped with (or instead of) media go to their own libraries.
        preset_files = [p for p in paths if p.lower().endswith(presets.PRESET_EXT)]
        font_files = [p for p in paths if p.lower().endswith(fonts.FONT_EXT)]
        if preset_files:
            self.import_presets(preset_files)
        if font_files:
            self.add_fonts(font_files)
        paths = [p for p in paths if p not in preset_files and p not in font_files]
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    files += [os.path.join(root, n) for n in sorted(names)
                              if os.path.splitext(n)[1].lower() in mediamod.MEDIA_EXT]
            elif p.lower().endswith(PROJECT_EXT):
                return self.load_project(p)
            else:
                files.append(p)
        if not files:
            return
        self.status(f"Importing {len(files)} file(s)…")

        def work():
            results, errors = [], []
            for f in files:
                existing = self.project.media_for_path(f)
                if existing is not None:
                    results.append(existing)
                    continue
                try:
                    results.append(mediamod.probe(f))
                except mediamod.ProbeError as exc:
                    errors.append(str(exc))
            GLib.idle_add(done, results, errors)

        def done(results, errors):
            fresh = []
            for item in results:
                if item.id not in self.project.media:
                    item.parent = parent
                    self.project.add_media(item)
                    fresh.append(item)
            added = len(fresh)
            if added:
                self.history.push(f"Import {added} Item(s)", self.project.to_dict())
                self.project.dirty = True
                self.emit("media")
                self.emit("history")
                self.emit("media_imported", fresh)
                limit = int(self.prefs["proxy_auto"] or 0)
                if limit:
                    big = [m for m in fresh if proxies.eligible(m) and min(m.width, m.height) >= limit]
                    if big:
                        self.create_proxies(big, quiet=True)
            msg = f"Imported {added} item(s)" if added else "Nothing new to import"
            if errors:
                msg += f" — skipped {len(errors)}: {errors[0]}"
            self.status(msg, warn=bool(errors))
            if then:
                then(results)
            return False

        threading.Thread(target=work, daemon=True).start()

    def cmd_import(self):
        from .ui.widgets import file_filter, open_files
        pats = ["*" + e for e in sorted(mediamod.MEDIA_EXT)]
        pats += [p.upper() for p in pats]
        parent = self.window.project_panel.current_bin if self.window else None
        open_files(self.window, "Import Media", lambda paths: self.import_paths(paths, parent),
                   [file_filter("Media files", pats), file_filter("All files", ["*"])])

    def _add_item(self, item, label):
        item.parent = self.window.project_panel.current_bin if self.window else None
        self.project.add_media(item)
        self.history.push(label, self.project.to_dict())
        self.project.dirty = True
        self.emit("media")
        self.emit("history")
        return item

    def cmd_new_bin(self):
        self._add_item(MediaItem(name=self.project.unique_name("Bin"), kind="bin"), "New Bin")

    def cmd_new_black(self):
        self._add_item(MediaItem(name=self.project.unique_name("Black Video"), kind="color", has_video=True,
                                 label=LABEL_GENERATED, params={"color": "#000000ff"}), "New Black Video")

    def cmd_new_bars(self):
        self._add_item(MediaItem(name=self.project.unique_name("Bars"), kind="bars", has_video=True,
                                 label=LABEL_GENERATED), "New Bars")
        self._add_item(MediaItem(name=self.project.unique_name("Tone 1 kHz"), kind="tone", has_audio=True,
                                 label=LABEL_AUDIO, params={"frequency": 1000.0, "level": -12.0}), "New Tone")

    def cmd_new_color_matte(self):
        dlg = Gtk.ColorDialog(title="Colour Matte", with_alpha=False)
        rgba = Gdk.RGBA()
        rgba.parse("#2a6f97")

        def done(d, result):
            try:
                c = d.choose_rgba_finish(result)
            except GLib.Error:
                return
            hexc = "#{:02x}{:02x}{:02x}ff".format(int(c.red * 255), int(c.green * 255), int(c.blue * 255))
            self._add_item(MediaItem(name=self.project.unique_name("Colour Matte"), kind="color",
                                     has_video=True, label=LABEL_GENERATED, params={"color": hexc}),
                           "New Colour Matte")

        dlg.choose_rgba(self.window, rgba, None, done)

    def cmd_new_title(self, text="Title", offset=None):
        params = {"text": text, "size": max(24, int(self.ctx()["height"] / 9))}
        if offset is not None:
            params.update(offset=offset, halign="left", valign="top")
        item = self._add_item(MediaItem(name=self.project.unique_name("Title"), kind="title", has_video=True,
                                        label=LABEL_GENERATED, params=params), "New Title")
        seq = self.seq
        if seq is None:
            return item
        t, dur = seq.playhead, int(round(5 * seq.fps))
        track = next((tr for tr in seq.video_tracks if not tr.locked
                      and not any(c.start < t + dur and c.end > t for c in tr.clips)), None)

        def run(ed):
            dest = track or seq.add_track("video")
            return ed.place(item, t, v_track=dest)

        clips = self.edit("Add Title", run)
        if clips:
            self.select([clips[0].id], expand=False)
            if self.window:
                self.window.show_panel("effect_controls")
        return item

    def cmd_new_sequence(self):
        from .ui.dialogs import SequenceDialog
        SequenceDialog(self, None).present()

    def cmd_project_settings(self):
        from .ui.dialogs import ProjectSettingsDialog
        ProjectSettingsDialog(self).present()

    def update_project_settings(self, width, height, fps_num, fps_den, sample_rate, scope="active"):
        """Set the project's frame shape and rates. ``scope`` says which
        existing sequences follow: "active", "all" or "new" (none)."""
        self.project.settings = {"width": width, "height": height, "fps_num": fps_num, "fps_den": fps_den,
                                 "sample_rate": sample_rate}
        targets = list(self.project.sequences.values()) if scope == "all" else \
            [self.seq] if scope == "active" and self.seq is not None else []
        for seq in targets:
            self._reshape_sequence(seq, width, height, fps_num, fps_den, sample_rate)
            seq._rev = getattr(seq, "_rev", 0) + 1
        self.commit("Project Settings")
        self.emit("media")
        self.emit("playhead")
        self.emit("project")

    def cmd_sequence_settings(self):
        if self.seq is None:
            return self.cmd_new_sequence()
        from .ui.dialogs import SequenceDialog
        SequenceDialog(self, self.seq).present()

    def create_sequence(self, name, width, height, fps_num, fps_den, sample_rate, vtracks, atracks):
        seq = Sequence.create(self.project.unique_name(name), width, height, fps_num, fps_den,
                              sample_rate, vtracks, atracks)
        parent = self.window.project_panel.current_bin if self.window else None
        self.project.add_sequence(seq, parent=parent)
        self.project.active_sequence_id = seq.id
        self.selection = []
        self.history.push("New Sequence", self.project.to_dict())
        self.project.dirty = True
        self.queue_rebuild()
        for ev in ("media", "sequence", "selection", "playhead", "history"):
            self.emit(ev)

    def _reshape_sequence(self, seq, width, height, fps_num, fps_den, sample_rate):
        """Change a sequence's frame size and rates, keeping its content in
        place: clips stay at the same time and the same relative position."""
        old_fps, sx, sy = seq.fps, width / seq.width, height / seq.height
        seq.width, seq.height = width, height
        seq.fps_num, seq.fps_den, seq.sample_rate = fps_num, fps_den, sample_rate
        item = self.project.media_for_sequence(seq.id)
        if item is not None:
            item.width, item.height, item.fps = width, height, seq.fps
        r = seq.fps / old_fps
        retime, resize = abs(r - 1.0) > 1e-9, (sx, sy) != (1.0, 1.0)
        if not retime and not resize:
            return
        for _, c in seq.all_clips():
            if retime:
                # Keep clips at the same time rather than the same frame number.
                end = int(round(c.end * r))
                c.start = int(round(c.start * r))
                c.duration = max(1, end - c.start)
                c.in_point = int(round(c.in_point * r))
            effects = []
            for e in c.effects:
                edef = EFFECTS.get(e.type)
                points = {p.name for p in edef.params if p.type == "point"} if (edef and resize) else set()
                move = lambda v: [round(v[0] * sx, 2), round(v[1] * sy, 2)]
                keyframes = {}
                for name, kfs in e.keyframes.items():
                    kfs = kf.scale_times(kfs, r) if retime else kfs
                    keyframes[name] = [[k[0], move(k[1])] + list(k[2:]) for k in kfs] if name in points else kfs
                params = {n: (move(v) if n in points and v else v) for n, v in e.params.items()}
                if params != e.params or keyframes != e.keyframes:
                    e = Effect(e.type, id=e.id, enabled=e.enabled, params=params, keyframes=keyframes,
                               collapsed=e.collapsed)
                effects.append(e)
            c.effects = effects
        if retime:
            for m in seq.markers:
                m.t = int(round(m.t * r))
            seq.playhead = int(round(seq.playhead * r))
            seq.in_point = seq.out_point = None

    def update_sequence(self, seq, name, width, height, fps_num, fps_den, sample_rate):
        seq.name = name
        item = self.project.media_for_sequence(seq.id)
        if item is not None:
            item.name = name
        self._reshape_sequence(seq, width, height, fps_num, fps_den, sample_rate)
        self.commit("Sequence Settings")
        self.emit("media")
        self.emit("playhead")

    def _add_track(self, kind):
        if self.seq:
            self.seq.add_track(kind)
            self.commit("Add Track")

    def cmd_add_video_track(self):
        self._add_track("video")

    def cmd_add_audio_track(self):
        self._add_track("audio")

    def cmd_delete_empty_tracks(self):
        seq = self.seq
        if seq is None:
            return
        changed = False
        for lst in (seq.video_tracks, seq.audio_tracks):
            while len(lst) > 1 and not lst[-1].clips:
                lst.pop()
                changed = True
        if changed:
            self.commit("Delete Empty Tracks")

    def delete_track(self, track):
        self.edit("Delete Track", lambda ed: ed.delete_track(track))

    def _track_heights(self, delta):
        if self.seq:
            for t in self.seq.tracks():
                t.height = max(28, min(200, t.height + delta))
            self.emit("sequence")

    def cmd_taller_tracks(self):
        self._track_heights(12)

    def cmd_shorter_tracks(self):
        self._track_heights(-12)

    def cmd_toggle_snap(self):
        self.snap = not self.snap
        self.status("Snapping " + ("on" if self.snap else "off"))
        self.emit("options")

    def cmd_toggle_linked(self):
        self.linked = not self.linked
        self.status("Linked selection " + ("on" if self.linked else "off"))
        self.emit("options")

    def cmd_zoom_in(self):
        self.emit("zoom", 1.4)

    def cmd_zoom_out(self):
        self.emit("zoom", 1 / 1.4)

    def cmd_zoom_fit(self):
        self.emit("view_fit")

    def cmd_toggle_safe_margins(self):
        self.emit("safe_margins")

    def cmd_maximize_panel(self):
        if self.window:
            self.window.toggle_maximize()

    def cmd_reset_workspace(self):
        if self.window:
            self.window.reset_workspace()

    def cmd_toggle_fullscreen(self):
        if self.window:
            self.window.toggle_fullscreen()

    # -- project files ---------------------------------------------------
    def _guard_unsaved(self, then):
        if not self.project.dirty:
            return then()
        from .ui.widgets import confirm

        def answer(i):
            if i == 1:
                self.cmd_save(then)
            elif i == 2:
                then()

        confirm(self.window, f"Save changes to “{self.project.name}”?",
                "Your changes will be lost if you don't save them.", "Save", answer, extra="Discard")

    def _reset_project(self, project, label):
        for p in (self.program, self.source):
            p.pause()
        self.source.set_producer(None, None)
        self.proxy_queue.cancel()
        self.project = project
        self.builder = Builder(project, proxies=self.prefs["use_proxies"])
        self.graph = None
        self.source_media = None
        self.selection, self.selected_transition, self.selected_gap = [], None, None
        self.history.reset(project.to_dict(), label)
        self.queue_rebuild()
        self.emit("frame", "source", None, 0, 0)
        for ev in ("project", "media", "sequence", "selection", "playhead", "marks", "source", "history"):
            self.emit(ev)

    def cmd_new_project(self):
        from .ui.launcher import NewProjectDialog
        self._guard_unsaved(lambda: NewProjectDialog(self).present())

    def new_blank_project(self):
        """An untitled project that is not saved anywhere yet."""
        self._reset_project(Project(), "New Project")

    def create_project(self, name, folder, width, height, fps_num, fps_den, sample_rate=48000):
        """Make ``folder/name/name.tideline`` with one sequence of that format
        and open it. Returns an error message, or None on success."""
        path, error = self.project_target(name, folder)
        if error:
            return error
        folder, target = os.path.dirname(os.path.dirname(path)), os.path.dirname(path)
        project = Project(name.strip())
        project.settings = {"width": width, "height": height, "fps_num": fps_num, "fps_den": fps_den,
                            "sample_rate": sample_rate}
        project.add_sequence(Sequence.create("Sequence 01", width, height, fps_num, fps_den, sample_rate))
        try:
            os.makedirs(target, exist_ok=True)
            project.save(path)
        except OSError as exc:
            return f"Could not create the project: {exc}"
        self.prefs["project_dir"] = folder
        self.prefs.add_recent(path)
        self._reset_project(project, "New Project")
        self.emit("view_fit")
        self.status(f"Created {path}")
        return None

    def project_target(self, name, folder):
        """(project file path, error message) for a new project."""
        name = name.strip()
        if not name or any(ch in name for ch in "/\\\0") or name in (".", ".."):
            return None, "Give the project a name (without slashes)."
        folder = os.path.abspath(os.path.expanduser(folder.strip() or self.prefs["project_dir"]))
        path = os.path.join(folder, name, name + PROJECT_EXT)
        if os.path.exists(path):
            return None, f"A project called “{name}” already exists in {folder}."
        return path, None

    def open_recent(self, path):
        self._guard_unsaved(lambda: self.load_project(path))

    def cmd_open_project(self):
        from .ui.widgets import file_filter, open_files

        def chosen(paths):
            self._guard_unsaved(lambda: self.load_project(paths[0]))

        open_files(self.window, "Open Project", chosen,
                   [file_filter(f"{APP_NAME} projects", ["*" + PROJECT_EXT])], multiple=False)

    def load_project(self, path):
        try:
            project = Project.load(path)
        except Exception as exc:
            from .ui.widgets import alert
            return alert(self.window, "Could not open the project", str(exc))
        self._reset_project(project, "Open")
        self.prefs.add_recent(path)
        offline = sum(1 for m in project.media.values() if m.offline)
        self.status(f"Opened {os.path.basename(path)}" + (f" — {offline} media file(s) offline" if offline else ""),
                    warn=bool(offline))

    def cmd_save(self, then=None):
        if not self.project.path:
            return self.cmd_save_as(then)
        try:
            self.project.save()
        except OSError as exc:
            from .ui.widgets import alert
            return alert(self.window, "Could not save the project", str(exc))
        auto = self.project.path + ".autosave"
        if os.path.exists(auto):
            os.remove(auto)
        self.prefs.add_recent(self.project.path)
        self.status(f"Saved {os.path.basename(self.project.path)}")
        self.emit("project")
        self.emit("project_saved", self.project.path)
        if then:
            then()

    def cmd_save_as(self, then=None):
        from .ui.widgets import file_filter, save_file

        def chosen(path):
            if not path.endswith(PROJECT_EXT):
                path += PROJECT_EXT
            self.project.path = path
            self.cmd_save(then)

        save_file(self.window, "Save Project As", self.project.name + PROJECT_EXT, chosen,
                  [file_filter(f"{APP_NAME} projects", ["*" + PROJECT_EXT])])

    def _autosave(self):
        if self.project.dirty and self.project.path:
            try:
                import json
                base = os.path.dirname(os.path.abspath(self.project.path))
                with open(self.project.path + ".autosave", "w", encoding="utf-8") as fh:
                    json.dump(self.project.file_dict(base), fh)
            except OSError:
                pass
        return True

    def cmd_quit(self):
        self._guard_unsaved(self.quit)

    # -- export ----------------------------------------------------------
    def cmd_export(self):
        if self.seq is None or self.seq.end() == 0:
            return self.status("There is nothing to export: the sequence is empty.", warn=True)
        from .ui.dialogs import ExportDialog
        ExportDialog(self).present()

    def export_frame_to(self, path, frame_no=None):
        """Render one full-resolution frame of the active sequence to an image."""
        seq = self.seq
        frame_no = seq.playhead if frame_no is None else frame_no
        graph = Builder(self.project).build(seq)
        graph.tractor.seek(max(0, min(frame_no, graph.length - 1)))
        f = graph.tractor.get_frame()
        f.set("consumer.rescale", "bicubic")
        data = mltlib.mlt.frame_get_image(f, mltlib.IMAGE_RGB, seq.width, seq.height)
        pix = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB,
                                              False, 8, seq.width, seq.height, seq.width * 3)
        pix.savev(path, "jpeg" if path.lower().endswith(("jpg", "jpeg")) else "png", [], [])

    def cmd_export_frame(self):
        seq = self.seq
        if seq is None:
            return
        from .ui.widgets import file_filter, save_file
        frame_no = seq.playhead

        def chosen(path):
            if not path.lower().endswith((".png", ".jpg", ".jpeg")):
                path += ".png"
            try:
                self.export_frame_to(path, frame_no)
            except Exception as exc:
                return self.status(f"Export frame failed: {exc}", warn=True)
            self.status(f"Saved frame to {path}")

        stamp = tc.frames_to_tc(frame_no, seq.fps_num, seq.fps_den).replace(":", "-").replace(";", "-")
        save_file(self.window, "Export Frame", f"{seq.name} {stamp}.png", chosen,
                  [file_filter("Images", ["*.png", "*.jpg"])])

    def relink(self, item, path):
        """Point a project item at a different file (e.g. after media moved)."""
        try:
            fresh = mediamod.probe(path)
        except mediamod.ProbeError as exc:
            return self.status(str(exc), warn=True)
        if (fresh.has_video, fresh.has_audio) != (item.has_video, item.has_audio):
            return self.status("The replacement must have the same kind of streams (video/audio).", warn=True)
        self.media_cache.forget(item.path or "")
        for name in ("path", "duration", "width", "height", "fps", "video_codec", "audio_codec",
                     "colorspace", "channels", "sample_rate", "kind"):
            setattr(item, name, getattr(fresh, name))
        item.offline = False
        for seq in self.project.sequences.values():
            seq._rev = getattr(seq, "_rev", 0) + 1
        self.history.push("Relink Media", self.project.to_dict())
        self.project.dirty = True
        self.queue_rebuild()
        for ev in ("media", "sequence", "history"):
            self.emit(ev)
        self.status(f"Relinked {item.name}")

    # -- proxies ---------------------------------------------------------
    def _proxy_items(self, items):
        """Video items among ``items``, looking inside bins."""
        out, stack, seen = [], list(items), set()
        while stack:
            m = stack.pop(0)
            if m is None or m.id in seen:
                continue
            seen.add(m.id)
            if m.is_bin:
                stack.extend(self.project.children(m.id))
            elif proxies.eligible(m):
                out.append(m)
        return out

    def proxy_targets(self):
        """Video items the proxy commands act on: the Project panel's
        selection when it has focus, else the media of the selected clips."""
        panel = self.window.project_panel.selected_items() if self.window else []
        clips = [self.project.media.get(c.media_id) for _, c in self.selected_clips()]
        first, second = (panel, clips) if self.focus == "project" else (clips, panel)
        return self._proxy_items(first) or self._proxy_items(second)

    def proxy_state(self, media):
        """Text for the Proxy column: size, queued, progress or nothing."""
        state = self.proxy_queue.state(media.id)
        if state == "creating":
            job = self.proxy_queue.current
            return f"Creating {int((job.progress if job else 0) * 100)}%"
        if state == "queued":
            return "Queued"
        path = proxies.find(self.project, media)
        if not path:
            return ""
        return "Proxy" if self.prefs["use_proxies"] else "Proxy (off)"

    def create_proxies(self, items, quiet=False):
        """Queue proxies for the video items among ``items``. Returns how many."""
        p = self.prefs
        size, codec, where = int(p["proxy_size"]), p["proxy_codec"], p["proxy_location"]
        jobs, small, offline, have = [], 0, 0, 0
        for m in self._proxy_items(items):
            if m.offline or not os.path.exists(m.path):
                offline += 1
            elif proxies.find(self.project, m):
                have += 1
            elif not proxies.worth_it(m, size):
                small += 1
            else:
                jobs.append(proxies.Job(m, proxies.target_path(self.project, m, where), size, codec))
        added = self.proxy_queue.submit(jobs)
        if not quiet or added:
            notes = [f"{n} {why}" for n, why in ((have, "already have one"), (small, f"are {size}p or smaller"),
                                                 (offline, "are offline")) if n]
            head = f"Creating {added} prox{'y' if added == 1 else 'ies'}" if added else "No proxies to create"
            self.status(head + (" — " + ", ".join(notes) if notes else ""), warn=not added)
        self.emit("proxies")
        return added

    def cmd_create_proxies(self):
        items = self.proxy_targets()
        if not items:
            return self.status("Select video in the Project panel (or clips in the timeline) first.", warn=True)
        self.create_proxies(items)

    def cmd_create_all_proxies(self):
        self.create_proxies(list(self.project.media.values()))

    def cmd_delete_proxies(self):
        items = self.proxy_targets()
        if not items:
            return self.status("Select video in the Project panel (or clips in the timeline) first.", warn=True)
        n = 0
        for m in items:
            self.proxy_queue.cancel(m.id)
            n += proxies.detach(self.project, m, delete_file=True)
        self.project.dirty = True
        self._proxies_changed()
        self.status(f"Deleted {n} prox{'y' if n == 1 else 'ies'}")

    def cmd_cancel_proxies(self):
        busy = self.proxy_queue.busy()
        self.proxy_queue.cancel()
        self.emit("proxies")
        self.status("Proxy jobs cancelled" if busy else "No proxy jobs are running")

    def cmd_attach_proxy(self):
        items = self.proxy_targets()
        if not items:
            return self.status("Select a video item in the Project panel first.", warn=True)
        from .ui.widgets import open_files
        media = items[0]
        open_files(self.window, f"Attach Proxy to “{media.name}”", lambda paths: self.attach_proxy(media, paths[0]),
                   multiple=False)

    def attach_proxy(self, media, path):
        """Use an existing file as the proxy of ``media``."""
        try:
            probe = mediamod.probe(path)
        except mediamod.ProbeError as exc:
            return self.status(str(exc), warn=True)
        if not probe.has_video or (media.has_audio and not probe.has_audio):
            return self.status("A proxy needs the same kind of streams as the original.", warn=True)
        if media.duration and abs(probe.duration - media.duration) > max(0.5, 2.0 / max(1.0, media.fps)):
            return self.status(f"The proxy lasts {probe.duration:.2f} s but the original {media.duration:.2f} s.",
                               warn=True)
        self.proxy_queue.cancel(media.id)
        proxies.attach(self.project, media, path)
        self.project.dirty = True
        self._proxies_changed()
        self.status(f"Attached proxy to {media.name}")

    def cmd_toggle_proxies(self):
        self.set_use_proxies(not self.prefs["use_proxies"])

    def set_use_proxies(self, on):
        on = bool(on)
        if on == self.prefs["use_proxies"]:
            return
        self.prefs["use_proxies"] = on
        self.prefs.save()
        self._proxies_changed()
        self.status("Playing proxies where available" if on else "Playing original media")

    def _proxies_changed(self):
        self.builder.proxies = self.prefs["use_proxies"]
        self.queue_rebuild()
        self._reload_source()
        self.emit("proxies")

    def _reload_source(self):
        """Re-open the source monitor's item in place (e.g. its proxy changed)."""
        media = self.source_media
        if media is None or media.kind != "video":
            return
        try:
            profile, producer, length, refs = self.builder.build_media(media, self.seq)
        except Exception:
            return
        self.source.set_producer(producer, profile, length, position=self.source.position, refs=refs)
        self.source.refresh()

    def _proxy_progress(self, job, index, total):
        self.status(f"Creating proxy {index} of {total}: {job.name} — {int(job.progress * 100)}%")
        self.emit("proxy_progress", job)
        return False

    def _proxy_done(self, job, ok):
        media = self.project.media.get(job.media_id)
        if ok and media is not None and media.path == job.src:
            proxies.attach(self.project, media, job.dest)
            self.project.dirty = True
            self._proxy_made += 1
            if self.prefs["use_proxies"]:
                self.builder.proxies = True
                self.queue_rebuild()
                if self.source_media is media:
                    self._reload_source()
            self.emit("proxy_ready", media)
        elif not ok and not job.cancelled:
            self.status(f"Proxy for {job.name} failed: {job.error}", warn=True)
        if not self.proxy_queue.busy():
            if self._proxy_made:
                self.status(f"{self._proxy_made} prox{'y' if self._proxy_made == 1 else 'ies'} ready")
            self._proxy_made = 0
        self.emit("proxies")
        return False

    # -- extensions ------------------------------------------------------
    def _load_extensions(self):
        if os.environ.get("TIDELINE_NO_EXTENSIONS"):
            return
        dirs = extensions.search_dirs()
        if os.environ.get("TIDELINE_DEBUG_SCRIPT"):
            dirs = dirs[1:]                       # test runs only see TIDELINE_EXTENSIONS_PATH
        self.extensions.load(dirs, disabled=set(self.prefs["disabled_extensions"]))
        failed = [e for e in self.extensions.extensions if e.error]
        if failed:
            first = failed[0]
            self._extension_note = (f"Extension “{first.name}” did not load: {first.error}"
                                    + (f" (and {len(failed) - 1} more; see Extensions ▸ Manage Extensions)"
                                       if len(failed) > 1 else ""))

    def add_extension_command(self, cmd):
        if self.lookup_action(cmd.action) is None:
            action = Gio.SimpleAction.new(cmd.action, None)
            action.connect("activate", lambda a, p, n=cmd.action: self.do(n))
            self.add_action(action)
        # An extension's shortcut never takes a key from a built-in command.
        accel = cmd.accel if cmd.accel and self.keymap.owner(cmd.accel) is None else ""
        self.keymap.extra = [e for e in self.keymap.extra if e[0] != cmd.action]
        self.keymap.extra.append((cmd.action, cmd.label, accel, "Extensions: " + cmd.ext.name))
        self._rebind()

    def remove_extension_command(self, cmd):
        self.remove_action(cmd.action)
        self.keymap.extra = [e for e in self.keymap.extra if e[0] != cmd.action]
        self._rebind()

    def cmd_extensions(self):
        from .ui.extensions_dialog import ExtensionsDialog
        ExtensionsDialog(self).present()

    def cmd_extensions_folder(self):
        folder = extensions.user_dir()
        os.makedirs(folder, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(folder)).launch(self.window, None, None)

    # -- misc ------------------------------------------------------------
    def cmd_shortcuts(self):
        from .ui.shortcuts import ShortcutMapper
        for win in self.get_windows():
            if isinstance(win, ShortcutMapper):
                return win.present()
        ShortcutMapper(self).present()

    def cmd_about(self):
        dlg = Adw.AboutDialog(
            application_name=APP_NAME, version=VERSION, application_icon=APP_ID,
            developer_name="Tideline contributors", license_type=Gtk.License.GPL_3_0,
            comments="A non-linear video editor for Linux.\nBuilt on MLT, FFmpeg and GTK 4.")
        dlg.present(self.window)

    def is_audio_transition(self, key):
        tdef = TRANSITIONS.get(key)
        return tdef.kind == "audio" if tdef is not None else key in AUDIO_TRANSITIONS
