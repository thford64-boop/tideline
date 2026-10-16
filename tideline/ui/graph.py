"""Value graphs for Effect Controls: an editable keyframe curve for one
parameter, and a read-only preview of an effect's generated motion.

Every kind of parameter has a graph. Numbers and points are curves; a colour
is four curves (red, green, blue, alpha) over a strip of the colour itself; a
switch or a choice is a staircase between its options; text, fonts and files
are a lane of keyframes labelled with their values.
"""

import os

from gi.repository import Gdk, Gio, Gtk

from .. import keyframes as kf
from . import theme
from .widgets import popup_menu

PAD_L, PAD_R, PAD_T, PAD_B = 48, 10, 16, 8
HIT = 7
SERIES = [theme.ACCENT, theme.WARN, "#5aa9e6", "#e06c9f"]
CHANNELS = ["#e0564b", "#2aa876", "#5aa9e6", theme.TEXT_DIM]      # red, green, blue, alpha
STRIP = 6                                                        # height of a colour graph's strip


def _short(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:max(1, n - 1)] + "…"


def _text(cr, text, x, y, color=theme.TEXT_DIM, size=9.5, right=False):
    cr.set_font_size(size)
    cr.set_source_rgba(*theme.rgb(color))
    if right:
        x -= cr.text_extents(text).x_advance
    cr.move_to(x, y)
    cr.show_text(text)


def _num(v):
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"


def _diamond(cr, x, y, r):
    cr.move_to(x, y - r)
    cr.line_to(x + r, y)
    cr.line_to(x, y + r)
    cr.line_to(x - r, y)
    cr.close_path()


class KeyframeGraph(Gtk.DrawingArea):
    """A parameter's value over the clip.

    Drag a keyframe to move it in time and value (Shift keeps one axis), drag
    the handles of the selected keyframe to shape a custom curve, double-click
    to add a keyframe, right-click for curve presets. Dragging empty space
    scrubs the playhead.
    """

    def __init__(self, row):
        super().__init__(hexpand=True)
        self.row = row
        self.app, self.clip, self.effect, self.pdef = row.app, row.clip, row.effect, row.pdef
        self.selected = None
        self.drag = None
        self.pending = None          # (frame, value) for "Add Keyframe" in the context menu
        p = self.pdef
        self.kind = ("color" if p.type == "color" else "steps" if p.type in ("bool", "choice")
                     else "lane" if p.type in ("text", "font", "file") else "number")
        self.levels = [False, True] if p.type == "bool" else [c[0] for c in p.choices]
        self.level_names = ["Off", "On"] if p.type == "bool" else [c[1] for c in p.choices]
        self.series = CHANNELS if self.kind == "color" else SERIES
        self.pad_l = PAD_L
        if self.kind == "steps":
            self.pad_l = max(PAD_L, min(124, 20 + 6 * max(len(n) for n in self.level_names)))
        self.set_content_height({"lane": 58, "steps": max(64, min(150, 34 + 20 * len(self.levels)))}.get(
            self.kind, 150))
        self.set_margin_start(26)
        self.set_margin_end(6)
        self.set_margin_bottom(4)
        self.set_draw_func(self._draw)
        self.set_tooltip_text(
            "Drag keyframes in time · double-click to add · right-click to delete" if self.kind == "lane" else
            "Drag keyframes · double-click to add · right-click to delete" if p.hold else
            "Drag keyframes · drag handles for a custom curve · double-click to add · right-click for curves")
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", lambda g, x, y: self.press(x, y))
        drag.connect("drag-update", lambda g, dx, dy: self._dragged(g, dx, dy))
        drag.connect("drag-end", lambda g, dx, dy: self.release())
        self.add_controller(drag)
        click = Gtk.GestureClick(button=1)
        click.connect("pressed", lambda g, n, x, y: n == 2 and self.add_key_at(x, y))
        self.add_controller(click)
        menu = Gtk.GestureClick(button=3)
        menu.connect("pressed", self._menu)
        self.add_controller(menu)

    # -- data ------------------------------------------------------------
    def keys(self):
        return self.effect.keyframes.get(self.pdef.name) or []

    def _comps(self):
        return 4 if self.kind == "color" else 2 if self.pdef.type == "point" else 1

    def _comp(self, value, c):
        """Height at which component ``c`` of a value is plotted."""
        if self.kind == "color":
            return float(kf.color_rgba(value)[c])
        if self.kind == "steps":
            return float(self.levels.index(value)) if value in self.levels else 0.0
        if self.kind == "lane":
            return 0.0
        return float(value[c]) if isinstance(value, (list, tuple)) else float(value)

    def _merge(self, value, c, v):
        """``value`` with component ``c`` moved to plotted height ``v``."""
        if self.kind == "color":
            rgba = kf.color_rgba(value)
            rgba[c] = max(0, min(255, int(round(v))))
            return kf.rgba_color(rgba)
        if self.kind == "steps":
            return self.levels[max(0, min(len(self.levels) - 1, int(round(v))))]
        if self.kind == "lane":
            return value
        if isinstance(value, (list, tuple)):
            return [self._round(v) if j == c else x for j, x in enumerate(value)]
        return self._round(v)

    def _describe(self, value):
        if self.kind == "steps":
            return self.level_names[self.levels.index(value)] if value in self.levels else str(value)
        if self.kind == "lane":
            text = os.path.basename(value) if self.pdef.type == "file" and value else value
            return _short(text, 28) if text else "(empty)"
        if self.kind == "color":
            return str(value)
        return ", ".join(_num(self._comp(value, c)) for c in range(self._comps())) + \
            (" " + self.pdef.unit if self.pdef.unit else "")

    def time_range(self):
        lo, hi = self.clip.in_point, self.clip.in_point + self.clip.duration - 1
        kfs = self.keys()
        if kfs:
            lo, hi = min(lo, kfs[0][0]), max(hi, kfs[-1][0])
        return lo, max(hi, lo + 1)

    def value_range(self):
        if self.drag is not None:
            return self.drag["range"]
        if self.kind != "number":
            return {"color": (-14.0, 269.0), "lane": (-1.0, 1.0)}.get(self.kind, (-0.5, len(self.levels) - 0.5))
        kfs = self.keys()
        lo_t, hi_t = self.time_range()
        vals = []
        n = min(160, hi_t - lo_t)
        for i in range(n + 1):
            v = kf.evaluate(kfs, lo_t + (hi_t - lo_t) * i / n)
            vals += [self._comp(v, c) for c in range(self._comps())]
        if not vals:
            vals = [0.0]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.18 if hi - lo > 1e-9 else max(1.0, abs(hi) * 0.2)
        return lo - pad, hi + pad

    def _plot(self):
        bottom = PAD_B + (STRIP + 3 if self.kind == "color" else 0)
        return (self.pad_l, PAD_T, max(10, self.get_width() - self.pad_l - PAD_R),
                max(10, self.get_height() - PAD_T - bottom))

    def x_of(self, t):
        x, _, w, _ = self._plot()
        lo, hi = self.time_range()
        return x + (t - lo) / (hi - lo) * w

    def y_of(self, v):
        _, y, _, h = self._plot()
        lo, hi = self.value_range()
        return y + h - (v - lo) / (hi - lo) * h

    def t_at(self, px):
        x, _, w, _ = self._plot()
        lo, hi = self.time_range()
        return lo + (px - x) / w * (hi - lo)

    def v_at(self, py):
        _, y, _, h = self._plot()
        lo, hi = self.value_range()
        return lo + (y + h - py) / h * (hi - lo)

    def _handles(self):
        """Curve handles of the selected keyframe: (segment, end, component, frame, value)."""
        kfs, i, out = self.keys(), self.selected, []
        if i is None or i >= len(kfs) or self.pdef.hold:
            return out
        for seg, end in ((i, 0), (i - 1, 1)):
            if seg < 0 or seg + 1 >= len(kfs) or kfs[seg][2] == "hold":
                continue
            a, b = kfs[seg], kfs[seg + 1]
            curve = kf.curve_of(a)
            hx, hy = curve[end * 2], curve[end * 2 + 1]
            for c in range(self._comps()):
                va, vb = self._comp(a[1], c), self._comp(b[1], c)
                out.append((seg, end, c, a[0] + hx * (b[0] - a[0]), va + hy * (vb - va)))
        return out

    def _hit(self, x, y):
        for seg, end, c, t, v in self._handles():
            if abs(self.x_of(t) - x) <= HIT and abs(self.y_of(v) - y) <= HIT:
                return "handle", (seg, end, c)
        for i, k in enumerate(self.keys()):
            for c in range(self._comps()):
                if abs(self.x_of(k[0]) - x) <= HIT and abs(self.y_of(self._comp(k[1], c)) - y) <= HIT:
                    return "key", (i, c)
        return None, None

    def _round(self, v):
        p = self.pdef
        if p.min is not None:
            v = max(p.min, v)
        if p.max is not None:
            v = min(p.max, v)
        return int(round(v)) if p.type == "int" else round(v, max(1, p.digits))

    # -- editing (also driven directly by the UI tests) --------------------
    def press(self, x, y):
        kind, what = self._hit(x, y)
        rng = self.value_range()
        self.drag = {"kind": kind or "scrub", "what": what, "x": x, "y": y, "range": rng, "moved": False,
                     "orig": [list(k) for k in self.keys()]}
        if kind == "key":
            self.selected = what[0]
        elif kind is None:
            self._scrub(x)
        self.queue_draw()

    def _dragged(self, gesture, dx, dy):
        if self.drag is not None:
            shift = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
            self.drag_to(self.drag["x"] + dx, self.drag["y"] + dy, shift)

    def drag_to(self, x, y, constrain=False):
        d = self.drag
        if d is None:
            return
        if d["kind"] == "scrub":
            return self._scrub(x)
        if constrain:
            if abs(x - d["x"]) >= abs(y - d["y"]):
                y = d["y"]
            else:
                x = d["x"]
        kfs = [list(k) for k in d["orig"]]
        if d["kind"] == "key":
            i, c = d["what"]
            t = int(round(self.t_at(x)))
            if i > 0:
                t = max(kfs[i - 1][0] + 1, t)
            if i + 1 < len(kfs):
                t = min(kfs[i + 1][0] - 1, t)
            # Moving only along time keeps the exact value.
            kfs[i][0], kfs[i][1] = t, (self._merge(kfs[i][1], c, self.v_at(y)) if y != d["y"] else kfs[i][1])
        else:
            seg, end, c = d["what"]
            a, b = kfs[seg], kfs[seg + 1]
            curve = kf.curve_of(a)
            curve[end * 2] = round(max(0.0, min(1.0, (self.t_at(x) - a[0]) / (b[0] - a[0]))), 3)
            va, vb = self._comp(a[1], c), self._comp(b[1], c)
            if abs(vb - va) > 1e-9:
                curve[end * 2 + 1] = round(max(-3.0, min(4.0, (self.v_at(y) - va) / (vb - va))), 3)
            kfs[seg] = a[:2] + ["bezier", curve]
        d["moved"] = True
        self.app.set_keyframes(self.effect, self.pdef, kfs, final=False)
        self.queue_draw()

    def release(self):
        d, self.drag = self.drag, None
        if d is not None and d["moved"]:
            label = "Move Keyframe" if d["kind"] == "key" else "Shape Keyframe Curve"
            self.app.set_keyframes(self.effect, self.pdef, [list(k) for k in self.keys()], label=label)
        self.queue_draw()

    def add_key_at(self, x, y):
        """Add a keyframe under the pointer (no-op on an existing one)."""
        if self._hit(x, y)[0] is not None:
            return
        lo, hi = self.time_range()
        t = max(lo, min(hi, int(round(self.t_at(x)))))
        self._add(t, self.v_at(y) if self._comps() == 1 else None)

    def _add(self, t, value=None):
        kfs = self.keys()
        current = kf.evaluate(kfs, t)
        value = self._merge(current, 0, value) if value is not None else current
        self.drag = None
        new = kf.set_key(kfs, t, value, "hold" if self.pdef.hold else None)
        self.selected = kf.find(new, t)
        self.app.set_keyframes(self.effect, self.pdef, new, label="Add Keyframe")
        self.queue_draw()

    def _scrub(self, x):
        clip = self.clip
        t = int(round(self.t_at(x))) - clip.in_point
        self.app.set_playhead(clip.start + max(0, min(clip.duration - 1, t)))

    def _menu(self, gesture, n, x, y):
        from .effect_controls import interp_menu
        panel = self.row.panel
        kind, what = self._hit(x, y)
        panel.interp_target = self.row
        if kind == "key":
            self.selected = what[0]
            panel.interp_time = self.keys()[what[0]][0]
            menu = Gio.Menu() if self.pdef.hold else interp_menu()
            section = Gio.Menu()
            section.append("Delete Keyframe", "ec.kf_delete")
            menu.append_section(None, section)
        else:
            lo, hi = self.time_range()
            self.pending = (max(lo, min(hi, int(round(self.t_at(x))))),
                            self.v_at(y) if self._comps() == 1 else None)
            menu = Gio.Menu()
            menu.append("Add Keyframe", "ec.kf_add")
        self.queue_draw()
        popup_menu(self, menu, x, y)

    def add_pending(self):
        if self.pending is not None:
            (t, value), self.pending = self.pending, None
            self._add(t, value)

    # -- drawing ---------------------------------------------------------
    def _draw(self, area, cr, w, h):
        px, py, pw, ph = self._plot()
        kfs = self.keys()
        if self.selected is not None and self.selected >= len(kfs):
            self.selected = None
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        lo_t, hi_t = self.time_range()
        lo_v, hi_v = self.value_range()
        clip = self.clip
        # frames outside the clip (trimmed-away keyframes) are shaded
        cr.set_source_rgba(*theme.rgb(theme.BG2, 0.9))
        for a, b in ((lo_t, clip.in_point), (clip.in_point + clip.duration - 1, hi_t)):
            if b > a:
                cr.rectangle(self.x_of(a), py, self.x_of(b) - self.x_of(a), ph)
                cr.fill()
        cr.set_line_width(1)
        if self.kind == "steps":
            grid = [(float(i), _short(name, (self.pad_l - 18) // 6)) for i, name in enumerate(self.level_names)]
        elif self.kind == "color":
            grid = [(v, _num(v)) for v in (0.0, 64.0, 128.0, 191.0, 255.0)]
        elif self.kind == "lane":
            grid = [(0.0, "")]
        else:
            grid = [(v, _num(v)) for v in (lo_v + (hi_v - lo_v) * i / 4 for i in range(5))]
        for v, text in grid:
            y = round(self.y_of(v)) + 0.5
            cr.set_source_rgba(*theme.rgb(theme.BG3, 0.8))
            cr.move_to(px, y)
            cr.line_to(px + pw, y)
            cr.stroke()
            _text(cr, text, px - (10 if self.kind == "steps" else 5), y + 3, theme.TEXT_FAINT, right=True)
        if self.kind == "color" and kfs:
            # the colour itself, under the channel curves
            for i in range(int(pw)):
                rgba = kf.color_rgba(kf.evaluate(kfs, lo_t + (hi_t - lo_t) * i / pw))
                cr.set_source_rgba(*(c / 255.0 for c in rgba))
                cr.rectangle(px + i, py + ph + 3, 1.5, STRIP)
                cr.fill()
        cr.save()
        cr.rectangle(px - HIT, py - HIT, pw + 2 * HIT, ph + 2 * HIT)
        cr.clip()
        if kfs:
            times = {lo_t + (hi_t - lo_t) * i / pw for i in range(0, int(pw) + 1, 2)} | {float(hi_t)}
            for k in kfs:
                times.update((float(k[0]), k[0] - 1e-6))
            times = sorted(t for t in times if lo_t <= t <= hi_t)
            for c in range(self._comps()):
                cr.set_source_rgba(*theme.rgb(self.series[c]))
                cr.set_line_width(1.6)
                for j, t in enumerate(times):
                    y = self.y_of(self._comp(kf.evaluate(kfs, t), c))
                    (cr.line_to if j else cr.move_to)(self.x_of(t), y)
                cr.stroke()
        # handles of the selected keyframe
        for seg, end, c, t, v in self._handles():
            anchor = kfs[seg + end]
            ax, ay = self.x_of(anchor[0]), self.y_of(self._comp(anchor[1], c))
            hx, hy = self.x_of(t), self.y_of(v)
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM))
            cr.set_line_width(1)
            cr.move_to(ax, ay)
            cr.line_to(hx, hy)
            cr.stroke()
            cr.arc(hx, hy, 3.5, 0, 6.2832)
            cr.set_source_rgba(*theme.rgb(theme.BG0))
            cr.fill_preserve()
            cr.set_source_rgba(*theme.rgb(theme.SELECT))
            cr.stroke()
        for i, k in enumerate(kfs):
            for c in range(self._comps()):
                x, y = self.x_of(k[0]), self.y_of(self._comp(k[1], c))
                _diamond(cr, x, y, 5)
                if i == self.selected:
                    cr.set_source_rgba(*theme.rgb(theme.SELECT))
                    cr.fill()
                else:
                    cr.set_source_rgba(*theme.rgb(theme.BG0))
                    cr.fill_preserve()
                    cr.set_source_rgba(*theme.rgb(self.series[c]))
                    cr.set_line_width(1.5)
                    cr.stroke()
        if self.kind == "lane":
            # each keyframe's value, written in the room before the next one
            for i, k in enumerate(kfs):
                x = self.x_of(k[0]) + 9
                room = (self.x_of(kfs[i + 1][0]) if i + 1 < len(kfs) else px + pw) - x - 6
                if room > 14:
                    _text(cr, _short(self._describe(k[1]), int(room // 6)), x, self.y_of(0.0) - 7, theme.TEXT_DIM)
        cr.restore()
        # playhead
        x = round(self.x_of(self.app.clip_time(clip))) + 0.5
        cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
        cr.set_line_width(1)
        cr.move_to(x, py - 4)
        cr.line_to(x, py + ph + 4)
        cr.stroke()
        # readout: the selected keyframe, else the value under the playhead
        if not kfs:
            return
        if self.selected is not None:
            k = kfs[self.selected]
            value = k[1]
            text = f"frame {k[0] - clip.in_point}  ·  "
            if not self.pdef.hold:
                text += f"{dict(kf.INTERPS).get(k[2], k[2])}  ·  "
        else:
            value = kf.evaluate(kfs, self.app.clip_time(clip))
            text = ""
        _text(cr, text + self._describe(value), px + pw - 2, py - 3, theme.TEXT_DIM, right=True)
        if self._comps() > 1:
            for c, name in enumerate("RGBA" if self.kind == "color" else "XY"):
                _text(cr, name, px + 4 + c * 14, py - 3, self.series[c], size=10)


class MotionPreview(Gtk.DrawingArea):
    """Read-only plot of what a generated effect (shake, oscillate…) does
    over the clip, so its settings can be judged without playing back."""

    def __init__(self, app, clip, effect, edef):
        super().__init__(hexpand=True)
        self.app, self.clip, self.effect, self.edef = app, clip, effect, edef
        self._cache = (None, [])
        self.set_content_height(72)
        self.set_margin_start(26)
        self.set_margin_end(6)
        self.set_margin_top(2)
        self.set_margin_bottom(5)
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", lambda g, x, y: self._scrub(x))
        drag.connect("drag-update", lambda g, dx, dy: self._scrub(g.get_start_point()[1] + dx))
        self.add_controller(drag)

    def refresh(self):
        self.queue_draw()

    def _scrub(self, x):
        clip = self.clip
        u = max(0.0, min(1.0, (x - 6) / max(1, self.get_width() - 12)))
        self.app.set_playhead(clip.start + int(round(u * (clip.duration - 1))))

    def series(self, n):
        """[(label, [values])] sampled at ``n`` points across the clip."""
        clip, effect = self.clip, self.effect
        first, last = clip.in_point, clip.in_point + clip.duration - 1
        key = (n, first, last, effect.enabled, repr(effect.params), repr(effect.keyframes))
        if self._cache[0] != key:
            ctx = dict(self.app.ctx(), clip=(first, last), range=(first, last))
            times = [first + (last - first) * i / max(1, n - 1) for i in range(n)]
            data = [(name, [fn(t) for t in times]) for name, fn in self.edef.preview(effect, ctx)]
            self._cache = (key, data)
        return self._cache[1]

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        x0, x1, top, bottom = 6, w - 6, 16, h - 5
        mid = (top + bottom) / 2
        cr.set_source_rgba(*theme.rgb(theme.BG3, 0.8))
        cr.set_line_width(1)
        cr.move_to(x0, round(mid) + 0.5)
        cr.line_to(x1, round(mid) + 0.5)
        cr.stroke()
        n = max(2, min(int(x1 - x0), self.clip.duration * 2, 600))
        legend_x = x0
        for i, (name, values) in enumerate(self.series(n)):
            rest = values[0] if len(set(values)) == 1 else (100.0 if name == "Scale" else 0.0)
            span = max(abs(v - rest) for v in values)
            if span < 1e-9:
                continue                      # this property is not animated
            color = SERIES[i % len(SERIES)]
            cr.set_source_rgba(*theme.rgb(color))
            cr.set_line_width(1.3)
            for j, v in enumerate(values):
                x = x0 + (x1 - x0) * j / (n - 1)
                y = mid - (v - rest) / span * (bottom - top) / 2
                (cr.line_to if j else cr.move_to)(x, y)
            cr.stroke()
            text = f"{name} ±{_num(span)}"
            _text(cr, text, legend_x, 11, color)
            legend_x += cr.text_extents(text).x_advance + 12
        if legend_x == x0:
            _text(cr, "No motion", x0, 11, theme.TEXT_FAINT)
        clip = self.clip
        u = (self.app.clip_time(clip) - clip.in_point) / max(1, clip.duration - 1)
        x = round(x0 + (x1 - x0) * u) + 0.5
        cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
        cr.move_to(x, 2)
        cr.line_to(x, h - 2)
        cr.stroke()
