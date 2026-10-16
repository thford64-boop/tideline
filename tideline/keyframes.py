"""Keyframe evaluation and compilation to MLT animation strings.

A keyframe list is ``[[t, value, interp], ...]`` sorted by ``t`` (a custom
curve adds a fourth element, see ``INTERP_GROUPS``). ``t`` is a
frame number in the clip's source timebase, so keyframes stay attached to the
picture when a clip is trimmed or moved. ``interp`` describes the segment that
leaves the keyframe.

Numbers, points (lists of numbers) and ``#rrggbbaa`` colours are interpolated;
any other value (switches, choices, text) holds until the next keyframe.
"""

import math
from bisect import bisect_right

# Interpolations, grouped the way the keyframe menus present them. "bezier"
# is the custom curve: the keyframe then carries a fourth element
# ``[x1, y1, x2, y2]``, the two control points of a cubic Bezier through
# (0, 0) and (1, 1) in the segment's own normalised time/value space.
INTERP_GROUPS = [
    ("", [("linear", "Linear"), ("hold", "Hold")]),
    ("Ease", [("ease_in", "Ease In"), ("ease_out", "Ease Out"), ("ease", "Ease In/Out")]),
    ("Sine", [("sine_in", "Sine In"), ("sine_out", "Sine Out"), ("sine", "Sine In/Out")]),
    ("Expo", [("expo_in", "Expo In"), ("expo_out", "Expo Out"), ("expo", "Expo In/Out")]),
    ("Overshoot", [("back_in", "Back In"), ("back_out", "Back Out"), ("back", "Back In/Out")]),
    ("Elastic", [("elastic_in", "Elastic In"), ("elastic_out", "Elastic Out")]),
    ("Bounce", [("bounce_in", "Bounce In"), ("bounce_out", "Bounce Out")]),
    ("", [("bezier", "Custom Curve")]),
]
INTERPS = [item for _, items in INTERP_GROUPS for item in items]

# Bezier control points that start a custom curve off close to each preset.
BEZIER_PRESETS = {
    "linear": [0.333, 0.333, 0.667, 0.667],
    "ease_in": [0.32, 0.0, 0.67, 0.0], "ease_out": [0.33, 1.0, 0.68, 1.0], "ease": [0.65, 0.0, 0.35, 1.0],
    "sine_in": [0.12, 0.0, 0.39, 0.0], "sine_out": [0.61, 1.0, 0.88, 1.0], "sine": [0.37, 0.0, 0.63, 1.0],
    "expo_in": [0.7, 0.0, 0.84, 0.0], "expo_out": [0.16, 1.0, 0.3, 1.0], "expo": [0.87, 0.0, 0.13, 1.0],
    "back_in": [0.36, 0.0, 0.66, -0.56], "back_out": [0.34, 1.56, 0.64, 1.0],
    "back": [0.68, -0.6, 0.32, 1.6],
}
DEFAULT_BEZIER = [0.33, 0.0, 0.67, 1.0]


def _bounce_out(u):
    n, d = 7.5625, 2.75
    if u < 1 / d:
        return n * u * u
    if u < 2 / d:
        u -= 1.5 / d
        return n * u * u + 0.75
    if u < 2.5 / d:
        u -= 2.25 / d
        return n * u * u + 0.9375
    u -= 2.625 / d
    return n * u * u + 0.984375


def _elastic_out(u):
    if u <= 0 or u >= 1:
        return float(max(0, min(1, u)))
    return 2 ** (-10 * u) * math.sin((u * 10 - 0.75) * (2 * math.pi / 3)) + 1


def _expo_in(u):
    return 0.0 if u <= 0 else 2 ** (10 * u - 10)


_B1, _B2 = 1.70158, 1.70158 * 1.525
_EASINGS = {
    "ease_in": lambda u: u * u * u,
    "ease_out": lambda u: 1 - (1 - u) ** 3,
    "ease": lambda u: 4 * u * u * u if u < 0.5 else 1 - ((-2 * u + 2) ** 3) / 2,
    "sine_in": lambda u: 1 - math.cos(u * math.pi / 2),
    "sine_out": lambda u: math.sin(u * math.pi / 2),
    "sine": lambda u: -(math.cos(math.pi * u) - 1) / 2,
    "expo_in": _expo_in,
    "expo_out": lambda u: 1 - _expo_in(1 - u),
    "expo": lambda u: _expo_in(2 * u) / 2 if u < 0.5 else 1 - _expo_in(2 - 2 * u) / 2,
    "back_in": lambda u: (_B1 + 1) * u ** 3 - _B1 * u * u,
    "back_out": lambda u: 1 + (_B1 + 1) * (u - 1) ** 3 + _B1 * (u - 1) ** 2,
    "back": lambda u: ((2 * u) ** 2 * ((_B2 + 1) * 2 * u - _B2)) / 2 if u < 0.5
    else ((2 * u - 2) ** 2 * ((_B2 + 1) * (2 * u - 2) + _B2) + 2) / 2,
    "elastic_in": lambda u: 1 - _elastic_out(1 - u),
    "elastic_out": _elastic_out,
    "bounce_in": lambda u: 1 - _bounce_out(1 - u),
    "bounce_out": _bounce_out,
}


def bezier(u, curve):
    """Value of the cubic Bezier easing ``[x1, y1, x2, y2]`` at time ``u``."""
    x1, y1, x2, y2 = curve
    x1, x2 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))
    lo, hi = 0.0, 1.0
    for _ in range(22):          # x(s) is monotonic for x1, x2 in 0..1
        s = (lo + hi) / 2
        x = 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s ** 3
        if x < u:
            lo = s
        else:
            hi = s
    s = (lo + hi) / 2
    return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s ** 3


def ease(u, interp, curve=None):
    """Progress 0..1 through a segment -> eased progress (may overshoot)."""
    if interp == "bezier":
        return bezier(u, curve or DEFAULT_BEZIER)
    fn = _EASINGS.get(interp)
    return fn(u) if fn else u


def curve_of(key):
    """Bezier control points equivalent (or close) to a keyframe's interpolation."""
    if key[2] == "bezier" and len(key) > 3:
        return list(key[3])
    return list(BEZIER_PRESETS.get(key[2], DEFAULT_BEZIER))


def is_color(v):
    return isinstance(v, str) and v.startswith("#") and len(v) in (7, 9)


def color_rgba(v):
    """'#rrggbb' or '#rrggbbaa' -> [r, g, b, a], each 0..255."""
    try:
        out = [int(v[i:i + 2], 16) for i in range(1, len(v), 2)]
    except (TypeError, ValueError):
        out = []
    return (out + [255, 255, 255, 255])[:4] if len(out) != 3 else out + [255]


def rgba_color(rgba):
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgba)


def interpolates(v):
    """True for values that blend between keyframes rather than stepping."""
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float, list, tuple)) or is_color(v)


def _lerp(a, b, u):
    if isinstance(a, (list, tuple)):
        return [x + (y - x) * u for x, y in zip(a, b)]
    if isinstance(a, str):
        return rgba_color(_lerp(color_rgba(a), color_rgba(b), max(0.0, min(1.0, u))))
    return a + (b - a) * u


def evaluate(kfs, t):
    """Value of a keyframe list at frame ``t`` (None when the list is empty)."""
    if not kfs:
        return None
    if t <= kfs[0][0]:
        return kfs[0][1]
    if t >= kfs[-1][0]:
        return kfs[-1][1]
    i = bisect_right([k[0] for k in kfs], t) - 1
    a, b = kfs[i], kfs[i + 1]
    if a[2] == "hold" or not interpolates(a[1]) or not interpolates(b[1]):
        return a[1]
    u = (t - a[0]) / (b[0] - a[0])
    return _lerp(a[1], b[1], ease(u, a[2], a[3] if len(a) > 3 else None))


def find(kfs, t):
    """Index of the keyframe at exactly frame ``t``, or -1."""
    for i, k in enumerate(kfs or ()):
        if k[0] == t:
            return i
    return -1


def set_key(kfs, t, value, interp=None):
    """Insert or update a keyframe; returns a new sorted list."""
    out = [list(k) for k in (kfs or [])]
    i = find(out, t)
    if i >= 0:
        out[i][1] = value
        if interp:
            out[i][2:] = [interp]
    else:
        tail = [interp]
        if interp is None:
            prev = [k for k in out if k[0] < t]
            tail = list(prev[-1][2:]) if prev else ["linear"]
        out.append([t, value] + tail)
        out.sort(key=lambda k: k[0])
    return out


def remove_key(kfs, t):
    return [list(k) for k in (kfs or []) if k[0] != t]


def neighbours(kfs, t):
    """(previous keyframe time or None, next keyframe time or None)."""
    prev = [k[0] for k in (kfs or []) if k[0] < t]
    nxt = [k[0] for k in (kfs or []) if k[0] > t]
    return (prev[-1] if prev else None, nxt[0] if nxt else None)


def sample_times(*lists):
    """Frames at which a set of keyframe lists must be sampled so that linear
    interpolation between the samples reproduces every list exactly."""
    times = set()
    for kfs in lists:
        if not kfs:
            continue
        for i, k in enumerate(kfs):
            times.add(k[0])
            if i + 1 < len(kfs) and k[2] not in ("linear", "hold"):
                times.update(range(k[0] + 1, kfs[i + 1][0]))
            if i + 1 < len(kfs) and k[2] == "hold" and kfs[i + 1][0] - 1 > k[0]:
                times.add(kfs[i + 1][0] - 1)
    return sorted(times)


def to_mlt(kfs, fmt=lambda v: f"{v:.6g}", offset=0):
    """Compile one keyframe list to an MLT animation string.

    Eased segments are baked per frame so MLT and the UI always agree.
    """
    parts = []
    for i, k in enumerate(kfs):
        t, v, interp = k[0], k[1], k[2]
        last = i + 1 >= len(kfs)
        if last or interp == "linear":
            parts.append(f"{t + offset}={fmt(v)}")
        elif interp == "hold" or not interpolates(v):
            parts.append(f"{t + offset}|={fmt(v)}")
        else:
            for f in range(t, kfs[i + 1][0]):
                parts.append(f"{f + offset}={fmt(evaluate(kfs, f))}")
    return ";".join(parts)


def sampled_to_mlt(times, fn, offset=0):
    """Compile ``fn(t) -> str`` sampled at ``times`` to an MLT animation."""
    return ";".join(f"{t + offset}={fn(t)}" for t in times)


def scale_times(kfs, factor, shift=0):
    out, seen = [], set()
    for k in kfs or []:
        t = int(round(k[0] * factor + shift))
        if t not in seen:
            seen.add(t)
            out.append([t] + list(k[1:]))
    out.sort(key=lambda k: k[0])
    return out
