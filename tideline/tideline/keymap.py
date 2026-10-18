"""Keyboard shortcuts: the defaults from the command table with the user's
changes on top.

Changes are kept as ``{command: accelerator}`` (``""`` for a shortcut that
was removed) in the preferences and can be saved to, and loaded from, a
``.tlkeys`` file. One key combination belongs to at most one command:
giving it to another command takes it away from the first. Accelerators use
GTK's syntax (``<Ctrl><Shift>k``) and are compared in GTK's normal form, so
``<Primary>K`` and ``<Control>k`` are the same key.
"""

import json
import os

from .commands import ALIASES, COMMANDS

KEYMAP_EXT = ".tlkeys"
FORMAT = 1
# Keys a command cannot have: Escape cancels drags, closes dialogs and leaves full screen.
RESERVED = {"Escape"}
MODIFIER_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                 "Super_L", "Super_R", "Hyper_L", "Hyper_R", "ISO_Level3_Shift", "Caps_Lock", "Num_Lock"}


def _gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk
    return Gdk, Gtk


MODS = None


def _mods():
    global MODS
    if MODS is None:
        Gdk, _ = _gtk()
        MODS = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK
    return MODS


def parse(accel):
    """(keyval, modifiers) with the key in lower case, or None."""
    if not accel:
        return None
    Gdk, Gtk = _gtk()
    ok, key, mods = Gtk.accelerator_parse(accel)
    if not ok or not key:
        return None
    return Gdk.keyval_to_lower(key), int(mods & _mods())


def normalize(accel):
    """GTK's normal form of ``accel`` ("<Control><Shift>k"), or "" when it is not a key."""
    parsed = parse(accel)
    if parsed is None:
        return ""
    Gdk, Gtk = _gtk()
    return Gtk.accelerator_name(parsed[0], Gdk.ModifierType(parsed[1]))


def event_accels(keyval, state):
    """The accelerators a key press can stand for, best first; [] for a lone
    modifier. With Shift, "!" is also tried as "1" (and so on), the way the
    shortcuts are written."""
    Gdk, Gtk = _gtk()
    key = Gdk.keyval_to_lower(keyval)
    if Gdk.keyval_name(key) in MODIFIER_KEYS:
        return []
    mods = Gdk.ModifierType(int(state) & int(_mods()))
    out = []
    if mods & Gdk.ModifierType.SHIFT_MASK and Gdk.keyval_name(key) in _UNSHIFT:
        out.append(Gtk.accelerator_name(Gdk.keyval_from_name(_UNSHIFT[Gdk.keyval_name(key)]), mods))
    out.append(Gtk.accelerator_name(key, mods))
    return out


def from_event(keyval, state):
    """The accelerator a key press makes, or "" for a lone modifier key."""
    found = event_accels(keyval, state)
    return found[0] if found else ""


# US layout: what Shift+key types -> the key itself
_UNSHIFT = {"exclam": "1", "at": "2", "numbersign": "3", "dollar": "4", "percent": "5", "asciicircum": "6",
            "ampersand": "7", "asterisk": "8", "parenleft": "9", "parenright": "0", "underscore": "minus",
            "plus": "equal", "braceleft": "bracketleft", "braceright": "bracketright", "bar": "backslash",
            "colon": "semicolon", "quotedbl": "apostrophe", "less": "comma", "greater": "period",
            "question": "slash", "asciitilde": "grave"}


def label(accel):
    """How a shortcut is shown: "Ctrl+Shift+K"."""
    parsed = parse(accel)
    if parsed is None:
        return ""
    Gdk, Gtk = _gtk()
    return Gtk.accelerator_get_label(parsed[0], Gdk.ModifierType(parsed[1]))


class Keymap:
    """The shortcuts in force. ``extra`` adds commands at run time (extensions):
    ``[(name, label, accel, group)]``."""

    def __init__(self, overrides=None, extra=None):
        self.extra = list(extra or [])
        self.overrides = {}
        for name, accel in (overrides or {}).items():
            if isinstance(name, str) and isinstance(accel, str):
                self.overrides[name] = normalize(accel)

    # -- lookup ------------------------------------------------------------
    def commands(self):
        return list(COMMANDS) + self.extra

    def _entry(self, name):
        return next((c for c in self.commands() if c[0] == name), None)

    def default(self, name):
        entry = self._entry(name)
        return normalize(entry[2]) if entry else ""

    def accel(self, name):
        """The shortcut ``name`` has now ("" for none)."""
        if name in self.overrides:
            return self.overrides[name]
        return self.default(name)

    def changed(self, name):
        return name in self.overrides and self.overrides[name] != self.default(name)

    def owner(self, accel, exclude=None):
        """The command that ``accel`` runs, apart from ``exclude``."""
        accel = normalize(accel)
        if not accel:
            return None
        for name, *_ in self.commands():
            if name != exclude and self.accel(name) == accel:
                return name
        return None

    def bindings(self):
        """{accelerator: command} for every shortcut in force. A command's own
        shortcut wins over an extra key (Backspace for Clear, …) and the first
        command listed wins when two claim the same key."""
        out = {}
        for accel, name in ALIASES:
            key = normalize(accel)
            if key and self._entry(name) is not None:
                out[key] = name
        claimed = set()
        for name, *_ in self.commands():
            key = self.accel(name)
            if key and key not in claimed:
                out[key] = name
                claimed.add(key)
        return out

    def conflicts(self):
        """[(accelerator, [commands])] where more than one command has the key."""
        seen = {}
        for name, *_ in self.commands():
            key = self.accel(name)
            if key:
                seen.setdefault(key, []).append(name)
        return [(k, v) for k, v in seen.items() if len(v) > 1]

    # -- changes -----------------------------------------------------------
    def _set(self, name, accel):
        if accel == self.default(name):
            self.overrides.pop(name, None)
        else:
            self.overrides[name] = accel

    def assign(self, name, accel):
        """Give ``accel`` to ``name``. Returns the command it was taken from, or None."""
        accel = normalize(accel)
        if not accel:
            raise ValueError("not a key")
        if parse(accel) and _keyname(accel) in RESERVED:
            raise ValueError(f"{label(accel)} is reserved")
        previous = self.owner(accel, exclude=name)
        if previous is not None:
            self._set(previous, "")
        self._set(name, accel)
        return previous

    def clear(self, name):
        self._set(name, "")

    def reset(self, name):
        """Back to the default shortcut. Returns the command that had to give it up, or None."""
        default = self.default(name)
        previous = self.owner(default, exclude=name) if default else None
        if previous is not None:
            self._set(previous, "")
        self.overrides.pop(name, None)
        return previous

    def reset_all(self):
        self.overrides = {}

    # -- files -------------------------------------------------------------
    def dump(self, path, name=""):
        data = {"tideline_keys": FORMAT, "name": name or os.path.splitext(os.path.basename(path))[0],
                "shortcuts": {k: v for k, v in sorted(self.overrides.items())}}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)

    def load(self, path):
        """Replace the changes with those in a ``.tlkeys`` file. Unknown commands
        are kept (an extension may not be installed yet); returns how many
        shortcuts were read. Raises ValueError for a file that is not a keymap."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read {os.path.basename(path)}: {exc}") from exc
        if not isinstance(data, dict) or "tideline_keys" not in data or not isinstance(data.get("shortcuts"), dict):
            raise ValueError(f"{os.path.basename(path)} is not a Tideline keyboard shortcut file")
        overrides = {}
        for name, accel in data["shortcuts"].items():
            if not isinstance(name, str) or not isinstance(accel, str):
                continue
            key = normalize(accel)
            if accel and not key:
                continue
            if key and _keyname(key) in RESERVED:
                continue
            overrides[name] = key
        self.overrides = overrides
        # A file edited by hand may give one key to two commands: the first listed keeps it.
        for key, names in self.conflicts():
            for other in names[1:]:
                self._set(other, "")
        return len(overrides)


def _keyname(accel):
    Gdk, _ = _gtk()
    parsed = parse(accel)
    return Gdk.keyval_name(parsed[0]) if parsed else ""
