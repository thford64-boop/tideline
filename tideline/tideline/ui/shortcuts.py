"""Keyboard Shortcuts (Edit ▸ Keyboard Shortcuts): a keyboard that shows
which keys do what, and a list of every command where any shortcut can be
changed, removed or reset. Changes apply at once."""

import os

import cairo
from gi.repository import Gdk, GLib, Gtk

from .. import keymap as km
from . import theme
from .widgets import file_filter, icon_button, label, open_files, save_file

GROUP_COLOURS = {"File": "#5aa9e6", "Edit": "#9aa5b4", "Clip": "#e5a93b", "Sequence": "#b07be0",
                 "Markers": "#e06c9f", "Playback": "#2fc6b5", "Tools": "#ff7a59", "Window": "#7ecb5f",
                 "Extensions": "#c9b458", "Help": "#9aa5b4"}

# An ANSI keyboard: (key name, label, width in key units). None is a gap.
ROWS = [
    [("Escape", "Esc", 1), None, ("F1", "F1", 1), ("F2", "F2", 1), ("F3", "F3", 1), ("F4", "F4", 1), None,
     ("F5", "F5", 1), ("F6", "F6", 1), ("F7", "F7", 1), ("F8", "F8", 1), None, ("F9", "F9", 1),
     ("F10", "F10", 1), ("F11", "F11", 1), ("F12", "F12", 1)],
    [("grave", "`", 1)] + [(c, c, 1) for c in "1234567890"] + [("minus", "-", 1), ("equal", "=", 1),
                                                               ("BackSpace", "Backspace", 2)],
    [("Tab", "Tab", 1.5)] + [(c, c.upper(), 1) for c in "qwertyuiop"] + [
        ("bracketleft", "[", 1), ("bracketright", "]", 1), ("backslash", "\\", 1.5)],
    [("Caps_Lock", "Caps", 1.75)] + [(c, c.upper(), 1) for c in "asdfghjkl"] + [
        ("semicolon", ";", 1), ("apostrophe", "'", 1), ("Return", "Enter", 2.25)],
    [("Shift_L", "Shift", 2.25)] + [(c, c.upper(), 1) for c in "zxcvbnm"] + [
        ("comma", ",", 1), ("period", ".", 1), ("slash", "/", 1), ("Shift_R", "Shift", 2.75)],
    [("Control_L", "Ctrl", 1.5), ("Super_L", "Super", 1.25), ("Alt_L", "Alt", 1.25), ("space", "Space", 6.25),
     ("Alt_R", "Alt", 1.25), ("Control_R", "Ctrl", 1.5)],
]
# The block to the right: navigation keys and arrows, (name, label, column, row).
NAV = [("Insert", "Ins", 0, 1), ("Home", "Home", 1, 1), ("Page_Up", "PgUp", 2, 1),
       ("Delete", "Del", 0, 2), ("End", "End", 1, 2), ("Page_Down", "PgDn", 2, 2),
       ("Up", "Up", 1, 4), ("Left", "Left", 0, 5), ("Down", "Down", 1, 5), ("Right", "Right", 2, 5)]
MODIFIER_NAMES = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Caps_Lock"}


def group_colour(group):
    return GROUP_COLOURS.get(group.split(":")[0], GROUP_COLOURS["Extensions"])


class KeyboardView(Gtk.DrawingArea):
    """The keyboard, each key tinted by the group of the command it runs
    with the modifiers chosen above it. Clicking a key lists its commands."""

    def __init__(self, mapper):
        super().__init__(hexpand=True)
        self.mapper = mapper
        self.mods = 0
        self.hover = None
        self.keys = []            # (name, label, x, y, w, h) in key units
        x_end = 0
        for r, row in enumerate(ROWS):
            x = 0.0
            for entry in row:
                if entry is None:
                    x += 0.5
                    continue
                name, text, w = entry
                self.keys.append((name, text, x, r + (0.25 if r else 0), w, 1))
                x += w
            x_end = max(x_end, x)
        for name, text, c, r in NAV:
            self.keys.append((name, text, x_end + 0.5 + c, r + 0.25, 1, 1))
        self.units_w = x_end + 0.5 + 3
        self.units_h = len(ROWS) + 0.25
        self.set_size_request(-1, 290)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)

    def _scale(self):
        w, h = self.get_width(), self.get_height()
        unit = min((w - 8) / self.units_w, (h - 8) / self.units_h)
        ox = (w - unit * self.units_w) / 2
        oy = (h - unit * self.units_h) / 2
        return unit, ox, oy

    def _key_at(self, px, py):
        unit, ox, oy = self._scale()
        for key in self.keys:
            name, text, x, y, w, h = key
            if ox + x * unit <= px < ox + (x + w) * unit and oy + y * unit <= py < oy + (y + h) * unit:
                return key
        return None

    def accel(self, name, mods=None):
        keyval = Gdk.keyval_from_name(name)
        return Gtk.accelerator_name(keyval, Gdk.ModifierType(self.mods if mods is None else mods))

    def command(self, name):
        if name in MODIFIER_NAMES:
            return None
        return self.mapper.app.keymap.owner(self.accel(name))

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.queue_draw()

    def _motion(self, ctrl, x, y):
        self._set_hover(self._key_at(x, y))

    def _click(self, gesture, n, x, y):
        key = self._key_at(x, y)
        if key is not None and key[0] not in MODIFIER_NAMES:
            self.mapper.filter_key(key[0], key[1])

    def _tooltip(self, widget, x, y, keyboard_mode, tooltip):
        key = self._key_at(x, y)
        if key is None or key[0] in MODIFIER_NAMES:
            return False
        lines = []
        for mods in (0, int(Gdk.ModifierType.SHIFT_MASK), int(Gdk.ModifierType.CONTROL_MASK),
                     int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK),
                     int(Gdk.ModifierType.ALT_MASK), int(Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK),
                     int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)):
            accel = self.accel(key[0], mods)
            owner = self.mapper.app.keymap.owner(accel)
            if owner:
                lines.append(f"{km.label(accel)}  —  {self.mapper.command_label(owner)}")
        tooltip.set_text("\n".join(lines) if lines else f"{key[1]}: not used")
        return True

    def _draw(self, area, cr, w, h):
        unit, ox, oy = self._scale()
        held = {"Shift_L": Gdk.ModifierType.SHIFT_MASK, "Shift_R": Gdk.ModifierType.SHIFT_MASK,
                "Control_L": Gdk.ModifierType.CONTROL_MASK, "Control_R": Gdk.ModifierType.CONTROL_MASK,
                "Alt_L": Gdk.ModifierType.ALT_MASK, "Alt_R": Gdk.ModifierType.ALT_MASK}
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        for key in self.keys:
            name, text, x, y, kw, kh = key
            px, py = ox + x * unit + 1.5, oy + y * unit + 1.5
            pw, ph = kw * unit - 3, kh * unit - 3
            owner = self.command(name)
            r = min(5.0, unit * 0.12)
            cr.new_sub_path()
            cr.arc(px + pw - r, py + r, r, -1.5708, 0)
            cr.arc(px + pw - r, py + ph - r, r, 0, 1.5708)
            cr.arc(px + r, py + ph - r, r, 1.5708, 3.1416)
            cr.arc(px + r, py + r, r, 3.1416, 4.7124)
            cr.close_path()
            mod_on = name in held and self.mods & int(held[name])
            if owner is not None:
                group = self.mapper.command_group(owner)
                cr.set_source_rgba(*theme.rgb(group_colour(group), 0.85 if key is self.hover else 0.6))
            elif mod_on:
                cr.set_source_rgba(*theme.rgb(theme.ACCENT_DIM))
            else:
                cr.set_source_rgba(*theme.rgb(theme.BG4 if key is self.hover else theme.BG3))
            cr.fill_preserve()
            cr.set_source_rgba(*theme.rgb(theme.LINE))
            cr.set_line_width(1)
            cr.stroke()
            cr.set_source_rgba(*theme.rgb(theme.TEXT if (owner or mod_on) else theme.TEXT_DIM))
            cr.set_font_size(max(8.0, unit * 0.24))
            cr.move_to(px + 4, py + unit * 0.3)
            cr.show_text(text)
            if owner is not None and unit > 30:
                cr.save()
                cr.rectangle(px + 2, py, pw - 4, ph)
                cr.clip()
                cr.set_font_size(max(7.0, unit * 0.17))
                cr.set_source_rgba(*theme.rgb("#ffffff", 0.92))
                words = self.mapper.command_label(owner).replace("…", "").split()
                lines, line = [], ""
                for word in words:
                    trial = (line + " " + word).strip()
                    if cr.text_extents(trial).width > pw - 6 and line:
                        lines.append(line)
                        line = word
                    else:
                        line = trial
                lines.append(line)
                for i, text_line in enumerate(lines[:2]):
                    cr.move_to(px + 4, py + unit * (0.56 + 0.2 * i))
                    cr.show_text(text_line)
                cr.restore()


class ShortcutMapper(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Keyboard Shortcuts", application=app, transient_for=app.window,
                         default_width=1040, default_height=820)
        self.app = app
        self.keymap = app.keymap
        self.capturing = None          # command name waiting for a key
        self._on_yes = None            # what the banner's button does
        self.key_filter = None         # (key name, label) chosen on the keyboard
        self.rows = {}                 # command -> (row widget, key button, reset button, clear button)
        self.headers = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.add_css_class("dialog-body")

        mods = Gtk.Box(spacing=6)
        mods.append(label("Show the keys for", css="dim"))
        self.mod_buttons = {}
        for text, mask in (("Ctrl", Gdk.ModifierType.CONTROL_MASK), ("Alt", Gdk.ModifierType.ALT_MASK),
                           ("Shift", Gdk.ModifierType.SHIFT_MASK)):
            btn = Gtk.ToggleButton(label=text, focusable=False)
            btn.connect("toggled", self._mods_changed)
            self.mod_buttons[int(mask)] = btn
            mods.append(btn)
        mods.append(Gtk.Box(hexpand=True))
        legend = Gtk.Box(spacing=10)
        for group in ("File", "Edit", "Clip", "Sequence", "Markers", "Playback", "Tools", "Window"):
            chip = Gtk.Box(spacing=4)
            dot = Gtk.DrawingArea()
            dot.set_size_request(10, 10)
            dot.set_valign(Gtk.Align.CENTER)
            dot.set_draw_func(lambda a, cr, w, h, g=group: (cr.set_source_rgba(*theme.rgb(group_colour(g))),
                                                            cr.rectangle(0, 0, w, h), cr.fill()))
            chip.append(dot)
            chip.append(label(group, css="faint"))
            legend.append(chip)
        mods.append(legend)
        outer.append(mods)
        self.keyboard = KeyboardView(self)
        outer.append(self.keyboard)

        tools = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search commands or shortcuts", hexpand=True)
        self.search.connect("search-changed", lambda *_: self._filter())
        tools.append(self.search)
        self.show = Gtk.DropDown.new_from_strings(["All commands", "With a shortcut", "Without a shortcut",
                                                    "Changed"])
        self.show.set_focusable(False)
        self.show.connect("notify::selected", lambda *_: self._filter())
        tools.append(self.show)
        self.key_chip = Gtk.Button(visible=False, focusable=False)
        self.key_chip.add_css_class("key-cap")
        self.key_chip.set_tooltip_text("Show every command again")
        self.key_chip.connect("clicked", lambda *_: self.filter_key(None, None))
        tools.append(self.key_chip)
        outer.append(tools)

        self.banner = Gtk.Box(spacing=8, visible=False)
        self.banner.add_css_class("shortcut-banner")
        self.banner_text = label("", hexpand=True, wrap=True)
        self.banner.append(self.banner_text)
        self.banner_yes = Gtk.Button()
        self.banner_yes.add_css_class("suggested-action")
        self.banner_yes.connect("clicked", lambda *_: self._confirm(True))
        self.banner_no = Gtk.Button(label="Cancel")
        self.banner_no.connect("clicked", lambda *_: self._confirm(False))
        self.banner.append(self.banner_yes)
        self.banner.append(self.banner_no)
        outer.append(self.banner)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("shortcut-list")
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.list)
        self.scroller = sw
        outer.append(sw)

        foot = Gtk.Box(spacing=8)
        for text, fn, tip in (("Import…", self._import, "Load shortcuts from a .tlkeys file"),
                              ("Export…", self._export, "Save your shortcuts to a .tlkeys file, to use on "
                                                       "another computer or share"),
                              ("Reset All", self._reset_all, "Go back to Tideline's default shortcuts")):
            btn = Gtk.Button(label=text, tooltip_text=tip)
            btn.connect("clicked", lambda b, f=fn: f())
            foot.append(btn)
        self.status = label("", css="faint", hexpand=True, ellipsize=True)
        foot.append(self.status)
        done = Gtk.Button(label="Close")
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda *_: self.close())
        foot.append(done)
        outer.append(foot)
        self.set_child(outer)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._build_list()
        self._update_status()
        app.on("keymap", self._keymap_changed)
        self.connect("close-request", self._closing)

    # -- commands ----------------------------------------------------------
    def _commands(self):
        return self.keymap.commands()

    def command_label(self, name):
        entry = next((c for c in self._commands() if c[0] == name), None)
        return entry[1] if entry else name

    def command_group(self, name):
        entry = next((c for c in self._commands() if c[0] == name), None)
        return entry[3] if entry else "Edit"

    # -- list --------------------------------------------------------------
    def _build_list(self):
        child = self.list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt
        self.rows, self.headers = {}, []
        group = None
        for name, text, _accel, grp in self._commands():
            if grp != group:
                group = grp
                head = label(grp, css="heading", margin_top=10, margin_bottom=2, margin_start=6)
                row = Gtk.ListBoxRow(activatable=False, selectable=False)
                row.set_child(head)
                self.list.append(row)
                self.headers.append((row, grp))
            box = Gtk.Box(spacing=8, margin_start=10, margin_end=6)
            dot = Gtk.DrawingArea()
            dot.set_size_request(4, 16)
            dot.set_valign(Gtk.Align.CENTER)
            dot.set_draw_func(lambda a, cr, w, h, g=grp: (cr.set_source_rgba(*theme.rgb(group_colour(g))),
                                                          cr.rectangle(0, 0, w, h), cr.fill()))
            box.append(dot)
            box.append(label(text, hexpand=True, ellipsize=True))
            key_btn = Gtk.Button(focusable=False)
            key_btn.add_css_class("shortcut-key")
            key_btn.set_tooltip_text("Click, then press the new shortcut")
            key_btn.connect("clicked", lambda b, n=name: self.capture(n))
            key_btn.set_size_request(150, -1)
            box.append(key_btn)
            clear = icon_button("close", "Remove this shortcut", lambda b, n=name: self.clear(n), size=11,
                                css="kf-button")
            reset = icon_button("reset", "Back to the default shortcut", lambda b, n=name: self.reset(n),
                                size=12, css="kf-button")
            box.append(clear)
            box.append(reset)
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.set_child(box)
            self.list.append(row)
            self.rows[name] = (row, key_btn, reset, clear, (text + " " + grp).lower())
        self._refresh_rows()
        self._filter()

    def _refresh_rows(self):
        for name, (row, key_btn, reset, clear, _text) in self.rows.items():
            accel = self.keymap.accel(name)
            if name == self.capturing:
                key_btn.set_label("Press a key…")
                key_btn.add_css_class("capturing")
            else:
                key_btn.set_label(km.label(accel) if accel else "—")
                key_btn.remove_css_class("capturing")
            if self.keymap.changed(name):
                key_btn.add_css_class("changed")
            else:
                key_btn.remove_css_class("changed")
            reset.set_sensitive(self.keymap.changed(name))
            clear.set_sensitive(bool(accel))
        self.keyboard.queue_draw()

    def _filter(self):
        q = self.search.get_text().strip().lower()
        mode = self.show.get_selected()
        visible_groups = set()
        key_name = self.key_filter[0] if self.key_filter else None
        for name, (row, key_btn, reset, clear, text) in self.rows.items():
            accel = self.keymap.accel(name)
            shown = True
            if q:
                shown = q in text or q in km.label(accel).lower()
            if mode == 1:
                shown = shown and bool(accel)
            elif mode == 2:
                shown = shown and not accel
            elif mode == 3:
                shown = shown and self.keymap.changed(name)
            if key_name is not None:
                parsed = km.parse(accel)
                shown = shown and parsed is not None and Gdk.keyval_name(parsed[0]) == Gdk.keyval_name(
                    Gdk.keyval_to_lower(Gdk.keyval_from_name(key_name)))
            row.set_visible(shown)
            if shown:
                visible_groups.add(self.command_group(name))
        for row, grp in self.headers:
            row.set_visible(grp in visible_groups)

    def filter_key(self, name, text):
        """List only the commands on one key (any modifiers); None shows all."""
        self.key_filter = (name, text) if name else None
        self.key_chip.set_visible(name is not None)
        if name:
            self.key_chip.set_label(f"Key: {text}  ✕")
        self._filter()

    def _mods_changed(self, *_):
        mods = 0
        for mask, btn in self.mod_buttons.items():
            if btn.get_active():
                mods |= mask
        self.keyboard.mods = mods
        self.keyboard.queue_draw()

    # -- editing -----------------------------------------------------------
    def capture(self, name):
        self._confirm(False)
        self.capturing = None if self.capturing == name else name
        if self.capturing:
            self._say(f"Press the new shortcut for “{self.command_label(name)}”. Esc cancels.")
        else:
            self._update_status()
        self._refresh_rows()

    def _key(self, ctrl, keyval, keycode, state):
        if self.capturing is None:
            if keyval == Gdk.KEY_Escape and not self.search.has_focus():
                self.close()
                return True
            return False
        if keyval == Gdk.KEY_Escape:
            self.capture(self.capturing)
            return True
        accel = km.from_event(keyval, state)
        if not accel:
            return True                 # a modifier on its own: wait for the key
        name, self.capturing = self.capturing, None
        self._offer(name, accel)
        return True

    def _offer(self, name, accel):
        """Assign ``accel`` to ``name``, asking first if another command has it."""
        owner = self.keymap.owner(accel, exclude=name)
        if owner is None:
            return self._apply(name, accel)
        esc = GLib.markup_escape_text
        self._ask(f"<b>{esc(km.label(accel))}</b> already runs “{esc(self.command_label(owner))}”. Give it to "
                  f"“{esc(self.command_label(name))}” instead? “{esc(self.command_label(owner))}” will have "
                  "no shortcut.", "Reassign", lambda: self._apply(name, accel))

    def _ask(self, markup, yes_label, on_yes):
        self._on_yes = on_yes
        self._update_status()
        self.banner_text.set_markup(markup)
        self.banner_yes.set_label(yes_label)
        self.banner.set_visible(True)
        self._refresh_rows()

    def _confirm(self, yes):
        on_yes, self._on_yes = self._on_yes, None
        self.banner.set_visible(False)
        if yes and on_yes is not None:
            on_yes()
        self._refresh_rows()

    def _apply(self, name, accel):
        try:
            taken = self.keymap.assign(name, accel)
        except ValueError as exc:
            self._say(str(exc), warn=True)
            self._refresh_rows()
            return
        self.app.set_shortcuts()
        text = f"{km.label(accel)} now runs “{self.command_label(name)}”"
        if taken:
            text += f"; “{self.command_label(taken)}” has no shortcut"
        self._say(text + ".")

    def clear(self, name):
        self.keymap.clear(name)
        self.app.set_shortcuts()
        self._say(f"“{self.command_label(name)}” has no shortcut now.")

    def reset(self, name):
        taken = self.keymap.reset(name)
        self.app.set_shortcuts()
        text = f"“{self.command_label(name)}” is back to " + (km.label(self.keymap.accel(name)) or "no shortcut")
        if taken:
            text += f"; “{self.command_label(taken)}” gave it up"
        self._say(text + ".")

    def _reset_all(self):
        def reset():
            self.keymap.reset_all()
            self.app.set_shortcuts()
            self._say("All shortcuts are back to the defaults.")

        self._ask("Go back to the default shortcuts? Every change made here is lost.", "Reset All", reset)

    def _import(self):
        def chosen(paths):
            try:
                n = self.keymap.load(paths[0])
            except ValueError as exc:
                self._say(str(exc), warn=True)
                return
            self.app.set_shortcuts()
            self._say(f"Loaded {n} shortcut change{'s' if n != 1 else ''} from {os.path.basename(paths[0])}.")

        open_files(self, "Import Keyboard Shortcuts", chosen,
                   [file_filter("Tideline shortcuts", ["*" + km.KEYMAP_EXT])], multiple=False)

    def _export(self):
        def chosen(path):
            if not path.endswith(km.KEYMAP_EXT):
                path += km.KEYMAP_EXT
            try:
                self.keymap.dump(path)
            except OSError as exc:
                self._say(f"Could not save {os.path.basename(path)}: {exc.strerror}", warn=True)
                return
            self._say(f"Saved to {path}.")

        save_file(self, "Export Keyboard Shortcuts", "My shortcuts" + km.KEYMAP_EXT, chosen,
                  [file_filter("Tideline shortcuts", ["*" + km.KEYMAP_EXT])])

    # -- feedback ----------------------------------------------------------
    def _say(self, text, warn=False):
        self.status.set_text(text)
        if warn:
            self.status.add_css_class("warning-text")
        else:
            self.status.remove_css_class("warning-text")

    def _update_status(self):
        changed = sum(1 for name, *_ in self._commands() if self.keymap.changed(name))
        self._say(f"{changed} shortcut{'s' if changed != 1 else ''} changed from the defaults." if changed
                  else "Click a shortcut to change it. Click a key on the keyboard to see what it does.")

    def _keymap_changed(self):
        names = [c[0] for c in self._commands()]
        if set(names) != set(self.rows):
            self._build_list()
        else:
            self._refresh_rows()
            self._filter()

    def _closing(self, *_):
        self.app.off("keymap", self._keymap_changed)
        return False
