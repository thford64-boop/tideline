"""Small shared widgets: icon buttons, drag-to-change numbers, timecode
fields, tabbed panel groups, and dialog helpers."""

from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk

from .. import timecode as tc
from .icons import Icon


def icon_button(icon, tooltip="", callback=None, toggle=False, size=16, css="icon-button"):
    btn = Gtk.ToggleButton() if toggle else Gtk.Button()
    btn.set_child(Icon(icon, size))
    btn.add_css_class(css)
    btn.set_focusable(False)
    btn.set_focus_on_click(False)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    if callback is not None:
        btn.connect("toggled" if toggle else "clicked", callback)
    return btn


def set_button_icon(btn, name):
    btn.get_child().set_name_(name)


def label(text="", css=None, xalign=0.0, ellipsize=False, **kw):
    lab = Gtk.Label(label=text, xalign=xalign, **kw)
    if css:
        for c in css.split():
            lab.add_css_class(c)
    if ellipsize:
        lab.set_ellipsize(3)
    return lab


def spacer():
    return Gtk.Box(hexpand=True)


def parse_rgba(text, default="#ffffffff"):
    rgba = Gdk.RGBA()
    if not rgba.parse(text or default):
        rgba.parse(default)
    return rgba


def rgba_to_hex(rgba):
    return "#{:02x}{:02x}{:02x}{:02x}".format(
        *(int(round(max(0.0, min(1.0, c)) * 255)) for c in (rgba.red, rgba.green, rgba.blue, rgba.alpha)))


class DragNumber(Gtk.Stack):
    """A number you drag sideways to change or click to type.

    Emits ``changed(value, final)``; ``final`` is False while dragging so the
    caller can coalesce undo history. Hold Shift for fine steps, Ctrl for coarse.
    """

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (float, bool))}

    def __init__(self, value=0.0, lo=None, hi=None, step=1.0, digits=1, unit=""):
        super().__init__()
        self.lo, self.hi, self.step, self.digits, self.unit = lo, hi, step, digits, unit
        self.value = float(value)
        self._start = 0.0
        self._moved = False
        self.set_hhomogeneous(False)
        self.label = Gtk.Label(xalign=0.0)
        self.label.add_css_class("hot-number")
        self.label.set_cursor(Gdk.Cursor.new_from_name("ew-resize"))
        self.entry = Gtk.Entry()
        self.entry.add_css_class("hot-entry")
        self.entry.set_width_chars(7)
        self.entry.set_max_width_chars(7)
        self.add_named(self.label, "label")
        self.add_named(self.entry, "entry")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.label.add_controller(drag)
        self.entry.connect("activate", self._commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.set_visible_child_name("label"))
        self.entry.add_controller(focus)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._entry_key)
        self.entry.add_controller(keys)
        self._show()

    def _fmt(self, v):
        return f"{v:.{self.digits}f}"

    def _show(self):
        self.label.set_text(self._fmt(self.value) + (f" {self.unit}" if self.unit else ""))

    def _clamp(self, v):
        if self.lo is not None:
            v = max(self.lo, v)
        if self.hi is not None:
            v = min(self.hi, v)
        return round(v, max(self.digits, 3))

    def set_value(self, v):
        v = float(v)
        if v != self.value:
            self.value = v
            self._show()

    def _begin(self, gesture, x, y):
        self._start, self._moved = self.value, False

    def _update(self, gesture, dx, dy):
        if abs(dx) < 3 and not self._moved:
            return
        self._moved = True
        state = gesture.get_current_event_state()
        mult = 0.1 if state & Gdk.ModifierType.SHIFT_MASK else 10.0 if state & Gdk.ModifierType.CONTROL_MASK else 1.0
        v = self._clamp(self._start + dx * self.step * mult * 0.5)
        if v != self.value:
            self.value = v
            self._show()
            self.emit("changed", v, False)

    def _end(self, gesture, dx, dy):
        if self._moved:
            self.emit("changed", self.value, True)
        else:
            self.entry.set_text(self._fmt(self.value))
            self.set_visible_child_name("entry")
            self.entry.grab_focus()

    def _commit(self, entry):
        try:
            v = self._clamp(float(entry.get_text().replace(",", ".").split()[0]))
        except (ValueError, IndexError):
            v = self.value
        self.set_visible_child_name("label")
        self.value = v
        self._show()
        self.emit("changed", v, True)

    def _entry_key(self, ctrl, keyval, code, state):
        if keyval == Gdk.KEY_Escape:
            self.set_visible_child_name("label")
            return True
        return False


class TimecodeField(Gtk.Stack):
    """Timecode readout: click to type a position, drag to scrub.

    Emits ``seek(frame)``. ``fps`` is a callable returning (num, den).
    """

    __gsignals__ = {"seek": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self, fps, dim=False, editable=True):
        super().__init__()
        self.fps = fps
        self.frame = 0
        self.set_hhomogeneous(False)
        self.label = Gtk.Label(label="00:00:00:00")
        self.label.add_css_class("timecode")
        if dim:
            self.label.add_css_class("dim")
        self.entry = Gtk.Entry()
        self.entry.add_css_class("timecode-entry")
        self.entry.set_width_chars(11)
        self.entry.set_max_width_chars(11)
        self.add_named(self.label, "label")
        self.add_named(self.entry, "entry")
        if editable:
            self.label.set_cursor(Gdk.Cursor.new_from_name("ew-resize"))
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._begin)
            drag.connect("drag-update", self._update)
            drag.connect("drag-end", self._end)
            self.label.add_controller(drag)
            self.entry.connect("activate", self._commit)
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", lambda *_: self.set_visible_child_name("label"))
            self.entry.add_controller(focus)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._key)
            self.entry.add_controller(keys)

    def set_frame(self, frame):
        frame = int(frame)
        num, den = self.fps()
        text = tc.frames_to_tc(frame, num, den)
        self.frame = frame
        if self.label.get_text() != text:
            self.label.set_text(text)

    def _begin(self, g, x, y):
        self._start, self._moved = self.frame, False

    def _update(self, g, dx, dy):
        if abs(dx) < 3 and not self._moved:
            return
        self._moved = True
        self.emit("seek", max(0, int(self._start + dx / 2)))

    def _end(self, g, dx, dy):
        if not self._moved:
            self.entry.set_text(self.label.get_text())
            self.set_visible_child_name("entry")
            self.entry.grab_focus()

    def _commit(self, entry):
        num, den = self.fps()
        parsed = tc.tc_to_frames(entry.get_text(), num, den)
        self.set_visible_child_name("label")
        if parsed is not None:
            frames, relative = parsed
            self.emit("seek", max(0, self.frame + frames if relative else frames))

    def _key(self, ctrl, keyval, code, state):
        if keyval == Gdk.KEY_Escape:
            self.set_visible_child_name("label")
            return True
        return False


class PanelGroup(Gtk.Box):
    """A frame holding one or more panels as tabs."""

    def __init__(self, window, on_focus=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.on_focus = on_focus
        self.add_css_class("panel-group")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.tabs = Gtk.Box(spacing=0)
        self.tabs.add_css_class("panel-tabs")
        self.extra = Gtk.Box(spacing=2, halign=Gtk.Align.END, hexpand=True)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.add_css_class("panel-body")
        self.append(self.tabs)
        self.append(self.stack)
        self.buttons = {}
        self.keys = []
        click = Gtk.GestureClick(button=0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self.focus_panel())
        self.add_controller(click)

    def add(self, key, title, widget):
        btn = Gtk.ToggleButton(label=title)
        btn.add_css_class("panel-tab")
        btn.set_focusable(False)
        if self.buttons:
            btn.set_group(next(iter(self.buttons.values())))
        btn.connect("toggled", lambda b: b.get_active() and self._switch(key))
        if self.extra.get_parent() is self.tabs:
            self.tabs.remove(self.extra)
        self.tabs.append(btn)
        self.tabs.append(self.extra)
        self.stack.add_named(widget, key)
        self.buttons[key] = btn
        self.keys.append(key)
        if len(self.buttons) == 1:
            btn.set_active(True)

    def _switch(self, key):
        self.stack.set_visible_child_name(key)
        self.focus_panel()

    def show(self, key):
        if key in self.buttons:
            self.buttons[key].set_active(True)

    def current(self):
        return self.stack.get_visible_child_name()

    def focus_panel(self):
        if self.on_focus:
            self.on_focus(self, self.current())


# -- dialogs ---------------------------------------------------------------
def alert(parent, message, detail=""):
    dlg = Gtk.AlertDialog(message=message, detail=detail)
    dlg.set_modal(True)
    dlg.show(parent)


def confirm(parent, message, detail, ok_label, callback, cancel_label="Cancel", extra=None):
    """Ask a question; ``callback(index)`` gets the chosen button index
    (0 = cancel, 1 = ok, 2 = extra)."""
    buttons = [cancel_label, ok_label] + ([extra] if extra else [])
    dlg = Gtk.AlertDialog(message=message, detail=detail, buttons=buttons)
    dlg.set_cancel_button(0)
    dlg.set_default_button(1)
    dlg.set_modal(True)

    def done(d, result):
        try:
            callback(d.choose_finish(result))
        except GLib.Error:
            callback(0)

    dlg.choose(parent, None, done)


def ask_text(parent, title, prompt, initial, callback):
    win = Gtk.Window(title=title, transient_for=parent, modal=True, resizable=False,
                     application=parent.get_application() if parent is not None else None)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("dialog-body")
    box.append(label(prompt))
    entry = Gtk.Entry(text=initial, width_chars=36)
    box.append(entry)
    row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
    cancel = Gtk.Button(label="Cancel")
    ok = Gtk.Button(label="OK")
    ok.add_css_class("suggested-action")
    row.append(cancel)
    row.append(ok)
    box.append(row)
    win.set_child(box)

    def accept(*_):
        text = entry.get_text().strip()
        win.destroy()
        if text:
            callback(text)

    cancel.connect("clicked", lambda *_: win.destroy())
    ok.connect("clicked", accept)
    entry.connect("activate", accept)
    win.present()
    entry.grab_focus()
    return win


def file_filter(name, patterns):
    f = Gtk.FileFilter()
    f.set_name(name)
    for p in patterns:
        f.add_pattern(p)
    return f


def open_files(parent, title, callback, filters=None, multiple=True, folder=None):
    dlg = Gtk.FileDialog(title=title, modal=True)
    if filters:
        store = Gio.ListStore.new(Gtk.FileFilter)
        for f in filters:
            store.append(f)
        dlg.set_filters(store)
        dlg.set_default_filter(filters[0])
    if folder:
        dlg.set_initial_folder(Gio.File.new_for_path(folder))

    def done(d, result):
        try:
            if multiple:
                files = d.open_multiple_finish(result)
                paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            else:
                paths = [d.open_finish(result).get_path()]
        except GLib.Error:
            return
        callback([p for p in paths if p])

    if multiple:
        dlg.open_multiple(parent, None, done)
    else:
        dlg.open(parent, None, done)


def save_file(parent, title, name, callback, filters=None, folder=None):
    dlg = Gtk.FileDialog(title=title, modal=True, initial_name=name)
    if filters:
        store = Gio.ListStore.new(Gtk.FileFilter)
        for f in filters:
            store.append(f)
        dlg.set_filters(store)
    if folder:
        dlg.set_initial_folder(Gio.File.new_for_path(folder))

    def done(d, result):
        try:
            path = d.save_finish(result).get_path()
        except GLib.Error:
            return
        if path:
            callback(path)

    dlg.save(parent, None, done)


def popup_menu(widget, model, x, y, autohide=True):
    """Show a context menu built from a Gio.Menu at widget coordinates."""
    # Popovers are only positioned automatically under a parent that uses a
    # layout manager, so custom-drawn widgets hand the menu to an ancestor.
    parent = widget
    while parent is not None and parent.get_layout_manager() is None:
        parent = parent.get_parent()
    if parent is None:
        parent = widget
    elif parent is not widget:
        ok, pt = widget.compute_point(parent, Graphene.Point().init(x, y))
        if ok:
            x, y = pt.x, pt.y
    pop = Gtk.PopoverMenu.new_from_model(model)
    pop.set_parent(parent)
    pop.set_has_arrow(False)
    pop.set_halign(Gtk.Align.START)
    pop.set_autohide(autohide)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
    pop.popup()
    return pop
