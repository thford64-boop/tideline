"""Effect presets: a clip's effects, with their keyframes and curves, saved as
a ``.tlpreset`` file that can be shared and applied to other clips.

A preset is JSON. Keyframe times are stored relative to the first frame of the
clip it was saved from, with the frame rate and frame size, so applying it to
a clip in a different sequence keeps the timing in seconds and the positions
in proportion.
"""

import json
import os
import re
import shutil

from . import keyframes as kf
from .engine.effects import EFFECTS
from .model import Effect

PRESET_EXT = ".tlpreset"
PRESET_FORMAT = 1
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "presets")
_INTERPS = {key for key, _ in kf.INTERPS}


class PresetError(Exception):
    pass


def user_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tideline", "presets")


# -- saving ------------------------------------------------------------------
def capture(pairs, seq, name):
    """Preset holding the effects of ``pairs`` ([(track, clip)], normally one
    clip or a linked video/audio pair). Untouched Motion/Opacity/Volume/Panner
    are left out so applying the preset does not reset them."""
    effects = []
    for track, clip in pairs:
        for e in clip.effects:
            edef = EFFECTS.get(e.type)
            if edef is None or edef.kind != track.kind:
                continue            # Time Remapping ("any") belongs to the clip, not a look
            keyframes = {n: kf.scale_times(k, 1.0, -clip.in_point) for n, k in e.keyframes.items() if k}
            if edef.intrinsic and not e.params and not keyframes and e.enabled:
                continue
            effects.append({"type": e.type, "enabled": e.enabled, "params": json.loads(json.dumps(e.params)),
                            "keyframes": keyframes})
    if not effects:
        raise PresetError("The selected clip has no effects or keyframes to save.")
    return {"tideline_preset": PRESET_FORMAT, "name": name, "fps": seq.fps, "width": seq.width,
            "height": seq.height, "duration": max(c.duration for _, c in pairs), "effects": effects}


def dump(preset, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(preset, fh, indent=1)
    os.replace(tmp, path)


# -- loading -----------------------------------------------------------------
def _value(pdef, v):
    """``v`` as a valid value for the parameter; raises ValueError otherwise."""
    t = pdef.type
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError(v)
        return v
    if t == "int" or t == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e12:
            raise ValueError(v)
        if pdef.min is not None:
            v = max(pdef.min, v)
        if pdef.max is not None:
            v = min(pdef.max, v)
        return int(round(v)) if t == "int" else float(v)
    if t == "point":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError(v)
        return [float(c) for c in v]
    if t == "color":
        if not kf.is_color(v):
            raise ValueError(v)
        int(v[1:], 16)
        return v.lower()
    if t == "choice":
        if v not in [c[0] for c in pdef.choices]:
            raise ValueError(v)
        return v
    if not isinstance(v, str) or len(v) > 20000:
        raise ValueError(v)
    return v


def _keyframes(pdef, raw):
    out = {}
    for k in raw if isinstance(raw, list) else []:
        try:
            t, interp = int(k[0]), k[2]
            key = [t, _value(pdef, k[1]), interp if interp in _INTERPS else "linear"]
            if pdef.hold or not kf.interpolates(key[1]):
                key[2] = "hold"
            elif interp == "bezier":
                key.append([float(c) for c in (list(k[3]) + [0, 0, 0, 0])[:4]] if len(k) > 3 else list(kf.DEFAULT_BEZIER))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        out[t] = key
    return [out[t] for t in sorted(out)]


def parse(data):
    """Validate decoded preset JSON. Everything in it is checked against the
    effect registry, since preset files come from other people."""
    if not isinstance(data, dict) or "tideline_preset" not in data or not isinstance(data.get("effects"), list):
        raise PresetError("This is not a Tideline preset file.")
    try:
        if int(data["tideline_preset"]) > PRESET_FORMAT:
            raise PresetError("This preset was saved by a newer version of the application.")
        fps = float(data.get("fps") or 30.0)
        width, height = int(data.get("width") or 0), int(data.get("height") or 0)
        duration = int(data.get("duration") or 0)
    except (TypeError, ValueError):
        raise PresetError("The preset file is damaged.") from None
    effects, unknown = [], []
    for raw in data["effects"][:200]:
        if not isinstance(raw, dict):
            continue
        edef = EFFECTS.get(raw.get("type"))
        if edef is None:
            unknown.append(str(raw.get("type"))[:40])
            continue
        params, keyframes = {}, {}
        for pdef in edef.params:
            src = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            if pdef.name in src:
                try:
                    params[pdef.name] = _value(pdef, src[pdef.name])
                except (TypeError, ValueError):
                    pass
            anim = raw.get("keyframes") if isinstance(raw.get("keyframes"), dict) else {}
            keys = _keyframes(pdef, anim.get(pdef.name)) if pdef.key else []
            if keys:
                keyframes[pdef.name] = keys
        effects.append({"type": edef.key, "enabled": raw.get("enabled", True) is not False,
                        "params": params, "keyframes": keyframes})
    if not effects:
        raise PresetError("The preset has no effects this version knows.")
    return {"name": str(data.get("name") or "Preset")[:80], "fps": fps if 1.0 <= fps <= 1000.0 else 30.0,
            "width": width, "height": height, "duration": duration, "effects": effects, "unknown": unknown}


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PresetError(f"Could not read {os.path.basename(path)}: {exc}") from None
    preset = parse(data)
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        preset["name"] = os.path.splitext(os.path.basename(path))[0]
    return preset


# -- applying ----------------------------------------------------------------
def kinds(preset):
    return {EFFECTS[e["type"]].kind for e in preset["effects"]}


def apply(preset, track, clip, seq):
    """New effect list for ``clip`` with the preset's effects of the track's
    kind applied, or None when it has none. Motion, Opacity, Volume, Panner
    and a title's Text are updated in place; other effects are added."""
    rate = seq.fps / preset["fps"]
    sx = seq.width / preset["width"] if preset["width"] > 0 else 1.0
    sy = seq.height / preset["height"] if preset["height"] > 0 else 1.0

    def fit(pdef, v):
        if pdef.type == "point":
            return [round(v[0] * sx, 2), round(v[1] * sy, 2)]
        if pdef.unit == "px" and pdef.type in ("int", "float") and sy != 1.0:
            return _value(pdef, v * sy)
        return v

    effects, applied = list(clip.effects), 0
    for item in preset["effects"]:
        edef = EFFECTS[item["type"]]
        if edef.kind != track.kind:
            continue
        params = {n: fit(edef.param(n), v) for n, v in item["params"].items()}
        keyframes = {}
        for n, keys in item["keyframes"].items():
            keys = kf.scale_times(keys, rate, clip.in_point)
            keyframes[n] = [[k[0], fit(edef.param(n), k[1])] + k[2:] for k in keys]
        current = next((i for i, e in enumerate(effects) if e.type == edef.key), None)
        if current is not None and (edef.intrinsic or edef.key == "text"):
            old = effects[current]
            if edef.key == "text" and "text" not in keyframes and "text" in old.params:
                params["text"] = old.params["text"]        # a style preset keeps the clip's own words
            merged = {n: v for n, v in dict(old.params, **params).items() if n not in keyframes}
            effects[current] = Effect(edef.key, id=old.id, enabled=item["enabled"], params=merged,
                                      keyframes=dict({n: k for n, k in old.keyframes.items() if n not in params},
                                                     **keyframes), collapsed=old.collapsed)
        elif edef.intrinsic:
            continue
        else:
            effects.append(Effect(edef.key, enabled=item["enabled"], params=params, keyframes=keyframes))
        applied += 1
    return effects if applied else None


# -- library -----------------------------------------------------------------
_cache = {}


def library():
    """[(path, preset, builtin)] for every preset that loads, built-ins first."""
    out = []
    for folder, builtin in ((BUILTIN_DIR, True), (user_dir(), False)):
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(PRESET_EXT):
                continue
            path = os.path.join(folder, name)
            try:
                stamp = os.stat(path).st_mtime_ns
                if _cache.get(path, (None,))[0] != stamp:
                    _cache[path] = (stamp, load(path))
            except (OSError, PresetError):
                continue
            out.append((path, _cache[path][1], builtin))
    return out


def file_name(name):
    return (re.sub(r"[^\w .()+-]", "_", name).strip(" .") or "Preset")[:60] + PRESET_EXT


def install(path):
    """Copy a preset file into the user's library; returns (new path, preset)."""
    preset = load(path)
    folder = user_dir()
    os.makedirs(folder, exist_ok=True)
    if os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder):
        return path, preset
    stem = os.path.splitext(os.path.basename(path))[0]
    dest, n = os.path.join(folder, stem + PRESET_EXT), 2
    while os.path.exists(dest):
        dest = os.path.join(folder, f"{stem} {n}{PRESET_EXT}")
        n += 1
    shutil.copyfile(path, dest)
    return dest, preset
