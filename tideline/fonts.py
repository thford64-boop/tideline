"""Custom fonts: font files the user adds are copied into a private folder
and made available to everything that draws text — the GTK interface (Pango),
title rendering (MLT's Qt text filter) and the export process.

All three find fonts through fontconfig, so the folder is added with a small
fontconfig file that includes the system configuration. That covers every
start of the application and of ``melt``. A font added while the application
is running is also registered with the already-initialised libraries.
"""

import ctypes
import ctypes.util
import os
import shutil
from xml.sax.saxutils import escape

FONT_EXT = (".ttf", ".otf", ".ttc", ".otc")
_fc = None
_config_done = False


class FontError(Exception):
    pass


def store_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "tideline", "fonts")


def _fontconfig():
    global _fc
    if _fc is None:
        lib = ctypes.CDLL(ctypes.util.find_library("fontconfig") or "libfontconfig.so.1")
        vp = ctypes.c_void_p
        lib.FcConfigAppFontAddFile.argtypes = [vp, ctypes.c_char_p]
        lib.FcFreeTypeQuery.restype = vp
        lib.FcFreeTypeQuery.argtypes = [ctypes.c_char_p, ctypes.c_int, vp, ctypes.POINTER(ctypes.c_int)]
        lib.FcPatternGetString.argtypes = [vp, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
        lib.FcPatternDestroy.argtypes = [vp]
        lib.FcConfigFilename.restype = vp
        lib.FcConfigFilename.argtypes = [ctypes.c_char_p]
        _fc = lib
    return _fc


def use_private_config():
    """Add the font folder to fontconfig for this process and its children.
    Must run before anything draws text. Returns the config file's path."""
    global _config_done
    conf = os.path.join(os.path.dirname(store_dir()), "fonts.conf")
    if _config_done or os.environ.get("FONTCONFIG_FILE") == conf:
        _config_done = True
        return conf
    base = os.environ.get("FONTCONFIG_FILE")
    if not base:
        try:
            raw = _fontconfig().FcConfigFilename(None)
            base = ctypes.string_at(raw).decode() if raw else ""
        except (OSError, AttributeError, UnicodeDecodeError):
            base = ""
    base = base or "/etc/fonts/fonts.conf"
    try:
        os.makedirs(store_dir(), exist_ok=True)
        text = ('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
                "<!-- Written by Tideline: the system fonts plus the fonts added in the application. -->\n"
                f'<fontconfig>\n  <include ignore_missing="yes">{escape(base)}</include>\n'
                f"  <dir>{escape(store_dir())}</dir>\n</fontconfig>\n")
        with open(conf + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(conf + ".tmp", conf)
    except OSError:
        return None
    os.environ["FONTCONFIG_FILE"] = conf
    _config_done = True
    return conf


def families(path):
    """Family names in a font file (several for a collection); [] if it is
    not a font fontconfig can read."""
    try:
        fc = _fontconfig()
    except OSError:
        return []
    names, index, count = [], 0, ctypes.c_int(1)
    while index < max(1, count.value) and index < 64:
        pattern = fc.FcFreeTypeQuery(os.fsencode(path), index, None, ctypes.byref(count))
        if not pattern:
            break
        value = ctypes.c_char_p()
        if fc.FcPatternGetString(pattern, b"family", 0, ctypes.byref(value)) == 0 and value.value:
            name = value.value.decode("utf-8", "replace")
            if name not in names:
                names.append(name)
        fc.FcPatternDestroy(pattern)
        index += 1
    return names


def installed():
    """[(path, [family names])] for the fonts in the store."""
    try:
        names = sorted(os.listdir(store_dir()), key=str.lower)
    except OSError:
        return []
    out = [(os.path.join(store_dir(), n), families(os.path.join(store_dir(), n)))
           for n in names if n.lower().endswith(FONT_EXT)]
    return [(p, f) for p, f in out if f]


# -- registering with libraries that are already running -------------------------
class _QString(ctypes.Structure):
    _fields_ = [("d", ctypes.c_void_p), ("ptr", ctypes.c_void_p), ("size", ctypes.c_ssize_t)]


class _QByteArrayView(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ssize_t), ("data", ctypes.c_char_p)]


def _register_qt(path):
    """Tell the Qt inside MLT about a font file. Qt lists the fonts once, when
    the first title is drawn, and never looks again, so a later addition has
    to go through QFontDatabase::addApplicationFont. Returns True on success;
    on failure the font still works after a restart."""
    try:
        from .engine.mltlib import init, make_profile, mlt
        init()
        # Qt's application object exists only once a Qt service has drawn.
        profile = make_profile(64, 36, 25)
        producer = mlt.Producer(profile, "color:#00000000")
        warm = mlt.Filter(profile, "qtext")
        if not (producer.is_valid() and warm.is_valid()):
            return False
        warm.set("argument", " ")
        producer.attach(warm)
        mlt.frame_get_image(producer.get_frame(), mlt.mlt_image_rgba, 64, 36)
        core, gui = ctypes.CDLL("libQt6Core.so.6"), ctypes.CDLL("libQt6Gui.so.6")
        from_utf8 = core._ZN7QString8fromUtf8E14QByteArrayView
        from_utf8.restype, from_utf8.argtypes = _QString, [_QByteArrayView]
        add = gui._ZN13QFontDatabase18addApplicationFontERK7QString
        add.restype, add.argtypes = ctypes.c_int, [ctypes.POINTER(_QString)]
        raw = os.fsencode(path)
        name = from_utf8(_QByteArrayView(len(raw), raw))     # never freed: a few bytes per added font
        return add(ctypes.byref(name)) >= 0
    except (OSError, AttributeError, ImportError):
        return False


def _register_pango():
    try:
        import gi
        gi.require_version("PangoCairo", "1.0")
        gi.require_version("PangoFc", "1.0")
        from gi.repository import PangoCairo, PangoFc
        fontmap = PangoCairo.FontMap.get_default()
        if isinstance(fontmap, PangoFc.FontMap):
            fontmap.config_changed()
    except (ImportError, ValueError, AttributeError):
        pass


def register(path):
    """Make a font file usable right now. Returns True when title rendering
    picked it up as well (otherwise it does after a restart)."""
    try:
        _fontconfig().FcConfigAppFontAddFile(None, os.fsencode(path))
    except OSError:
        return False
    _register_pango()
    return _register_qt(path)


def add(path):
    """Copy a font file into the store and register it. Returns
    (family names, usable in titles without a restart)."""
    if not path.lower().endswith(FONT_EXT):
        raise FontError(f"{os.path.basename(path)} is not a TrueType or OpenType font file.")
    names = families(path)
    if not names:
        raise FontError(f"{os.path.basename(path)} could not be read as a font.")
    os.makedirs(store_dir(), exist_ok=True)
    dest = os.path.join(store_dir(), os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(dest):
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            raise FontError(str(exc)) from None
    return names, register(dest)
