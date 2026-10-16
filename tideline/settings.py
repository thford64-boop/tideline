"""User preferences and the recent-projects list, kept in
``~/.config/tideline/settings.json``. Plain JSON, independent of GTK."""

import json
import os

from . import PROJECT_EXT

MAX_RECENT = 15

# Playback quality presets: what each one sets. "Custom" is any other mix.
QUALITY_PRESETS = {
    "performance": {"preview_scale": 0.25, "scaling": "nearest", "drop_frames": True, "threads": 2,
                    "ui_fps": 20, "live_values": False},
    "balanced": {"preview_scale": 0.5, "scaling": "bilinear", "drop_frames": True, "threads": 1,
                 "ui_fps": 30, "live_values": True},
    "quality": {"preview_scale": 1.0, "scaling": "bicubic", "drop_frames": False, "threads": 1,
                "ui_fps": 60, "live_values": True},
}
QUALITY_NAMES = [("performance", "Performance"), ("balanced", "Balanced"), ("quality", "Quality")]
PREVIEW_SCALES = [("Full", 1.0), ("1/2", 0.5), ("1/4", 0.25), ("1/8", 0.125)]
SCALING = [("nearest", "Fast"), ("bilinear", "Smooth"), ("bicubic", "Sharp")]
THREADS = [(1, "1 (most compatible)"), (2, "2"), (4, "4"), (0, "Automatic")]
UI_RATES = [15, 20, 30, 60]
LAYOUTS = [("editing", "Editing"), ("effects", "Effects"), ("audio", "Audio")]

DEFAULTS = dict(QUALITY_PRESETS["balanced"], quality="balanced", setup_done=False, layout="editing",
                fullscreen=True, show_launcher=True, recent=[], project_dir="",
                popout_monitor="", popout_timecode=False,
                caption_engine="", caption_model="base", caption_language="auto", caption_chars=42,
                caption_lines=2, use_proxies=True, proxy_size=540, proxy_codec="mjpeg",
                proxy_location="project", proxy_auto=0, disabled_extensions=[])


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tideline")


def path():
    return os.path.join(config_dir(), "settings.json")


def default_project_dir():
    try:
        from gi.repository import GLib
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    except Exception:
        videos = None
    return os.path.join(videos or os.path.expanduser("~"), "Tideline")


def render_threads(value):
    """Worker threads for the preview consumer; 0 picks from the core count.
    More than four mostly adds start-up latency, so automatic stops there."""
    if value:
        return max(1, int(value))
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def matching_preset(prefs):
    """Name of the quality preset ``prefs`` matches, or "custom"."""
    for name, values in QUALITY_PRESETS.items():
        if all(prefs.get(k) == v for k, v in values.items()):
            return name
    return "custom"


class Settings(dict):
    """A dict of preferences that writes itself back on ``save()``."""

    def __init__(self, file=None):
        super().__init__(DEFAULTS)
        self["recent"] = []
        self.file = file or path()
        try:
            with open(self.file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for k, v in data.items():
                kind = (int, float) if k == "preview_scale" else type(DEFAULTS.get(k))
                if k in DEFAULTS and isinstance(v, kind):
                    self[k] = v
        if not self["project_dir"]:
            self["project_dir"] = default_project_dir()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict(self), fh, indent=1)
            os.replace(tmp, self.file)
        except OSError:
            pass

    def apply_preset(self, name):
        if name in QUALITY_PRESETS:
            self.update(QUALITY_PRESETS[name])
        self["quality"] = name

    # -- recent projects ---------------------------------------------------
    def add_recent(self, project_path):
        if not project_path:
            return
        p = os.path.abspath(project_path)
        self["recent"] = [p] + [r for r in self["recent"] if r != p][:MAX_RECENT - 1]
        self.save()

    def remove_recent(self, project_path):
        self["recent"] = [r for r in self["recent"] if r != project_path]
        self.save()

    def recent_projects(self):
        """[(path, exists)] newest first, only ``.tideline`` files."""
        return [(p, os.path.exists(p)) for p in self["recent"] if p.endswith(PROJECT_EXT)]
