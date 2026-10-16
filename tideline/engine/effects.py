"""Effect and transition registry.

Each effect maps a set of user-facing parameters onto one or more MLT filters.
The same definitions drive the Effects panel, the Effect Controls editors, and
the graph builder.

Every parameter can be keyframed. How the keyframes reach MLT depends on the
property behind it (``Param.anim``): most are handed over as an MLT animation;
the ones MLT only reads once are covered by several copies of the filter, each
limited to the frames over which the value is constant (``EffectDef.segments``).
"""

import math
from dataclasses import dataclass, field

from .. import keyframes as kf
from .. import procedural as proc
from . import mltlib
from .mltlib import color_to_mlt, mlt


DISCRETE = ("bool", "choice", "text", "font", "file")


@dataclass
class Param:
    name: str
    label: str
    type: str = "float"      # float | int | bool | color | choice | text | font | file | point
    default: object = 0.0
    min: float | None = None
    max: float | None = None
    step: float = 1.0
    digits: int = 1
    unit: str = ""
    key: bool = True         # keyframable
    choices: list = field(default_factory=list)   # [(value, label)]
    mlt: str | None = None   # MLT property name (generic effects)
    mul: float = 1.0
    add: float = 0.0
    conv: object = None      # optional callable(value) -> MLT value
    slider: bool = True
    # How keyframes are applied: "smooth" MLT animates the property itself,
    # "bake" one filter per frame the value changes on, "hold" the value only
    # changes at keyframes (filters that would restart if it moved every frame).
    anim: str = "smooth"

    @property
    def hold(self):
        """Keyframes step from one value to the next instead of blending."""
        return self.type in DISCRETE or self.anim == "hold"

    @property
    def segmented(self):
        """MLT cannot animate the property: it needs one filter per value."""
        return self.hold or self.anim == "bake"

    def to_mlt(self, v):
        if v is None:
            v = self.default
        if self.conv is not None:
            return self.conv(v)
        if self.type == "bool":
            return 1 if v else 0
        if self.type == "color":
            return color_to_mlt(v)
        if self.type in ("choice", "text", "font", "file"):
            return str(v)
        v = float(v) * self.mul + self.add
        return int(round(v)) if self.type == "int" else v

    def fmt(self, v):
        m = self.to_mlt(v)
        return f"{m:.6g}" if isinstance(m, float) else str(m)


def F(name, label, default=0.0, lo=None, hi=None, mlt_name=None, mul=1.0, add=0.0, unit="",
      step=1.0, digits=1, conv=None, anim="smooth"):
    return Param(name, label, "float", default, lo, hi, step, digits, unit,
                 mlt=mlt_name, mul=mul, add=add, conv=conv, anim=anim)


def I(name, label, default=0, lo=None, hi=None, mlt_name=None, unit="", anim="smooth"):
    return Param(name, label, "int", default, lo, hi, 1.0, 0, unit, mlt=mlt_name, anim=anim)


def B(name, label, default=False, mlt_name=None):
    return Param(name, label, "bool", default, mlt=mlt_name)


def C(name, label, default="#ffffffff", mlt_name=None):
    return Param(name, label, "color", default, mlt=mlt_name)


def CH(name, label, choices, default=None, mlt_name=None):
    return Param(name, label, "choice", default if default is not None else choices[0][0],
                 choices=choices, mlt=mlt_name)


@dataclass
class EffectDef:
    key: str
    label: str
    category: str
    kind: str                       # video | audio | any
    services: list                  # MLT filter services this effect creates
    params: list
    static: dict = field(default_factory=dict)
    intrinsic: bool = False
    stage: int = 1                  # render order: 1 applied effects, 2 motion, 3 opacity/level
    custom: object = None           # callable(filters, effect, ctx)
    hidden: bool = False
    motion: object = None           # callable(effect, ctx) -> fn(frame) -> (dx, dy, degrees, scale)
    preview: object = None          # callable(effect, ctx) -> [(label, fn(frame) -> value)]
    baked: bool = False             # computed per frame here, so no parameter needs segments

    def fits(self, kind):
        """True when the effect can sit on a track of that kind."""
        return self.kind in (kind, "any")

    def param(self, name):
        for p in self.params:
            if p.name == name:
                return p
        return None

    def available(self):
        return all(mltlib.has_service("filter", s) for s in self.services)

    def create(self, profile):
        filters = [mlt.Filter(profile, s) for s in self.services]
        if not all(f.is_valid() for f in filters):
            return []
        for k, v in self.static.items():
            filters[0].set(k, v)
        return filters

    def segments(self, effect, ctx):
        """Stretches of a cut over which every parameter MLT cannot animate
        keeps one value: ``[(first, last)]`` in source frames, each needing
        its own copy of the filters. ``last`` is None when the whole cut is one
        stretch, and the result is ``[None]`` when nothing is stepped at all."""
        stepped = [(p, effect.keyframes.get(p.name)) for p in self.params if p.key and p.segmented]
        stepped = [(p, kfs) for p, kfs in stepped if kfs]
        if self.baked or not stepped:
            return [None]
        lo, hi = (int(v) for v in ctx.get("cut") or ctx.get("range") or (0, 0))
        starts = {lo}
        for p, kfs in stepped:
            if p.hold:
                starts.update(k[0] for k in kfs[1:] if lo < k[0] <= hi)    # the first key also rules before it
                continue
            last = p.fmt(effect.value(p.name, lo))
            for t in range(max(lo + 1, kfs[0][0]), min(hi, kfs[-1][0]) + 1):
                text = p.fmt(effect.value(p.name, t))
                if text != last:
                    starts.add(t)
                last = text
        starts = sorted(starts)
        if starts[:2] == [0, 1]:
            # A filter cannot be limited to frame 0 alone (an out point of 0
            # means "no limit"), so the source's very first frame shares the
            # value of the second.
            starts.pop(1)
        if len(starts) == 1:
            return [(lo, None)]
        return list(zip(starts, [t - 1 for t in starts[1:]] + [max(hi, starts[-1])]))

    def apply(self, filters, effect, ctx):
        """Push the effect's current parameters into its MLT filters."""
        if self.custom is not None:
            self.custom(filters, effect, ctx)
            return
        f = filters[0]
        f.set("disable", 0 if effect.enabled else 1)
        t0, shift = segment_of(ctx)
        for p in self.params:
            if not p.mlt:
                continue
            kfs = effect.keyframes.get(p.name) if p.key else None
            if not kfs:
                f.set(p.mlt, p.to_mlt(effect.params.get(p.name, p.default)))
            elif p.segmented:
                f.set(p.mlt, p.to_mlt(effect.value(p.name, t0)))
            elif p.conv is not None:
                # The conversion need not be linear, so MLT cannot be left to
                # interpolate between converted keyframes.
                times = range(kfs[0][0], kfs[-1][0] + 1)
                f.set(p.mlt, kf.sampled_to_mlt(times, lambda t: p.fmt(kf.evaluate(kfs, t)), shift))
            else:
                f.set(p.mlt, kf.to_mlt(kfs, p.fmt, shift))

    def default(self, name, ctx=None):
        p = self.param(name)
        if p is None:
            return None
        if p.name == "position" and p.default is None and ctx is not None:
            return [ctx["width"] / 2, ctx["height"] / 2]
        return p.default


def segment_of(ctx):
    """(frame at which stepped parameters are read, offset for animation
    times) for the filters being set up. A filter limited to a frame range
    counts animation time from its in point."""
    seg = ctx.get("segment")
    if not seg:
        return None, 0
    return seg[0], (-seg[0] if seg[1] is not None else 0)


def fade_gain(kind, x, fade_in):
    """Amplitude (0..1) of an audio fade curve at progress x in 0..1."""
    x = max(0.0, min(1.0, x))
    if not fade_in:
        x = 1.0 - x
    if kind == "constant_power":
        return math.sin(x * math.pi / 2)
    if kind == "exponential_fade":
        return x * x
    return x


def gain_to_db(g):
    return 20.0 * math.log10(g) if g > 0.001 else -60.0


BLEND_MODES = [
    ("normal", "Normal"), ("add", "Add"), ("multiply", "Multiply"), ("screen", "Screen"),
    ("overlay", "Overlay"), ("darken", "Darken"), ("lighten", "Lighten"),
    ("colordodge", "Color Dodge"), ("colorburn", "Color Burn"), ("hardlight", "Hard Light"),
    ("softlight", "Soft Light"), ("difference", "Difference"), ("exclusion", "Exclusion"),
    ("hslhue", "Hue"), ("hslsaturation", "Saturation"), ("hslcolor", "Color"),
    ("hslluminosity", "Luminosity"),
]


# -- custom appliers -----------------------------------------------------
def _apply_motion(filters, effect, ctx):
    f = filters[0]
    W, H = ctx["width"], ctx["height"]
    uniform = effect.params.get("uniform", True)

    def rect_at(t):
        pos = effect.value("position", t) or [W / 2, H / 2]
        sh = max(0.01, float(effect.value("scale", t, 100.0)))
        sw = sh if uniform else max(0.01, float(effect.value("scale_w", t, 100.0)))
        w, h = W * sw / 100.0, H * sh / 100.0
        return f"{pos[0] - w / 2:.3f} {pos[1] - h / 2:.3f} {w:.3f} {h:.3f} 1"

    names = ("position", "scale") if uniform else ("position", "scale", "scale_w")
    times = kf.sample_times(*[effect.keyframes.get(n) for n in names])
    rect = kf.sampled_to_mlt(times, rect_at) if times else rect_at(None)
    f.set("transition.rect", rect)
    rot = effect.keyframes.get("rotation")
    if rot:
        f.set("transition.fix_rotate_x", kf.to_mlt(rot))
    else:
        f.set("transition.fix_rotate_x", float(effect.params.get("rotation", 0.0)))
    f.set("transition.distort", 0 if uniform else 1)
    identity = (not times and not rot and float(effect.params.get("rotation", 0.0)) == 0.0
                and rect == f"0.000 0.000 {W:.3f} {H:.3f} 1")
    f.set("disable", 1 if (identity or not effect.enabled) else 0)


def _apply_opacity(filters, effect, ctx):
    fade, blend = filters
    t0, shift = segment_of(ctx)
    kfs = effect.keyframes.get("opacity")
    if kfs:
        fade.set("alpha", kf.to_mlt(kfs, lambda v: f"{max(0.0, min(100.0, v)) / 100:.5f}", shift))
        opaque = False
    else:
        v = max(0.0, min(100.0, float(effect.params.get("opacity", 100.0))))
        fade.set("alpha", v / 100.0)
        opaque = v >= 100.0
    fade.set("level", 1.0)
    fade.set("disable", 1 if (opaque or not effect.enabled) else 0)
    mode = effect.value("blend", t0, "normal")
    blend.set("mode", mode)
    blend.set("disable", 1 if (mode == "normal" or not effect.enabled) else 0)


def _apply_text(filters, effect, ctx):
    f = filters[0]
    W, H = ctx["width"], ctx["height"]
    t0, shift = segment_of(ctx)
    p = lambda name, default: effect.value(name, t0, default)
    f.set("disable", 0 if effect.enabled else 1)
    # The text, its font and its metrics are read once per filter: keyframes
    # on them split the effect into segments. Colours and the offset animate.
    f.set("argument", p("text", "Title") or " ")
    f.set("family", p("font", "Sans"))
    f.set("size", int(round(p("size", 96))))
    f.set("weight", 700 if p("bold", False) else 400)
    f.set("style", "italic" if p("italic", False) else "normal")
    for prop, name, default in (("fgcolour", "fill", "#ffffffff"), ("bgcolour", "background", "#00000000"),
                                ("olcolour", "stroke", "#000000ff")):
        kfs = effect.keyframes.get(name)
        f.set(prop, kf.to_mlt(kfs, color_to_mlt, shift) if kfs else color_to_mlt(effect.params.get(name, default)))
    f.set("outline", int(max(0, min(3, round(p("stroke_width", 0))))))
    f.set("pad", int(round(p("padding", 0))))
    f.set("halign", p("halign", "center"))
    f.set("valign", p("valign", "middle"))

    def geom(t):
        off = effect.value("offset", t) or [0.0, 0.0]
        return f"{off[0]:.2f} {off[1]:.2f} {W} {H} 1"

    times = kf.sample_times(effect.keyframes.get("offset"))
    f.set("geometry", kf.sampled_to_mlt(times, geom, shift) if times else geom(None))


def _apply_crop(filters, effect, ctx):
    f = filters[0]
    W, H = ctx["width"], ctx["height"]
    f.set("disable", 0 if effect.enabled else 1)

    def rect(t):
        l, r, tp, b = (max(0.0, min(100.0, float(effect.value(n, t, 0.0))))
                       for n in ("left", "right", "top", "bottom"))
        w = max(0.0, 100.0 - l - r) * W / 100.0
        h = max(0.0, 100.0 - tp - b) * H / 100.0
        return f"{l * W / 100:.2f} {tp * H / 100:.2f} {w:.2f} {h:.2f} 1"

    times = kf.sample_times(*[effect.keyframes.get(n) for n in ("left", "right", "top", "bottom")])
    f.set("rect", kf.sampled_to_mlt(times, rect) if times else rect(None))
    round_kfs = effect.keyframes.get("roundness")
    if round_kfs:
        f.set("radius", kf.to_mlt(round_kfs, lambda v: f"{max(0.0, min(100.0, v)) / 100:.4f}"))
    else:
        f.set("radius", float(effect.params.get("roundness", 0.0)) / 100.0)
    f.set("circle", 0)
    f.set("color", "#00000000")


def _apply_volume(filters, effect, ctx):
    f = filters[0]
    kfs = effect.keyframes.get("level")
    if kfs:
        f.set("level", kf.to_mlt(kfs, lambda v: f"{max(-70.0, v):.3f}"))
        flat = False
    else:
        v = float(effect.params.get("level", 0.0))
        f.set("level", max(-70.0, v))
        flat = v == 0.0
    f.set("disable", 1 if (flat or not effect.enabled) else 0)


def _balance_gains(v):
    """(left dB, right dB) for a balance value in -100..100."""
    b = max(-100.0, min(100.0, float(v))) / 100.0
    left = 1.0 if b <= 0 else 1.0 - b
    right = 1.0 if b >= 0 else 1.0 + b
    return gain_to_db(left), gain_to_db(right)


def _apply_pan(filters, effect, ctx):
    # MLT's panner cannot be keyframed reliably, so balance is two volume
    # filters, one masked to each channel.
    left, right = filters
    left.set("channel_mask", 1)
    right.set("channel_mask", 2)
    kfs = effect.keyframes.get("balance")
    if kfs:
        times = range(kfs[0][0], kfs[-1][0] + 1)
        left.set("level", kf.sampled_to_mlt(times, lambda t: f"{_balance_gains(kf.evaluate(kfs, t))[0]:.2f}"))
        right.set("level", kf.sampled_to_mlt(times, lambda t: f"{_balance_gains(kf.evaluate(kfs, t))[1]:.2f}"))
        centre = False
    else:
        v = float(effect.params.get("balance", 0.0))
        l, r = _balance_gains(v)
        left.set("level", l)
        right.set("level", r)
        centre = v == 0.0
    for f in filters:
        f.set("disable", 1 if (centre or not effect.enabled) else 0)


# -- generated motion ------------------------------------------------------
# A generated effect is baked per frame over the part of the source the cut
# shows (``ctx["range"]``); ``ctx["clip"]`` is the clip's own first and last
# source frame.
def _frames(ctx):
    lo, hi = ctx.get("range") or (0, 0)
    return range(int(lo), int(hi) + 1)


def _at(effect, name, default):
    """fn(frame) -> a parameter's value there: constant unless keyframed."""
    kfs = effect.keyframes.get(name)
    if kfs:
        return lambda t: kf.evaluate(kfs, t)
    value = effect.params.get(name, default)
    return lambda t: value


def _cycles(effect, name, default, ctx, per_second=1.0):
    """fn(frame) -> cycles completed by then, for a rate parameter given in
    cycles per ``1 / per_second`` seconds. A keyframed rate is summed frame by
    frame, so the motion speeds up and slows down without jumping."""
    k = per_second / ctx["fps"]
    kfs = effect.keyframes.get(name)
    if not kfs:
        rate = float(effect.params.get(name, default))
        return lambda t: t * k * rate
    total = [0.0]                # total[i]: cycles before source frame i

    def at(t):
        i = max(0, int(math.floor(t)))
        while len(total) <= i:
            total.append(total[-1] + float(kf.evaluate(kfs, len(total) - 1)) * k)
        return total[i] + (t - i) * float(kf.evaluate(kfs, t)) * k

    return at


def _amount(effect, t, default=100.0):
    return max(0.0, float(effect.value("amount", t, default))) / 100.0


def _steps(pairs, fmt):
    """Per-frame (frame, value) pairs -> MLT animation that only lists changes."""
    parts, last = [], None
    for t, v in pairs:
        text = fmt(v)
        if text != last:
            parts.append(f"{t}|={text}")
            last = text
    return ";".join(parts) or "0"


def _apply_generated_motion(filters, effect, ctx):
    f = filters[0]
    W, H = ctx["width"], ctx["height"]
    if not effect.enabled:
        f.set("disable", 1)
        return
    at = EFFECTS[effect.type].motion(effect, ctx)
    rects, rots = [], []
    for t in _frames(ctx):
        dx, dy, rot, sc = at(t)
        w, h = W * max(0.01, sc), H * max(0.01, sc)
        rects.append(f"{t}={W / 2 + dx - w / 2:.2f} {H / 2 + dy - h / 2:.2f} {w:.2f} {h:.2f} 1")
        rots.append(f"{t}={rot:.3f}")
    f.set("transition.rect", ";".join(rects))
    f.set("transition.fix_rotate_x", ";".join(rots))
    f.set("transition.distort", 0)
    f.set("disable", 0)


def _motion_preview(effect, ctx):
    at = EFFECTS[effect.type].motion(effect, ctx)
    return [("X", lambda t: at(t)[0]), ("Y", lambda t: at(t)[1]),
            ("Rotation", lambda t: at(t)[2]), ("Scale", lambda t: at(t)[3] * 100.0)]


def _shake_motion(effect, ctx):
    W, H = ctx["width"], ctx["height"]
    g = lambda name, default: _at(effect, name, default)
    x, y, rot, zoom = g("x", 30.0), g("y", 30.0), g("rotation", 1.0), g("zoom", 0.0)
    rough, seed, fit = g("roughness", 2), g("seed", 0), g("fit", True)
    cycles = _cycles(effect, "frequency", 4.0, ctx)

    def at(t):
        a, u = _amount(effect, t), cycles(t)
        sx, sy, sr, sz = float(x(t)) * a, float(y(t)) * a, float(rot(t)) * a, float(zoom(t)) / 100.0 * a
        s, octaves = int(seed(t)) * 4 + 1, max(1, int(round(rough(t))))
        wobble = proc.fractal(s + 3, u, octaves) * sz
        base = (1.0 + sz) * proc.cover_scale(W, H, sx, sy, sr) if fit(t) else 1.0
        return (proc.fractal(s, u, octaves) * sx, proc.fractal(s + 1, u, octaves) * sy,
                proc.fractal(s + 2, u, octaves) * sr, base + wobble)

    return at


def _oscillate_motion(effect, ctx):
    g = lambda name, default: _at(effect, name, default)
    kind, phase, circular = g("wave", "sine"), g("phase", 0.0), g("circular", False)
    x, y, rot, scale = g("x", 0.0), g("y", 40.0), g("rotation", 0.0), g("scale", 0.0)
    cycles = _cycles(effect, "frequency", 1.0, ctx)

    def at(t):
        a, ph = _amount(effect, t), cycles(t) + float(phase(t)) / 360.0
        w = proc.wave(kind(t), ph)
        wx = proc.wave(kind(t), ph + 0.25) if circular(t) else w
        return (wx * float(x(t)) * a, w * float(y(t)) * a, w * float(rot(t)) * a,
                1.0 + w * float(scale(t)) / 100.0 * a)

    return at


def _spin_motion(effect, ctx):
    start, turns = _at(effect, "start", 0.0), _cycles(effect, "speed", 0.5, ctx)
    return lambda t: (0.0, 0.0, float(start(t)) + 360.0 * turns(t), 1.0)


def _pulse_motion(effect, ctx):
    g = lambda name, default: _at(effect, name, default)
    offset, size, sharp = g("offset", 0.0), g("size", 10.0), g("sharpness", 6.0)
    beats = _cycles(effect, "bpm", 120.0, ctx, per_second=1 / 60.0)

    def at(t):
        b = beats(t) + float(offset(t)) / 100.0
        return (0.0, 0.0, 0.0, 1.0 + float(size(t)) / 100.0 * _amount(effect, t)
                * math.exp(-float(sharp(t)) * (b - math.floor(b))))

    return at


def _pan_zoom_motion(effect, ctx):
    W, H = ctx["width"], ctx["height"]
    first, last = ctx.get("clip") or ctx.get("range") or (0, 0)
    g = lambda name, default: _at(effect, name, default)
    interp = g("ease", "ease")
    sx, sy, ss = g("start_x", 0.0), g("start_y", 0.0), g("start_scale", 100.0)
    ex, ey, es = g("end_x", 0.0), g("end_y", 0.0), g("end_scale", 120.0)

    def at(t):
        u = kf.ease(max(0.0, min(1.0, (t - first) / max(1, last - first))), interp(t))
        mix = lambda a, b: float(a(t)) + (float(b(t)) - float(a(t))) * u
        return mix(sx, ex) * W / 100.0, mix(sy, ey) * H / 100.0, 0.0, mix(ss, es) / 100.0

    return at


def _strobe_level(effect, ctx):
    duty, low = _at(effect, "duty", 50.0), _at(effect, "low", 0.0)
    cycles = _cycles(effect, "frequency", 8.0, ctx)

    def at(t):
        ph = cycles(t)
        return 1.0 if ph - math.floor(ph) < float(duty(t)) / 100.0 else max(0.0, min(100.0, float(low(t)))) / 100.0

    return at


def _apply_strobe(filters, effect, ctx):
    f = filters[0]
    at = _strobe_level(effect, ctx)
    f.set("alpha", _steps(((t, at(t)) for t in _frames(ctx)), lambda v: f"{v:.4f}"))
    f.set("level", 1.0)
    f.set("disable", 0 if effect.enabled else 1)


def _flicker_level(effect, ctx):
    seed, cycles = _at(effect, "seed", 0), _cycles(effect, "speed", 12.0, ctx)
    return lambda t: max(0.0, 1.0 - min(1.0, _amount(effect, t, 30.0))
                         * (0.5 + 0.5 * proc.noise(int(seed(t)) * 4 + 2, cycles(t))))


def _apply_flicker(filters, effect, ctx):
    f = filters[0]
    at = _flicker_level(effect, ctx)
    f.set("level", ";".join(f"{t}={at(t):.4f}" for t in _frames(ctx)) or "1")
    f.set("disable", 0 if effect.enabled else 1)


def _db_to_linear(v):
    return 10 ** (float(v) / 20.0)


HALIGN = [("left", "Left"), ("center", "Centre"), ("right", "Right")]
VALIGN = [("top", "Top"), ("middle", "Middle"), ("bottom", "Bottom")]

EFFECTS = {}


def _reg(*defs):
    for d in defs:
        EFFECTS[d.key] = d


# -- intrinsic effects (always present on a clip) -------------------------
_reg(
    EffectDef("motion", "Motion", "Fixed", "video", ["affine"], [
        Param("position", "Position", "point", None, unit="px"),
        F("scale", "Scale", 100.0, 0.0, 1000.0, unit="%"),
        F("scale_w", "Scale Width", 100.0, 0.0, 1000.0, unit="%"),
        Param("uniform", "Uniform Scale", "bool", True, key=False),   # links the two scale rows
        F("rotation", "Rotation", 0.0, -3600.0, 3600.0, unit="°"),
    ], static={"background": "color:#00000000", "transition.fill": 1, "transition.valign": "middle",
               "transition.halign": "center", "transition.threads": 0},
        intrinsic=True, stage=2, custom=_apply_motion),
    EffectDef("opacity", "Opacity", "Fixed", "video", ["brightness", "cairoblend_mode"], [
        F("opacity", "Opacity", 100.0, 0.0, 100.0, unit="%"),
        CH("blend", "Blend Mode", BLEND_MODES),
    ], intrinsic=True, stage=3, custom=_apply_opacity),
    EffectDef("volume", "Volume", "Fixed", "audio", ["volume"], [
        F("level", "Level", 0.0, -60.0, 15.0, unit="dB"),
    ], intrinsic=True, stage=2, custom=_apply_volume),
    EffectDef("pan", "Panner", "Fixed", "audio", ["volume", "volume"], [
        F("balance", "Balance", 0.0, -100.0, 100.0, digits=0),
    ], intrinsic=True, stage=3, custom=_apply_pan),
)

# -- time remapping ---------------------------------------------------------
# Not a filter: the builder reads it to retime the clip's producer (see
# timeremap.py). It is added from Clip > Time Remapping, on both halves of a
# linked clip, and kept the same on both.
_reg(EffectDef("time_remap", "Time Remapping", "Fixed", "any", [], [
    F("speed", "Speed", 100.0, -1000.0, 1000.0, unit="%"),
    Param("blend", "Frame Blending", "bool", False, key=False),
], hidden=True, stage=0, custom=lambda filters, effect, ctx: None))

# -- text -----------------------------------------------------------------
_reg(EffectDef("text", "Text", "Generate", "video", ["qtext"], [
    Param("text", "Text", "text", "Title"),
    Param("font", "Font", "font", "Sans"),
    I("size", "Size", 96, 4, 800, unit="px", anim="bake"),
    B("bold", "Bold", False),
    B("italic", "Italic", False),
    C("fill", "Fill", "#ffffffff"),
    C("stroke", "Stroke", "#000000ff"),
    I("stroke_width", "Stroke Width", 0, 0, 3, anim="bake"),
    C("background", "Background", "#00000000"),
    I("padding", "Padding", 0, 0, 200, unit="px", anim="bake"),
    CH("halign", "Align", HALIGN, "center"),
    CH("valign", "Vertical Align", VALIGN, "middle"),
    Param("offset", "Offset", "point", [0.0, 0.0], unit="px"),
], custom=_apply_text))

# -- video: animate -------------------------------------------------------
_MOTION_STATIC = {"background": "color:#00000000", "transition.fill": 1, "transition.valign": "middle",
                  "transition.halign": "center", "transition.threads": 0}
_AMOUNT = lambda: F("amount", "Amount", 100.0, 0.0, 400.0, unit="%")
_PAN_EASES = [("linear", "Linear"), ("ease", "Ease In/Out"), ("ease_in", "Ease In"), ("ease_out", "Ease Out"),
              ("sine", "Sine In/Out"), ("expo_out", "Expo Out"), ("back_out", "Overshoot")]


def _generated(key, label, motion, params):
    return EffectDef(key, label, "Animate", "video", ["affine"], params, static=dict(_MOTION_STATIC),
                     custom=_apply_generated_motion, motion=motion, preview=_motion_preview, baked=True)


_reg(
    _generated("shake", "Shake", _shake_motion, [
        _AMOUNT(),
        F("x", "Horizontal", 30.0, 0.0, 1000.0, unit="px"),
        F("y", "Vertical", 30.0, 0.0, 1000.0, unit="px"),
        F("rotation", "Rotation", 1.0, 0.0, 90.0, unit="°", step=0.1),
        F("zoom", "Zoom", 0.0, 0.0, 100.0, unit="%"),
        F("frequency", "Frequency", 4.0, 0.1, 30.0, unit="Hz", step=0.1),
        I("roughness", "Roughness", 2, 1, 5),
        I("seed", "Random Seed", 0, 0, 999),
        B("fit", "Scale to Hide Edges", True),
    ]),
    _generated("oscillate", "Oscillate", _oscillate_motion, [
        _AMOUNT(),
        CH("wave", "Wave", proc.WAVES),
        F("frequency", "Frequency", 1.0, 0.01, 30.0, unit="Hz", step=0.05, digits=2),
        F("phase", "Phase", 0.0, -360.0, 360.0, unit="°"),
        F("x", "Horizontal", 0.0, -2000.0, 2000.0, unit="px"),
        F("y", "Vertical", 40.0, -2000.0, 2000.0, unit="px"),
        F("rotation", "Rotation", 0.0, -360.0, 360.0, unit="°"),
        F("scale", "Scale", 0.0, -100.0, 400.0, unit="%"),
        B("circular", "Circular Motion", False),
    ]),
    _generated("spin", "Spin", _spin_motion, [
        F("speed", "Speed", 0.5, -20.0, 20.0, unit="rev/s", step=0.05, digits=2),
        F("start", "Start Angle", 0.0, -360.0, 360.0, unit="°"),
    ]),
    _generated("pulse", "Pulse", _pulse_motion, [
        _AMOUNT(),
        F("bpm", "Tempo", 120.0, 1.0, 600.0, unit="BPM"),
        F("size", "Size", 10.0, -90.0, 300.0, unit="%"),
        F("sharpness", "Sharpness", 6.0, 0.5, 30.0, step=0.5),
        F("offset", "Beat Offset", 0.0, 0.0, 100.0, unit="%"),
    ]),
    _generated("pan_zoom", "Pan & Zoom", _pan_zoom_motion, [
        F("start_scale", "Start Scale", 100.0, 1.0, 1000.0, unit="%"),
        F("end_scale", "End Scale", 120.0, 1.0, 1000.0, unit="%"),
        F("start_x", "Start X", 0.0, -200.0, 200.0, unit="%"),
        F("start_y", "Start Y", 0.0, -200.0, 200.0, unit="%"),
        F("end_x", "End X", 0.0, -200.0, 200.0, unit="%"),
        F("end_y", "End Y", 0.0, -200.0, 200.0, unit="%"),
        CH("ease", "Curve", _PAN_EASES, "ease"),
    ]),
    EffectDef("strobe", "Strobe", "Animate", "video", ["brightness"], [
        F("frequency", "Frequency", 8.0, 0.1, 30.0, unit="Hz", step=0.1),
        F("duty", "On Time", 50.0, 1.0, 99.0, unit="%"),
        F("low", "Off Opacity", 0.0, 0.0, 100.0, unit="%"),
    ], custom=_apply_strobe, baked=True,
        preview=lambda e, ctx: [("Opacity", lambda t, at=_strobe_level(e, ctx): at(t) * 100.0)]),
    EffectDef("flicker", "Flicker", "Animate", "video", ["brightness"], [
        F("amount", "Amount", 30.0, 0.0, 100.0, unit="%"),
        F("speed", "Speed", 12.0, 0.1, 60.0, unit="Hz", step=0.5),
        I("seed", "Random Seed", 0, 0, 999),
    ], custom=_apply_flicker, baked=True,
        preview=lambda e, ctx: [("Brightness", lambda t, at=_flicker_level(e, ctx): at(t) * 100.0)]),
)

# -- video: colour --------------------------------------------------------
_reg(
    EffectDef("brightness_contrast", "Brightness & Contrast", "Color", "video", ["avfilter.eq"], [
        F("brightness", "Brightness", 0.0, -100.0, 100.0, "av.brightness", mul=0.01),
        F("contrast", "Contrast", 0.0, -100.0, 100.0, "av.contrast", mul=0.01, add=1.0),
        F("saturation", "Saturation", 100.0, 0.0, 300.0, "av.saturation", mul=0.01, unit="%"),
        F("gamma", "Gamma", 1.0, 0.1, 5.0, "av.gamma", step=0.01, digits=2),
    ]),
    EffectDef("color_wheels", "Lift / Gamma / Gain", "Color", "video", ["lift_gamma_gain"], [
        F("lift_r", "Lift Red", 0.0, -100.0, 100.0, "lift_r", mul=0.005),
        F("lift_g", "Lift Green", 0.0, -100.0, 100.0, "lift_g", mul=0.005),
        F("lift_b", "Lift Blue", 0.0, -100.0, 100.0, "lift_b", mul=0.005),
        F("gamma_r", "Gamma Red", 100.0, 10.0, 300.0, "gamma_r", mul=0.01),
        F("gamma_g", "Gamma Green", 100.0, 10.0, 300.0, "gamma_g", mul=0.01),
        F("gamma_b", "Gamma Blue", 100.0, 10.0, 300.0, "gamma_b", mul=0.01),
        F("gain_r", "Gain Red", 100.0, 0.0, 400.0, "gain_r", mul=0.01),
        F("gain_g", "Gain Green", 100.0, 0.0, 400.0, "gain_g", mul=0.01),
        F("gain_b", "Gain Blue", 100.0, 0.0, 400.0, "gain_b", mul=0.01),
    ]),
    EffectDef("white_balance", "White Balance", "Color", "video", ["avfilter.colortemperature"], [
        F("temperature", "Temperature", 6500.0, 1000.0, 15000.0, "av.temperature", unit="K",
          step=50, digits=0),
        F("strength", "Strength", 100.0, 0.0, 100.0, "av.mix", mul=0.01, unit="%"),
    ]),
    EffectDef("hue_saturation", "Hue / Saturation", "Color", "video", ["avfilter.hue"], [
        F("hue", "Hue", 0.0, -180.0, 180.0, "av.h", unit="°"),
        F("saturation", "Saturation", 100.0, 0.0, 300.0, "av.s", mul=0.01, unit="%"),
        F("lightness", "Lightness", 0.0, -100.0, 100.0, "av.b", mul=0.1),
    ]),
    EffectDef("vibrance", "Vibrance", "Color", "video", ["avfilter.vibrance"], [
        F("intensity", "Vibrance", 0.0, -200.0, 200.0, "av.intensity", mul=0.01),
    ]),
    EffectDef("exposure", "Exposure", "Color", "video", ["avfilter.exposure"], [
        F("exposure", "Exposure", 0.0, -3.0, 3.0, "av.exposure", unit="EV", step=0.05, digits=2),
        F("black", "Black Level", 0.0, -1.0, 1.0, "av.black", step=0.01, digits=2),
    ]),
    EffectDef("color_balance", "Color Balance", "Color", "video", ["avfilter.colorbalance"], [
        F("rs", "Shadows Red", 0.0, -100.0, 100.0, "av.rs", mul=0.01),
        F("gs", "Shadows Green", 0.0, -100.0, 100.0, "av.gs", mul=0.01),
        F("bs", "Shadows Blue", 0.0, -100.0, 100.0, "av.bs", mul=0.01),
        F("rm", "Midtones Red", 0.0, -100.0, 100.0, "av.rm", mul=0.01),
        F("gm", "Midtones Green", 0.0, -100.0, 100.0, "av.gm", mul=0.01),
        F("bm", "Midtones Blue", 0.0, -100.0, 100.0, "av.bm", mul=0.01),
        F("rh", "Highlights Red", 0.0, -100.0, 100.0, "av.rh", mul=0.01),
        F("gh", "Highlights Green", 0.0, -100.0, 100.0, "av.gh", mul=0.01),
        F("bh", "Highlights Blue", 0.0, -100.0, 100.0, "av.bh", mul=0.01),
    ]),
    EffectDef("levels", "Levels", "Color", "video", ["frei0r.levels"], [
        CH("channel", "Channel", [("0.3", "Luma"), ("0", "Red"), ("0.1", "Green"), ("0.2", "Blue")],
           "0.3", mlt_name="0"),
        F("in_black", "Input Black", 0.0, 0.0, 100.0, "1", mul=0.01),
        F("in_white", "Input White", 100.0, 0.0, 100.0, "2", mul=0.01),
        F("gamma", "Gamma", 1.0, 0.01, 4.0, "3", mul=0.25, step=0.01, digits=2),
        F("out_black", "Output Black", 0.0, 0.0, 100.0, "4", mul=0.01),
        F("out_white", "Output White", 100.0, 0.0, 100.0, "5", mul=0.01),
    ], static={"6": 0}),
    EffectDef("tint", "Tint", "Color", "video", ["frei0r.tint0r"], [
        C("black", "Map Black To", "#000000ff", "0"),
        C("white", "Map White To", "#ffd9a0ff", "1"),
        F("amount", "Amount", 50.0, 0.0, 100.0, "2", mul=0.01, unit="%"),
    ]),
    EffectDef("curve_preset", "Tone Curve Preset", "Color", "video", ["avfilter.curves"], [
        CH("preset", "Curve", [("medium_contrast", "Medium Contrast"), ("increase_contrast", "More Contrast"),
                               ("strong_contrast", "Strong Contrast"), ("linear_contrast", "Linear Contrast"),
                               ("lighter", "Lighter"), ("darker", "Darker"), ("vintage", "Vintage"),
                               ("cross_process", "Cross Process"), ("color_negative", "Colour Negative"),
                               ("negative", "Negative")], mlt_name="av.preset"),
    ]),
    EffectDef("lut", "Apply LUT", "Color", "video", ["avfilter.lut3d"], [
        Param("file", "LUT File (.cube)", "file", "", mlt="av.file"),
        CH("interp", "Interpolation", [("tetrahedral", "Tetrahedral"), ("trilinear", "Trilinear"),
                                       ("nearest", "Nearest")], mlt_name="av.interp"),
    ]),
    EffectDef("black_white", "Black & White", "Color", "video", ["greyscale"], []),
    EffectDef("invert", "Invert", "Color", "video", ["invert"], []),
    EffectDef("sepia", "Sepia", "Color", "video", ["sepia"], [
        I("u", "Yellow–Blue", 75, 0, 255, "u"),
        I("v", "Cyan–Red", 150, 0, 255, "v"),
    ]),
)

# -- video: blur & sharpen -------------------------------------------------
_reg(
    EffectDef("gaussian_blur", "Gaussian Blur", "Blur & Sharpen", "video", ["avfilter.gblur"], [
        F("amount", "Blurriness", 10.0, 0.0, 200.0, "av.sigma"),
    ]),
    EffectDef("box_blur", "Box Blur", "Blur & Sharpen", "video", ["box_blur"], [
        F("h", "Horizontal Radius", 10.0, 0.0, 300.0, "hradius"),
        F("v", "Vertical Radius", 10.0, 0.0, 300.0, "vradius"),
    ]),
    EffectDef("directional_blur", "Directional Blur", "Blur & Sharpen", "video", ["avfilter.dblur"], [
        F("angle", "Direction", 0.0, 0.0, 360.0, "av.angle", unit="°"),
        F("radius", "Length", 10.0, 0.0, 200.0, "av.radius"),
    ]),
    EffectDef("sharpen", "Sharpen", "Blur & Sharpen", "video", ["frei0r.sharpness"], [
        F("amount", "Amount", 30.0, 0.0, 100.0, "0", mul=0.01),
        F("size", "Radius", 0.0, 0.0, 100.0, "1", mul=0.01),
    ]),
)

# -- video: distort & transform -------------------------------------------
_reg(
    EffectDef("crop", "Crop", "Transform", "video", ["qtcrop"], [
        F("left", "Left", 0.0, 0.0, 100.0, unit="%"),
        F("top", "Top", 0.0, 0.0, 100.0, unit="%"),
        F("right", "Right", 0.0, 0.0, 100.0, unit="%"),
        F("bottom", "Bottom", 0.0, 0.0, 100.0, unit="%"),
        F("roundness", "Corner Roundness", 0.0, 0.0, 100.0, unit="%"),
    ], custom=_apply_crop),
    EffectDef("flip_h", "Horizontal Flip", "Transform", "video", ["avfilter.hflip"], []),
    EffectDef("flip_v", "Vertical Flip", "Transform", "video", ["avfilter.vflip"], []),
    EffectDef("mirror", "Mirror", "Distort", "video", ["mirror"], [
        CH("mode", "Axis", [("horizontal", "Horizontal"), ("vertical", "Vertical"),
                            ("diagonal", "Diagonal"), ("xdiagonal", "Cross Diagonal")], mlt_name="mirror"),
        B("reverse", "Reverse Side", False, "reverse"),
    ]),
    EffectDef("corner_pin", "Corner Pin", "Distort", "video", ["frei0r.c0rners"], [
        F("x1", "Top Left X", 0.0, -100.0, 200.0, "0", mul=1 / 300, add=1 / 3, unit="%"),
        F("y1", "Top Left Y", 0.0, -100.0, 200.0, "1", mul=1 / 300, add=1 / 3, unit="%"),
        F("x2", "Top Right X", 100.0, -100.0, 200.0, "2", mul=1 / 300, add=1 / 3, unit="%"),
        F("y2", "Top Right Y", 0.0, -100.0, 200.0, "3", mul=1 / 300, add=1 / 3, unit="%"),
        F("x3", "Bottom Right X", 100.0, -100.0, 200.0, "4", mul=1 / 300, add=1 / 3, unit="%"),
        F("y3", "Bottom Right Y", 100.0, -100.0, 200.0, "5", mul=1 / 300, add=1 / 3, unit="%"),
        F("x4", "Bottom Left X", 0.0, -100.0, 200.0, "6", mul=1 / 300, add=1 / 3, unit="%"),
        F("y4", "Bottom Left Y", 100.0, -100.0, 200.0, "7", mul=1 / 300, add=1 / 3, unit="%"),
    ], static={"12": 1}),
    EffectDef("lens_distortion", "Lens Distortion", "Distort", "video", ["frei0r.defish0r"], [
        F("amount", "Curvature", 0.0, 0.0, 100.0, "0", mul=0.01),
        B("defish", "Remove Fisheye", False, "1"),
    ]),
    EffectDef("wave", "Wave Warp", "Distort", "video", ["wave"], [
        I("amplitude", "Amplitude", 10, 1, 200, "wave"),
        I("speed", "Speed", 5, 0, 100, "speed"),
        B("x", "Deform Horizontally", True, "deformX"),
        B("y", "Deform Vertically", True, "deformY"),
    ]),
)

# -- video: keying ---------------------------------------------------------
_reg(
    EffectDef("chroma_key", "Chroma Key", "Keying", "video", ["frei0r.bluescreen0r"], [
        C("color", "Key Colour", "#00ef00ff", "0"),
        F("tolerance", "Tolerance", 28.8, 0.0, 100.0, "1", mul=0.01),
        B("invert", "Invert", False, "2"),
    ]),
    EffectDef("luma_key", "Luma Key", "Keying", "video", ["lumakey"], [
        I("threshold", "Threshold", 128, 0, 255, "threshold"),
        I("slope", "Softness", 0, 0, 128, "slope"),
        I("prelevel", "Level Below", 0, 0, 255, "prelevel"),
        I("postlevel", "Level Above", 255, 0, 255, "postlevel"),
    ]),
)

# -- video: stylize --------------------------------------------------------
_reg(
    EffectDef("vignette", "Vignette", "Stylize", "video", ["vignette"], [
        F("radius", "Radius", 50.0, 0.0, 100.0, "radius", mul=0.01),
        F("feather", "Feather", 80.0, 0.0, 100.0, "smooth", mul=0.01),
        F("x", "Centre X", 50.0, 0.0, 100.0, "x", mul=0.01, unit="%"),
        F("y", "Centre Y", 50.0, 0.0, 100.0, "y", mul=0.01, unit="%"),
        F("opacity", "Edge Brightness", 0.0, 0.0, 100.0, "opacity", mul=0.01, unit="%"),
    ]),
    EffectDef("glow", "Glow", "Stylize", "video", ["frei0r.glow"], [
        F("amount", "Radius", 30.0, 0.0, 100.0, "0", mul=0.01),
    ]),
    EffectDef("drop_shadow", "Drop Shadow", "Stylize", "video", ["dropshadow"], [
        C("color", "Colour", "#000000b4", "color"),
        F("radius", "Softness", 8.0, 0.0, 100.0, "radius"),
        F("x", "Offset X", 8.0, -500.0, 500.0, "x", unit="px"),
        F("y", "Offset Y", 8.0, -500.0, 500.0, "y", unit="px"),
    ]),
    EffectDef("posterize", "Posterize", "Stylize", "video", ["frei0r.posterize"], [
        F("levels", "Levels", 5.0, 2.0, 48.0, "0", mul=1 / 48, digits=0),
    ]),
    EffectDef("mosaic", "Mosaic", "Stylize", "video", ["frei0r.pixeliz0r"], [
        F("w", "Block Width", 2.0, 0.1, 50.0, "0", mul=0.01, unit="%"),
        F("h", "Block Height", 3.5, 0.1, 50.0, "1", mul=0.01, unit="%"),
    ]),
    EffectDef("threshold", "Threshold", "Stylize", "video", ["threshold"], [
        I("midpoint", "Level", 128, 0, 255, "midpoint", anim="bake"),
        B("invert", "Invert", False, "invert"),
    ]),
    EffectDef("charcoal", "Charcoal", "Stylize", "video", ["charcoal"], [
        I("x", "Line Width", 1, 1, 10, "x_scatter"),
        I("y", "Line Height", 1, 1, 10, "y_scatter"),
        F("contrast", "Contrast", 1.5, 0.0, 10.0, "scale", step=0.1),
        F("color", "Colour Amount", 0.0, 0.0, 10.0, "mix", step=0.1),
        B("invert", "Invert", False, "invert"),
    ]),
    EffectDef("cartoon", "Cartoon", "Stylize", "video", ["frei0r.cartoon"], [
        F("levels", "Edge Level", 100.0, 0.0, 100.0, "0", mul=0.01),
        F("diff", "Edge Width", 1.0, 0.0, 100.0, "1", mul=1 / 256),
    ]),
    EffectDef("emboss", "Emboss", "Stylize", "video", ["frei0r.emboss"], [
        F("azimuth", "Direction", 135.0, 0.0, 360.0, "0", mul=1 / 360, unit="°"),
        F("elevation", "Elevation", 30.0, 0.0, 90.0, "1", mul=1 / 90, unit="°"),
        F("width", "Relief", 10.0, 1.0, 40.0, "2", mul=1 / 40),
    ]),
    EffectDef("edge_detect", "Find Edges", "Stylize", "video", ["avfilter.edgedetect"], [
        F("low", "Low Threshold", 8.0, 0.0, 100.0, "av.low", mul=0.01, anim="bake"),
        F("high", "High Threshold", 20.0, 0.0, 100.0, "av.high", mul=0.01, anim="bake"),
        CH("mode", "Mode", [("wires", "Wires"), ("colormix", "Colour Mix"), ("canny", "Canny")],
           mlt_name="av.mode"),
    ]),
    EffectDef("noise", "Noise", "Stylize", "video", ["avfilter.noise"], [
        I("strength", "Amount", 20, 0, 100, "av.alls", anim="bake"),
    ], static={"av.allf": "t+u"}),
    EffectDef("film_grain", "Film Grain", "Stylize", "video", ["grain"], [
        I("noise", "Grain", 40, 0, 200, "noise"),
        I("contrast", "Contrast", 100, 0, 400, "contrast"),
        I("brightness", "Brightness", 100, 0, 400, "brightness"),
    ]),
    EffectDef("old_film", "Old Film", "Stylize", "video", ["oldfilm"], [
        I("delta", "Vertical Jitter", 14, 0, 400, "delta"),
        I("every", "Jitter Frequency", 20, 0, 100, "every", unit="%"),
        I("up", "Flicker Up", 20, 0, 100, "brightnessdelta_up"),
        I("down", "Flicker Down", 30, 0, 100, "brightnessdelta_down"),
        I("flicker", "Flicker Frequency", 70, 0, 100, "brightnessdelta_every", unit="%"),
    ]),
    EffectDef("rgb_split", "RGB Split", "Stylize", "video", ["frei0r.rgbsplit0r"], [
        F("v", "Vertical", 0.0, -100.0, 100.0, "0", mul=0.005, add=0.5),
        F("h", "Horizontal", 10.0, -100.0, 100.0, "1", mul=0.005, add=0.5),
    ]),
    EffectDef("glitch", "Glitch", "Stylize", "video", ["frei0r.glitch0r"], [
        F("frequency", "Frequency", 30.0, 0.0, 100.0, "0", mul=0.01),
        F("block", "Block Height", 50.0, 0.0, 100.0, "1", mul=0.01),
        F("shift", "Shift", 50.0, 0.0, 100.0, "2", mul=0.01),
        F("color", "Colour Glitch", 20.0, 0.0, 100.0, "3", mul=0.01),
    ]),
    EffectDef("timecode", "Timecode Burn-in", "Generate", "video", ["dynamictext"], [
        CH("content", "Show", [("#timecode#", "Timecode"), ("#frame#", "Frame Number"),
                               ("#resource#", "File Name")], mlt_name="argument"),
        I("size", "Size", 48, 8, 400, "size", unit="px", anim="bake"),
        C("fg", "Colour", "#ffffffff", "fgcolour"),
        C("bg", "Background", "#00000099", "bgcolour"),
        CH("halign", "Horizontal", [("left", "Left"), ("centre", "Centre"), ("right", "Right")],
           "centre", mlt_name="halign"),
        CH("valign", "Vertical", VALIGN, "bottom", mlt_name="valign"),
    ], static={"family": "Monospace", "pad": 8, "geometry": "0%/0%:100%x100%:100"}),
)

# -- audio -----------------------------------------------------------------
_reg(
    EffectDef("gain", "Gain", "Amplitude", "audio", ["volume"], [
        F("level", "Gain", 0.0, -60.0, 24.0, "level", unit="dB"),
    ]),
    EffectDef("normalize", "Dynamic Normalize", "Amplitude", "audio", ["avfilter.dynaudnorm"], [
        F("peak", "Target Peak", 95.0, 0.0, 100.0, "av.p", mul=0.01, unit="%"),
        F("maxgain", "Maximum Gain", 10.0, 1.0, 100.0, "av.m"),
    ]),
    EffectDef("bass", "Bass", "Filter & EQ", "audio", ["avfilter.bass"], [
        F("gain", "Boost", 6.0, -20.0, 20.0, "av.g", unit="dB"),
        F("freq", "Frequency", 100.0, 20.0, 500.0, "av.f", unit="Hz", digits=0),
    ]),
    EffectDef("treble", "Treble", "Filter & EQ", "audio", ["avfilter.treble"], [
        F("gain", "Boost", 6.0, -20.0, 20.0, "av.g", unit="dB"),
        F("freq", "Frequency", 3000.0, 1000.0, 16000.0, "av.f", unit="Hz", step=50, digits=0),
    ]),
    EffectDef("highpass", "Highpass", "Filter & EQ", "audio", ["avfilter.highpass"], [
        F("freq", "Cutoff", 120.0, 10.0, 5000.0, "av.f", unit="Hz", step=10, digits=0),
    ]),
    EffectDef("lowpass", "Lowpass", "Filter & EQ", "audio", ["avfilter.lowpass"], [
        F("freq", "Cutoff", 8000.0, 100.0, 20000.0, "av.f", unit="Hz", step=50, digits=0),
    ]),
    EffectDef("parametric_eq", "Parametric EQ Band", "Filter & EQ", "audio", ["avfilter.equalizer"], [
        F("freq", "Frequency", 1000.0, 20.0, 20000.0, "av.f", unit="Hz", step=10, digits=0),
        F("q", "Q", 1.0, 0.1, 10.0, "av.w", step=0.1),
        F("gain", "Gain", 0.0, -24.0, 24.0, "av.g", unit="dB"),
    ], static={"av.t": "q"}),
    EffectDef("compressor", "Compressor", "Dynamics", "audio", ["avfilter.acompressor"], [
        F("threshold", "Threshold", -18.0, -60.0, 0.0, "av.threshold", unit="dB",
          conv=_db_to_linear),
        F("ratio", "Ratio", 3.0, 1.0, 20.0, "av.ratio", step=0.1),
        F("attack", "Attack", 20.0, 0.01, 2000.0, "av.attack", unit="ms"),
        F("release", "Release", 250.0, 0.01, 9000.0, "av.release", unit="ms"),
        F("makeup", "Makeup Gain", 0.0, 0.0, 36.0, "av.makeup", unit="dB",
          conv=_db_to_linear),
    ]),
    EffectDef("limiter", "Limiter", "Dynamics", "audio", ["avfilter.alimiter"], [
        F("limit", "Ceiling", -1.0, -24.0, 0.0, "av.limit", unit="dB", conv=_db_to_linear),
        F("attack", "Attack", 5.0, 0.1, 80.0, "av.attack", unit="ms", anim="hold"),
        F("release", "Release", 50.0, 1.0, 8000.0, "av.release", unit="ms", anim="hold"),
    ]),
    EffectDef("denoise", "Noise Reduction", "Dynamics", "audio", ["avfilter.afftdn"], [
        F("nr", "Reduction", 12.0, 0.01, 97.0, "av.nr", unit="dB"),
        F("nf", "Noise Floor", -50.0, -80.0, -20.0, "av.nf", unit="dB"),
    ]),
    EffectDef("echo", "Echo", "Time & Pitch", "audio", ["avfilter.aecho"], [
        F("delay", "Delay", 300.0, 1.0, 5000.0, "av.delays", unit="ms", digits=0, anim="hold",
          conv=lambda v: f"{float(v):.0f}"),
        F("decay", "Feedback", 40.0, 0.0, 100.0, "av.decays", unit="%", anim="hold",
          conv=lambda v: f"{float(v) / 100:.3f}"),
        F("mix", "Wet Level", 60.0, 0.0, 100.0, "av.out_gain", mul=0.01, unit="%", anim="hold"),
    ]),
    EffectDef("pitch", "Pitch Shift", "Time & Pitch", "audio", ["rbpitch"], [
        F("semitones", "Semitones", 0.0, -24.0, 24.0, "octaveshift", mul=1 / 12, step=0.1),
    ]),
    EffectDef("tremolo", "Tremolo", "Time & Pitch", "audio", ["avfilter.tremolo"], [
        F("rate", "Rate", 5.0, 0.1, 20.0, "av.f", unit="Hz", step=0.1, anim="hold"),
        F("depth", "Depth", 50.0, 0.0, 100.0, "av.d", mul=0.01, unit="%", anim="hold"),
    ]),
    EffectDef("stereo_widen", "Stereo Widener", "Stereo", "audio", ["avfilter.stereowiden"], [
        F("delay", "Delay", 20.0, 1.0, 100.0, "av.delay", unit="ms", anim="hold"),
        F("feedback", "Feedback", 30.0, 0.0, 90.0, "av.feedback", mul=0.01, unit="%"),
        F("crossfeed", "Crossfeed", 30.0, 0.0, 80.0, "av.crossfeed", mul=0.01, unit="%"),
    ]),
    EffectDef("mono", "Fill Mono", "Stereo", "audio", ["mono"], [], static={"channels": 2}),
    EffectDef("swap_channels", "Swap Channels", "Stereo", "audio", ["channelswap"], [],
              static={"from": 0, "to": 1, "swap": 1}),
)

VIDEO_CATEGORIES = ["Animate", "Color", "Blur & Sharpen", "Transform", "Distort", "Keying", "Stylize", "Generate"]
AUDIO_CATEGORIES = ["Amplitude", "Filter & EQ", "Dynamics", "Time & Pitch", "Stereo"]


def effects_in(category):
    return [d for d in EFFECTS.values() if d.category == category and not d.intrinsic and not d.hidden]


# -- transitions -----------------------------------------------------------
@dataclass
class TransitionDef:
    key: str
    label: str
    kind: str            # video | audio
    category: str
    mode: str            # luma | dip | frei0r | audio
    resource: str = ""   # luma file or frei0r transition service
    reverse: bool = False
    color: str = "#000000ff"


TRANSITIONS = {t.key: t for t in [
    TransitionDef("cross_dissolve", "Cross Dissolve", "video", "Dissolve", "luma"),
    TransitionDef("dip_black", "Dip to Black", "video", "Dissolve", "dip", color="#000000ff"),
    TransitionDef("dip_white", "Dip to White", "video", "Dissolve", "dip", color="#ffffffff"),
    TransitionDef("wipe_right", "Wipe Right", "video", "Wipe", "luma", "%luma01.pgm"),
    TransitionDef("wipe_left", "Wipe Left", "video", "Wipe", "luma", "%luma01.pgm", reverse=True),
    TransitionDef("wipe_down", "Wipe Down", "video", "Wipe", "luma", "%luma02.pgm"),
    TransitionDef("wipe_up", "Wipe Up", "video", "Wipe", "luma", "%luma02.pgm", reverse=True),
    TransitionDef("barn_door", "Barn Doors", "video", "Wipe", "luma", "%luma03.pgm"),
    TransitionDef("iris", "Iris", "video", "Wipe", "luma", "%luma14.pgm"),
    TransitionDef("clock_wipe", "Clock Wipe", "video", "Wipe", "luma", "%luma20.pgm"),
    TransitionDef("push_left", "Push Left", "video", "Slide", "frei0r", "frei0r.sleid0r_push-left"),
    TransitionDef("push_right", "Push Right", "video", "Slide", "frei0r", "frei0r.sleid0r_push-right"),
    TransitionDef("slide_left", "Slide Left", "video", "Slide", "frei0r", "frei0r.sleid0r_slide-left"),
    TransitionDef("slide_right", "Slide Right", "video", "Slide", "frei0r", "frei0r.sleid0r_slide-right"),
    TransitionDef("constant_power", "Constant Power", "audio", "Crossfade", "audio"),
    TransitionDef("constant_gain", "Constant Gain", "audio", "Crossfade", "audio"),
    TransitionDef("exponential_fade", "Exponential Fade", "audio", "Crossfade", "audio"),
]}

DEFAULT_VIDEO_TRANSITION = "cross_dissolve"
DEFAULT_AUDIO_TRANSITION = "constant_power"
