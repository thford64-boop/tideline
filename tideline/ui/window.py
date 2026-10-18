"""Main window: menu bar, dockable panel layout, keyboard dispatch, status bar."""

import copy
import os

from gi.repository import Gdk, Gio, GLib, Gtk

from .. import APP_NAME
from .. import dock
from ..commands import BY_NAME, MENUS
from .effect_controls import EffectControls
from .monitor import MonitorPanel
from .multicam import MultiCamPanel
from .panels import AudioMeters, AudioMixer, EffectsPanel, HistoryPanel, InfoPanel, ToolStrip
from .project_panel import ProjectPanel
from .scopes import ScopesPanel
from .timeline import TimelinePanel
from .widgets import PanelGroup, icon_button, label, popup_menu, set_button_icon

# The workspaces' starting layouts (dock.DEFAULTS); people rearrange them freely.
WORKSPACES = dock.DEFAULTS


def build_menu(entries, accel_of=None):
    """A menu from the command table; ``accel_of(name)`` gives the shortcut
    shown beside each item (default: the command table's)."""
    menu = Gio.Menu()
    section = Gio.Menu()
    for entry in entries:
        if entry is None:
            menu.append_section(None, section)
            section = Gio.Menu()
        elif isinstance(entry, tuple):
            section.append_submenu(entry[0], build_menu(entry[1], accel_of))
        else:
            name, text, accel, _ = BY_NAME[entry]
            if accel_of is not None:
                accel = accel_of(name)
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
        self.hovered = None
        self.maximized_group = None
        self.workspace = app.prefs["layout"] if app.prefs["layout"] in WORKSPACES else "editing"

        # header: menu bar + workspace switch
        bar = Gtk.HeaderBar()
        self.menubar = Gtk.PopoverMenuBar.new_from_model(self._menu_model())
        self.menubar.set_focusable(False)
        bar.pack_start(self.menubar)
        app.on("keymap", lambda: self.menubar.set_menu_model(self._menu_model()))
        switch = Gtk.Box(spacing=2)
        switch.add_css_class("workspace-switch")
        self.ws_buttons = {}
        first = None
        for key, text in (("editing", "Editing"), ("effects", "Effects"), ("color", "Colour"), ("audio", "Audio")):
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
        self.scopes_panel = ScopesPanel(app)
        self.multicam_panel = MultiCamPanel(app)
        self.timeline_panel = TimelinePanel(app)
        self.timeline = self.timeline_panel.view
        self.mixer = AudioMixer(app)
        self.tools = ToolStrip(app)
        self.tools.set_valign(Gtk.Align.START)
        self.meters = AudioMeters(app)
        meter_frame = Gtk.Box()
        meter_frame.add_css_class("tool-strip")
        meter_frame.append(self.meters)
        # The timeline docks together with its tools and the level meters.
        timeline_dock = Gtk.Box(spacing=4)
        timeline_dock.append(self.tools)
        self.timeline_panel.set_hexpand(True)
        timeline_dock.append(self.timeline_panel)
        timeline_dock.append(meter_frame)
        self.panels = {"source": self.source_monitor, "program": self.program_monitor,
                       "effect_controls": self.effect_controls, "mixer": self.mixer,
                       "multicam": self.multicam_panel, "project": self.project_panel,
                       "effects": self.effects_panel, "scopes": self.scopes_panel, "history": self.history_panel,
                       "info": self.info_panel, "timeline": timeline_dock}

        # layout: one dock state per workspace, as people left them
        saved = app.prefs["dock_layouts"] if isinstance(app.prefs["dock_layouts"], dict) else {}
        self.layouts = {ws: dock.sanitize(saved.get(ws), ws, self.panels) for ws in WORKSPACES}
        self.groups = []              # PanelGroup of every tab group shown, floating ones too
        self.panel_group = {}         # panel -> its PanelGroup
        self.floats = []              # floating windows, in the order of the state's "floating" list
        self.paneds = []              # (Gtk.Paned, path of its split)
        self._sizing = False
        self._building = False
        self._save_id = 0
        self.dock_frame = Gtk.Box(hexpand=True, vexpand=True)
        dock_actions = Gio.SimpleActionGroup()
        for name, fn in (("float", self.float_panel), ("close", self.close_panel)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda a, p, fn=fn: fn(p.get_string()))
            dock_actions.add_action(action)
        reset = Gio.SimpleAction.new("reset", None)
        reset.connect("activate", lambda *_: self.reset_workspace())
        dock_actions.add_action(reset)
        self.dock_actions = dock_actions
        self.insert_action_group("dock", dock_actions)

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
        frame.append(self.dock_frame)
        root.append(frame)
        root.append(status)
        self.set_child(root)
        self._build_dock()

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("close-request", self._close_request)
        self.connect("realize", self._realized)
        self.connect("destroy", self._destroyed)

        app.on("status", self._status)
        app.on("tool", self._tool_hint)
        for ev in ("project", "sequence", "history", "media"):
            app.on(ev, self._retitle)
        self._status_id = 0
        self._retitle()
        self._tool_hint()
        if "timeline" in self.panel_group:
            self.focus_group(self.panel_group["timeline"], "timeline")

    # -- layout ----------------------------------------------------------
    @property
    def state(self):
        """The dock state of the workspace showing."""
        return self.layouts[self.workspace]

    def _realized(self, *_):
        GLib.idle_add(self._apply_positions)
        self.get_surface().connect("layout", lambda *_: GLib.idle_add(self._apply_positions))

    def _detach_panels(self):
        for widget in self.panels.values():
            parent = widget.get_parent()
            if isinstance(parent, Gtk.Stack):
                parent.remove(widget)

    def _build_dock(self, tree=None):
        """(Re)build the panel layout from the workspace's state; ``tree``
        shows that tree instead (a maximised group)."""
        for win in self.floats:
            win.closing = True
            win.destroy()
        self.floats = []
        focus = self.get_focus()
        refocus = focus is not None and (focus is self.timeline or focus.is_ancestor(self.dock_frame))
        self.set_focus(None)          # a focused widget inside a torn-down split upsets GtkPaned
        self._detach_panels()
        child = self.dock_frame.get_first_child()
        if child is not None:
            self.dock_frame.remove(child)
        self.groups, self.panel_group, self.paneds = [], {}, []
        state = self.state
        root = self._build_node(tree or state["tree"], "")
        root.set_hexpand(True)
        root.set_vexpand(True)
        self.dock_frame.append(root)
        if tree is None:
            for i, win in enumerate(state["floating"]):
                self.floats.append(FloatingPanels(self, i, win))
        focused = self.app.focus if self.app.focus in self.panel_group else None
        if focused:
            self.focus_group(self.panel_group[focused], None)
        GLib.idle_add(self._apply_positions)
        if refocus and "timeline" in self.panel_group:
            GLib.idle_add(lambda: self.timeline.grab_focus() and False)

    def _build_node(self, node, path):
        if dock.is_leaf(node):
            return self._group(node)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL if node["split"] == "h"
                          else Gtk.Orientation.VERTICAL, wide_handle=True)
        paned.set_start_child(self._build_node(node["a"], path + "a"))
        paned.set_end_child(self._build_node(node["b"], path + "b"))
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.split_path = path
        paned.last_size = 0
        paned.connect("notify::position", self._paned_moved)
        self.paneds.append(paned)
        return paned

    def _group(self, node):
        group = PanelGroup(self, on_focus=self.focus_group, dock=self)
        self._building = True
        try:
            for key in node["tabs"]:
                group.add(key, dock.PANELS[key], self.panels[key])
                self.panel_group[key] = group
            group.show(node["active"])
        finally:
            self._building = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda c, x, y, g=group: setattr(self, "hovered", g))
        group.add_controller(motion)
        self.groups.append(group)
        return group

    def _split_node(self, path):
        return dock._at(self.state, path)

    def _apply_positions(self):
        """Give every split its share of the room (after building and when
        the window changes size)."""
        self._sizing = True
        try:
            for paned in self.paneds:
                node = self._split_node(paned.split_path) if self.maximized_group is None else None
                size = paned.get_width() if paned.get_orientation() == Gtk.Orientation.HORIZONTAL \
                    else paned.get_height()
                if node is None or dock.is_leaf(node) or size < 40:
                    continue
                paned.last_size = size
                want = int(round(size * node["pos"]))
                if abs(paned.get_position() - want) > 1:
                    paned.set_position(want)
        finally:
            self._sizing = False
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.sync_scrollbar()
        return False

    def _paned_moved(self, paned, _p):
        """Someone dragged a split's handle: remember its new share."""
        if self._sizing or self.maximized_group is not None:
            return
        size = paned.get_width() if paned.get_orientation() == Gtk.Orientation.HORIZONTAL else paned.get_height()
        if size < 40 or size != paned.last_size:
            return           # the window changed size; _apply_positions restores the shares
        node = self._split_node(paned.split_path)
        if node is not None and not dock.is_leaf(node):
            node["pos"] = max(dock.MIN_POS, min(dock.MAX_POS, paned.get_position() / size))
            self._save_layouts()

    def _save_layouts(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(800, self._write_layouts)

    def _write_layouts(self):
        self._save_id = 0
        for win in self.floats:
            win.remember_size()
        self.app.prefs["dock_layouts"] = copy.deepcopy(self.layouts)
        self.app.prefs.save()
        return False

    def _set_state(self, state):
        """Show a changed layout and keep it."""
        self.layouts[self.workspace] = dock.sanitize(state, self.workspace, self.panels)
        if self.maximized_group is not None:
            self.maximized_group = None
        self._build_dock()
        self._save_layouts()

    # -- docking (called by the panel groups) ------------------------------
    def dock_panel(self, panel, target, zone):
        if panel == target and zone == "center":
            return
        for win in self.floats:
            win.remember_size()
        self._set_state(dock.dock(self.state, panel, target, zone))
        self.show_panel(panel)

    def float_panel(self, panel):
        group = self.panel_group.get(panel)
        size = (max(420, group.get_width()), max(300, group.get_height())) if group is not None else (640, 420)
        for win in self.floats:
            win.remember_size()
        self._set_state(dock.float_panel(self.state, panel, size))

    def close_panel(self, panel):
        for win in self.floats:
            win.remember_size()
        self._set_state(dock.remove(self.state, panel))
        self.app.status(f"Closed {dock.PANELS[panel]} — Window ▸ {dock.PANELS[panel]} brings it back.")

    def dock_back(self, index):
        """A floating window was closed: its panels go back into the layout."""
        for win in self.floats:
            win.remember_size()
        self._set_state(dock.dock_back(self.state, index, self.workspace))

    def tab_shown(self, panel):
        if self._building or self.maximized_group is not None:
            return
        lf = dock.leaf_of(self.state, panel)
        if lf is not None and lf["active"] != panel:
            lf["active"] = panel
            self._save_layouts()

    def tab_menu(self, widget, panel, x, y):
        menu = Gio.Menu()
        if dock.floating_index(self.state, panel) is None or \
                dock.panels(self.state["floating"][dock.floating_index(self.state, panel)]["tree"]) != [panel]:
            menu.append("Float in a Window", f"dock.float('{panel}')")
        menu.append(f"Close {dock.PANELS[panel]}", f"dock.close('{panel}')")
        tail = Gio.Menu()
        tail.append("Reset Workspace Layout", "dock.reset")
        menu.append_section(None, tail)
        popup_menu(widget, menu, x, y)

    def reset_workspace(self):
        self._set_state(dock.default(self.workspace))
        self.app.status(f"The {self.workspace.capitalize()} workspace is back to its starting layout.")

    def focus_group(self, group, panel):
        for g in self.groups:
            if g is group:
                g.add_css_class("focused")
            else:
                g.remove_css_class("focused")
        if panel:
            self.app.focus = panel

    def show_panel(self, name):
        if name not in self.panels:
            return
        if name not in self.panel_group or (self.maximized_group is not None
                                            and self.panel_group[name] is not self.maximized_group):
            self.maximized_group = None
            self.layouts[self.workspace] = dock.show(self.state, name, self.workspace)
            self._build_dock()
            self._save_layouts()
        group = self.panel_group.get(name)
        if group is None:
            return
        group.show(name)
        win = group.get_root()
        if win is not self and isinstance(win, Gtk.Window):
            win.present()
        if name in ("source", "program", "timeline", "project", "effect_controls", "effects", "multicam"):
            self.focus_group(group, name)

    def current_tab(self, panel):
        """The tab showing in ``panel``'s group, or None when it is closed."""
        group = self.panel_group.get(panel)
        return group.current() if group is not None else None

    def command(self, name):
        if name.startswith("workspace_"):
            self.ws_buttons[name[10:]].set_active(True)
        elif name.startswith("panel_"):
            self.show_panel(name[6:])

    def _menu_model(self):
        menubar = Gio.Menu()
        for title, entries in MENUS:
            menu = build_menu(entries, self.app.keymap.accel)
            if title == "Extensions":
                self._add_extension_commands(menu)
            menubar.append_submenu(title, menu)
        return menubar

    def _add_extension_commands(self, menu):
        """One section per extension that added commands, labelled with its name."""
        by_ext = {}
        for cmd in self.app.extensions.commands:
            by_ext.setdefault(cmd.ext.name, []).append(cmd)
        for name, cmds in by_ext.items():
            section = Gio.Menu()
            for cmd in cmds:
                item = Gio.MenuItem.new(cmd.label, "app." + cmd.action)
                accel = self.app.keymap.accel(cmd.action)
                if accel:
                    item.set_attribute_value("accel", GLib.Variant.new_string(accel))
                section.append_item(item)
            menu.append_section(name, section)

    def set_workspace(self, key):
        if key == self.workspace and hasattr(self, "dock_frame"):
            return
        self.workspace = key
        if hasattr(self, "dock_frame"):
            for win in self.floats:
                win.remember_size()
            self.maximized_group = None
            self._build_dock()

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fullscreen_changed(self, *_):
        full = self.is_fullscreen()
        set_button_icon(self.fs_button, "restore" if full else "fullscreen")
        self.fs_button.set_tooltip_text(self.app.tip("Leave full screen" if full else "Full screen",
                                                     "toggle_fullscreen"))

    def toggle_maximize(self):
        """Let the panel group under the pointer fill the window, or restore."""
        if self.maximized_group is not None:
            self.maximized_group = None
            self._build_dock()
            return
        target = self.hovered if self.hovered in self.groups else self.panel_group.get("timeline")
        if target is None or target.get_root() is not self:
            return
        node = copy.deepcopy(dock.leaf_of(self.state, target.keys[0]))
        node["active"] = target.current() or node["active"]
        self._build_dock(tree=node)
        self.maximized_group = self.groups[0]

    # -- input -----------------------------------------------------------
    def _editing_text(self, window=None):
        focus = (window or self).get_focus()
        while focus is not None:
            if isinstance(focus, (Gtk.Text, Gtk.TextView, Gtk.Entry, Gtk.SearchEntry, Gtk.SpinButton)):
                return True
            focus = focus.get_parent() if isinstance(focus, Gtk.Text) else None
        return False

    def _key(self, ctrl, keyval, keycode, state, window=None):
        """Keys pressed in the main window, or in ``window`` (a floating one)."""
        if keyval == Gdk.KEY_Escape:
            if self.app.picker is not None:
                self.app.cancel_pick()
                return True
            if self._editing_text(window):
                if window is not None:
                    window.set_focus(None)
                else:
                    self.timeline.grab_focus()
                return True
            if self.timeline.cancel_drag():
                return True
            if self.maximized_group is not None:
                self.toggle_maximize()
                return True
            self.app.clear_selection()
            return True
        if self._editing_text(window):
            return False
        name = self.app.key_command(keyval, state)
        if name is None:
            return False
        self.app.do(name)
        return True

    def _destroyed(self, *_):
        for win in self.floats:
            win.closing = True
            win.destroy()
        self.floats = []

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


class FloatingPanels(Gtk.Window):
    """Panels floating in their own window (for a second display, say).
    Closing it docks them back into the main window."""

    def __init__(self, main, index, win):
        super().__init__(application=main.app, default_width=win["size"][0], default_height=win["size"][1])
        self.main, self.index, self.closing = main, index, False
        names = [dock.PANELS[p] for p in dock.panels(win["tree"])]
        self.set_title(f"{' · '.join(names)} — {APP_NAME}")
        self.insert_action_group("dock", main.dock_actions)
        body = main._build_node(win["tree"], f"f{index}:")
        frame = Gtk.Box(margin_start=4, margin_end=4, margin_top=4, margin_bottom=4)
        body.set_hexpand(True)
        body.set_vexpand(True)
        frame.append(body)
        self.set_child(frame)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda c, keyval, code, state: main._key(c, keyval, code, state, self))
        self.add_controller(keys)
        self.connect("close-request", self._close)
        self.connect("realize", lambda *_: self.get_surface().connect(
            "layout", lambda *_: GLib.idle_add(main._apply_positions)))
        self.present()

    def remember_size(self):
        if not self.closing and self.index < len(self.main.state["floating"]):
            w, h = self.get_width(), self.get_height()
            if w > 50 and h > 50:
                self.main.state["floating"][self.index]["size"] = [w, h]

    def _close(self, *_):
        if self.closing:
            return False
        self.remember_size()
        self.closing = True
        GLib.idle_add(lambda: self.main.dock_back(self.index) and False)
        return False

