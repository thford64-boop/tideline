"""Multi-camera editing: the dialog that lines cameras up into a
multi-camera sequence, and the Multi-Camera panel that shows every camera
at the playhead and cuts between them."""

import ctypes
import math
import threading

import cairo
import numpy as np
from gi.repository import GLib, Gtk

from .. import multicam
from ..engine import mltlib
from ..engine.builder import Builder
from . import theme
from .dialogs import _Dialog
from .widgets import icon_button, label


class MulticamDialog(_Dialog):
    """Name, sync method and sound of a new multi-camera sequence."""

    def __init__(self, app, items):
        super().__init__(app, "Create Multi-Camera Sequence", "Create")
        self.items = items
        self.job = None
        base = items[0].name.rsplit(".", 1)[0]
        self.name = self.add_row("Name", Gtk.Entry(text=f"{base} Multicam", width_chars=32))
        self.sync = self.add_row("Sync by", Gtk.DropDown.new_from_strings([m[1] for m in multicam.SYNC_MODES]))
        self.sync.set_tooltip_text("Audio lines the cameras up by their sound (a clap or speech works best). "
                                   "In points uses the In mark of each clip in the Source monitor.")
        self.sync.set_selected(0 if sum(1 for m in items if m.has_audio) >= 2 else 2)
        sound = [m for m in items if m.has_audio]
        self.audio_items = sound
        names = [m.name for m in sound] + ["Follow the camera", "Mix all cameras"]
        self.audio = self.add_row("Sound", Gtk.DropDown.new_from_strings(names))
        external = next((i for i, m in enumerate(sound) if not m.has_video), None)
        self.audio.set_selected(external if external is not None else 0)
        self.edit_seq = Gtk.CheckButton(label="Create a sequence with it and start cutting", active=True)
        self.grid.attach(self.edit_seq, 1, self._row, 1, 1)
        self._row += 1
        cams = [m.name for m in items if m.has_video]
        self.extra.append(label(f"{len(cams)} camera{'s' if len(cams) != 1 else ''}: " + ", ".join(cams[:9])
                                + (" …" if len(cams) > 9 else ""), css="faint", wrap=True, max_width_chars=60))
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.extra.append(self.progress)
        self.connect("close-request", self._closing)

    def _audio_mode(self):
        i = self.audio.get_selected()
        n = len(self.audio_items)
        if i >= n:
            return "follow" if i == n else "all"
        # The audio tracks are made in item order, one per item with sound.
        return str(i)

    def accept(self):
        if self.job is not None:
            return
        mode = multicam.SYNC_MODES[self.sync.get_selected()][0]
        name = self.name.get_text().strip() or "Multicam"
        audio, edit_seq = self._audio_mode(), self.edit_seq.get_active()
        self.ok_btn.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_text("Lining up the cameras…")
        state = {"cancel": False}
        self.job = state

        def progress(fraction, text):
            GLib.idle_add(self._progress, fraction, text)

        def work():
            try:
                starts, failed = multicam.offsets(self.items, mode, progress, lambda: state["cancel"])
                error = None
            except multicam.SyncError as exc:
                starts, failed, error = None, [], str(exc)
            GLib.idle_add(self._done, state, starts, failed, error, name, audio, edit_seq)

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, fraction, text):
        if self.job is not None:
            self.progress.set_fraction(fraction)
            self.progress.set_text(text + "…")
        return False

    def _done(self, state, starts, failed, error, name, audio, edit_seq):
        if state is not self.job:
            return False
        self.job = None
        if state["cancel"]:
            super().close()
            return False
        if error:
            self.ok_btn.set_sensitive(True)
            self.progress.set_text(error)
            return False
        self.app.create_multicam(self.items, starts, name, audio, edit_seq)
        if failed:
            self.app.status("Could not sync by sound (left at the start): " + ", ".join(failed), warn=True)
        super().close()
        return False

    def _closing(self, *_):
        if self.job is not None:
            self.job["cancel"] = True
        return False


class AngleRenderer:
    """Renders every camera of a multi-camera sequence at one frame on a
    worker thread, small; when it falls behind, only the newest frame asked
    for is drawn. ``on_ready(generation, frame, images)`` runs in the main
    loop with ``images`` as cairo surfaces."""

    def __init__(self, on_ready):
        self.on_ready = on_ready
        self.generation = 0
        self._graphs = []
        self._want = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    def set_source(self, project, seq, proxies, width):
        """Build the cameras (main thread). ``seq`` None stops rendering."""
        graphs = []
        if seq is not None:
            builder = Builder(project, proxies=proxies)
            graphs = [builder.build_angle(seq, i, width) for i in range(len(seq.video_tracks))]
        with self._lock:
            self.generation += 1
            self._graphs = graphs
            self._want = None
        return self.generation

    def request(self, frame):
        with self._lock:
            if not self._graphs:
                return
            self._want = (self.generation, self._graphs, int(frame))
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            with self._lock:
                job, self._want = self._want, None
                self._wake.clear()
            if job is None:
                continue
            gen, graphs, frame = job
            images = [self._render(g, frame) for g in graphs]
            GLib.idle_add(self.on_ready, gen, frame, images)

    @staticmethod
    def _render(graph, frame):
        """BGRA pixels of one camera at ``frame``, or None past its end."""
        w, h = graph.profile.width(), graph.profile.height()
        tractor = graph.tractor
        tractor.seek(max(0, min(frame, graph.length - 1)))
        f = tractor.get_frame()
        f.set("consumer.rescale", "bilinear")
        cw, ch = ctypes.c_int(w), ctypes.c_int(h)
        fmt = ctypes.c_int(mltlib.IMAGE_RGB)
        buf = ctypes.POINTER(ctypes.c_uint8)()
        # ctypes lets the decode run without the GIL, so the interface stays responsive.
        if mltlib.lib.mlt_frame_get_image(mltlib.ptr(f.get_frame()), ctypes.byref(buf), ctypes.byref(fmt),
                                          ctypes.byref(cw), ctypes.byref(ch), 0) or not buf \
                or fmt.value != mltlib.IMAGE_RGB:
            return None
        rgb = np.frombuffer(ctypes.string_at(buf, cw.value * ch.value * 3), np.uint8).reshape(ch.value, cw.value, 3)
        out = np.empty((ch.value, cw.value, 4), np.uint8)
        out[..., 0], out[..., 1], out[..., 2], out[..., 3] = rgb[..., 2], rgb[..., 1], rgb[..., 0], 255
        return out, cw.value, ch.value


class MultiCamPanel(Gtk.Box):
    """Every camera of the multi-camera clip under the playhead. Click one
    (or press its number) to cut to it: while playing the cuts are made when
    playback stops; when stopped, a selected clip changes camera, otherwise
    it is cut at the playhead."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.target = None              # (track, clip, multi-camera sequence)
        self.source_key = None
        self.images = {}                # angle -> cairo surface
        self.hover = None
        self._frame = None
        self._busy = False
        self.renderer = AngleRenderer(self._ready)

        bar = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
        self.title = label("", css="dim", ellipsize=True, hexpand=True)
        bar.append(self.title)
        bar.append(label("Sound", css="faint"))
        self.audio = Gtk.DropDown.new_from_strings([""])
        self.audio.set_focusable(False)
        self.audio.set_tooltip_text("What the multi-camera clip sounds like")
        self.audio.connect("notify::selected", self._audio_changed)
        self._audio_modes = []
        bar.append(self.audio)
        bar.append(icon_button("new_item", "Create a multi-camera sequence from the clips selected in the Project "
                               "panel", lambda b: app.do("create_multicam"), size=15))
        self.append(bar)
        self.area = Gtk.DrawingArea(vexpand=True, hexpand=True)
        self.area.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.area.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: self._hover(self._tile_at(x, y)))
        motion.connect("leave", lambda c: self._hover(None))
        self.area.add_controller(motion)
        self.append(self.area)
        for ev in ("playhead", "sequence", "selection", "project", "multicam", "media", "proxies"):
            app.on(ev, self.sync)
        self.connect("map", self.sync)

    # -- state -------------------------------------------------------------
    def sync(self, *_):
        if not self.get_mapped():
            return
        app = self.app
        target = app.multicam_target()
        self.target = target
        if target is None:
            self._set_source(None)
            self.title.set_text("")
            self.area.queue_draw()
            return
        track, clip, inner = target
        self._set_source(inner)
        names = multicam.angle_names(inner)
        angle = min(clip.angle or 0, len(names) - 1)
        self.title.set_text(f"{inner.name}  ·  {names[angle]}" if names else inner.name)
        modes = multicam.audio_choices(inner)
        if [m for m, _ in modes] != self._audio_modes:
            self._audio_modes = [m for m, _ in modes]
            self._busy = True
            self.audio.set_model(Gtk.StringList.new([t for _, t in modes]))
            self._busy = False
        index = self._audio_modes.index(inner.multicam_audio) if inner.multicam_audio in self._audio_modes else 0
        if self.audio.get_selected() != index:
            self._busy = True
            self.audio.set_selected(index)
            self._busy = False
        frame = clip.source_frame(app.seq.playhead)
        if frame != self._frame or not self.images:
            self._frame = frame
            self.renderer.request(frame)
        self.area.queue_draw()

    def _set_source(self, inner):
        key = None if inner is None else (inner.id, id(inner), getattr(inner, "_rev", 0), len(inner.video_tracks),
                                          bool(self.app.prefs["use_proxies"]))
        if key == self.source_key:
            return
        self.source_key = key
        self.images = {}
        self._frame = None
        n = len(inner.video_tracks) if inner is not None else 0
        width = 384 if n <= 4 else 288 if n <= 9 else 224
        self.renderer.set_source(self.app.project, inner, self.app.prefs["use_proxies"], width)

    def _ready(self, gen, frame, images):
        if gen != self.renderer.generation:
            return False
        self.images = {}
        for i, img in enumerate(images):
            if img is not None:
                pixels, w, h = img
                data = bytearray(pixels.tobytes())
                self.images[i] = (cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, w, h, w * 4), data)
        self.area.queue_draw()
        return False

    def _audio_changed(self, drop, _p):
        if self._busy or self.target is None:
            return
        i = drop.get_selected()
        if 0 <= i < len(self._audio_modes):
            self.app.set_multicam_audio(self.target[2], self._audio_modes[i])

    # -- layout ------------------------------------------------------------
    def _tiles(self):
        """[(angle, x, y, w, h)] filling the area, keeping the frame shape."""
        if self.target is None:
            return []
        inner = self.target[2]
        n = len(inner.video_tracks)
        if n == 0:
            return []
        W, H = self.area.get_width(), self.area.get_height()
        aspect = inner.width / max(1, inner.height)
        best = None
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            tw = min((W - 6) / cols - 6, ((H - 6) / rows - 6) * aspect)
            if best is None or tw > best[0]:
                best = (tw, cols, rows)
        tw, cols, rows = best
        th = tw / aspect
        ox = (W - cols * (tw + 6) + 6) / 2
        oy = (H - rows * (th + 6) + 6) / 2
        return [(i, ox + (i % cols) * (tw + 6), oy + (i // cols) * (th + 6), tw, th) for i in range(n)]

    def _tile_at(self, x, y):
        for i, tx, ty, tw, th in self._tiles():
            if tx <= x <= tx + tw and ty <= y <= ty + th:
                return i
        return None

    def _hover(self, angle):
        if angle != self.hover:
            self.hover = angle
            self.area.queue_draw()

    def _click(self, gesture, n, x, y):
        angle = self._tile_at(x, y)
        if angle is not None:
            self.app.switch_camera(angle)

    # -- drawing -----------------------------------------------------------
    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.paint()
        if self.target is None:
            layout_text = ("No multi-camera clip under the playhead.\n\nSelect the camera clips in the Project panel "
                           "and choose Clip ▸ Multi-Camera ▸ Create Multi-Camera Sequence.")
            cr.set_source_rgba(*theme.rgb(theme.TEXT_FAINT))
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(12)
            y = h / 2 - 30
            for line in self._wrap(cr, layout_text, w - 40):
                ext = cr.text_extents(line)
                cr.move_to((w - ext.width) / 2, y)
                cr.show_text(line)
                y += 17
            return
        track, clip, inner = self.target
        names = multicam.angle_names(inner)
        live = clip.angle or 0
        recording = self.app.program.playing and self.app.camera_cuts
        if recording:
            live = self.app.camera_cuts[-1][1]
        for i, x, y, tw, th in self._tiles():
            cr.set_source_rgba(*theme.rgb(theme.BG2))
            cr.rectangle(x, y, tw, th)
            cr.fill()
            img = self.images.get(i)
            if img is not None:
                surf = img[0]
                cr.save()
                cr.rectangle(x, y, tw, th)
                cr.clip()
                cr.translate(x, y)
                cr.scale(tw / surf.get_width(), th / surf.get_height())
                cr.set_source_surface(surf, 0, 0)
                cr.paint()
                cr.restore()
            title = f"{i + 1}  {names[i]}" if i < 9 else names[i]
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(11)
            ext = cr.text_extents(title)
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.rectangle(x + 4, y + 4, min(tw - 8, ext.width + 10), 17)
            cr.fill()
            cr.save()
            cr.rectangle(x + 4, y + 4, tw - 8, 17)
            cr.clip()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(x + 9, y + 16)
            cr.show_text(title)
            cr.restore()
            if i == live or i == self.hover:
                colour = theme.DANGER if (i == live and recording) else theme.ACCENT
                cr.set_source_rgba(*theme.rgb(colour, 1.0 if i == live else 0.5))
                cr.set_line_width(3 if i == live else 2)
                cr.rectangle(x + 1.5, y + 1.5, tw - 3, th - 3)
                cr.stroke()

    @staticmethod
    def _wrap(cr, text, width):
        out = []
        for para in text.split("\n"):
            line = ""
            for word in para.split():
                trial = f"{line} {word}".strip()
                if line and cr.text_extents(trial).width > width:
                    out.append(line)
                    line = word
                else:
                    line = trial
            out.append(line)
        return out
