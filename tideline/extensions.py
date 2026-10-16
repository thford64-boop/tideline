"""Extensions: folders that add effects, transitions, export formats, presets,
menu commands and event hooks to Tideline.

An extension is a folder holding an ``extension.json`` manifest. Effects,
transitions, export formats and presets can be declared in the manifest
alone; a ``main`` Python file whose ``register(tl)`` receives an
:class:`ExtensionAPI` can add anything the manifest can plus commands, event
hooks and effects with their own parameter logic. See docs/EXTENSIONS.md.

Extensions are looked for in ``~/.config/tideline/extensions`` and in any
folder listed in ``TIDELINE_EXTENSIONS_PATH`` (colon-separated), and are not
loaded at all with ``TIDELINE_NO_EXTENSIONS=1``. They run with the same
rights as Tideline itself, so only install ones you trust.

Loading is independent of GTK: without an app (tests, scripts) commands and
hooks are only recorded, and ``attach(app)`` connects them later.
"""

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile

from . import VERSION
from . import keyframes as kf
from . import presets
from .engine import effects as fx
from .engine import export

API_VERSION = 1
MANIFEST = "extension.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
PARAM_TYPES = ("float", "int", "bool", "color", "choice", "text")
ANIM = ("smooth", "bake", "hold")
EVENTS = ("project", "project_saved", "media", "media_imported", "sequence", "selection", "playhead",
          "history", "effects", "proxy_ready")


class ExtensionError(Exception):
    pass


def user_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tideline", "extensions")


def data_dir(ext_id):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tideline", "extension-data", ext_id)


def search_dirs():
    dirs = [user_dir()]
    dirs += [d for d in os.environ.get("TIDELINE_EXTENSIONS_PATH", "").split(os.pathsep) if d]
    return dirs


def _version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3]) or (0,)


class Extension:
    """One extension folder and what became of loading it."""

    def __init__(self, path, manifest):
        self.path = path
        self.manifest = manifest
        self.id = manifest.get("id", "")
        self.name = manifest.get("name") or self.id
        self.version = str(manifest.get("version", ""))
        self.author = manifest.get("author", "")
        self.description = manifest.get("description", "")
        self.enabled = True
        self.loaded = False
        self.error = ""
        self.module = None
        self.hook_fns = []          # (event, wrapped fn) connected for it
        self.settings = None        # its saved settings, loaded on first use
        self.added = {"effects": [], "transitions": [], "formats": [], "presets": [], "commands": [],
                      "hooks": []}

    @property
    def user_installed(self):
        return os.path.dirname(os.path.abspath(self.path)) == os.path.abspath(user_dir())

    def summary(self):
        """"2 effects, 1 command" style description of what it added."""
        parts = []
        for key, one, many in (("effects", "effect", "effects"), ("transitions", "transition", "transitions"),
                               ("formats", "export format", "export formats"),
                               ("presets", "preset folder", "preset folders"),
                               ("commands", "command", "commands"), ("hooks", "event hook", "event hooks")):
            n = len(self.added[key])
            if n:
                parts.append(f"{n} {one if n == 1 else many}")
        return ", ".join(parts) or "nothing"


class Command:
    def __init__(self, ext, name, label, callback, accel="", action=""):
        self.ext = ext
        self.name = name
        self.label = label
        self.callback = callback
        self.accel = accel or ""
        self.action = action


# -- the API handed to register() ------------------------------------------
class ExtensionAPI:
    """What an extension's ``register(tl)`` receives.

    Effect and transition keys are given without a prefix; Tideline stores
    them as ``<extension id>/<key>`` so two extensions never collide."""

    api_version = API_VERSION
    version = VERSION

    def __init__(self, ext, registry):
        self._ext = ext
        self._registry = registry

    # -- identity --------------------------------------------------------
    @property
    def id(self):
        return self._ext.id

    @property
    def path(self):
        """The extension's own folder."""
        return self._ext.path

    @property
    def app(self):
        """The running App (None in headless use). Its internals may change
        between versions; prefer the methods here where they exist."""
        return self._registry.app

    def file(self, *parts):
        """Absolute path of a file inside the extension's folder."""
        return os.path.join(self._ext.path, *parts)

    # -- parameters ------------------------------------------------------
    @staticmethod
    def number(name, label, default=0.0, min=None, max=None, mlt=None, unit="", step=1.0, digits=1,
               scale=1.0, offset=0.0, animate="smooth", keyframes=True):
        return fx.Param(name, label, "float", float(default), min, max, step, digits, unit, key=keyframes,
                        mlt=mlt, mul=scale, add=offset, anim=animate)

    @staticmethod
    def integer(name, label, default=0, min=None, max=None, mlt=None, unit="", animate="smooth",
                keyframes=True):
        return fx.Param(name, label, "int", int(default), min, max, 1.0, 0, unit, key=keyframes, mlt=mlt,
                        anim=animate)

    @staticmethod
    def toggle(name, label, default=False, mlt=None, keyframes=True):
        return fx.Param(name, label, "bool", bool(default), key=keyframes, mlt=mlt)

    @staticmethod
    def color(name, label, default="#ffffffff", mlt=None, keyframes=True):
        return fx.Param(name, label, "color", default, key=keyframes, mlt=mlt)

    @staticmethod
    def choice(name, label, choices, default=None, mlt=None, keyframes=True):
        choices = [tuple(c) if isinstance(c, (list, tuple)) else (c, str(c)) for c in choices]
        return fx.Param(name, label, "choice", choices[0][0] if default is None else default,
                        choices=choices, key=keyframes, mlt=mlt)

    @staticmethod
    def text(name, label, default="", mlt=None, keyframes=True):
        return fx.Param(name, label, "text", default, key=keyframes, mlt=mlt, slider=False)

    # -- content ---------------------------------------------------------
    def effect(self, key, label, services, params=(), category="Extensions", kind="video", static=None,
               apply=None):
        """Add an effect built from MLT filter ``services``.

        Each parameter with an ``mlt`` name is handed to the first filter,
        keyframes included. ``apply(filters, effect, ctx)``, when given,
        replaces that and is called whenever the effect changes."""
        _check_key(key, "effect")
        if kind not in ("video", "audio"):
            raise ExtensionError(f"effect {key}: kind must be \"video\" or \"audio\"")
        if isinstance(services, str):
            services = [services]
        if not services or not all(isinstance(s, str) and s for s in services):
            raise ExtensionError(f"effect {key}: services must name at least one MLT filter")
        names = set()
        for p in params:
            if not isinstance(p, fx.Param):
                raise ExtensionError(f"effect {key}: parameters must be made with tl.number(), tl.choice()…")
            if p.name in names:
                raise ExtensionError(f"effect {key}: two parameters are called {p.name!r}")
            names.add(p.name)
        full = f"{self.id}/{key}"
        if full in fx.EFFECTS:
            raise ExtensionError(f"effect {key} is already registered")
        custom = None
        if apply is not None:
            def custom(filters, effect, ctx, _apply=apply):
                filters[0].set("disable", 0 if effect.enabled else 1)
                _apply(filters, effect, ctx)
        edef = fx.EffectDef(full, str(label), str(category or "Extensions"), kind, list(services),
                            list(params), static=dict(static or {}), custom=custom)
        fx.EFFECTS[full] = edef
        self._ext.added["effects"].append(full)
        return edef

    def transition(self, key, label, wipe=None, frei0r=None, dip=None, kind="video", category="Extensions",
                   reverse=False):
        """Add a video transition: a wipe drawn by a greyscale image
        (``wipe``, relative to the extension folder: black is revealed
        first, white last), a frei0r transition service (``frei0r``), a
        dip through a colour (``dip``, "#rrggbbaa"), or a plain dissolve when
        none is given. ``kind="audio"`` adds a crossfade."""
        _check_key(key, "transition")
        full = f"{self.id}/{key}"
        if full in fx.TRANSITIONS:
            raise ExtensionError(f"transition {key} is already registered")
        if kind == "audio":
            tdef = fx.TransitionDef(full, str(label), "audio", category or "Extensions", "audio")
        elif wipe:
            path = wipe if os.path.isabs(wipe) else self.file(wipe)
            if not os.path.isfile(path):
                raise ExtensionError(f"transition {key}: wipe image {wipe} not found")
            tdef = fx.TransitionDef(full, str(label), "video", category, "luma", path, reverse=bool(reverse))
        elif frei0r:
            service = frei0r if frei0r.startswith("frei0r.") else "frei0r." + frei0r
            tdef = fx.TransitionDef(full, str(label), "video", category, "frei0r", service)
        elif dip:
            tdef = fx.TransitionDef(full, str(label), "video", category, "dip", color=str(dip))
        else:
            tdef = fx.TransitionDef(full, str(label), "video", category, "luma")
        fx.TRANSITIONS[full] = tdef
        self._ext.added["transitions"].append(full)
        return tdef

    def export_format(self, key, label, ext, video=None, audio=None, quality=None, quality_key="crf",
                      audio_only=False, note=""):
        """Add an Export dialog format. ``video`` and ``audio`` are FFmpeg
        options as melt's avformat consumer takes them (``vcodec``,
        ``acodec``, ``f``, ``pix_fmt``…); ``quality`` is the (high, medium,
        low) value of ``quality_key``."""
        _check_key(key, "export format")
        full = f"{self.id}/{key}"
        if full in export.FORMAT_BY_KEY:
            raise ExtensionError(f"export format {key} is already registered")
        ext = ext if str(ext).startswith(".") else "." + str(ext)
        if quality is not None and len(quality) != 3:
            raise ExtensionError(f"export format {key}: quality needs three values (high, medium, low)")
        fmt = export.Format(full, str(label), ext, {str(k): str(v) for k, v in (video or {}).items()},
                            {str(k): str(v) for k, v in (audio or {}).items()},
                            crf=tuple(quality) if quality else None, crf_key=quality_key,
                            audio_only=bool(audio_only), note=str(note))
        export.FORMATS.append(fmt)
        export.FORMAT_BY_KEY[full] = fmt
        self._ext.added["formats"].append(full)
        return fmt

    def preset_folder(self, folder="presets"):
        """List the ``.tlpreset`` files of a folder in the Effects panel."""
        path = folder if os.path.isabs(folder) else self.file(folder)
        if not os.path.isdir(path):
            raise ExtensionError(f"preset folder {folder} not found")
        if path not in presets.EXTRA_DIRS:
            presets.EXTRA_DIRS.append(path)
            self._ext.added["presets"].append(path)

    def command(self, name, label, callback, accel=""):
        """Add ``label`` to the Extensions menu; choosing it (or pressing
        ``accel``, e.g. "<Ctrl><Alt>j") calls ``callback(app)``."""
        _check_key(name, "command")
        action = "ext-" + re.sub(r"[^A-Za-z0-9-]", "-", f"{self.id}-{name}")
        if any(c.action == action for c in self._registry.commands):
            raise ExtensionError(f"command {name} is already registered")
        if not callable(callback):
            raise ExtensionError(f"command {name}: callback is not callable")
        cmd = Command(self._ext, name, str(label), callback, accel, action)
        self._registry.commands.append(cmd)
        self._ext.added["commands"].append(name)
        if self._registry.app is not None:
            self._registry.app.add_extension_command(cmd)
        return cmd

    def on(self, event, fn):
        """Call ``fn`` when ``event`` happens (see EVENTS)."""
        if event not in EVENTS:
            raise ExtensionError(f"unknown event {event!r}; one of: {', '.join(EVENTS)}")
        guarded = self._registry.guard(self._ext, fn)
        self._registry.hooks.append((event, guarded))
        self._ext.hook_fns.append((event, guarded))
        self._ext.added["hooks"].append(event)
        if self._registry.app is not None:
            self._registry.app.on(event, guarded)

    # -- helpers for commands and custom effects -------------------------
    def status(self, text, warn=False):
        """Show a message in the status bar (or print it when headless)."""
        if self._registry.app is not None:
            self._registry.app.status(text, warn)
        else:
            print(f"[{self.id}] {text}")

    def edit(self, label, fn):
        """Run ``fn(editor)`` on the active sequence as one undoable step."""
        if self._registry.app is None:
            raise ExtensionError("edit() needs the running app")
        return self._registry.app.edit(label, fn)

    @staticmethod
    def mlt_value(effect, param, fmt=None):
        """An effect parameter as an MLT property value: the constant, or an
        animation string when it has keyframes. ``param`` is a Param."""
        fmt = fmt or param.fmt
        kfs = effect.keyframes.get(param.name)
        if kfs:
            return kf.to_mlt(kfs, fmt)
        return fmt(effect.params.get(param.name, param.default))

    @property
    def settings(self):
        """A dict saved between sessions with ``save_settings()``."""
        if self._ext.settings is None:
            try:
                with open(os.path.join(data_dir(self.id), "settings.json"), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            self._ext.settings = data if isinstance(data, dict) else {}
        return self._ext.settings

    def save_settings(self):
        folder = data_dir(self.id)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(self.settings, fh, indent=1)


def _check_key(key, what):
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ExtensionError(f"{what} key {key!r}: use lower-case letters, digits, - and _ (up to 48)")


# -- manifest declarations --------------------------------------------------
def _param_from_json(tl, spec, where):
    if not isinstance(spec, dict) or not spec.get("name"):
        raise ExtensionError(f"{where}: every parameter needs a \"name\"")
    name = spec["name"]
    label = spec.get("label", name)
    kind = spec.get("type", "float")
    if kind not in PARAM_TYPES:
        raise ExtensionError(f"{where}, parameter {name}: type must be one of {', '.join(PARAM_TYPES)}")
    animate = spec.get("animate", "smooth")
    if animate not in ANIM:
        raise ExtensionError(f"{where}, parameter {name}: animate must be one of {', '.join(ANIM)}")
    common = {"mlt": spec.get("mlt"), "keyframes": bool(spec.get("keyframes", True))}
    if kind == "float":
        return tl.number(name, label, spec.get("default", 0.0), spec.get("min"), spec.get("max"),
                         unit=spec.get("unit", ""), step=spec.get("step", 1.0), digits=spec.get("digits", 1),
                         scale=spec.get("scale", 1.0), offset=spec.get("offset", 0.0), animate=animate, **common)
    if kind == "int":
        return tl.integer(name, label, spec.get("default", 0), spec.get("min"), spec.get("max"),
                          unit=spec.get("unit", ""), animate=animate, **common)
    if kind == "bool":
        return tl.toggle(name, label, spec.get("default", False), **common)
    if kind == "color":
        return tl.color(name, label, spec.get("default", "#ffffffff"), **common)
    if kind == "choice":
        choices = spec.get("choices")
        if not choices:
            raise ExtensionError(f"{where}, parameter {name}: a choice needs \"choices\"")
        return tl.choice(name, label, choices, spec.get("default"), **common)
    return tl.text(name, label, spec.get("default", ""), **common)


def _declare(tl, manifest):
    for spec in manifest.get("effects", []):
        if not isinstance(spec, dict):
            raise ExtensionError("\"effects\" must be a list of objects")
        where = f"effect {spec.get('key', '?')}"
        params = [_param_from_json(tl, p, where) for p in spec.get("params", [])]
        tl.effect(spec.get("key"), spec.get("label", spec.get("key")), spec.get("services"), params,
                  category=spec.get("category", "Extensions"), kind=spec.get("kind", "video"),
                  static=spec.get("static"))
    for spec in manifest.get("transitions", []):
        if not isinstance(spec, dict):
            raise ExtensionError("\"transitions\" must be a list of objects")
        tl.transition(spec.get("key"), spec.get("label", spec.get("key")), wipe=spec.get("wipe"),
                      frei0r=spec.get("frei0r"), dip=spec.get("dip"), kind=spec.get("kind", "video"),
                      category=spec.get("category", "Extensions"), reverse=spec.get("reverse", False))
    for spec in manifest.get("export_formats", []):
        if not isinstance(spec, dict):
            raise ExtensionError("\"export_formats\" must be a list of objects")
        tl.export_format(spec.get("key"), spec.get("label", spec.get("key")), spec.get("ext", ".mp4"),
                         spec.get("video"), spec.get("audio"), spec.get("quality"),
                         spec.get("quality_key", "crf"), spec.get("audio_only", False), spec.get("note", ""))
    folders = manifest.get("presets")
    for folder in ([folders] if isinstance(folders, str) else folders or []):
        tl.preset_folder(folder)


def read_manifest(folder):
    path = os.path.join(folder, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        raise ExtensionError(f"cannot read {MANIFEST}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise ExtensionError(f"{MANIFEST} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ExtensionError(f"{MANIFEST} must hold a JSON object")
    ext_id = manifest.get("id")
    if not isinstance(ext_id, str) or not ID_RE.match(ext_id):
        raise ExtensionError("\"id\" must be 2-64 lower-case letters, digits, '.', '-' or '_'")
    return manifest


# -- registry ---------------------------------------------------------------
class Registry:
    def __init__(self):
        self.extensions = []
        self.commands = []
        self.hooks = []             # (event, fn)
        self.app = None

    def guard(self, ext, fn):
        """Wrap an extension callback so its errors are reported, not raised."""
        def call(*args):
            try:
                return fn(*args)
            except Exception as exc:
                traceback.print_exc()
                if self.app is not None:
                    self.app.status(f"Extension “{ext.name}”: {exc}", warn=True)
        return call

    def discover(self, dirs=None):
        """[(folder, manifest or None, error)] for every extension folder."""
        found = []
        for base in dirs if dirs is not None else search_dirs():
            try:
                names = sorted(os.listdir(base), key=str.lower)
            except OSError:
                continue
            for name in names:
                folder = os.path.join(base, name)
                if not os.path.isdir(folder) or name.startswith("."):
                    continue
                if not os.path.exists(os.path.join(folder, MANIFEST)):
                    continue
                try:
                    found.append((folder, read_manifest(folder), ""))
                except ExtensionError as exc:
                    found.append((folder, None, str(exc)))
        return found

    def load(self, dirs=None, disabled=()):
        """Load every extension found; returns the list of Extension."""
        seen = {e.id for e in self.extensions}
        for folder, manifest, error in self.discover(dirs):
            ext = Extension(folder, manifest or {"id": os.path.basename(folder)})
            self.extensions.append(ext)
            if error:
                ext.error = error
                continue
            if ext.id in seen:
                ext.error = "another extension with this id is already loaded"
                continue
            seen.add(ext.id)
            if ext.id in disabled:
                ext.enabled = False
                continue
            self._load_one(ext)
        return self.extensions

    def _load_one(self, ext):
        manifest = ext.manifest
        need = manifest.get("tideline")
        if need and _version_tuple(VERSION) < _version_tuple(need):
            ext.error = f"needs Tideline {need} or later"
            return
        api_need = manifest.get("api", 1)
        if not isinstance(api_need, int) or api_need > API_VERSION:
            ext.error = f"needs extension API {api_need}; this Tideline has {API_VERSION}"
            return
        tl = ExtensionAPI(ext, self)
        try:
            _declare(tl, manifest)
            main = manifest.get("main")
            if main:
                ext.module = _import(ext, main)
                register = getattr(ext.module, "register", None)
                if not callable(register):
                    raise ExtensionError(f"{main} has no register(tl) function")
                register(tl)
        except Exception as exc:
            if not isinstance(exc, ExtensionError):
                traceback.print_exc()
            ext.error = str(exc) or type(exc).__name__
            self._undo(ext)
            return
        ext.loaded = True

    def _undo(self, ext):
        """Take back whatever a failed extension managed to add."""
        for key in ext.added["effects"]:
            fx.EFFECTS.pop(key, None)
        for key in ext.added["transitions"]:
            fx.TRANSITIONS.pop(key, None)
        for key in ext.added["formats"]:
            fmt = export.FORMAT_BY_KEY.pop(key, None)
            if fmt in export.FORMATS:
                export.FORMATS.remove(fmt)
        for path in ext.added["presets"]:
            if path in presets.EXTRA_DIRS:
                presets.EXTRA_DIRS.remove(path)
        mine = [c for c in self.commands if c.ext is ext]
        self.commands = [c for c in self.commands if c.ext is not ext]
        if self.app is not None:
            for c in mine:
                self.app.remove_extension_command(c)
        self.hooks = [h for h in self.hooks if h not in ext.hook_fns]
        if self.app is not None:
            for event, fn in ext.hook_fns:
                self.app.off(event, fn)
        ext.hook_fns = []
        for value in ext.added.values():
            value.clear()

    def unload_all(self):
        for ext in self.extensions:
            if ext.loaded:
                self._undo(ext)
        for ext in self.extensions:
            if ext.module is not None:
                sys.modules.pop(ext.module.__name__, None)
        self.extensions, self.commands, self.hooks = [], [], []

    def attach(self, app):
        """Connect recorded commands and hooks to a running app."""
        self.app = app
        for cmd in self.commands:
            app.add_extension_command(cmd)
        for event, fn in self.hooks:
            app.on(event, fn)

    def command(self, action):
        return next((c for c in self.commands if c.action == action), None)

    def get(self, ext_id):
        return next((e for e in self.extensions if e.id == ext_id), None)


def _import(ext, main):
    path = os.path.normpath(os.path.join(ext.path, main))
    if not path.startswith(os.path.abspath(ext.path) + os.sep) or not path.endswith(".py"):
        raise ExtensionError("\"main\" must be a .py file inside the extension folder")
    if not os.path.isfile(path):
        raise ExtensionError(f"{main} not found")
    name = "tideline_ext_" + re.sub(r"\W", "_", ext.id)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    added = ext.path not in sys.path
    if added:
        sys.path.insert(0, ext.path)          # so the extension can import its own helper modules
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        if added:
            try:
                sys.path.remove(ext.path)
            except ValueError:
                pass
    return module


# -- installing -------------------------------------------------------------
def install(source, dest_dir=None):
    """Install an extension from a folder or a .zip into the user folder,
    replacing an older copy with the same id. Returns (id, installed folder)."""
    dest_dir = dest_dir or user_dir()
    with tempfile.TemporaryDirectory(prefix="tideline-ext-") as tmp:
        if os.path.isdir(source):
            root = source
        elif zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as zf:
                for info in zf.infolist():
                    name = info.filename
                    if name.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", name):
                        raise ExtensionError(f"unsafe path in archive: {name}")
                zf.extractall(tmp)
            root = _manifest_root(tmp)
        else:
            raise ExtensionError("choose an extension folder or a .zip file")
        manifest = read_manifest(root)
        ext_id = manifest["id"]
        target = os.path.join(dest_dir, ext_id)
        if os.path.abspath(root) == os.path.abspath(target):
            return ext_id, target
        os.makedirs(dest_dir, exist_ok=True)
        staging = target + ".installing"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(root, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    return ext_id, target


def _manifest_root(folder):
    if os.path.exists(os.path.join(folder, MANIFEST)):
        return folder
    entries = [e for e in os.listdir(folder) if not e.startswith((".", "__MACOSX"))]
    if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])):
        return _manifest_root(os.path.join(folder, entries[0]))
    raise ExtensionError(f"no {MANIFEST} at the top of the archive")


def uninstall(ext):
    """Delete a user-installed extension's folder."""
    if not ext.user_installed:
        raise ExtensionError("only extensions in the user extensions folder can be removed here")
    shutil.rmtree(ext.path)


registry = Registry()
