"""Pop-out preview: the Program (or Source) picture in a window of its own,
to put full screen on another display — a client monitor, a projector or a
TV. It shows the frames the monitor panel already receives, so playback costs
nothing extra; the picture's resolution follows that monitor's setting."""

from gi.repository import Gdk, GLib, Gtk

from .. import timecode as tc
from .monitor import VideoView
from .widgets import icon_button, label, set_button_icon

FEEDS = [("program", "Program"), ("source", "Source")]
HIDE_CURSOR_MS = 1500


def monitor_name(i, monitor):
    geo = monitor.get_geometry()
    scale = monitor.get_scale_factor()
    model = monitor.get_model() or monitor.get_connector() or "Display"
    conn = monitor.get_connector()
    extra = f" ({conn})" if conn and conn != model else ""
    return f"{i + 1}: {model}{extra}  {geo.width * scale}×{geo.height * scale}"


class PopoutMonitor(Gtk.Window):
    def __init__(self, app, feed="program"):
        super().__init__(application=app, title="Tideline — Program", default_width=960, default_height=580)
        self.app = app
        self.feed = feed
        self._hide_id = 0
        self._monitors = []

        self.view = VideoView("No picture yet — play or scrub the timeline")
        self.view.overlay = self._overlay
        self.view.safe_margins = False

        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=5, margin_bottom=5)
        bar.add_css_class("popout-bar")
        bar.append(label("Show", css="dim"))
        self.feed_menu = Gtk.DropDown.new_from_strings([f[1] for f in FEEDS])
        self.feed_menu.set_focusable(False)
        self.feed_menu.set_selected(next((i for i, f in enumerate(FEEDS) if f[0] == feed), 0))
        self.feed_menu.connect("notify::selected", lambda d, _p: self.set_feed(FEEDS[d.get_selected()][0]))
        bar.append(self.feed_menu)
        bar.append(label("on", css="dim", margin_start=6))
        self.display_menu = Gtk.DropDown.new_from_strings([])
        self.display_menu.set_focusable(False)
        self.display_menu.set_tooltip_text("The display to fill when going full screen")
        bar.append(self.display_menu)
        go = Gtk.Button(label="Full Screen", focusable=False)
        go.add_css_class("suggested-action")
        go.set_tooltip_text("Fill the chosen display (F · double-click the picture). Esc returns to a window.")
        go.connect("clicked", lambda b: self.go_fullscreen())
        bar.append(go)
        bar.append(Gtk.Box(hexpand=True))
        self.tc_button = icon_button("marker", "Show timecode over the picture", self._toggle_tc,
                                     toggle=True, size=15)
        self.tc_button.set_active(bool(app.prefs.get("popout_timecode", False)))
        bar.append(self.tc_button)
        self.safe_button = icon_button("safe", "Safe margins", self._toggle_safe, toggle=True, size=15)
        bar.append(self.safe_button)
        self.play_button = icon_button("play", "Play / Stop (Space)", lambda b: self._command("play_toggle"),
                                       size=15)
        bar.append(self.play_button)
        self.bar = bar

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("popout")
        root.append(bar)
        root.append(self.view)
        self.set_child(root)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.view.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._moved)
        self.add_controller(motion)

        self.connect("notify::fullscreened", self._fullscreen_changed)
        self.connect("close-request", self._closed)
        display = Gdk.Display.get_default()
        self._model = display.get_monitors()
        self._items_id = self._model.connect("items-changed", lambda *_: self._list_monitors())
        self._list_monitors()

        self._handlers = [("frame", self._frame), ("playstate", self._playstate),
                          ("playhead", self.view.queue_draw), ("source_pos", self.view.queue_draw),
                          ("sequence", self._retitle), ("source", self._retitle)]
        for ev, fn in self._handlers:
            app.on(ev, fn)
        self.set_feed(feed)

    # -- feed --------------------------------------------------------------
    def set_feed(self, feed):
        self.feed = feed
        panel = None
        if self.app.window is not None:
            panel = self.app.window.program_monitor if feed == "program" else self.app.window.source_monitor
        self.view.texture = panel.view.texture if panel is not None else None
        self.view.queue_draw()
        self._retitle()
        self._playstate()

    def _player(self):
        return self.app.program if self.feed == "program" else self.app.source

    def _frame(self, kind, pixels, w, h):
        if kind == self.feed:
            self.view.set_frame(pixels, w, h)

    def _playstate(self, *_):
        set_button_icon(self.play_button, "pause" if self._player().playing else "play")

    def _retitle(self, *_):
        if self.feed == "program":
            seq = self.app.seq
            name = seq.name if seq is not None else "Program"
        else:
            m = self.app.source_media
            name = m.name if m is not None else "Source"
        self.set_title(f"Tideline — {name}")

    def _position(self):
        if self.feed == "program":
            seq = self.app.seq
            return (seq.playhead, seq.fps_num, seq.fps_den) if seq is not None else None
        player = self.app.source
        if self.app.source_media is None:
            return None
        seq = self.app.seq
        num, den = (seq.fps_num, seq.fps_den) if seq is not None else (int(round(player.fps * 1000)), 1000)
        return player.position, num, den

    def _overlay(self, cr, x, y, w, h):
        if not self.tc_button.get_active():
            return
        pos = self._position()
        if pos is None:
            return
        text = tc.frames_to_tc(*pos)
        size = max(14.0, h / 22.0)
        cr.select_font_face("Monospace", 0, 1)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        bx, by = x + w / 2 - ext.x_advance / 2 - 10, y + h - size * 2.2
        cr.set_source_rgba(0, 0, 0, 0.6)
        cr.rectangle(bx, by, ext.x_advance + 20, size * 1.5)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.move_to(bx + 10, by + size * 1.12)
        cr.show_text(text)

    def _toggle_tc(self, btn):
        self.app.prefs["popout_timecode"] = btn.get_active()
        self.app.prefs.save()
        self.view.queue_draw()

    def _toggle_safe(self, btn):
        self.view.safe_margins = btn.get_active()
        self.view.queue_draw()

    # -- displays ----------------------------------------------------------
    def _list_monitors(self):
        self._monitors = [self._model.get_item(i) for i in range(self._model.get_n_items())]
        names = [monitor_name(i, m) for i, m in enumerate(self._monitors)]
        self.display_menu.set_model(Gtk.StringList.new(names))
        self.display_menu.set_selected(self._preferred_monitor())

    def _preferred_monitor(self):
        """The display used last time, else one the editor is not on."""
        want = self.app.prefs.get("popout_monitor", "")
        for i, m in enumerate(self._monitors):
            if want and m.get_connector() == want:
                return i
        main = None
        if self.app.window is not None and self.app.window.get_surface() is not None:
            main = Gdk.Display.get_default().get_monitor_at_surface(self.app.window.get_surface())
        for i, m in enumerate(self._monitors):
            if m != main:
                return i
        return 0

    def chosen_monitor(self):
        i = self.display_menu.get_selected()
        return self._monitors[i] if 0 <= i < len(self._monitors) else None

    def go_fullscreen(self):
        monitor = self.chosen_monitor()
        if monitor is None:
            return self.fullscreen()
        self.app.prefs["popout_monitor"] = monitor.get_connector() or ""
        self.app.prefs.save()
        self.fullscreen_on_monitor(monitor)

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.go_fullscreen()

    def _fullscreen_changed(self, *_):
        full = self.is_fullscreen()
        self.bar.set_visible(not full)
        self.view.background = "#000000" if full else None
        self.view.queue_draw()
        if full:
            self.add_css_class("fullscreen-output")
            self._schedule_hide()
        else:
            self.remove_css_class("fullscreen-output")
            self._show_cursor()

    # -- input -------------------------------------------------------------
    def _command(self, name):
        self.app.focus = self.feed
        self.app.do(name)

    def _key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self.is_fullscreen():
                self.unfullscreen()
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11) and not state & Gdk.ModifierType.CONTROL_MASK:
            self.toggle_fullscreen()
            return True
        name = self.app.key_command(keyval, state)
        if name is None:
            return False
        self._command(name)
        return True

    def _click(self, gesture, n, x, y):
        if n == 2:
            self.toggle_fullscreen()

    def _moved(self, *_):
        if self.is_fullscreen():
            self._show_cursor()
            self._schedule_hide()

    def _schedule_hide(self):
        if self._hide_id:
            GLib.source_remove(self._hide_id)
        self._hide_id = GLib.timeout_add(HIDE_CURSOR_MS, self._hide_cursor)

    def _hide_cursor(self):
        self._hide_id = 0
        if self.is_fullscreen():
            self.set_cursor(Gdk.Cursor.new_from_name("none", None))
        return False

    def _show_cursor(self):
        self.set_cursor(None)

    def _closed(self, *_):
        for ev, fn in self._handlers:
            self.app.off(ev, fn)
        self._model.disconnect(self._items_id)
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0
        if getattr(self.app, "popout", None) is self:
            self.app.popout = None
        return False

