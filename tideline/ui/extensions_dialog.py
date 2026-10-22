"""Extensions ▸ Manage Extensions: what is installed, what it added, what
went wrong, and switches to turn extensions on or off."""

import os

from gi.repository import Gtk

from .. import extensions
from .widgets import confirm, file_filter, label, open_files


class ExtensionsDialog(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Extensions", application=app, transient_for=app.window, modal=True,
                         default_width=620, default_height=520)
        self.app = app
        self.changed = False
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.add_css_class("dialog-body")
        intro = label("Extensions add effects, transitions, export formats, presets and commands. "
                      "They run with the same rights as Tideline, so only install ones you trust. "
                      "How to write one: docs/EXTENSIONS.md.", css="dim")
        intro.set_wrap(True)
        outer.append(intro)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list)
        outer.append(scroller)
        self.note = label("", css="faint")
        self.note.set_wrap(True)
        outer.append(self.note)
        buttons = Gtk.Box(spacing=8)
        install_zip = Gtk.Button(label="Install from Zip…")
        install_zip.connect("clicked", lambda *_: self._choose_zip())
        install_dir = Gtk.Button(label="Install from Folder…")
        install_dir.connect("clicked", lambda *_: self._choose_folder())
        folder = Gtk.Button(label="Open Folder")
        folder.connect("clicked", lambda *_: app.do("extensions_folder"))
        close = Gtk.Button(label="Close", hexpand=True, halign=Gtk.Align.END)
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda *_: self.close())
        for b in (install_zip, install_dir, folder, close):
            buttons.append(b)
        outer.append(buttons)
        self.set_child(outer)
        self.refresh()

    def refresh(self):
        self.list.remove_all()
        found = list(self.app.extensions.extensions)
        known = {os.path.abspath(e.path) for e in found}
        # Folders installed since start-up show too, waiting for a restart.
        for path, manifest, error in self.app.extensions.discover():
            if os.path.abspath(path) not in known and not (
                    os.path.dirname(os.path.abspath(path)) == os.path.abspath(extensions.builtin_dir())
                    and manifest and any(e.id == manifest.get("id") for e in found)):
                ext = extensions.Extension(path, manifest or {"id": os.path.basename(path)})
                ext.error = error or "installed — restart Tideline to load it"
                ext.enabled = ext.id not in self.app.prefs["disabled_extensions"]
                found.append(ext)
        if not found:
            empty = label(f"No extensions installed.\nPut extension folders in {extensions.user_dir()}",
                          css="hint", xalign=0.5)
            empty.set_wrap(True)
            empty.set_margin_top(30)
            empty.set_margin_bottom(30)
            self.list.append(empty)
        for ext in found:
            self.list.append(self._row(ext))
        self.note.set_text("Changes take effect when Tideline is restarted." if self.changed else "")

    def _row(self, ext):
        row = Gtk.Box(spacing=12, margin_start=10, margin_end=10, margin_top=8, margin_bottom=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = ext.name + (f"  {ext.version}" if ext.version else "")
        text.append(label(title, ellipsize=True))
        meta = ext.id + (f" · {ext.author}" if ext.author else "") + (" · built in" if ext.builtin else "")
        text.append(label(meta, css="faint", ellipsize=True))
        if ext.description:
            desc = label(ext.description, css="dim")
            desc.set_wrap(True)
            text.append(desc)
        if ext.error:
            state = label(ext.error, css="warning-text")
            state.set_wrap(True)
        elif not ext.enabled:
            state = label("Turned off", css="faint")
        else:
            state = label("Added " + ext.summary(), css="faint")
        text.append(state)
        row.append(text)
        if ext.user_installed:
            remove = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
            remove.connect("clicked", lambda *_: self._remove(ext))
            row.append(remove)
        switch = Gtk.Switch(active=ext.enabled, valign=Gtk.Align.CENTER)
        switch.set_tooltip_text("Load this extension when Tideline starts")
        switch.connect("notify::active", lambda s, _p: self._set_enabled(ext, s.get_active()))
        row.append(switch)
        return row

    def _set_enabled(self, ext, on):
        off = [i for i in self.app.prefs["disabled_extensions"] if i != ext.id]
        if not on:
            off.append(ext.id)
        self.app.prefs["disabled_extensions"] = off
        self.app.prefs.save()
        self.changed = True
        self.note.set_text("Changes take effect when Tideline is restarted.")

    def _install(self, path):
        try:
            ext_id, _ = extensions.install(path)
        except (extensions.ExtensionError, OSError) as exc:
            from .widgets import alert
            return alert(self, "Could not install the extension", str(exc))
        self.changed = True
        self.app.status(f"Installed extension {ext_id}. Restart Tideline to load it.")
        self.refresh()

    def _choose_zip(self):
        open_files(self, "Install Extension", lambda paths: self._install(paths[0]),
                   [file_filter("Extension archives", ["*.zip"])], multiple=False)

    def _choose_folder(self):
        dlg = Gtk.FileDialog(title="Choose an Extension Folder", modal=True)

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except Exception:
                return
            if folder is not None and folder.get_path():
                self._install(folder.get_path())

        dlg.select_folder(self, None, done)

    def _remove(self, ext):
        def answer(choice):
            if choice != 1:
                return
            try:
                extensions.uninstall(ext)
            except (extensions.ExtensionError, OSError) as exc:
                return self.app.status(str(exc), warn=True)
            if ext in self.app.extensions.extensions:
                self.app.extensions.extensions.remove(ext)
            self.changed = True
            self.refresh()

        confirm(self, f"Remove “{ext.name}”?", "Its folder is deleted. Anything it added stays until "
                "Tideline is restarted; projects using its effects keep them but cannot show them.",
                "Remove", answer)

