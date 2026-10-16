"""Main window: menu bar, workspace layout, keyboard dispatch, status bar."""

import os

from gi.repository import Gdk, Gio, GLib, Gtk

from .. import APP_NAME
from ..commands import BY_NAME, MENUS
from .effect_controls import EffectControls
from .monitor import MonitorPanel
from .panels import AudioMeters, AudioMixer, EffectsPanel, HistoryPanel, InfoPanel, ToolStrip
from .project_panel import ProjectPanel
from .timeline import TimelinePanel
from .widgets import PanelGroup, icon_button, label, set_button_icon

# Where each panel lives: panel key -> (group attribute, tab key)
WORKSPACES = {
    "editing": {"top": 0.50, "left": 0.42, "bottom_left": 0.26, "tabs": {"a": "source", "c": "project"}},
    "effects": {"top": 0.50, "left": 0.36, "bottom_left": 0.24, "tabs": {"a": "effect_controls", "c": "effects"}},
    "audio": {"top": 0.42, "left": 0.36, "bottom_left": 0.22, "tabs": {"a": "mixer", "c": "effects"}},
}


def build_menu(entries):
    menu = Gio.Menu()
    section = Gio.Menu()
    for entry in entries:
        if entry is None:
            menu.append_section(None, section)
            section = Gio.Menu()
        elif isinstance(entry, tuple):
            section.append_submenu(entry[0], build_menu(entry[1]))
        else:
            name, text, accel, _ = BY_NAME[entry]
            item = Gio.MenuItem.new(text, "app." + name)
            if accel:
                item.set_attribute_value("accel", GLib.Variant.new_string(accel))
            section.append_item(item)
    menu.append_section(None, section)
    return menu


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1600, default_height=940)
        self.app = app
        self.groups = {}
        self.panel_group = {}
        self.hovered = None
        self.maximized_group = None
        self.workspace = app.prefs["layout"] if app.prefs["layout"] in WORKSPACES else "editing"

        # header: menu bar + workspace switch
        bar = Gtk.HeaderBar()
        menubar = Gio.Menu()
        for title, entries in MENUS:
            menubar.append_submenu(title, build_menu(entries))
        pmb = Gtk.PopoverMenuBar.new_from_model(menubar)
        pmb.set_focusable(False)
        bar.pack_start(pmb)
        switch = Gtk.Box(spacing=2)
        switch.add_css_class("workspace-switch")
        self.ws_buttons = {}
        first = None
        for key, text in (("editing", "Editing"), ("effects", "Effects"), ("audio", "Audio")):
            btn = Gtk.ToggleButton(label=text, focusable=False)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", lambda b, k=key: b.get_active() and self.set_workspace(k))
            self.ws_buttons[key] = btn
            switch.append(btn)
        self.ws_buttons[self.workspace].set_active(True)
        bar.set_title_widget(switch)
        self.title_label = label("", css="title")
        self.fs_button = icon_button("fullscreen", "", lambda b: self.toggle_fullscreen(), size=14)
        bar.pack_end(self.fs_button)
        bar.pack_end(self.title_label)
        # GTK hides a window's title bar in full screen, which would take the
        # menus with it, so the header bar is part of the content instead.
        self.set_titlebar(Gtk.Box(visible=False))
        self.header = bar
        self.connect("notify::fullscreened", self._fullscreen_changed)
        self._fullscreen_changed()
        # The editor opens full screen; F11 or the header button leaves it.
        if app.prefs["fullscreen"] and not os.environ.get("TIDELINE_WINDOWED"):
            self.fullscreen()

        # panels
        self.source_monitor = MonitorPanel(app, "source")
        self.program_monitor = MonitorPanel(app, "program")
        self.effect_controls = EffectControls(app)
        self.project_panel = ProjectPanel(app)
        self.effects_panel = EffectsPanel(app)
        self.history_panel = HistoryPanel(app)
        self.info_panel = InfoPanel(app)
        self.timeline_panel = TimelinePanel(app)
        self.timeline = self.timeline_panel.view

        self.mixer = AudioMixer(app)
        a = self._group("a", [("source", "Source", self.source_monitor),
                              ("effect_controls", "Effect Controls", self.effect_controls),
                              ("mixer", "Audio Mixer", self.mixer)])
        b = self._group("b", [("program", "Program", self.program_monitor)])
        c = self._group("c", [("project", "Project", self.project_panel), ("effects", "Effects", self.effects_panel),
                              ("history", "History", self.history_panel), ("info", "Info", self.info_panel)])
        d = self._group("d", [("timeline", "Timeline", self.timeline_panel)])
        self.tools = ToolStrip(app)
        self.tools.set_valign(Gtk.Align.START)
        self.meters = AudioMeters(app)
        meter_frame = Gtk.Box()
        meter_frame.add_css_class("tool-strip")
        meter_frame.append(self.meters)

        self.top = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        self.top.set_start_child(a)
        self.top.set_end_child(b)
        right = Gtk.Box(spacing=4)
        right.append(self.tools)
        d.set_hexpand(True)
        right.append(d)
        right.append(meter_frame)
        self.right = right
        self.bottom = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        self.bottom.set_start_child(c)
        self.bottom.set_end_child(right)
        self.main = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, wide_handle=True)
        self.main.set_start_child(self.top)
        self.main.set_end_child(self.bottom)
        for paned in (self.top, self.bottom, self.main):
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)

        status = Gtk.Box(spacing=10)
        status.add_css_class("statusbar")
        self.status_label = label("Ready", ellipsize=True)
        self.status_label.set_hexpand(True)
        self.hint_label = label("", css="faint")
        status.append(self.status_label)
        status.append(self.hint_label)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.header)
        frame = Gtk.Box(margin_start=4, margin_end=4, margin_top=4, margin_bottom=2, vexpand=True)
        frame.append(self.main)
        self.main.set_hexpand(True)
        root.append(frame)
        root.append(status)
        self.set_child(root)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("close-request", self._close_request)
        self._main_size = None
        self.connect("realize", self._realized)

        app.on("status", self._status)
        app.on("tool", self._tool_hint)
        for ev in ("project", "sequence", "history", "media"):
            app.on(ev, self._retitle)
        self._status_id = 0
        self._retitle()
        self._tool_hint()
        self.focus_group(d, "timeline")

    # -- layout ----------------------------------------------------------
    def _realized(self, *_):
        GLib.idle_add(self._apply_workspace)
        self.get_surface().connect("layout", lambda *_: GLib.idle_add(self._rescale))

    def _group(self, key, panels):
        group = PanelGroup(self, on_focus=self.focus_group)
        for name, title, widget in panels:
            group.add(name, title, widget)
            self.panel_group[name] = group
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda c, x, y, g=group: setattr(self, "hovered", g))
        group.add_controller(motion)
        self.groups[key] = group
        return group

    def focus_group(self, group, panel):
        for g in self.groups.values():
            if g is group:
                g.add_css_class("focused")
            else:
                g.remove_css_class("focused")
        if panel:
            self.app.focus = panel

    def show_panel(self, name):
        group = self.panel_group.get(name)
        if group is None:
            return
        group.show(name)
        if name in ("source", "program", "timeline", "project", "effect_controls", "effects"):
            self.focus_group(group, name)

    def command(self, name):
        if name.startswith("workspace_"):
            self.ws_buttons[name[10:]].set_active(True)
        elif name.startswith("panel_"):
            self.show_panel(name[6:])

    def set_workspace(self, key):
        self.workspace = key
        if hasattr(self, "main"):
            self._apply_workspace()

    def _apply_workspace(self):
        ws = WORKSPACES[self.workspace]
        w, h = self.main.get_width(), self.main.get_height()
        if w > 100 and h > 100:
            self._main_size = (w, h)
            self.main.set_position(int(h * ws["top"]))
            self.top.set_position(int(w * ws["left"]))
            self.bottom.set_position(int(w * ws["bottom_left"]))
        for gkey, tab in ws["tabs"].items():
            self.groups[gkey].show(tab)
        self.timeline_panel.sync_scrollbar()
        return False

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fullscreen_changed(self, *_):
        full = self.is_fullscreen()
        set_button_icon(self.fs_button, "restore" if full else "fullscreen")
        self.fs_button.set_tooltip_text("Leave full screen (F11)" if full else "Full screen (F11)")

    def _rescale(self):
        """Keep the panel proportions when the window changes size."""
        w, h = self.main.get_width(), self.main.get_height()
        if w <= 100 or h <= 100 or (w, h) == self._main_size:
            return False
        if self._main_size is None:
            return self._apply_workspace()
        ow, oh = self._main_size
        self._main_size = (w, h)
        self.main.set_position(round(self.main.get_position() * h / oh))
        for paned in (self.top, self.bottom):
            paned.set_position(round(paned.get_position() * w / ow))
        self.timeline_panel.sync_scrollbar()
        return False

    def toggle_maximize(self):
        """Let the panel group under the pointer fill the window, or restore."""
        others = [self.tools, self.right.get_last_child()]
        if self.maximized_group is not None:
            for g in self.groups.values():
                g.set_visible(True)
            for wdg in others + [self.top, self.bottom, self.right]:
                wdg.set_visible(True)
            self.maximized_group = None
            GLib.idle_add(self._apply_workspace)
            return
        target = self.hovered or self.groups["d"]
        self.maximized_group = target
        for g in self.groups.values():
            g.set_visible(g is target)
        in_top = target in (self.groups["a"], self.groups["b"])
        self.top.set_visible(in_top)
        self.bottom.set_visible(not in_top)
        self.right.set_visible(target is self.groups["d"])
        for wdg in others:
            wdg.set_visible(target is self.groups["d"])

    # -- input -----------------------------------------------------------
    def _editing_text(self):
        focus = self.get_focus()
        while focus is not None:
            if isinstance(focus, (Gtk.Text, Gtk.TextView, Gtk.Entry, Gtk.SearchEntry, Gtk.SpinButton)):
                return True
            focus = focus.get_parent() if isinstance(focus, Gtk.Text) else None
        return False

    def _key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self._editing_text():
                self.timeline.grab_focus()
                return True
            if self.timeline.cancel_drag():
                return True
            if self.maximized_group is not None:
                self.toggle_maximize()
                return True
            self.app.clear_selection()
            return True
        if self._editing_text():
            return False
        name = self.app.key_command(keyval, state)
        if name is None:
            return False
        self.app.do(name)
        return True

    def _close_request(self, *_):
        if self.app.project.dirty:
            self.app.cmd_quit()
            return True
        return False

    # -- status ----------------------------------------------------------
    def _status(self, text, warn=False):
        self.status_label.set_text(text)
        if warn:
            self.status_label.add_css_class("warning")
        else:
            self.status_label.remove_css_class("warning")
        if self._status_id:
            GLib.source_remove(self._status_id)
        self._status_id = GLib.timeout_add_seconds(8, self._status_clear)

    def _status_clear(self):
        self._status_id = 0
        self.status_label.set_text("")
        self.status_label.remove_css_class("warning")
        return False

    def _tool_hint(self):
        hints = {
            "select": "Selection: drag clips to move, drag edges to trim · Ctrl+drag an edge to ripple · "
                      "Alt+drag to duplicate · Ctrl+drop to insert",
            "track_fwd": "Track Select Forward: click to select everything to the right · Shift for one track",
            "ripple": "Ripple Edit: drag a clip edge; later clips follow",
            "roll": "Rolling Edit: drag a cut to move it between two clips",
            "rate": "Rate Stretch: drag a clip edge to change its speed",
            "razor": "Razor: click a clip to cut it · Shift+click cuts all tracks",
            "slip": "Slip: drag a clip to change which part of the source it shows",
            "slide": "Slide: drag a clip between its neighbours",
            "pen": "Pen: click a clip's line to add an opacity or volume keyframe, drag to adjust",
            "hand": "Hand: drag to scroll the timeline",
            "zoom": "Zoom: click to zoom in · Alt+click to zoom out · drag to zoom to a range",
            "type": "Type: click in the Program monitor to add a title",
        }
        self.hint_label.set_text(hints.get(self.app.tool, ""))

    def _retitle(self, *_):
        self.set_title(self.app.title())
        self.title_label.set_text(self.app.project.name + (" •" if self.app.project.dirty else ""))

    def edit_marker(self, marker):
        from .dialogs import MarkerDialog
        MarkerDialog(self.app, marker).present()
