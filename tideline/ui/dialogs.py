"""Dialogs: project and sequence settings, speed/duration, export, markers."""

import os

from gi.repository import GLib, Gtk

from .. import timecode as tc
from ..engine import export as exportmod
from ..engine import hwenc
from ..engine.export import FORMATS, QUALITIES, ExportJob, ExportSettings
from ..model import ASPECTS, RESOLUTIONS, aspect_index, size_for_aspect
from . import theme
from .widgets import alert, file_filter, label, save_file

SEQUENCE_PRESETS = [
    ("HD 1080p 30", 1920, 1080, 30, 1), ("HD 1080p 29.97", 1920, 1080, 30000, 1001),
    ("HD 1080p 25", 1920, 1080, 25, 1), ("HD 1080p 24", 1920, 1080, 24, 1),
    ("HD 1080p 23.976", 1920, 1080, 24000, 1001), ("HD 1080p 60", 1920, 1080, 60, 1),
    ("HD 1080p 59.94", 1920, 1080, 60000, 1001), ("HD 1080p 50", 1920, 1080, 50, 1),
    ("HD 720p 30", 1280, 720, 30, 1), ("HD 720p 60", 1280, 720, 60, 1),
    ("UHD 4K 30", 3840, 2160, 30, 1), ("UHD 4K 24", 3840, 2160, 24, 1), ("UHD 4K 60", 3840, 2160, 60, 1),
    ("DCI 4K 24", 4096, 2160, 24, 1), ("Vertical 1080×1920 30", 1080, 1920, 30, 1),
    ("Vertical 1080×1920 60", 1080, 1920, 60, 1), ("Square 1080 30", 1080, 1080, 30, 1),
]
FRAME_RATES = [("23.976", 24000, 1001), ("24", 24, 1), ("25", 25, 1), ("29.97", 30000, 1001), ("30", 30, 1),
               ("48", 48, 1), ("50", 50, 1), ("59.94", 60000, 1001), ("60", 60, 1), ("120", 120, 1)]
SAMPLE_RATES = [44100, 48000, 96000]


class _Dialog(Gtk.Window):
    def __init__(self, app, title, ok_label="OK"):
        super().__init__(title=title, application=app, transient_for=app.window, modal=True, resizable=False)
        self.app = app
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.add_css_class("dialog-body")
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        outer.append(self.grid)
        self.extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.append(self.extra)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        self.ok_btn = Gtk.Button(label=ok_label)
        self.ok_btn.add_css_class("suggested-action")
        self.ok_btn.connect("clicked", lambda *_: self.accept())
        buttons.append(self.cancel_btn)
        buttons.append(self.ok_btn)
        outer.append(buttons)
        self.set_child(outer)
        self.set_default_widget(self.ok_btn)
        self._row = 0

    def add_row(self, text, widget):
        lab = label(text, css="dim", xalign=1.0)
        self.grid.attach(lab, 0, self._row, 1, 1)
        self.grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return widget

    def accept(self):
        self.close()


def _spin(value, lo, hi, step=1, digits=0):
    spin = Gtk.SpinButton.new_with_range(lo, hi, step)
    spin.set_digits(digits)
    spin.set_value(value)
    spin.set_width_chars(8)
    return spin


class FrameSizeRows:
    """Aspect ratio, resolution and frame size rows of a settings dialog.
    Picking a shape or a resolution sets the size; typing a size shows which
    shape it is (or "Custom")."""

    def __init__(self, dialog, width, height):
        self._busy = False
        self.aspect = dialog.add_row("Aspect ratio", Gtk.DropDown.new_from_strings(
            ["Custom"] + [a[0] for a in ASPECTS]))
        self.resolution = dialog.add_row("Resolution", Gtk.DropDown.new_from_strings(
            ["Custom"] + [f"{r}p" + ("  (4K)" if r == 2160 else "") for r in RESOLUTIONS]))
        size = Gtk.Box(spacing=6)
        self.width = _spin(width, 16, 16384, 2)
        self.height = _spin(height, 16, 16384, 2)
        size.append(self.width)
        size.append(label("×"))
        size.append(self.height)
        dialog.add_row("Frame size", size)
        self._show_shape()
        self.aspect.connect("notify::selected", self._picked)
        self.resolution.connect("notify::selected", self._picked)
        self.width.connect("value-changed", self._typed)
        self.height.connect("value-changed", self._typed)

    def get(self):
        return int(self.width.get_value()) // 2 * 2, int(self.height.get_value()) // 2 * 2

    def set(self, width, height):
        self._busy = True
        self.width.set_value(width)
        self.height.set_value(height)
        self._busy = False
        self._show_shape()

    def _show_shape(self):
        w, h = self.get()
        self._busy = True
        self.aspect.set_selected(aspect_index(w, h) + 1)
        self.resolution.set_selected(RESOLUTIONS.index(min(w, h)) + 1 if min(w, h) in RESOLUTIONS else 0)
        self._busy = False

    def _picked(self, *_):
        if self._busy:
            return
        w, h = self.get()
        # A custom shape keeps its own proportions when only the resolution is picked.
        aw, ah = ASPECTS[self.aspect.get_selected() - 1][1:] if self.aspect.get_selected() else (w, h)
        i = self.resolution.get_selected()
        self.set(*size_for_aspect(aw, ah, RESOLUTIONS[i - 1] if i else min(w, h)))

    def _typed(self, *_):
        if not self._busy:
            self._show_shape()


class ProjectSettingsDialog(_Dialog):
    """Frame shape and rates of the project: applied to the open sequence (or
    all of them) and used for every sequence created afterwards."""

    SCOPES = [("active", "This sequence and new sequences"), ("all", "Every sequence in the project"),
              ("new", "New sequences only")]

    def __init__(self, app):
        super().__init__(app, "Project Settings")
        seq, cur = app.seq, app.project.settings
        w, h = (seq.width, seq.height) if seq else (cur["width"], cur["height"])
        self.frame = FrameSizeRows(self, w, h)
        rate = (seq.fps_num, seq.fps_den) if seq else (cur["fps_num"], cur["fps_den"])
        self.fps = self.add_row("Frame rate", Gtk.DropDown.new_from_strings([f[0] + " fps" for f in FRAME_RATES]))
        self.fps.set_selected(next((i for i, f in enumerate(FRAME_RATES) if (f[1], f[2]) == rate), 4))
        sample = seq.sample_rate if seq else cur["sample_rate"]
        self.rate = self.add_row("Audio sample rate", Gtk.DropDown.new_from_strings(
            [f"{r} Hz" for r in SAMPLE_RATES]))
        self.rate.set_selected(SAMPLE_RATES.index(sample) if sample in SAMPLE_RATES else 1)
        self.scope = self.add_row("Apply to", Gtk.DropDown.new_from_strings([s[1] for s in self.SCOPES]))
        self.scope.set_sensitive(bool(app.project.sequences))
        self.extra.append(label("Clips keep their place in the frame when its shape changes: positions and\n"
                                "their keyframes move in proportion. Footage is fitted inside the new frame.",
                                css="faint"))

    def accept(self):
        w, h = self.frame.get()
        _, num, den = FRAME_RATES[self.fps.get_selected()]
        self.app.update_project_settings(w, h, num, den, SAMPLE_RATES[self.rate.get_selected()],
                                         self.SCOPES[self.scope.get_selected()][0])
        self.close()


class SequenceDialog(_Dialog):
    """New sequence, or settings of an existing one."""

    def __init__(self, app, seq=None):
        super().__init__(app, "Sequence Settings" if seq else "New Sequence", "OK" if seq else "Create")
        self.seq = seq
        self.name = self.add_row("Name", Gtk.Entry(text=seq.name if seq else app.project.unique_name("Sequence 01"),
                                                    width_chars=30, activates_default=True))
        self.preset = self.add_row("Preset", Gtk.DropDown.new_from_strings(
            ["Custom"] + [p[0] for p in SEQUENCE_PRESETS]))
        cur = app.project.settings
        self.frame = FrameSizeRows(self, seq.width if seq else cur["width"], seq.height if seq else cur["height"])
        self.fps = self.add_row("Frame rate", Gtk.DropDown.new_from_strings([f[0] + " fps" for f in FRAME_RATES]))
        self.rate = self.add_row("Audio sample rate", Gtk.DropDown.new_from_strings(
            [f"{r} Hz" for r in SAMPLE_RATES]))
        rate = (seq.fps_num, seq.fps_den) if seq else (cur["fps_num"], cur["fps_den"])
        sample = seq.sample_rate if seq else cur["sample_rate"]
        self.fps.set_selected(next((i for i, f in enumerate(FRAME_RATES) if (f[1], f[2]) == rate), 4))
        self.rate.set_selected(SAMPLE_RATES.index(sample) if sample in SAMPLE_RATES else 1)
        if seq is None:
            self.vtracks = self.add_row("Video tracks", _spin(3, 1, 32))
            self.atracks = self.add_row("Audio tracks", _spin(3, 1, 32))
        elif seq.end():
            self.extra.append(label("Changing the frame rate keeps every clip at the same time, so cut points\n"
                                    "can move by up to half a frame.", css="faint"))
        self.preset.connect("notify::selected", self._preset)

    def _preset(self, drop, _p):
        i = drop.get_selected()
        if i == 0:
            return
        _, w, h, num, den = SEQUENCE_PRESETS[i - 1]
        self.frame.set(w, h)
        self.fps.set_selected(next(k for k, f in enumerate(FRAME_RATES) if (f[1], f[2]) == (num, den)))

    def accept(self):
        name = self.name.get_text().strip() or "Sequence"
        w, h = self.frame.get()
        _, num, den = FRAME_RATES[self.fps.get_selected()]
        rate = SAMPLE_RATES[self.rate.get_selected()]
        if self.seq is None:
            self.app.create_sequence(name, w, h, num, den, rate, int(self.vtracks.get_value()),
                                     int(self.atracks.get_value()))
        else:
            self.app.update_sequence(self.seq, name, w, h, num, den, rate)
        self.close()


class SpeedDialog(_Dialog):
    def __init__(self, app, clip):
        super().__init__(app, "Clip Speed / Duration")
        self.clip = clip
        seq = app.seq
        self.source_frames = clip.duration * clip.speed
        self.speed = self.add_row("Speed (%)", _spin(clip.speed * 100, 1, 10000, 1, 2))
        self.duration = self.add_row("Duration", Gtk.Entry(
            text=tc.frames_to_tc(clip.duration, seq.fps_num, seq.fps_den), width_chars=12, activates_default=True))
        self.reverse = Gtk.CheckButton(label="Reverse speed", active=clip.reverse)
        self.ripple = Gtk.CheckButton(label="Ripple edit, shifting trailing clips", active=False)
        self.extra.append(self.reverse)
        self.extra.append(self.ripple)
        self.extra.append(label("Audio keeps its pitch between 50% and 200%.", css="faint"))
        self._busy = False
        self.speed.connect("value-changed", self._speed_changed)
        self.duration.connect("changed", self._duration_changed)

    def _speed_changed(self, spin):
        if self._busy:
            return
        self._busy = True
        seq = self.app.seq
        frames = max(1, int(round(self.source_frames / (spin.get_value() / 100.0))))
        self.duration.set_text(tc.frames_to_tc(frames, seq.fps_num, seq.fps_den))
        self._busy = False

    def _duration_changed(self, entry):
        if self._busy:
            return
        seq = self.app.seq
        parsed = tc.tc_to_frames(entry.get_text(), seq.fps_num, seq.fps_den)
        if parsed and parsed[0] > 0:
            self._busy = True
            self.speed.set_value(max(1.0, min(10000.0, self.source_frames / parsed[0] * 100.0)))
            self._busy = False

    def accept(self):
        self.app.apply_speed(self.speed.get_value() / 100.0, self.reverse.get_active(), self.ripple.get_active())
        self.close()


class MarkerDialog(_Dialog):
    def __init__(self, app, marker):
        super().__init__(app, "Marker")
        self.marker = marker
        self.name = self.add_row("Name", Gtk.Entry(text=marker.name, width_chars=32, activates_default=True))
        self.comment = self.add_row("Comment", Gtk.Entry(text=marker.comment, width_chars=32, activates_default=True))
        colors = Gtk.Box(spacing=4)
        self.color = marker.color
        first = None
        for i, hexc in enumerate(theme.MARKER_COLORS):
            btn = Gtk.ToggleButton()
            sw = Gtk.DrawingArea(content_width=16, content_height=16)
            sw.set_draw_func(lambda a, cr, w, h, c=hexc: (cr.set_source_rgba(*theme.rgb(c)), cr.paint()))
            btn.set_child(sw)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(i == marker.color % len(theme.MARKER_COLORS))
            btn.connect("toggled", lambda b, i=i: b.get_active() and setattr(self, "color", i))
            colors.append(btn)
        self.add_row("Colour", colors)
        delete = Gtk.Button(label="Delete Marker", halign=Gtk.Align.START)
        delete.connect("clicked", self._delete)
        self.extra.append(delete)

    def _delete(self, _b):
        seq = self.app.seq
        if seq and self.marker in seq.markers:
            seq.markers.remove(self.marker)
            self.app.commit("Clear Marker", rebuild=False)
        self.close()

    def accept(self):
        self.marker.name = self.name.get_text()
        self.marker.comment = self.comment.get_text()
        self.marker.color = self.color
        self.app.commit("Edit Marker", rebuild=False)
        self.close()


class ExportDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "Export Media", "Export")
        self.set_modal(False)
        seq = app.seq
        self.seq = seq
        self.job = None
        # Hardware formats join the list once the probe has found them working.
        self.formats = [f for f in FORMATS if not f.hw]
        self.format_names = Gtk.StringList.new([f.label for f in self.formats])
        self.format = self.add_row("Format", Gtk.DropDown(model=self.format_names))
        self.device_names = Gtk.StringList.new([])
        self.devices = []
        self.device = self.add_row("GPU", Gtk.DropDown(model=self.device_names))
        self.device.get_prev_sibling().set_visible(False)
        self.device.set_visible(False)
        self.quality = self.add_row("Quality", Gtk.DropDown.new_from_strings([q[1] for q in QUALITIES]))
        sizes = [("Match sequence", 0, 0), ("3840 × 2160", 3840, 2160), ("1920 × 1080", 1920, 1080),
                 ("1280 × 720", 1280, 720), ("854 × 480", 854, 480), ("Half size", seq.width // 4 * 2,
                                                                      seq.height // 4 * 2)]
        self.sizes = sizes
        self.size = self.add_row("Frame size", Gtk.DropDown.new_from_strings([s[0] for s in sizes]))
        has_range = seq.in_point is not None or seq.out_point is not None
        self.range = self.add_row("Range", Gtk.DropDown.new_from_strings(
            ["Entire sequence", "Sequence In to Out"]))
        self.range.set_selected(1 if has_range else 0)
        self.range.set_sensitive(has_range)
        self.audio = Gtk.CheckButton(label="Export audio", active=True)
        self.grid.attach(self.audio, 1, self._row, 1, 1)
        self._row += 1
        base = os.path.dirname(app.project.path) if app.project.path else os.path.expanduser("~/Videos")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
        self.path = os.path.join(base, seq.name + FORMATS[0].ext)
        row = Gtk.Box(spacing=6)
        self.path_label = label(self.path, css="dim", ellipsize=True)
        self.path_label.set_max_width_chars(44)
        self.path_label.set_hexpand(True)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._browse)
        row.append(self.path_label)
        row.append(browse)
        self.add_row("Output", row)
        self.summary = label("", css="faint")
        self.extra.append(self.summary)
        self.hw_note = label("Looking for hardware encoders…", css="faint", wrap=True, max_width_chars=60)
        self.extra.append(self.hw_note)
        if app.project.proxies:
            self.extra.append(label("Renders from the original media, not from proxies.", css="faint"))
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.extra.append(self.progress)
        self.format.connect("notify::selected", self._format_changed)
        for w in (self.size, self.range, self.quality, self.device):
            w.connect("notify::selected", lambda *_: self._summarise())
        self.connect("close-request", self._closing)
        self._format_changed()
        exportmod.detect_hardware(lambda found: GLib.idle_add(self._hardware_found))

    def _fmt(self):
        return self.formats[min(self.format.get_selected(), len(self.formats) - 1)]

    def _hardware_found(self):
        """Offer the hardware formats that work, after their software twins."""
        current = self._fmt()
        working = [f for f in exportmod.hardware_formats() if exportmod.hardware_devices(f)]
        soft = [f for f in FORMATS if not f.hw]
        at = next((i for i, f in enumerate(soft) if f.key == "vp9"), len(soft) - 1) + 1
        self.formats = soft[:at] + working + soft[at:]
        self.format_names.splice(0, self.format_names.get_n_items(), [f.label for f in self.formats])
        self.format.set_selected(self.formats.index(current) if current in self.formats else 0)
        if working:
            gpus = sorted({hwenc.device_name(d) for f in working for d in exportmod.hardware_devices(f) if d})
            apis = sorted({hwenc.APIS[f.hw] for f in working})
            self.hw_note.set_text(f"GPU encoding: {len(working)} format{'s' if len(working) > 1 else ''} with "
                                  f"{', '.join(apis)}" + (f" on {', '.join(gpus)}" if gpus else "")
                                  + ". Much faster; files are a little larger at the same quality.")
        else:
            self.hw_note.set_text("No working hardware encoder was found (VA-API, NVENC or Quick Sync); "
                                  "exports use the CPU.")
        self._format_changed()
        return False

    def _format_changed(self, *_):
        fmt = self._fmt()
        devices = [d for d in (exportmod.hardware_devices(fmt) or []) if d] if fmt.hw else []
        if devices != self.devices:
            self.devices = devices
            self.device_names.splice(0, self.device_names.get_n_items(),
                                     [f"{hwenc.device_name(d)} ({os.path.basename(d)})" for d in devices])
            self.device.set_selected(0)
        show = len(devices) > 1
        self.device.set_visible(show)
        self.device.get_prev_sibling().set_visible(show)
        self.path = os.path.splitext(self.path)[0] + fmt.ext
        self.path_label.set_text(self.path)
        self.quality.set_sensitive(bool(fmt.crf))
        self.size.set_sensitive(not fmt.audio_only)
        self.audio.set_sensitive(not fmt.audio_only and "an" not in fmt.audio)
        self._summarise()

    def _frames(self):
        seq = self.seq
        if self.range.get_selected() == 1:
            return (seq.out_point if seq.out_point is not None else seq.end()) - (seq.in_point or 0)
        return seq.end()

    def _summarise(self):
        seq, fmt = self.seq, self._fmt()
        _, w, h = self.sizes[self.size.get_selected()]
        w, h = (w, h) if w else (seq.width, seq.height)
        text = f"{tc.frames_to_tc(self._frames(), seq.fps_num, seq.fps_den)}  ·  "
        text += "audio only" if fmt.audio_only else f"{w}×{h} at {seq.fps:.3g} fps"
        if fmt.note:
            text += f"  ·  {fmt.note}"
        if fmt.hw:
            devices = self.devices
            where = hwenc.device_name(devices[self.device.get_selected()]) if devices else hwenc.APIS[fmt.hw]
            text += f"  ·  encoded on {where}"
        self.summary.set_text(text)

    def _browse(self, _b):
        fmt = self._fmt()

        def chosen(path):
            if not path.lower().endswith(fmt.ext):
                path += fmt.ext
            self.path = path
            self.path_label.set_text(path)

        save_file(self, "Export As", os.path.basename(self.path), chosen,
                  [file_filter(fmt.label, ["*" + fmt.ext])], folder=os.path.dirname(self.path))

    def accept(self):
        if self.job is not None and self.job.running:
            return
        seq, fmt = self.seq, self._fmt()
        _, w, h = self.sizes[self.size.get_selected()]
        settings = ExportSettings(format=fmt.key, path=self.path, width=w, height=h,
                                  quality=QUALITIES[self.quality.get_selected()][0],
                                  audio=self.audio.get_active())
        if fmt.hw == "vaapi" and self.devices:
            settings.device = self.devices[min(self.device.get_selected(), len(self.devices) - 1)]
        if self.range.get_selected() == 1:
            settings.range_in = seq.in_point or 0
            settings.range_out = seq.out_point if seq.out_point is not None else seq.end()
        folder = os.path.dirname(self.path)
        if not os.path.isdir(folder):
            return alert(self, "The output folder does not exist", folder)
        self.app.program.pause()
        self.job = ExportJob(self.app.project, seq, settings,
                             on_progress=lambda j: GLib.idle_add(self._progress),
                             on_done=lambda j: GLib.idle_add(self._done))
        self.app.export_job = self.job
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text("Preparing…")
        self.ok_btn.set_sensitive(False)
        self.cancel_btn.set_label("Cancel Export")
        try:
            self.job.start()
        except Exception as exc:
            self.job = None
            self._reset()
            alert(self, "Export could not start", str(exc))

    def _progress(self):
        job = self.job
        if job is None:
            return False
        eta = job.eta()
        self.progress.set_fraction(job.percent / 100)
        self.progress.set_text(f"{job.percent}%" + (f"  ·  about {tc.format_duration(eta)} left" if eta else ""))
        self.app.status(f"Exporting… {job.percent}%")
        return False

    def _reset(self):
        self.ok_btn.set_sensitive(True)
        self.cancel_btn.set_label("Close")

    def _done(self):
        job = self.job
        if job is None:
            return False
        self._reset()
        if job.error:
            self.progress.set_text("Cancelled" if job.cancelled else "Failed")
            if not job.cancelled:
                alert(self, "Export failed", job.error)
            self.app.status("Export cancelled" if job.cancelled else "Export failed", warn=True)
        else:
            self.progress.set_fraction(1.0)
            self.progress.set_text("Done")
            self.app.status(f"Exported {job.settings.path}")
        return False

    def close(self):
        if self.job is not None and self.job.running:
            self.job.cancel()
            return
        super().close()

    def _closing(self, *_):
        if self.job is not None and self.job.running:
            self.job.cancel()
        return False
