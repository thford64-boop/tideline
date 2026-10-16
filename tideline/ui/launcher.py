"""Start-up screens: the first-run setup (layout, playback quality, first
project), the project picker shown on later starts, the New Project dialog
and Preferences."""

import os
import time

from gi.repository import Gio, GLib, Gtk, Pango, PangoCairo

from .. import APP_ID, APP_NAME, PROJECT_EXT, VERSION
from .. import settings as st
from ..model import DEFAULT_SETTINGS
from . import theme
from .dialogs import FRAME_RATES, SAMPLE_RATES, SEQUENCE_PRESETS, FrameSizeRows, _Dialog
from .widgets import alert, file_filter, label, open_files
from .window import WORKSPACES

LAYOUT_TEXT = {
    "editing": "Source and Program monitors side by side over the Project panel and Timeline. "
               "The classic cutting layout.",
    "effects": "Effect Controls next to the Program monitor and the Effects browser beside the "
               "Timeline, for grading and animation.",
    "audio": "The Audio Mixer up top with a taller Timeline, for mixing levels and balance.",
}
QUALITY_TEXT = {
    "performance": "Smoothest playback on any machine: quarter-resolution preview, fast scaling, "
                   "frames dropped to keep time, two render threads.",
    "balanced": "Half-resolution preview with smooth scaling. A good default for most computers.",
    "quality": "Full-resolution, sharp preview that shows every frame. Needs a fast machine.",
}
TAB_NAMES = {"source": "Source", "effect_controls": "Effect Controls", "mixer": "Audio Mixer",
             "project": "Project", "effects": "Effects"}


# -- pieces ------------------------------------------------------------------
class LayoutPreview(Gtk.DrawingArea):
    """A miniature of a workspace drawn from the same proportions the window uses."""

    def __init__(self, key):
        super().__init__()
        self.key = key
        self.set_content_width(220)
        self.set_content_height(130)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        ws = WORKSPACES[self.key]
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        g = 3
        top_h = (h - 3 * g) * ws["top"]
        left_w = (w - 3 * g) * ws["left"]
        bl_w = (w - 3 * g) * ws["bottom_left"]
        y2 = g + top_h + g
        panels = [
            (g, g, left_w, top_h, TAB_NAMES[ws["tabs"]["a"]], theme.BG2),
            (2 * g + left_w, g, w - 3 * g - left_w, top_h, "Program", theme.BG2),
            (g, y2, bl_w, h - y2 - g, TAB_NAMES[ws["tabs"]["c"]], theme.BG1),
            (2 * g + bl_w, y2, w - 3 * g - bl_w, h - y2 - g, "Timeline", theme.BG1),
        ]
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 9px"))
        for i, (x, y, pw, ph, name, bg) in enumerate(panels):
            cr.set_source_rgba(*theme.rgb(bg))
            cr.rectangle(x, y, pw, ph)
            cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.ACCENT if i in (0, 2) else theme.BG4))
            cr.rectangle(x, y, pw, 2)
            cr.fill()
            if name == "Program" or name == "Source":
                # a picture inside the monitor
                cr.set_source_rgba(*theme.rgb(theme.BG0))
                cr.rectangle(x + pw * 0.15, y + 14, pw * 0.7, max(4, ph - 26))
                cr.fill()
            elif name == "Timeline":
                for row in range(4):
                    ty = y + 16 + row * 9
                    if ty + 6 > y + ph:
                        break
                    cr.set_source_rgba(*theme.rgb(_lane_colour(row)))
                    cr.rectangle(x + 8 + row * 9, ty, pw * 0.45, 6)
                    cr.fill()
                cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
                cr.rectangle(x + pw * 0.38, y + 12, 1.2, ph - 14)
                cr.fill()
            layout.set_text(name, -1)
            layout.set_width(int((pw - 8) * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM))
            cr.move_to(x + 4, y + 3)
            PangoCairo.show_layout(cr, layout)


def _lane_colour(row):
    return ("#3b79a8", "#3c8a67", "#3b79a8", "#23968d")[row % 4]


def choice_card(title, text, preview=None, group=None):
    """A large toggle button: optional preview, a title and a description."""
    btn = Gtk.ToggleButton()
    btn.add_css_class("choice-card")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    if preview is not None:
        box.append(preview)
    box.append(label(title, css="heading"))
    box.append(label(text, css="dim", wrap=True, max_width_chars=28, valign=Gtk.Align.START))
    btn.set_child(box)
    if group is not None:
        btn.set_group(group)
    return btn


class NewProjectForm(Gtk.Box):
    """Name, location and format of a new project."""

    def __init__(self, app, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.window = window
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.append(self.grid)
        self._row = 0
        self.name = self.add_row("Name", Gtk.Entry(text=self._free_name(), width_chars=32, activates_default=True))
        where = Gtk.Box(spacing=6)
        self.folder = Gtk.Entry(text=app.prefs["project_dir"], hexpand=True, width_chars=32)
        browse = Gtk.Button(label="Choose…")
        browse.connect("clicked", self._browse)
        where.append(self.folder)
        where.append(browse)
        self.add_row("Location", where)
        self.preset = self.add_row("Format", Gtk.DropDown.new_from_strings(
            ["Custom"] + [p[0] for p in SEQUENCE_PRESETS]))
        d = DEFAULT_SETTINGS
        self.frame = FrameSizeRows(self, d["width"], d["height"])
        self.fps = self.add_row("Frame rate", Gtk.DropDown.new_from_strings([f[0] + " fps" for f in FRAME_RATES]))
        self.fps.set_selected(next(i for i, f in enumerate(FRAME_RATES) if (f[1], f[2]) == (d["fps_num"], d["fps_den"])))
        self.rate = self.add_row("Audio sample rate", Gtk.DropDown.new_from_strings(
            [f"{r} Hz" for r in SAMPLE_RATES]))
        self.rate.set_selected(SAMPLE_RATES.index(d["sample_rate"]))
        self.preset.set_selected(1)
        self.preset.connect("notify::selected", self._preset)
        self.where = label("", css="faint", ellipsize=True)
        self.error = label("", css="error-text", wrap=True)
        self.append(self.where)
        self.append(self.error)
        for w in (self.name, self.folder):
            w.connect("changed", lambda *_: self._update())
        self._update()

    def add_row(self, text, widget):
        self.grid.attach(label(text, css="dim", xalign=1.0), 0, self._row, 1, 1)
        self.grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return widget

    def _free_name(self):
        base, n = "My Project", 1
        folder = self.app.prefs["project_dir"]
        name = base
        while os.path.exists(os.path.join(folder, name)):
            n += 1
            name = f"{base} {n}"
        return name

    def _preset(self, drop, _p):
        i = drop.get_selected()
        if i == 0:
            return
        _, w, h, num, den = SEQUENCE_PRESETS[i - 1]
        self.frame.set(w, h)
        self.fps.set_selected(next(k for k, f in enumerate(FRAME_RATES) if (f[1], f[2]) == (num, den)))

    def _browse(self, *_):
        dlg = Gtk.FileDialog(title="Project Location", modal=True)
        start = self.folder.get_text().strip()
        while start and not os.path.isdir(start):
            start = os.path.dirname(start)
        if start:
            dlg.set_initial_folder(Gio.File.new_for_path(start))

        def done(d, result):
            try:
                path = d.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            if path:
                self.folder.set_text(path)

        dlg.select_folder(self.window, None, done)

    def _update(self):
        path, error = self.app.project_target(self.name.get_text(), self.folder.get_text())
        self.where.set_text(f"Saved as {path}" if path else "")
        self.error.set_text(error or "")
        return path, error

    def submit(self):
        """Arguments for ``App.create_project``, or None (the error is shown)."""
        path, error = self._update()
        if error:
            return None
        w, h = self.frame.get()
        _, num, den = FRAME_RATES[self.fps.get_selected()]
        args = (self.name.get_text(), self.folder.get_text(), w, h, num, den, SAMPLE_RATES[self.rate.get_selected()])
        return args

    def create(self, args):
        error = self.app.create_project(*args)
        if error:
            alert(self.app.window, "Could not create the project", error)


# -- the start-up window -----------------------------------------------------
class Launcher(Gtk.Window):
    """First run (``setup=True``): welcome → layout → playback → first
    project. Afterwards: recent projects, New Project and Open."""

    def __init__(self, app, setup=False):
        super().__init__(title=APP_NAME, application=app, default_width=980, default_height=640)
        self.app = app
        self.setup = setup
        if app.window is not None:
            self.set_transient_for(app.window)
            self.set_modal(True)
        self.add_css_class("launcher")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=160,
                               vexpand=True, hexpand=True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.stack)
        self.footer = Gtk.Box(spacing=8)
        self.footer.add_css_class("launcher-footer")
        root.append(self.footer)
        self.set_child(root)
        self.form = NewProjectForm(app, self)
        if setup:
            self._build_setup()
        else:
            self._build_home()

    # -- shared ----------------------------------------------------------
    def _page(self, title, subtitle, body):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=48, margin_end=48,
                       margin_top=36, margin_bottom=12)
        page.append(label(title, css="launcher-title"))
        if subtitle:
            page.append(label(subtitle, css="dim", wrap=True))
        body.set_margin_top(18)
        body.set_vexpand(True)
        page.append(body)
        return page

    def _clear_footer(self):
        child = self.footer.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.footer.remove(child)
            child = nxt

    def _finish(self, action):
        self.app.prefs.save()
        self.app.apply_playback_prefs()
        self.app.launcher_done(action)

    # -- first run -------------------------------------------------------
    STEPS = ["welcome", "layout", "playback", "project"]

    def _build_setup(self):
        prefs = self.app.prefs
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        logo = Gtk.Image.new_from_icon_name(APP_ID)
        logo.set_pixel_size(112)
        welcome.append(logo)
        welcome.append(label(f"Welcome to {APP_NAME}", css="launcher-hero", xalign=0.5))
        welcome.append(label("A few choices and you are ready to edit. You can change all of them later\n"
                             "in Edit ▸ Preferences and the Window menu.", css="dim", xalign=0.5,
                             justify=Gtk.Justification.CENTER))
        self.stack.add_named(welcome, "welcome")

        cards = Gtk.Box(spacing=14, homogeneous=True, valign=Gtk.Align.START)
        first = None
        for key, name in st.LAYOUTS:
            card = choice_card(name, LAYOUT_TEXT[key], LayoutPreview(key), first)
            first = first or card
            card.set_active(prefs["layout"] == key)
            card.connect("toggled", lambda b, k=key: b.get_active() and prefs.update(layout=k))
            cards.append(card)
        layout_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        layout_page.append(cards)
        full = Gtk.CheckButton(label="Open the editor full screen", active=prefs["fullscreen"])
        full.connect("toggled", lambda b: prefs.update(fullscreen=b.get_active()))
        layout_page.append(full)
        self.stack.add_named(self._page("Choose your layout", "How the panels are arranged when the editor "
                                        "opens. Switch any time with the buttons at the top of the window.",
                                        layout_page), "layout")

        qcards = Gtk.Box(spacing=14, homogeneous=True, valign=Gtk.Align.START)
        first = None
        current = st.matching_preset(prefs)
        for key, name in st.QUALITY_NAMES:
            card = choice_card(name, QUALITY_TEXT[key], None, first)
            first = first or card
            card.set_active(current == key or (current == "custom" and key == "balanced"))
            card.connect("toggled", lambda b, k=key: b.get_active() and prefs.apply_preset(k))
            qcards.append(card)
        if current == "custom":
            prefs.apply_preset("balanced")
        self.stack.add_named(self._page("Playback quality", "Lower quality previews play back smoothly on "
                                        "slower computers. Exports always render at full quality.", qcards),
                             "playback")

        self.stack.add_named(self._page("Your first project", "Projects are saved in their own folder, "
                                        "so the project and its media can be moved together.", self.form),
                             "project")
        self._go(0)

    def _go(self, i):
        self.step = i
        self.stack.set_visible_child_name(self.STEPS[i])
        self._clear_footer()
        if i == 0:
            skip = Gtk.Button(label="Skip setup")
            skip.add_css_class("flat")
            skip.set_tooltip_text("Use the default layout and playback settings with an empty, unsaved project")
            skip.connect("clicked", lambda *_: self._skip())
            self.footer.append(skip)
        else:
            back = Gtk.Button(label="Back")
            back.connect("clicked", lambda *_: self._go(i - 1))
            self.footer.append(back)
        dots = Gtk.Box(spacing=6, hexpand=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        for k in range(len(self.STEPS)):
            dot = Gtk.Box()
            dot.add_css_class("step-dot")
            if k == i:
                dot.add_css_class("current")
            dots.append(dot)
        self.footer.append(dots)
        last = i == len(self.STEPS) - 1
        nxt = Gtk.Button(label="Create Project" if last else ("Get Started" if i == 0 else "Next"))
        nxt.add_css_class("suggested-action")
        nxt.connect("clicked", lambda *_: self._create() if last else self._go(i + 1))
        self.footer.append(nxt)
        self.set_default_widget(nxt)

    def _skip(self):
        self.app.prefs["setup_done"] = True
        self._finish(lambda: None)

    def _create(self):
        args = self.form.submit()
        if not args:
            return
        self.app.prefs["setup_done"] = True
        self._finish(lambda: self.form.create(args))

    # -- later starts: project picker ------------------------------------
    def _build_home(self):
        app = self.app
        home = Gtk.Box(vexpand=True)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.add_css_class("launcher-side")
        side.set_size_request(260, -1)
        logo = Gtk.Image.new_from_icon_name(APP_ID)
        logo.set_pixel_size(72)
        logo.set_halign(Gtk.Align.START)
        side.append(logo)
        side.append(label(APP_NAME, css="launcher-hero"))
        side.append(label(f"Version {VERSION}", css="faint"))
        side.append(Gtk.Box(height_request=18))
        new = Gtk.Button(label="New Project…")
        new.add_css_class("suggested-action")
        new.add_css_class("launcher-action")
        new.connect("clicked", lambda *_: self._show_new())
        side.append(new)
        opn = Gtk.Button(label="Open Project…")
        opn.add_css_class("launcher-action")
        opn.connect("clicked", lambda *_: self._open_other())
        side.append(opn)
        if app.window is None:
            blank = Gtk.Button(label="Empty Project (Unsaved)")
            blank.add_css_class("launcher-action")
            blank.add_css_class("flat")
            blank.connect("clicked", lambda *_: self._finish(lambda: None))
            side.append(blank)
        side.append(Gtk.Box(vexpand=True))
        prefs_btn = Gtk.Button(label="Preferences…")
        prefs_btn.add_css_class("flat")
        prefs_btn.connect("clicked", lambda *_: PreferencesDialog(app, parent=self).present())
        side.append(prefs_btn)
        show = Gtk.CheckButton(label="Show at startup", active=app.prefs["show_launcher"])
        show.connect("toggled", lambda b: (app.prefs.update(show_launcher=b.get_active()), app.prefs.save()))
        side.append(show)
        home.append(side)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, margin_start=28,
                        margin_end=28, margin_top=28, margin_bottom=12)
        right.append(label("Recent projects", css="launcher-title"))
        self.search = Gtk.SearchEntry(placeholder_text="Filter projects")
        self.search.connect("search-changed", lambda *_: self.recent.invalidate_filter())
        right.append(self.search)
        self.recent = Gtk.ListBox()
        self.recent.add_css_class("recent-list")
        self.recent.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.recent.connect("row-activated", lambda lb, row: self._open(row.path))
        self.recent.set_filter_func(self._filter)
        self.recent.set_placeholder(label("No recent projects yet. Create one, or open an existing\n"
                                          f"{PROJECT_EXT} file.", css="hint", xalign=0.5,
                                          justify=Gtk.Justification.CENTER, margin_top=40))
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.recent)
        right.append(scroll)
        home.append(right)
        self._fill_recent()
        self.stack.add_named(home, "home")
        self.stack.add_named(self._page("New project", "The project gets its own folder at this location, "
                                        "with one sequence in the format you pick.", self.form), "new")
        self._show_home()

    def _fill_recent(self):
        self.recent.remove_all()
        for path, exists in self.app.prefs.recent_projects():
            row = Gtk.ListBoxRow()
            row.path = path
            row.add_css_class("recent-row")
            box = Gtk.Box(spacing=10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            name = os.path.splitext(os.path.basename(path))[0]
            text.append(label(name, css="heading" if exists else "heading faint", ellipsize=True))
            text.append(label(path, css="faint", ellipsize=True))
            box.append(text)
            if exists:
                stamp = time.strftime("%d %b %Y, %H:%M", time.localtime(os.path.getmtime(path)))
                box.append(label(stamp, css="dim"))
            else:
                box.append(label("missing", css="warning-text"))
            forget = Gtk.Button(label="✕", tooltip_text="Remove from this list")
            forget.add_css_class("flat")
            forget.connect("clicked", lambda b, p=path: (self.app.prefs.remove_recent(p), self._fill_recent()))
            box.append(forget)
            row.set_child(box)
            row.set_sensitive(True)
            self.recent.append(row)

    def _filter(self, row):
        text = self.search.get_text().strip().lower()
        return not text or text in row.path.lower()

    def _show_home(self):
        self.stack.set_visible_child_name("home")
        self._clear_footer()
        self.footer.append(label("Click a project to open it.", css="faint", hexpand=True))
        cancel = Gtk.Button(label="Close" if self.app.window is not None else "Quit")
        cancel.connect("clicked", lambda *_: self.close())
        self.footer.append(cancel)

    def _show_new(self):
        self.stack.set_visible_child_name("new")
        self._clear_footer()
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self._show_home())
        self.footer.append(back)
        self.footer.append(Gtk.Box(hexpand=True))
        create = Gtk.Button(label="Create Project")
        create.add_css_class("suggested-action")
        create.connect("clicked", lambda *_: self._create_from_home())
        self.footer.append(create)
        self.set_default_widget(create)
        self.form.name.grab_focus()

    def _create_from_home(self):
        args = self.form.submit()
        if args:
            app = self.app
            if app.window is not None:
                # Opened from the editor: the current project may need saving first.
                self.destroy()
                return app._guard_unsaved(lambda: self.form.create(args))
            self._finish(lambda: self.form.create(args))

    def _open(self, path):
        if not os.path.exists(path):
            return alert(self, "The project file is missing", f"{path} was moved or deleted.")
        if self.app.window is not None:
            self.destroy()
            return self.app.open_recent(path)
        self._finish(lambda: self.app.load_project(path))

    def _open_other(self):
        open_files(self, "Open Project", lambda paths: self._open(paths[0]),
                   [file_filter(f"{APP_NAME} projects", ["*" + PROJECT_EXT])], multiple=False,
                   folder=self.app.prefs["project_dir"] if os.path.isdir(self.app.prefs["project_dir"]) else None)


class NewProjectDialog(_Dialog):
    """File ▸ New Project: the same form as the start-up screen."""

    def __init__(self, app):
        super().__init__(app, "New Project", "Create")
        self.form = NewProjectForm(app, self)
        self.grid.attach(self.form, 0, 0, 1, 1)
        blank = Gtk.Button(label="Empty Project (Unsaved)")
        blank.add_css_class("flat")
        blank.set_tooltip_text("Start an untitled project and choose where to save it later")
        blank.connect("clicked", lambda *_: (self.close(), app.new_blank_project()))
        self.cancel_btn.get_parent().prepend(blank)

    def accept(self):
        args = self.form.submit()
        if args:
            self.close()
            self.form.create(args)


class PreferencesDialog(_Dialog):
    """Playback quality and start-up options."""

    def __init__(self, app, parent=None):
        super().__init__(app, "Preferences")
        if parent is not None:
            self.set_transient_for(parent)
        p = app.prefs
        self._busy = False
        self.grid.attach(label("Playback", css="heading"), 0, self._row, 2, 1)
        self._row += 1
        self.quality = self.add_row("Quality", Gtk.DropDown.new_from_strings(
            [n for _, n in st.QUALITY_NAMES] + ["Custom"]))
        self.scale = self.add_row("Preview resolution", Gtk.DropDown.new_from_strings(
            [n for n, _ in st.PREVIEW_SCALES]))
        self.scaling = self.add_row("Preview scaling", Gtk.DropDown.new_from_strings([n for _, n in st.SCALING]))
        self.threads = self.add_row("Render threads", Gtk.DropDown.new_from_strings([n for _, n in st.THREADS]))
        self.threads.set_tooltip_text("Render several preview frames at once. Faster on multi-core machines; "
                                      "a few effects may flicker with more than one thread.")
        self.drop = self.add_row("Drop late frames", Gtk.Switch(halign=Gtk.Align.START))
        self.drop.set_tooltip_text("Skip frames that are not ready in time so playback keeps pace with the "
                                   "audio. Off shows every frame, but may stutter.")
        self.ui_fps = self.add_row("Interface updates", Gtk.DropDown.new_from_strings(
            [f"{r} per second" for r in st.UI_RATES]))
        self.ui_fps.set_tooltip_text("How often the timeline, timecodes and panels follow the playhead while "
                                     "playing. The picture itself always plays at full frame rate.")
        self.live = self.add_row("Effect Controls while playing", Gtk.Switch(halign=Gtk.Align.START))
        self.live.set_tooltip_text("Update parameter values during playback (they always update when stopped)")

        self.grid.attach(label("Start-up", css="heading", margin_top=10), 0, self._row, 2, 1)
        self._row += 1
        self.layout = self.add_row("Layout", Gtk.DropDown.new_from_strings([n for _, n in st.LAYOUTS]))
        self.full = self.add_row("Open full screen", Gtk.Switch(halign=Gtk.Align.START))
        self.launch = self.add_row("Show project manager", Gtk.Switch(halign=Gtk.Align.START))

        self._load(p)
        self.quality.connect("notify::selected", self._quality_picked)
        for w in (self.scale, self.scaling, self.threads, self.ui_fps):
            w.connect("notify::selected", self._detail_changed)
        for w in (self.drop, self.live):
            w.connect("notify::active", self._detail_changed)
        self.extra.append(label("Exports always render at full quality.", css="faint"))

    def _load(self, values):
        self._busy = True
        scales = [v for _, v in st.PREVIEW_SCALES]
        self.scale.set_selected(min(range(len(scales)), key=lambda i: abs(scales[i] - values["preview_scale"])))
        self.scaling.set_selected(next((i for i, (k, _) in enumerate(st.SCALING) if k == values["scaling"]), 1))
        self.threads.set_selected(next((i for i, (k, _) in enumerate(st.THREADS) if k == values["threads"]), 0))
        self.drop.set_active(bool(values["drop_frames"]))
        self.ui_fps.set_selected(min(range(len(st.UI_RATES)), key=lambda i: abs(st.UI_RATES[i] - values["ui_fps"])))
        self.live.set_active(bool(values["live_values"]))
        if "layout" in values:
            self.layout.set_selected(next((i for i, (k, _) in enumerate(st.LAYOUTS) if k == values["layout"]), 0))
            self.full.set_active(bool(values["fullscreen"]))
            self.launch.set_active(bool(values["show_launcher"]))
        self._show_quality()
        self._busy = False

    def _values(self):
        return {"preview_scale": st.PREVIEW_SCALES[self.scale.get_selected()][1],
                "scaling": st.SCALING[self.scaling.get_selected()][0],
                "threads": st.THREADS[self.threads.get_selected()][0],
                "drop_frames": self.drop.get_active(),
                "ui_fps": st.UI_RATES[self.ui_fps.get_selected()],
                "live_values": self.live.get_active()}

    def _show_quality(self):
        name = st.matching_preset(self._values())
        names = [k for k, _ in st.QUALITY_NAMES]
        self._busy, was = True, self._busy
        self.quality.set_selected(names.index(name) if name in names else len(names))
        self._busy = was

    def _quality_picked(self, *_):
        if self._busy:
            return
        i = self.quality.get_selected()
        if i < len(st.QUALITY_NAMES):
            self._load(st.QUALITY_PRESETS[st.QUALITY_NAMES[i][0]])

    def _detail_changed(self, *_):
        if not self._busy:
            self._show_quality()

    def accept(self):
        app, p = self.app, self.app.prefs
        values = self._values()
        rescale = abs(values["preview_scale"] - p["preview_scale"]) > 1e-9
        p.update(values)
        p["quality"] = st.matching_preset(values)
        p["layout"] = st.LAYOUTS[self.layout.get_selected()][0]
        p["fullscreen"] = self.full.get_active()
        p["show_launcher"] = self.launch.get_active()
        p.save()
        # A changed default resolution resets both monitors; otherwise a
        # resolution picked on one monitor stays.
        app.apply_playback_prefs(scale_monitors=rescale)
        app.status("Preferences saved")
        self.close()
