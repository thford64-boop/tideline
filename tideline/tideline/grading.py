"""Colour grading maths: colour wheels, hue/saturation curves and secondary
(qualifier) correction, and the 3D LUTs that carry them to FFmpeg.

Each grade is a function on RGB in 0..1 (numpy arrays of shape (..., 3)).
Every one of them works pixel by pixel, so it is baked into a 3D LUT
(``.cube``) that FFmpeg's ``lut3d`` filter applies with tetrahedral
interpolation; the LUT files are cached by the grade's settings. Luma and
colour difference use the Rec. 709 coefficients, as the scopes do, and the
colour wheels are oriented like the vectorscope (+Cb right, +Cr up), so a
push towards a colour on a wheel moves the picture towards that colour on the
scope. Plain numpy, no GTK or MLT.
"""

import hashlib
import json
import math
import os

import numpy as np

from . import curves as cv

KR, KB = 0.2126, 0.0722
KG = 1.0 - KR - KB
LUT_SIZE = 33
LUT_VERSION = 1          # bump when the maths change, so cached LUTs are not reused
MAX_CACHED = 400


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "tideline", "luts")


CACHE_DIR = None         # tests point this somewhere else; None means cache_dir()


# -- colour spaces -------------------------------------------------------------
def to_ycc(rgb):
    """(Y, Cb, Cr) of RGB: Y in 0..1, Cb and Cr in -0.5..0.5."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = KR * r + KG * g + KB * b
    return y, (b - y) / (2 * (1 - KB)), (r - y) / (2 * (1 - KR))


def from_ycc(y, cb, cr):
    r = y + 2 * (1 - KR) * cr
    b = y + 2 * (1 - KB) * cb
    g = (y - KR * r - KB * b) / KG
    return np.stack([r, g, b], axis=-1)


def hue_of(rgb):
    """Hue in 0..1 as on a colour wheel: red 0, yellow 1/6, green 1/3, cyan
    1/2, blue 2/3, magenta 5/6 (0 for greys)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    d = mx - mn
    safe = np.where(d > 1e-9, d, 1.0)
    h = np.where(mx == r, ((g - b) / safe) % 6.0, np.where(mx == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0))
    return np.where(d > 1e-9, h / 6.0, 0.0) % 1.0


def saturation_of(cb, cr):
    """Saturation from the colour difference, 0 for grey and about 1 for a
    fully saturated primary or secondary, whatever its brightness."""
    return np.clip(np.hypot(cb, cr) / 0.5, 0.0, 1.0)


def hue_rgb(h, value=1.0):
    """The pure colour of hue ``h`` (0..1) as (r, g, b): what the curve
    editor's hue strip shows."""
    k = lambda n: (n + h * 6.0) % 6.0
    f = lambda n: value - value * max(0.0, min(k(n), 4.0 - k(n), 1.0))
    return f(5.0), f(3.0), f(1.0)


def push_rgb(u, v):
    """The RGB change of a wheel push (u, v) = (Cb, Cr) with no change in luma."""
    return np.array([2 * (1 - KR) * v, (-KR * 2 * (1 - KR) * v - KB * 2 * (1 - KB) * u) / KG, 2 * (1 - KB) * u])


# -- colour wheels ---------------------------------------------------------------
WHEELS = [("lift", "Lift"), ("gamma", "Gamma"), ("gain", "Gain"), ("offset", "Offset")]
NEUTRAL_WHEEL = [0.0, 0.0, 0.0]
# How far the edge of each wheel pushes (in Cb/Cr) and its master range.
WHEEL_REACH = {"lift": 0.12, "gamma": 0.35, "gain": 0.25, "offset": 0.12}
WHEEL_MASTER = {"lift": 0.5, "gamma": 1.0, "gain": 1.0, "offset": 0.5}


def clean_wheel(value):
    """``[u, v, master]`` with (u, v) inside the unit circle and master in -1..1."""
    try:
        u, v, m = (float(c) for c in (list(value) + [0, 0, 0])[:3])
    except (TypeError, ValueError):
        return list(NEUTRAL_WHEEL)
    if not all(math.isfinite(c) for c in (u, v, m)):
        return list(NEUTRAL_WHEEL)
    r = math.hypot(u, v)
    if r > 1.0:
        u, v = u / r, v / r
    return [u, v, max(-1.0, min(1.0, m))]


def wheel_vector(name, value):
    """Per-channel amount of a wheel: its colour push plus its master."""
    u, v, m = clean_wheel(value)
    reach = WHEEL_REACH[name]
    return push_rgb(u * reach, v * reach) + m * WHEEL_MASTER[name]


def wheels(rgb, lift=NEUTRAL_WHEEL, gamma=NEUTRAL_WHEEL, gain=NEUTRAL_WHEEL, offset=NEUTRAL_WHEEL,
           contrast=100.0, pivot=43.5, saturation=100.0):
    """Lift, gamma, gain and offset wheels, then contrast about a pivot and
    saturation: ``(gain * (x + lift * (1 - x))) ^ (1 / gamma) + offset``."""
    lift_c, gain_c = wheel_vector("lift", lift), 1.0 + wheel_vector("gain", gain)
    gamma_c, offset_c = 2.0 ** wheel_vector("gamma", gamma), wheel_vector("offset", offset)
    x = gain_c * (rgb + lift_c * (1.0 - rgb))
    x = np.sign(x) * np.abs(x) ** (1.0 / np.maximum(gamma_c, 1e-3))
    x = x + offset_c
    p = float(pivot) / 100.0
    x = (x - p) * (float(contrast) / 100.0) + p
    s = float(saturation) / 100.0
    if s != 1.0:
        y = (KR * x[..., 0] + KG * x[..., 1] + KB * x[..., 2])[..., None]
        x = y + (x - y) * s
    return np.clip(x, 0.0, 1.0)


# -- hue / saturation curves ---------------------------------------------------
HUE_CURVES = [("hue_hue", "Hue vs Hue"), ("hue_sat", "Hue vs Sat"), ("hue_luma", "Hue vs Luma"),
              ("luma_sat", "Luma vs Sat"), ("sat_sat", "Sat vs Sat")]
PERIODIC = ("hue_hue", "hue_sat", "hue_luma")
FLAT = 0.5               # a curve's neutral height
HUE_SHIFT = 180.0        # degrees at the top and bottom of Hue vs Hue
LUMA_SHIFT = 0.5         # brightness change at the top and bottom of Hue vs Luma


def default_hue_curves():
    return {key: [] for key, _ in HUE_CURVES}


def clean_hue_curve(points, periodic):
    """A hue curve's points: sorted, clamped, thinned like an RGB curve. An
    empty list is a flat, neutral curve."""
    if not points:
        return []
    pts = cv.clean(points)
    if periodic:
        # A point at 1.0 is the same hue as one at 0.0.
        pts = [p for p in pts if p[0] < 1.0 - 1e-9] or [[0.0, pts[-1][1]]]
    elif pts == [[0.0, FLAT], [1.0, FLAT]]:
        return []
    return pts


def clean_hue_curves(value):
    out = default_hue_curves()
    if isinstance(value, dict):
        for key, _ in HUE_CURVES:
            try:
                out[key] = clean_hue_curve(value.get(key) or [], key in PERIODIC)
            except ValueError:
                pass
    return out


def hue_curve_is_neutral(points):
    return all(abs(p[1] - FLAT) < 1e-6 for p in points or [])


def evaluate_curve(points, x, periodic):
    """Heights of a hue curve at ``x`` (numpy array or float in 0..1). A
    periodic curve wraps from 1 back to 0; both are drawn as the same
    no-overshoot cubic as the RGB curves."""
    x = np.asarray(x, dtype=np.float64)
    if not points:
        return np.full_like(x, FLAT)
    if len(points) == 1:
        return np.full_like(x, points[0][1])
    if periodic:
        ext = [[p[0] - 1.0, p[1]] for p in points] + points + [[p[0] + 1.0, p[1]] for p in points]
    else:
        ext = points
    xs = np.array([p[0] for p in ext])
    ys = np.array([p[1] for p in ext])
    m = np.array(cv._slopes(list(xs), list(ys)))
    xx = np.mod(x, 1.0) if periodic else np.clip(x, xs[0], xs[-1])
    i = np.clip(np.searchsorted(xs, xx, side="right") - 1, 0, len(xs) - 2)
    h = xs[i + 1] - xs[i]
    t = (xx - xs[i]) / h
    t2, t3 = t * t, t * t * t
    v = ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i]
         + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1])
    return np.clip(v, 0.0, 1.0)


def hue_curves(rgb, curves):
    """Apply the five hue/saturation curves. Hue rotates and saturation
    scales the colour difference, which leaves greys and brightness alone;
    Hue vs Luma is weighted by saturation for the same reason."""
    c = clean_hue_curves(curves)
    y, cb, cr = to_ycc(rgb)
    h, s = hue_of(rgb), saturation_of(cb, cr)
    gain = np.ones_like(y)
    if not hue_curve_is_neutral(c["hue_sat"]):
        gain = gain * 2.0 * evaluate_curve(c["hue_sat"], h, True)
    if not hue_curve_is_neutral(c["luma_sat"]):
        gain = gain * 2.0 * evaluate_curve(c["luma_sat"], y, False)
    if not hue_curve_is_neutral(c["sat_sat"]):
        gain = gain * 2.0 * evaluate_curve(c["sat_sat"], s, False)
    if not hue_curve_is_neutral(c["hue_hue"]):
        a = np.radians((evaluate_curve(c["hue_hue"], h, True) - FLAT) * 2.0 * HUE_SHIFT)
        cb, cr = cb * np.cos(a) - cr * np.sin(a), cb * np.sin(a) + cr * np.cos(a)
    if not hue_curve_is_neutral(c["hue_luma"]):
        weight = np.clip(s / 0.2, 0.0, 1.0)
        y = y + (evaluate_curve(c["hue_luma"], h, True) - FLAT) * 2.0 * LUMA_SHIFT * weight
    return np.clip(from_ycc(y, cb * gain, cr * gain), 0.0, 1.0)


# -- secondary correction ------------------------------------------------------
MASK_VIEWS = [("off", "Off"), ("mask", "Mask"), ("highlight", "Highlight")]
SECONDARY_DEFAULTS = {
    "use_hue": True, "hue": 120.0, "hue_width": 50.0, "hue_soft": 30.0,
    "use_sat": True, "sat_low": 15.0, "sat_high": 100.0, "sat_soft": 10.0,
    "use_luma": True, "luma_low": 0.0, "luma_high": 100.0, "luma_soft": 10.0,
    "invert": False, "view": "off",
    "hue_shift": 0.0, "saturation": 100.0, "lightness": 0.0, "tint": [0.0, 0.0, 0.0],
}


def _smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _band(x, low, high, soft):
    """1 inside [low, high], easing to 0 over ``soft`` outside it."""
    if soft <= 1e-6:
        return ((x >= low) & (x <= high)).astype(np.float64)
    return _smooth((x - (low - soft)) / soft) * _smooth(((high + soft) - x) / soft)


def qualifier(rgb, p):
    """How strongly (0..1) each colour is selected by the qualifier ``p``."""
    y, cb, cr = to_ycc(rgb)
    key = np.ones_like(y)
    if p["use_hue"]:
        d = np.abs((hue_of(rgb) * 360.0 - float(p["hue"]) + 180.0) % 360.0 - 180.0)
        half = max(0.0, float(p["hue_width"])) / 2.0
        key = key * _band(d, -1.0, half, max(0.0, float(p["hue_soft"])))
        # Hue means nothing for greys: fade the key out towards no saturation.
        key = key * np.clip(saturation_of(cb, cr) / 0.04, 0.0, 1.0)
    if p["use_sat"]:
        key = key * _band(saturation_of(cb, cr) * 100.0, float(p["sat_low"]), float(p["sat_high"]),
                          max(0.0, float(p["sat_soft"])))
    if p["use_luma"]:
        key = key * _band(y * 100.0, float(p["luma_low"]), float(p["luma_high"]), max(0.0, float(p["luma_soft"])))
    return 1.0 - key if p["invert"] else key


def clean_secondary(params):
    p = dict(SECONDARY_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p:
            p[k] = clean_wheel(v) if k == "tint" else v
    return p


def secondary(rgb, params):
    """Correct only what the qualifier selects: hue shift, saturation,
    lightness and a tint push, blended in by the key. "mask" shows the key
    itself; "highlight" shows the selection over a dimmed grey picture."""
    p = clean_secondary(params)
    key = qualifier(rgb, p)[..., None]
    if p["view"] == "mask":
        return np.repeat(key, 3, axis=-1)
    y, cb, cr = to_ycc(rgb)
    a = math.radians(float(p["hue_shift"]))
    s = max(0.0, float(p["saturation"])) / 100.0
    cb, cr = (cb * math.cos(a) - cr * math.sin(a)) * s, (cb * math.sin(a) + cr * math.cos(a)) * s
    u, v, m = p["tint"]
    out = from_ycc(y + float(p["lightness"]) / 200.0 + m * 0.25, cb, cr) + push_rgb(u * 0.15, v * 0.15)
    out = rgb + (np.clip(out, 0.0, 1.0) - rgb) * key
    if p["view"] == "highlight":
        grey = np.repeat((y * 0.35 + 0.08)[..., None], 3, axis=-1)
        out = grey + (out - grey) * key
    return np.clip(out, 0.0, 1.0)


# -- grades by effect ------------------------------------------------------------
def _wheels_params(params):
    get = lambda k, d: params.get(k, d)
    return dict(lift=clean_wheel(get("lift", NEUTRAL_WHEEL)), gamma=clean_wheel(get("gamma", NEUTRAL_WHEEL)),
                gain=clean_wheel(get("gain", NEUTRAL_WHEEL)), offset=clean_wheel(get("offset", NEUTRAL_WHEEL)),
                contrast=float(get("contrast", 100.0)), pivot=float(get("pivot", 43.5)),
                saturation=float(get("saturation", 100.0)))


def canonical(kind, params):
    """The settings that decide a grade, in a stable form (None when the
    grade changes nothing and needs no filter)."""
    params = params or {}
    if kind == "wheels":
        p = _wheels_params(params)
        if all(p[k] == NEUTRAL_WHEEL for k, _ in WHEELS) and p["contrast"] == 100.0 and p["saturation"] == 100.0:
            return None
        return p
    if kind == "hue_curves":
        c = clean_hue_curves(params.get("curves"))
        if all(hue_curve_is_neutral(v) for v in c.values()):
            return None
        return c
    if kind == "secondary":
        p = clean_secondary(params)
        neutral = (p["view"] == "off" and p["hue_shift"] == 0.0 and p["saturation"] == 100.0
                   and p["lightness"] == 0.0 and p["tint"] == NEUTRAL_WHEEL)
        return None if neutral else p
    raise KeyError(kind)


def transform(kind, settings):
    """fn(rgb) -> rgb for canonical settings."""
    if kind == "wheels":
        return lambda rgb: wheels(rgb, **settings)
    if kind == "hue_curves":
        return lambda rgb: hue_curves(rgb, settings)
    return lambda rgb: secondary(rgb, settings)


def apply(kind, params, rgb):
    """The grade applied straight to an RGB array (what the LUT samples)."""
    settings = canonical(kind, params)
    return np.asarray(rgb, dtype=np.float64) if settings is None else transform(kind, settings)(
        np.asarray(rgb, dtype=np.float64))


# -- LUT files -------------------------------------------------------------------
def lattice(size=LUT_SIZE):
    """Every grid colour of a ``size``³ LUT in .cube order (red fastest)."""
    g = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    return np.stack([r, gg, b], axis=-1).reshape(-1, 3)


def write_cube(path, table, size=LUT_SIZE, title="Tideline"):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(fh, table, fmt="%.5f")
    os.replace(tmp, path)


def _prune(folder):
    try:
        names = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".cube")]
        if len(names) <= MAX_CACHED:
            return
        names.sort(key=lambda p: os.path.getmtime(p))
        for p in names[:len(names) - MAX_CACHED]:
            os.remove(p)
    except OSError:
        pass


def lut_file(kind, params, size=LUT_SIZE):
    """Path of a .cube LUT for the grade (made once and cached), or None when
    the grade changes nothing."""
    settings = canonical(kind, params)
    if settings is None:
        return None
    folder = CACHE_DIR or cache_dir()
    key = hashlib.sha1(json.dumps([LUT_VERSION, size, kind, settings], sort_keys=True).encode()).hexdigest()[:20]
    path = os.path.join(folder, f"{kind}-{key}.cube")
    if os.path.exists(path):
        try:
            os.utime(path)
        except OSError:
            pass
        return path
    os.makedirs(folder, exist_ok=True)
    write_cube(path, transform(kind, settings)(lattice(size)), size)
    _prune(folder)
    return path


def sample_qualifier(rgb):
    """Qualifier settings that select colours like ``rgb`` (0..1 floats):
    centred on its hue, with a saturation and brightness range around it."""
    arr = np.asarray(rgb, dtype=np.float64).reshape(1, 3)
    y, cb, cr = to_ycc(arr)
    s = float(saturation_of(cb, cr)[0]) * 100.0
    return {"hue": round(float(hue_of(arr)[0]) * 360.0, 1), "hue_width": 30.0, "hue_soft": 25.0,
            "sat_low": round(max(0.0, s - 30.0), 1), "sat_high": round(min(100.0, s + 40.0), 1),
            "luma_low": round(max(0.0, float(y[0]) * 100.0 - 35.0), 1),
            "luma_high": round(min(100.0, float(y[0]) * 100.0 + 35.0), 1),
            "use_hue": s > 4.0}
