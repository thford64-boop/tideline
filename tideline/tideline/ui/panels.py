"""Smaller panels: tool strip, audio meters, history, info, effects browser."""

import math

from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo

from .. import presets
from .. import timecode as tc
from ..commands import TOOLS
from ..engine.effects import TRANSITIONS, categories, effects_in, transition_categories
from ..model import aspect_name
from . import theme
from .icons import Icon
from .widgets import icon_button, label


class ToolStrip(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.app = app
        self.add_css_class("tool-strip")
        self.buttons = {}
        for name, title, key in TOOLS:
            btn = icon_button(name, "", None, toggle=True, size=18, css="tool-button")
            btn.connect("clicked", lambda b, n=name: app.set_tool(n))
            self.buttons[name] = btn
            self.append(btn)
        app.on("tool", self.refresh)
        app.on("keymap", self._tips)
        self._tips()
        self.refresh()

    def _tips(self):
        for name, title, key in TOOLS:
            self.buttons[name].set_tooltip_text(self.app.tip(title, "tool_" + name))

    def refresh(self):
        for name, btn in self.buttons.items():
            if btn.get_active() != (name == self.app.tool):
                btn.set_active(name == self.app.tool)


class AudioMeters(Gtk.DrawingArea):
    """Stereo level meters with peak hold, fed by the playing monitor."""

    FLOOR = -60.0

    def __init__(self, app):
        super().__init__(vexpand=True)
        self.set_content_width(48)
        self.levels = [self.FLOOR, self.FLOOR]
        self.peaks = [self.FLOOR, self.FLOOR]
        self.clipped = [False, False]
        self._timer = 0
        self.set_draw_func(self._draw)
        app.on("levels", self.feed)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self._reset())
        self.add_controller(click)
        self.set_tooltip_text("Output level (dBFS). Click to reset the clip indicators.")

    @staticmethod
    def _db(v):
        # MLT reports either dB (<= 0) or a 0..1 amplitude depending on build.
        if v is None or v != v:
            return AudioMeters.FLOOR
        if v <= 0.0:
            return max(AudioMeters.FLOOR, v) if v < 0.0 else AudioMeters.FLOOR
        return max(AudioMeters.FLOOR, 20.0 * math.log10(min(4.0, v)))

    def feed(self, left, right, playing):
        for i, v in enumerate((left, right)):
            db = self._db(v) if playing else self.FLOOR
            self.levels[i] = max(db, self.levels[i] - 3.0)
            if db >= self.peaks[i]:
                self.peaks[i] = db
            if db >= -0.1:
                self.clipped[i] = True
        if not self._timer:
            self._timer = GLib.timeout_add(60, self._decay)
        self.queue_draw()

    def _decay(self):
        active = False
        for i in range(2):
            self.levels[i] = max(self.FLOOR, self.levels[i] - 2.5)
            self.peaks[i] = max(self.FLOOR, self.peaks[i] - 0.7)
            active = active or self.levels[i] > self.FLOOR or self.peaks[i] > self.FLOOR
        self.queue_draw()
        if not active:
            self._timer = 0
            return False
        return True

    def _reset(self):
        self.clipped = [False, False]
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(*theme.rgb(theme.BG1))
        cr.paint()
        top, bottom = 16, h - 8
        span = bottom - top
        y_of = lambda db: bottom - (max(self.FLOOR, min(0.0, db)) - self.FLOOR) / -self.FLOOR * span
        bar_w = 9
        x0 = 6
        for i in range(2):
            x = x0 + i * (bar_w + 3)
            cr.set_source_rgba(*theme.rgb(theme.DANGER if self.clipped[i] else theme.BG3))
            cr.rectangle(x, 6, bar_w, 6)
            cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.BG0))
            cr.rectangle(x, top, bar_w, span)
            cr.fill()
            lvl = self.levels[i]
            for lo, hi, color in ((self.FLOOR, -18.0, "#2aa876"), (-18.0, -6.0, "#d6c13c"), (-6.0, 0.0, "#e0564b")):
                if lvl > lo:
                    ya, yb = y_of(min(lvl, hi)), y_of(lo)
                    cr.set_source_rgba(*theme.rgb(color))
                    cr.rectangle(x, ya, bar_w, yb - ya)
                    cr.fill()
            if self.peaks[i] > self.FLOOR:
                cr.set_source_rgba(*theme.rgb(theme.TEXT))
                cr.rectangle(x, y_of(self.peaks[i]) - 1, bar_w, 1.5)
                cr.fill()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 8px"))
        cr.set_source_rgba(*theme.rgb(theme.TEXT_FAINT))
        for db in (0, -6, -12, -18, -24, -36, -48):
            y = y_of(db)
            cr.rectangle(x0 + 2 * bar_w + 5, y, 3, 1)
            cr.fill()
            layout.set_text(str(db), -1)
            cr.move_to(x0 + 2 * bar_w + 10, y - 5)
            PangoCairo.show_layout(cr, layout)


class HistoryPanel(Gtk.ScrolledWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.connect("row-activated", lambda lb, row: app.history_jump(row.get_index()))
        self.list.set_activate_on_single_click(True)
        self.set_child(self.list)
        app.on("history", self.refresh)
        app.on("project", self.refresh)
        self.refresh()

    def refresh(self, *_):
        self.list.remove_all()
        hist = self.app.history
        for i, text in enumerate(hist.labels()):
            row = Gtk.Box(spacing=6)
            row.append(label("●" if i == hist.index else " ", css="dim"))
            lab = label(text, css="" if i <= hist.index else "faint")
            row.append(lab)
            self.list.append(row)
        adj = self.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)


class InfoPanel(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, margin_start=10, margin_end=10, margin_top=8)
        self.app = app
        self.text = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=False)
        self.append(self.text)
        for ev in ("selection", "sequence", "playhead", "project"):
            app.on(ev, self.refresh)
        self.connect("map", self.refresh)       # skipped while hidden, so catch up when shown
        self.refresh()

    def refresh(self, *_):
        app = self.app
        seq = app.seq
        if not self.get_mapped() and self.text.get_text():
            return
        if seq is None:
            return self.text.set_markup("<span alpha='60%'>No sequence open.</span>")
        f = lambda n: tc.frames_to_tc(n, seq.fps_num, seq.fps_den)
        esc = GLib.markup_escape_text
        lines = [f"<b>{esc(seq.name)}</b>", f"{seq.width}×{seq.height} ({aspect_name(seq.width, seq.height)}) · {seq.fps:.3g} fps · "
                 f"{seq.sample_rate} Hz",
                 f"Duration {f(seq.end())} · Playhead {f(seq.playhead)}",
                 f"{len(seq.video_tracks)} video / {len(seq.audio_tracks)} audio tracks", ""]
        pairs = app.selected_clips()
        if len(pairs) == 1:
            track, c = pairs[0]
            m = app.project.media.get(c.media_id)
            lines += [f"<b>{esc(c.name or (m.name if m else '?'))}</b>  ({seq.track_label(track)})",
                      f"Start {f(c.start)}   End {f(c.end)}", f"Duration {f(c.duration)}   In {f(c.in_point)}",
                      f"Speed {c.speed * 100:.4g}%{' reversed' if c.reverse else ''}"]
            if m is not None and m.path:
                lines.append(f"<span alpha='65%'>{esc(m.path)}</span>")
        elif pairs:
            lines.append(f"{len(pairs)} clips selected")
        self.text.set_markup("\n".join(lines))


class EffectsPanel(Gtk.Box):
    """Browser for presets, effects and transitions. Double-click applies to
    the selection; rows can also be dragged onto clips in the timeline."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.expanded = {"Presets", "Presets/My Presets", "Presets/Built-in", "Video Effects", "Video Transitions"}
        self.search = Gtk.SearchEntry(placeholder_text="Search effects", margin_start=6, margin_end=6,
                                      margin_top=5, margin_bottom=5)
        self.search.connect("search-changed", lambda *_: self.refresh())
        self.search.set_hexpand(True)
        top = Gtk.Box()
        top.append(self.search)
        top.append(icon_button("import", "Import preset files (.tlpreset)", lambda b: app.do("import_preset"),
                               size=15))
        top.append(icon_button("new_item", "Save the selected clip's effects and keyframes as a preset",
                               lambda b: app.do("save_preset"), size=15))
        self.append(top)
        app.on("presets", self.refresh)
        self.list = Gtk.ListBox()
        self.list.set_activate_on_single_click(False)
        self.list.connect("row-activated", self._activated)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list)
        self.append(scroller)
        self.refresh()

    def _tree(self):
        tr = lambda kind, cat: [("transition", t.key, t.label) for t in TRANSITIONS.values()
                                if t.kind == kind and t.category == cat]
        fx = lambda cat: [("effect", d.key, d.label) for d in effects_in(cat) if d.available()]
        library = presets.library()
        saved = lambda builtin: [("preset", path, p["name"]) for path, p, b in library if b == builtin]
        return [
            ("Presets", [("My Presets", saved(False)), ("Built-in", saved(True))]),
            ("Video Effects", [(c, fx(c)) for c in categories("video")]),
            ("Audio Effects", [(c, fx(c)) for c in categories("audio")]),
            ("Video Transitions", [(c, tr("video", c)) for c in transition_categories("video")]),
            ("Audio Transitions", [(c, tr("audio", c)) for c in transition_categories("audio")]),
        ]

    def refresh(self):
        self.list.remove_all()
        query = self.search.get_text().strip().lower()
        for group, cats in self._tree():
            cats = [(c, [it for it in items if not query or query in it[2].lower() or query in c.lower()])
                    for c, items in cats]
            cats = [(c, items) for c, items in cats if items]
            if not cats:
                continue
            open_group = bool(query) or group in self.expanded
            self._header(group, 0, open_group, group)
            if not open_group:
                continue
            for cat, items in cats:
                key = f"{group}/{cat}"
                open_cat = bool(query) or key in self.expanded
                self._header(cat, 1, open_cat, key)
                if open_cat:
                    for kind, k, text in items:
                        self._item(kind, k, text)

    def _header(self, text, level, is_open, key):
        row = Gtk.Box(spacing=5, margin_start=4 + level * 14)
        row.append(Icon("down" if is_open else "right", 12))
        row.append(Icon("folder", 14))
        row.append(label(text, css="" if level == 0 else "dim"))
        row.toggle_key = key
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self._toggle(key))
        row.add_controller(click)
        self.list.append(row)

    def _item(self, kind, key, text):
        row = Gtk.Box(spacing=6, margin_start=36)
        row.append(Icon({"effect": "fx", "preset": "curve"}.get(kind, "roll"), 14))
        row.append(label(text, ellipsize=True, hexpand=True))
        if kind == "preset" and not presets.is_builtin(key):
            row.append(icon_button("trash", "Delete this preset", lambda b: self._delete(key, text), size=12,
                                   css="kf-button"))
        row.payload = (kind, key)
        src = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        src.connect("prepare", self._drag, kind, key)
        src.connect("drag-end", lambda *_: setattr(self.app, "drag_payload", None))
        row.add_controller(src)
        self.list.append(row)

    def _drag(self, source, x, y, kind, key):
        self.app.drag_payload = (kind, key)
        return Gdk.ContentProvider.new_for_value(GObject.Value(str, f"tideline:{kind}:{key}"))

    def _toggle(self, key):
        self.expanded.symmetric_difference_update({key})
        self.refresh()

    def _delete(self, path, name):
        from .widgets import confirm
        confirm(self.app.window, f"Delete the preset “{name}”?", "The preset file is removed from your library.",
                "Delete", lambda i: i == 1 and self.app.delete_preset(path))

    def _activated(self, listbox, row):
        child = row.get_child()
        payload = getattr(child, "payload", None)
        if payload is None:
            return
        kind, key = payload
        if kind == "effect":
            self.app.apply_effect(key)
        elif kind == "preset":
            self.app.apply_preset(key)
        else:
            self.app.apply_transition(key)


class AudioMixer(Gtk.ScrolledWindow):
    """Audio track mixer: one strip per audio track with fader, pan, mute, solo."""

    def __init__(self, app):
        super().__init__(vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app = app
        self.box = Gtk.Box(spacing=2, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        self.set_child(self.box)
        self.strips = {}
        self.signature = None
        self._commit_id = 0
        for ev in ("sequence", "project"):
            app.on(ev, self.sync)
        self.sync()

    def sync(self, *_):
        seq = self.app.seq
        sig = tuple((t.id, id(t), t.name) for t in seq.audio_tracks) if seq else None
        if sig != self.signature:
            self.signature = sig
            self._rebuild(seq)
        self.refresh()

    def _rebuild(self, seq):
        child = self.box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.box.remove(child)
            child = nxt
        self.strips = {}
        if seq is None:
            self.box.append(label("No sequence open", css="hint", hexpand=True, xalign=0.5))
            return
        for track in seq.audio_tracks:
            strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            strip.set_size_request(64, -1)
            strip.append(label(seq.track_label(track), css="heading", xalign=0.5))
            strip.append(label(track.name or "Audio", css="faint", xalign=0.5, ellipsize=True))
            from .widgets import DragNumber
            pan = DragNumber(track.pan, -100, 100, 1.0, 0, "")
            pan.set_halign(Gtk.Align.CENTER)
            pan.set_tooltip_text("Balance: -100 left … 100 right")
            pan.connect("changed", lambda w, v, final, t=track: self._set(t, "pan", v, final))
            strip.append(pan)
            fader = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -60, 12, 0.5)
            fader.set_inverted(True)
            fader.set_vexpand(True)
            fader.set_draw_value(False)
            fader.set_focusable(False)
            fader.set_halign(Gtk.Align.CENTER)
            fader.add_mark(0, Gtk.PositionType.RIGHT, None)
            fader.connect("value-changed", lambda w, t=track: self._fader(t, w))
            strip.append(fader)
            gain = DragNumber(track.volume, -60, 12, 0.5, 1, "dB")
            gain.set_halign(Gtk.Align.CENTER)
            gain.connect("changed", lambda w, v, final, t=track: self._set(t, "volume", v, final))
            strip.append(gain)
            buttons = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER)
            mute = Gtk.ToggleButton(label="M", focusable=False, tooltip_text="Mute")
            solo = Gtk.ToggleButton(label="S", focusable=False, tooltip_text="Solo")
            mute.connect("toggled", lambda b, t=track: self._flag(t, "hidden", b.get_active()))
            solo.connect("toggled", lambda b, t=track: self._flag(t, "solo", b.get_active()))
            buttons.append(mute)
            buttons.append(solo)
            strip.append(buttons)
            self.box.append(strip)
            self.box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            self.strips[track.id] = (track, pan, fader, gain, mute, solo)

    def refresh(self):
        self._busy = True
        for track, pan, fader, gain, mute, solo in self.strips.values():
            pan.set_value(track.pan)
            gain.set_value(track.volume)
            if abs(fader.get_value() - track.volume) > 0.01:
                fader.set_value(track.volume)
            if mute.get_active() != track.hidden:
                mute.set_active(track.hidden)
            if solo.get_active() != track.solo:
                solo.set_active(track.solo)
        self._busy = False

    def _set(self, track, attr, value, final):
        if getattr(self, "_busy", False):
            return
        setattr(track, attr, float(value))
        self.app.track_audio_changed(final, "Track Volume" if attr == "volume" else "Track Pan")
        self.refresh()

    def _fader(self, track, scale):
        if getattr(self, "_busy", False) or abs(scale.get_value() - track.volume) < 0.01:
            return
        track.volume = round(scale.get_value(), 1)
        self.app.track_audio_changed(False)
        self.strips[track.id][3].set_value(track.volume)
        if self._commit_id:
            GLib.source_remove(self._commit_id)
        self._commit_id = GLib.timeout_add(500, self._commit)

    def _commit(self):
        self._commit_id = 0
        self.app.commit("Track Volume", rebuild=False)
        return False

    def _flag(self, track, attr, value):
        if getattr(self, "_busy", False) or getattr(track, attr) == value:
            return
        setattr(track, attr, value)
        self.app.track_audio_changed(True, "Track Mute" if attr == "hidden" else "Track Solo")
