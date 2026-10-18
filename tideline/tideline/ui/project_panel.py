"""Project panel: the bin of imported media, sequences and generated items."""

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from .. import timecode as tc
from ..model import LABELS
from .icons import MEDIA_ICONS, Icon
from .widgets import ask_text, confirm, icon_button, label, popup_menu, spacer


class MediaRow(GObject.Object):
    def __init__(self, item):
        super().__init__()
        self.item = item


def _draw_chip(area, cr, w, h):
    from . import theme
    cr.set_source_rgba(*theme.rgb(area.color))
    cr.rectangle(0, 0, w, h)
    cr.fill()


def _fps_text(m):
    return f"{m.fps:.2f} fps" if m.fps else ""


def _duration_text(m, project):
    if m.kind == "sequence":
        seq = project.sequences.get(m.sequence_id)
        return tc.frames_to_tc(seq.end(), seq.fps_num, seq.fps_den) if seq else ""
    if m.is_bin:
        return f"{len(project.children(m.id))} items"
    return tc.format_duration(m.duration) if m.duration else ""


def _video_text(m, project=None):
    if m.is_bin or not m.has_video:
        return ""
    inner = project.multicam_of(m) if project is not None else None
    if inner is not None:
        return f"Multi-camera, {len(inner.video_tracks)} cameras"
    size = f"{m.width}×{m.height}" if m.width else ""
    return " ".join(x for x in (size, m.video_codec) if x) or m.kind.capitalize()


def _audio_text(m):
    if not m.has_audio or m.kind == "sequence":
        return ""
    rate = f"{m.sample_rate / 1000:g} kHz" if m.sample_rate else ""
    ch = {1: "Mono", 2: "Stereo"}.get(m.channels, f"{m.channels} ch" if m.channels else "")
    return " ".join(x for x in (rate, ch) if x) or "Audio"


class ProjectPanel(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.current_bin = None
        self._thumbs = {}            # bound Gtk.Picture -> media item
        self._proxy_cells = {}       # bound Proxy column label -> media item

        top = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=5, margin_bottom=5)
        self.up_btn = icon_button("up", "Up to parent bin", lambda b: self.enter(self._parent_of_current()))
        self.path = label("", css="dim", ellipsize=True)
        self.search = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.refresh())
        top.append(self.up_btn)
        top.append(self.path)
        top.append(self.search)
        self.append(top)

        self.store = Gio.ListStore.new(MediaRow)
        self.selection = Gtk.MultiSelection.new(self.store)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self._build_list(), "list")
        self.stack.add_named(self._build_grid(), "grid")
        self.append(self.stack)
        self.empty = label("Import media to begin  (Ctrl+I, or drop files here)", css="hint", xalign=0.5)
        self.empty.set_wrap(True)
        self.stack.add_named(self.empty, "empty")

        bottom = Gtk.Box(spacing=2, margin_start=4, margin_end=4, margin_top=2, margin_bottom=3)
        self.list_btn = icon_button("list", "List view", lambda b: self._set_view("list"), toggle=True)
        self.grid_btn = icon_button("grid", "Icon view", lambda b: self._set_view("grid"), toggle=True)
        self.grid_btn.set_group(self.list_btn)
        self.list_btn.set_active(True)
        self.count = label("", css="faint")
        bottom.append(self.list_btn)
        bottom.append(self.grid_btn)
        bottom.append(spacer())
        bottom.append(self.count)
        bottom.append(icon_button("import", "Import media (Ctrl+I)", lambda b: app.do("import")))
        bottom.append(icon_button("folder", "New bin (Ctrl+B)", lambda b: app.do("new_bin")))
        new_menu = Gio.Menu()
        for text, action in (("Sequence…", "app.new_sequence"), ("Title", "app.new_title"),
                             ("Colour Matte…", "app.new_color_matte"), ("Black Video", "app.new_black"),
                             ("Bars and Tone", "app.new_bars")):
            new_menu.append(text, action)
        new_btn = Gtk.MenuButton(menu_model=new_menu, tooltip_text="New item", focusable=False)
        new_btn.set_child(Icon("new_item", 16))
        new_btn.add_css_class("icon-button")
        bottom.append(new_btn)
        bottom.append(icon_button("trash", "Delete (Delete)", lambda b: self.delete_selected()))
        self.append(bottom)

        self._view = "list"
        group = Gio.SimpleActionGroup()
        for name, fn in (("open", self._open_selected), ("sequence", self._sequence_from_selected),
                         ("rename", self.rename_selected), ("delete", self.delete_selected),
                         ("relink", self._relink_selected),
                         ("proxy_create", lambda: app.create_proxies(self.selected_items())),
                         ("proxy_attach", lambda: self._focus_do("attach_proxy")),
                         ("proxy_delete", lambda: self._focus_do("delete_proxies")),
                         ("root", lambda: self._move_to(None))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, f=fn: f())
            group.add_action(action)
        move = Gio.SimpleAction.new("move", GLib.VariantType.new("s"))
        move.connect("activate", lambda a, p: self._move_to(p.get_string()))
        group.add_action(move)
        lab = Gio.SimpleAction.new("label", GLib.VariantType.new("i"))
        lab.connect("activate", lambda a, p: self._set_label(p.get_int32()))
        group.add_action(lab)
        self.insert_action_group("proj", group)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._files_dropped)
        self.add_controller(drop)
        for ev in ("media", "project"):
            app.on(ev, self.refresh)
        app.on("sequence", self._sequence_changed)
        app.on("thumbs", self._thumbs_ready)
        app.on("proxies", self._proxies_changed)
        app.on("proxy_progress", lambda job: self._proxies_changed())
        self.refresh()

    # -- views -----------------------------------------------------------
    def _name_cell(self, big=False):
        def setup(factory, li):
            box = Gtk.Box(spacing=6, orientation=Gtk.Orientation.VERTICAL if big else Gtk.Orientation.HORIZONTAL)
            w, h = (132, 74) if big else (38, 22)
            overlay = Gtk.Overlay()
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.set_size_request(w, h)
            pic.add_css_class("thumb")
            icon = Icon("film", 30 if big else 14)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            overlay.set_child(pic)
            overlay.add_overlay(icon)
            overlay.set_size_request(w, h)
            overlay.set_halign(Gtk.Align.CENTER)
            chip = Gtk.DrawingArea(content_width=10, content_height=10, valign=Gtk.Align.CENTER)
            chip.color = "#8b94a2"
            chip.set_draw_func(_draw_chip)
            name = Gtk.Label(xalign=0.5 if big else 0.0, ellipsize=3, hexpand=True)
            if big:
                name.set_max_width_chars(16)
                box.append(overlay)
                row = Gtk.Box(spacing=5, halign=Gtk.Align.CENTER)
                row.append(chip)
                row.append(name)
                box.append(row)
            else:
                box.append(chip)
                box.append(overlay)
                box.append(name)
            li.set_child(box)
            li.widgets = (pic, icon, chip, name)

        def bind(factory, li):
            m = li.get_item().item
            pic, icon, chip, name = li.widgets
            name.set_text(m.name + ("  (offline)" if m.offline else ""))
            icon.set_name_("multicam" if self.app.project.multicam_of(m) else MEDIA_ICONS.get(m.kind, "film"))
            chip.color = LABELS[m.label % len(LABELS)][1] if not m.is_bin else "#8b94a2"
            chip.queue_draw()
            tex = self.app.media_cache.thumbnail(m, height=150 if big else 54) if m.path else None
            pic.set_paintable(tex)
            icon.set_visible(tex is None)
            self._thumbs[pic] = (m, 150 if big else 54, icon)

        def unbind(factory, li):
            self._thumbs.pop(li.widgets[0], None)

        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        return f

    def _text_cell(self, fn):
        def setup(factory, li):
            li.set_child(Gtk.Label(xalign=0.0, ellipsize=3))
            li.get_child().add_css_class("dim")

        def bind(factory, li):
            li.get_child().set_text(fn(li.get_item().item))

        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        return f

    def _proxy_cell(self):
        def setup(factory, li):
            li.set_child(Gtk.Label(xalign=0.0, ellipsize=3))
            li.get_child().add_css_class("dim")

        def bind(factory, li):
            m = li.get_item().item
            self._proxy_cells[li.get_child()] = m
            li.get_child().set_text(self.app.proxy_state(m) if m.kind == "video" else "")

        def unbind(factory, li):
            self._proxy_cells.pop(li.get_child(), None)

        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        return f

    def _proxies_changed(self):
        for lab, m in list(self._proxy_cells.items()):
            lab.set_text(self.app.proxy_state(m) if m.kind == "video" else "")

    def _focus_do(self, command):
        self.app.focus = "project"
        self.app.do(command)

    def _controllers(self, view):
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda g, n, x, y: self._context_menu(view, x, y))
        view.add_controller(click)
        src = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        src.connect("prepare", self._drag_prepare)
        src.connect("drag-end", lambda *_: setattr(self.app, "drag_payload", None))
        view.add_controller(src)

    def _build_list(self):
        view = Gtk.ColumnView(model=self.selection, show_row_separators=False, reorderable=False)
        view.connect("activate", lambda v, pos: self._activate(pos))
        project = lambda: self.app.project
        cols = [("Name", self._name_cell(), False, 230),
                ("Frame Rate", self._text_cell(_fps_text), False, 78),
                ("Duration", self._text_cell(lambda m: _duration_text(m, project())), False, 92),
                ("Video", self._text_cell(lambda m: _video_text(m, project())), False, 128),
                ("Audio", self._text_cell(_audio_text), False, 112),
                ("Proxy", self._proxy_cell(), False, 96)]
        for title, factory, expand, width in cols:
            col = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
            if width:
                col.set_fixed_width(width)
            view.append_column(col)
        self._controllers(view)
        self.list_view = view
        sw = Gtk.ScrolledWindow()
        sw.set_child(view)
        return sw

    def _build_grid(self):
        view = Gtk.GridView(model=self.selection, factory=self._name_cell(big=True), max_columns=12,
                            min_columns=1)
        view.connect("activate", lambda v, pos: self._activate(pos))
        self._controllers(view)
        self.grid_view = view
        sw = Gtk.ScrolledWindow()
        sw.set_child(view)
        return sw

    def _set_view(self, name):
        self._view = name
        self._show_stack()

    def _show_stack(self):
        self.stack.set_visible_child_name("empty" if self.store.get_n_items() == 0 else self._view)

    def _thumbs_ready(self):
        for pic, (m, height, icon) in list(self._thumbs.items()):
            if pic.get_paintable() is None and m.path:
                tex = self.app.media_cache.thumbnail(m, height=height)
                if tex is not None:
                    pic.set_paintable(tex)
                    icon.set_visible(False)

    # -- content ---------------------------------------------------------
    def _parent_of_current(self):
        item = self.app.project.media.get(self.current_bin)
        return item.parent if item else None

    def enter(self, bin_id):
        self.current_bin = bin_id if bin_id in self.app.project.media else None
        self.refresh()

    def _sequence_changed(self):
        if self._view == "list":
            # Durations of sequences change as they are edited.
            n = self.store.get_n_items()
            for i in range(n):
                if self.store.get_item(i).item.kind == "sequence":
                    self.refresh()
                    break

    def refresh(self, *_):
        project = self.app.project
        if self.current_bin not in project.media:
            self.current_bin = None
        query = self.search.get_text().strip().lower()
        if query:
            items = [m for m in project.media.values() if query in m.name.lower()]
        else:
            items = project.children(self.current_bin)
        items.sort(key=lambda m: (0 if m.is_bin else 1 if m.kind == "sequence" else 2))
        selected = {self.store.get_item(i).item.id for i in range(self.store.get_n_items())
                    if self.selection.is_selected(i)}
        self.store.splice(0, self.store.get_n_items(), [MediaRow(m) for m in items])
        for i, m in enumerate(items):
            if m.id in selected:
                self.selection.select_item(i, False)
        names, node = [], project.media.get(self.current_bin)
        while node is not None:
            names.append(node.name)
            node = project.media.get(node.parent)
        self.path.set_text(" / ".join(reversed(names)))
        self.path.set_visible(bool(names))
        self.up_btn.set_visible(self.current_bin is not None)
        self.count.set_text(f"{len(items)} item{'s' if len(items) != 1 else ''}  ")
        self._show_stack()

    def selected_items(self):
        return [self.store.get_item(i).item for i in range(self.store.get_n_items())
                if self.selection.is_selected(i)]

    def _activate(self, pos):
        row = self.store.get_item(pos)
        if row is None:
            return
        if row.item.is_bin:
            self.enter(row.item.id)
        else:
            self.app.open_item(row.item)

    def reveal(self, media_id):
        item = self.app.project.media.get(media_id)
        if item is None:
            return
        self.search.set_text("")
        self.enter(item.parent)
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).item.id == media_id:
                self.selection.select_item(i, True)
                self.list_view.scroll_to(i, None, Gtk.ListScrollFlags.FOCUS, None)
        self.app.window.show_panel("project")

    # -- actions ---------------------------------------------------------
    def _push(self, label_):
        app = self.app
        app.history.push(label_, app.project.to_dict())
        app.project.dirty = True
        app.emit("media")
        app.emit("history")

    def _open_selected(self):
        items = self.selected_items()
        if items:
            self._activate(next(i for i in range(self.store.get_n_items())
                                if self.store.get_item(i).item is items[0]))

    def _sequence_from_selected(self):
        items = [m for m in self.selected_items() if not m.is_bin and m.kind != "sequence"]
        if not items:
            return
        app = self.app
        app.project.active_sequence_id = None
        app.selection = []
        app.drop_media(items, 0)
        app.emit("playhead")

    def rename_selected(self):
        items = self.selected_items()
        if not items:
            return
        item = items[0]

        def apply(text):
            item.name = text
            if item.kind == "sequence":
                seq = self.app.project.sequences.get(item.sequence_id)
                if seq is not None:
                    seq.name = text
                self.app.emit("sequence")
            self._push("Rename")

        ask_text(self.app.window, "Rename", "Name", item.name, apply)

    def _relink_selected(self):
        items = [m for m in self.selected_items() if m.path]
        if not items:
            return
        from .widgets import open_files
        item = items[0]
        import os
        folder = os.path.dirname(item.path) if os.path.isdir(os.path.dirname(item.path)) else None
        open_files(self.app.window, f"Relink “{item.name}”", lambda paths: self.app.relink(item, paths[0]),
                   multiple=False, folder=folder)

    def delete_selected(self):
        items = self.selected_items()
        if not items:
            return
        app = self.app
        project = app.project
        used = sum(1 for m in items if project.media_in_use(m.id))

        def remove(choice=1):
            if choice != 1:
                return
            for m in items:
                removed = project.remove_media(m.id)
                for mid in removed:
                    if app.source_media is not None and app.source_media.id == mid:
                        app.source_media = None
                        app.source.set_producer(None, None)
                        app.emit("frame", "source", None, 0, 0)
                        app.emit("source")
            app._prune_selection()
            app.queue_rebuild()
            self._push("Delete Item(s)")
            for ev in ("sequence", "selection", "playhead"):
                app.emit(ev)

        if used or any(m.kind in ("sequence", "bin") for m in items):
            detail = (f"{used} of the selected items are used in a sequence; their clips will be removed too."
                      if used else "Sequences and bin contents are deleted with them.")
            confirm(app.window, f"Delete {len(items)} item(s)?", detail + " This can be undone.", "Delete", remove)
        else:
            remove()

    def _move_to(self, bin_id):
        moved = False
        for m in self.selected_items():
            if m.id != bin_id:
                m.parent = bin_id
                moved = True
        if moved:
            self._push("Move to Bin")

    def _set_label(self, index):
        for m in self.selected_items():
            m.label = index
        self._push("Label")
        self.app.emit("sequence")

    def _context_menu(self, view, x, y):
        self.app.focus = "project"
        menu = Gio.Menu()
        items = self.selected_items()
        if items:
            a = Gio.Menu()
            a.append("Open", "proj.open")
            a.append("New Sequence From Clip", "proj.sequence")
            if len([m for m in items if m.kind in ("video", "audio")]) >= 2:
                a.append("Create Multi-Camera Sequence…", "app.create_multicam")
            menu.append_section(None, a)
            b = Gio.Menu()
            b.append("Rename…", "proj.rename")
            if any(m.path for m in items):
                b.append("Relink Media…", "proj.relink")
            if any(m.kind == "video" or m.is_bin for m in items):
                proxy = Gio.Menu()
                proxy.append("Create Proxies", "proj.proxy_create")
                proxy.append("Attach Proxy…", "proj.proxy_attach")
                proxy.append("Delete Proxies", "proj.proxy_delete")
                b.append_submenu("Proxy", proxy)
            labels = Gio.Menu()
            for i, (name, _) in enumerate(LABELS):
                labels.append(name, f"proj.label({i})")
            b.append_submenu("Label", labels)
            bins = Gio.Menu()
            bins.append("Project Root", "proj.root")
            for m in self.app.project.media.values():
                if m.is_bin and m not in items:
                    bins.append(m.name, f"proj.move('{m.id}')")
            b.append_submenu("Move to Bin", bins)
            menu.append_section(None, b)
            c = Gio.Menu()
            c.append("Delete", "proj.delete")
            menu.append_section(None, c)
        d = Gio.Menu()
        d.append("Import Media…", "app.import")
        d.append("New Bin", "app.new_bin")
        d.append("New Sequence…", "app.new_sequence")
        menu.append_section(None, d)
        popup_menu(view, menu, x, y)

    def _drag_prepare(self, source, x, y):
        ids = [m.id for m in self.selected_items() if not m.is_bin]
        if not ids:
            return None
        self.app.drag_payload = ("media", ids)
        return Gdk.ContentProvider.new_for_value(GObject.Value(str, "tideline:media:" + ",".join(ids)))

    def _files_dropped(self, target, value, x, y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        self.app.import_paths(paths, self.current_bin)
        return True
