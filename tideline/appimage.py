"""Running from an AppImage.

The AppImage's ``AppRun`` points the dynamic linker, GObject introspection,
GdkPixbuf, GSettings and GIO at the libraries inside the image, and keeps
the values they had before in ``TIDELINE_HOST_<NAME>``. Those settings must
not leak into programs Tideline starts on the host (a file manager, a web
browser, whisper.cpp): once everything that reads them has done so, they
are put back. The bundled ``melt``, ``ffmpeg`` and ``ffprobe`` are wrappers
that set up their own library path, so they keep working.
"""

import os

HOST_PREFIX = "TIDELINE_HOST_"
UNSET = "@unset@"
# Read once at start-up (or initialised below before being restored).
RESTORE = ["LD_LIBRARY_PATH", "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR", "GSETTINGS_SCHEMA_DIR",
           "GIO_MODULE_DIR", "XDG_DATA_DIRS", "GI_TYPELIB_PATH", "PYTHONNOUSERSITE"]


def active():
    return bool(os.environ.get("APPDIR")) and bool(os.environ.get("TIDELINE_APPIMAGE"))


def restore_host_env():
    """Give child processes the environment of the session that started
    the AppImage. Call after GTK, GdkPixbuf, GSettings and MLT are up."""
    if not active():
        return
    try:
        from gi.repository import GdkPixbuf, Gio, GLib
        GdkPixbuf.Pixbuf.get_formats()           # loads the image loaders list
        Gio.SettingsSchemaSource.get_default()   # reads the bundled schemas
        Gio.Vfs.get_default()                    # loads GIO modules
        GLib.get_system_data_dirs()              # caches XDG_DATA_DIRS for icons and themes
    except Exception:
        pass
    for name in RESTORE:
        saved = os.environ.pop(HOST_PREFIX + name, None)
        if saved is None:
            continue
        if saved == UNSET:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
