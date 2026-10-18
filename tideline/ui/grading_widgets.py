"""Editors for the grading effects in Effect Controls: colour wheels, the
hue/saturation curves, and the qualifier view of the secondary correction."""

import math

import cairo
import numpy as np
from gi.repository import Gdk, Gtk

from .. import curves as cv
from .. import grading
from .. import scopes
from . import theme
from .icons import Icon
from .widgets import DragNumber, icon_button, label

HIT = 8              # pointer distance (px) that picks a curve point
PAD = 8


def _wheel_colour(angle, value=0.55):
    """The colour a push at ``angle`` (radians from +Cb, vectorscope style) gives."""
    rgb = grading.from_ycc(np.array(value), np.array(math.cos(angle) * 0.32), np.array(math.sin(angle) * 0.32))
    return tuple(float(c) for c in np.clip(rgb, 0.0, 1.0))


class ColourWheel(Gtk.Box):
    """One colour wheel: drag anywhere on it to push the colour (Shift for
    fine control), double-click to centre it again; the number under it is
    the wheel's master. ``on_change(value, final)`` gets ``[u, v, master]``."""

    def __init__(self, title, on_change, size=116):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.add_css_class("colour-wheel")
        self.on_change = on_change
        self.value = list(grading.NEUTRAL_WHEEL)
        self._start = None
        head = Gtk.Box(spacing=2)
        head.append(label(title, css="dim", hexpand=True, xalign=0.5))
        head.append(icon_button("reset", f"Reset {title}", lambda b: self._reset(), size=12, css="kf-button"))
        self.append(head)
        self.area = Gtk.DrawingArea(halign=Gtk.Align.CENTER)
        self.area.set_content_width(size)
        self.area.set_content_height(size)
        self.area.set_draw_func(self._draw)
        self.area.set_tooltip_text("Drag to push the colour (Shift: fine) · double-click to centre")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.area.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.area.add_controller(click)
        self.append(self.area)
        self.master = DragNumber(0.0, -100.0, 100.0, 1.0, 0, "")
        self.master.set_halign(Gtk.Align.CENTER)
        self.master.set_tooltip_text(f"{title} master: brightness of the whole wheel")
        self.master.connect("changed", self._master_changed)
        self.append(self.master)

    def set_value(self, value):
        value = grading.clean_wheel(value)
        if self._start is None and value != self.value:
            self.value = value
            self.area.queue_draw()
        self.master.set_value(round(self.value[2] * 100.0, 1))

    def _radius(self):
        return min(self.area.get_width(), self.area.get_height()) / 2 - 9

    def _emit(self, final):
        self.area.queue_draw()
        self.on_change(list(self.value), final)

    def _reset(self):
        self.value = [0.0, 0.0, 0.0]
        self.master.set_value(0.0)
        self._emit(True)

    def _master_changed(self, w, v, final):
        self.value[2] = max(-1.0, min(1.0, v / 100.0))
        self._emit(final)

    def _click(self, gesture, n, x, y):
        if n == 2:
            self.value[0] = self.value[1] = 0.0
            self._emit(True)

    def _begin(self, gesture, x, y):
        self._start = (self.value[0], self.value[1], False)

    def _update(self, gesture, dx, dy):
        if self._start is None or (abs(dx) + abs(dy) < 2 and not self._start[2]):
            return
        u0, v0, _ = self._start
        self._start = (u0, v0, True)
        fine = gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
        k = (0.2 if fine else 1.0) / max(10.0, self._radius())
        u, v, _m = grading.clean_wheel([u0 + dx * k, v0 - dy * k, 0.0])
        self.value[0], self.value[1] = round(u, 4), round(v, 4)
        self._emit(False)

    def _end(self, gesture, dx, dy):
        moved = self._start is not None and self._start[2]
        self._start = None
        if moved:
            self._emit(True)

    def _draw(self, area, cr, w, h):
        cx, cy, r = w / 2, h / 2, self._radius()
        steps = 120
        cr.set_line_width(5)
        for i in range(steps):
            a0, a1 = 2 * math.pi * i / steps, 2 * math.pi * (i + 1.2) / steps
            cr.set_source_rgb(*_wheel_colour(a0))
            cr.arc_negative(cx, cy, r + 4, -a0, -a1)
            cr.stroke()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb(theme.BG3))
        cr.arc(cx, cy, r / 2, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(cx - r, cy + .5)
        cr.line_to(cx + r, cy + .5)
        cr.move_to(cx + .5, cy - r)
        cr.line_to(cx + .5, cy + r)
        cr.stroke()
        u, v, _ = self.value
        px, py = cx + u * r, cy - v * r
        if abs(u) + abs(v) > 1e-4:
            cr.set_source_rgba(*_wheel_colour(math.atan2(v, u), 0.7), 0.9)
            cr.set_line_width(1.5)
            cr.move_to(cx, cy)
            cr.line_to(px, py)
            cr.stroke()
        cr.arc(px, py, 5, 0, 2 * math.pi)
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.fill_preserve()
        cr.set_source_rgba(*theme.rgb(theme.TEXT))
        cr.set_line_width(1.6)
        cr.stroke()


class HueCurveEditor(Gtk.Box):
    """The five hue/saturation curves. The flat line in the middle changes
    nothing; drag a point up or down. The hue curves wrap around, so the
    left and right edges are the same red. ``on_change(value, final)`` gets
    the whole ``{curve: points}`` value."""

    def __init__(self, app, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.app = app
        self.on_change = on_change
        self.value = grading.default_hue_curves()
        self.curve = "hue_sat"
        self.selected = None
        self._drag = None
        self._hist = None
        self.add_css_class("curve-editor")
        bar = Gtk.Box(spacing=2)
        first, self.buttons = None, {}
        for key, text in grading.HUE_CURVES:
            btn = Gtk.ToggleButton(label=text, focusable=False)
            btn.add_css_class("curve-channel")
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(key == self.curve)
            btn.connect("toggled", lambda b, k=key: b.get_active() and self._pick(k))
            self.buttons[key] = btn
            bar.append(btn)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(icon_button("reset", "Flatten this curve", lambda b: self.reset_curve(), size=13, css="kf-button"))
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(bar)
        self.append(scroller)
        self.area = Gtk.DrawingArea(hexpand=True)
        self.area.set_size_request(-1, 190)
        self.area.set_draw_func(self._draw)
        self.area.set_focusable(True)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.area.add_controller(drag)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._click)
        self.area.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.area.add_controller(keys)
        self.append(self.area)
        self.append(label("Click the line to add a point and drag it up or down; double-click or right-click "
                          "removes it.", css="faint", wrap=True))

    # -- value -------------------------------------------------------------
    @property
    def periodic(self):
        return self.curve in grading.PERIODIC

    @property
    def points(self):
        return self.value[self.curve]

    def set_value(self, value):
        value = grading.clean_hue_curves(value)
        if value != self.value and self._drag is None:
            self.value = value
            if self.selected is not None and self.selected >= len(self.points):
                self.selected = None
        self.area.queue_draw()

    def _set_points(self, pts, final):
        self.value = dict(self.value, **{self.curve: pts})
        self.area.queue_draw()
        self.on_change(grading.clean_hue_curves(self.value), final)

    def _pick(self, key):
        self.curve, self.selected = key, None
        self.area.queue_draw()

    def reset_curve(self):
        self.selected = None
        self._set_points([], True)

    def height_at(self, x):
        return float(grading.evaluate_curve(self.points, x, self.periodic))

    # -- geometry ----------------------------------------------------------
    def _box(self):
        w, h = self.area.get_width(), self.area.get_height()
        return PAD, PAD, max(10, w - 2 * PAD), max(10, h - 2 * PAD - 14)

    def _to_screen(self, x, y):
        bx, by, bw, bh = self._box()
        return bx + x * bw, by + (1 - y) * bh

    def _to_curve(self, px, py):
        bx, by, bw, bh = self._box()
        return min(1.0, max(0.0, (px - bx) / bw)), min(1.0, max(0.0, 1 - (py - by) / bh))

    def _hit(self, px, py):
        best, dist = None, HIT * HIT
        for i, (x, y) in enumerate(self.points):
            sx, sy = self._to_screen(x, y)
            d = (sx - px) ** 2 + (sy - py) ** 2
            if d <= dist:
                best, dist = i, d
        return best

    # -- input -------------------------------------------------------------
    def _click(self, gesture, n, px, py):
        self.area.grab_focus()
        hit = self._hit(px, py)
        if hit is not None and (gesture.get_current_button() == 3 or n == 2):
            self._remove(hit)

    def _remove(self, index):
        pts = [list(p) for p in self.points]
        if 0 <= index < len(pts):
            del pts[index]
            self.selected = None
            if not self.periodic and len(pts) <= 2 and all(abs(p[1] - grading.FLAT) < 1e-6 for p in pts):
                pts = []
            self._set_points(pts, True)

    def _add(self, x):
        """A point on the curve at ``x``; a flat curve also gets two neighbours
        that keep the rest of it in place. Returns the new point's index."""
        y = self.height_at(x)
        pts = [list(p) for p in self.points]
        if not pts:
            if self.periodic:
                pts = [[(x - 0.12) % 1.0, grading.FLAT], [(x + 0.12) % 1.0, grading.FLAT]]
            else:
                pts = [[0.0, grading.FLAT], [1.0, grading.FLAT]]
        if any(abs(p[0] - x) < cv.MIN_GAP for p in pts) or len(pts) >= cv.MAX_POINTS:
            return -1, pts
        pts.append([x, y])
        pts.sort(key=lambda p: p[0])
        return next(i for i, p in enumerate(pts) if p == [x, y]), pts

    def _begin(self, gesture, px, py):
        if gesture.get_current_button() != 1:
            return
        hit = self._hit(px, py)
        if hit is None:
            x, _ = self._to_curve(px, py)
            hit, pts = self._add(x)
            if hit < 0:
                return
            self._set_points(pts, False)
        self.selected = hit
        self._drag = [hit, px, py, False]
        self.area.queue_draw()

    def _update(self, gesture, dx, dy):
        if self._drag is None or self._drag[0] >= len(self.points) or abs(dx) + abs(dy) < 1:
            return
        index, px, py, _ = self._drag
        x, y = self._to_curve(px + dx, py + dy)
        self._drag[3] = True
        self._set_points(cv.move_point(self.points, index, x, y), False)

    def _end(self, gesture, dx, dy):
        if self._drag is not None:
            self._drag = None
            self._set_points(self.points, True)

    def _key(self, ctrl, keyval, code, state):
        if self.selected is None or self.selected >= len(self.points):
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self._remove(self.selected)
            return True
        step = 0.01 if state & Gdk.ModifierType.SHIFT_MASK else 1 / 255
        moves = {Gdk.KEY_Up: (0, step), Gdk.KEY_Down: (0, -step), Gdk.KEY_Left: (-step, 0),
                 Gdk.KEY_Right: (step, 0)}
        if keyval in moves:
            x, y = self.points[self.selected]
            ddx, ddy = moves[keyval]
            self._set_points(cv.move_point(self.points, self.selected, x + ddx, y + ddy), True)
            return True
        return False

    # -- drawing -----------------------------------------------------------
    def _distribution(self):
        """Share of the Program picture along this curve's axis: hue
        (weighted by saturation), luma or saturation, in 64 bins."""
        frame = self.app.frames.get("program")
        if not frame or frame[0] is None:
            return None
        if self._hist is None or self._hist[0] is not frame[0]:
            img = scopes.frame_array(*frame)[::2, ::2].reshape(-1, 3).astype(np.float64) / 255.0
            y, cb, cr = grading.to_ycc(img)
            s = grading.saturation_of(cb, cr)
            h = grading.hue_of(img)
            bins = lambda v, wgt=None: np.histogram(v, 64, (0.0, 1.0), weights=wgt)[0].astype(np.float64)
            self._hist = (frame[0], {"hue": bins(h, s), "luma": bins(y), "sat": bins(s)})
        axis = "hue" if self.periodic else "luma" if self.curve == "luma_sat" else "sat"
        return self._hist[1][axis]

    def _axis_colour(self, x):
        if self.periodic:
            return grading.hue_rgb(x, 0.85)
        if self.curve == "luma_sat":
            return x, x, x
        r, g, b = grading.hue_rgb(0.02, 0.8)
        return 0.55 + (r - 0.55) * x, 0.55 + (g - 0.55) * x, 0.55 + (b - 0.55) * x

    def _draw(self, area, cr, w, h):
        bx, by, bw, bh = self._box()
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(bx, by, bw, bh)
        cr.fill()
        grad = cairo.LinearGradient(bx, 0, bx + bw, 0)
        for i in range(25):
            x = i / 24
            grad.add_color_stop_rgb(x, *self._axis_colour(x))
        cr.set_source(grad)
        cr.rectangle(bx, by + bh + 4, bw, 8)
        cr.fill()
        # the picture's distribution along the axis, faintly, in its colours
        counts = self._distribution()
        if counts is not None and counts.max() > 0:
            heights = np.sqrt(counts / counts.max())
            for i, v in enumerate(heights):
                if v > 0.02:
                    cr.set_source_rgba(*self._axis_colour((i + 0.5) / 64), 0.22)
                    cr.rectangle(bx + i / 64 * bw, by + bh * (1 - v * 0.9), bw / 64 + 0.5, bh * v * 0.9)
                    cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb(theme.BG3))
        for i in range(1, 6):
            cr.move_to(bx + bw * i / 6 + .5, by)
            cr.line_to(bx + bw * i / 6 + .5, by + bh)
        cr.stroke()
        cr.set_source_rgba(*theme.rgb(theme.BG4))
        cr.rectangle(bx + .5, by + .5, bw - 1, bh - 1)
        cr.stroke()
        cr.set_dash([3, 3])
        cr.move_to(bx, by + bh / 2 + .5)
        cr.line_to(bx + bw, by + bh / 2 + .5)
        cr.stroke()
        cr.set_dash([])
        xs = np.linspace(0.0, 1.0, max(48, int(bw / 2)))
        ys = grading.evaluate_curve(self.points, xs, self.periodic)
        for i, (x, y) in enumerate(zip(xs, ys)):
            sx, sy = self._to_screen(x, y)
            if i:
                cr.line_to(sx, sy)
            else:
                cr.move_to(sx, sy)
        cr.set_source_rgba(*theme.rgb(theme.TEXT))
        cr.set_line_width(1.8)
        cr.stroke()
        for i, (x, y) in enumerate(self.points):
            sx, sy = self._to_screen(x, y)
            cr.arc(sx, sy, 4.5 if i == self.selected else 3.5, 0, 2 * math.pi)
            cr.set_source_rgb(*self._axis_colour(x))
            cr.fill_preserve()
            cr.set_source_rgba(*theme.rgb(theme.TEXT if i == self.selected else theme.BG0))
            cr.set_line_width(1.5)
            cr.stroke()
        if self.selected is not None and self.selected < len(self.points):
            x, y = self.points[self.selected]
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10)
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM))
            cr.move_to(bx + 5, by + 13)
            cr.show_text(self._readout(x, y))

    def _readout(self, x, y):
        where = f"Hue {round(x * 360)}°" if self.periodic else \
            f"{'Luma' if self.curve == 'luma_sat' else 'Saturation'} {round(x * 100)} %"
        d = y - grading.FLAT
        if self.curve == "hue_hue":
            change = f"shift {d * 2 * grading.HUE_SHIFT:+.0f}°"
        elif self.curve == "hue_luma":
            change = f"brightness {d * 2 * grading.LUMA_SHIFT * 100:+.0f}"
        else:
            change = f"saturation {y * 200:.0f} %"
        return f"{where}  →  {change}"


class QualifierView(Gtk.Box):
    """The secondary's selection at a glance: the hue, saturation and luma
    ranges it picks (soft edges shaded), and a picker that samples them from
    the Program monitor."""

    def __init__(self, app, clip, effect):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=30, margin_end=8,
                         margin_top=4, margin_bottom=4)
        self.app, self.clip, self.effect = app, clip, effect
        row = Gtk.Box(spacing=6)
        self.pick = Gtk.ToggleButton(focusable=False)
        inner = Gtk.Box(spacing=5)
        inner.append(Icon("eyedropper", 14))
        inner.append(Gtk.Label(label="Pick Colour"))
        self.pick.set_child(inner)
        self.pick.set_tooltip_text("Click a colour in the Program monitor to select colours like it (Esc cancels)")
        self.pick.connect("toggled", self._toggle_pick)
        row.append(self.pick)
        row.append(label("then fine-tune the ranges below", css="faint", hexpand=True))
        self.append(row)
        self.area = Gtk.DrawingArea(hexpand=True)
        self.area.set_size_request(-1, 66)
        self.area.set_draw_func(self._draw)
        self.append(self.area)
        self._busy = False

    def refresh(self):
        """Redraw, and show whether a pick is waiting for a click."""
        active = self.app.picker is not None and getattr(self.app.picker, "effect", None) is self.effect
        if self.pick.get_active() != active:
            self._busy = True
            self.pick.set_active(active)
            self._busy = False
        self.area.queue_draw()

    def _toggle_pick(self, button):
        if self._busy:
            return
        if button.get_active():
            self.app.pick_qualifier(self.clip, self.effect)
        else:
            self.app.cancel_pick()

    def _draw(self, area, cr, w, h):
        p = grading.clean_secondary(self.effect.params)
        rows = [("Hue", p["use_hue"]), ("Sat", p["use_sat"]), ("Luma", p["use_luma"])]
        lab_w, bar_h, gap = 38, 14, 8
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        bw = max(10, w - lab_w - 4)
        for i, (name, on) in enumerate(rows):
            y = i * (bar_h + gap) + 2
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM if on else theme.TEXT_FAINT))
            cr.move_to(0, y + bar_h - 3)
            cr.show_text(name)
            grad = cairo.LinearGradient(lab_w, 0, lab_w + bw, 0)
            for k in range(25):
                x = k / 24
                if i == 0:
                    colour = grading.hue_rgb(x, 0.9)
                elif i == 1:
                    r, g, b = grading.hue_rgb(p["hue"] / 360.0, 0.85)
                    colour = (0.5 + (r - 0.5) * x, 0.5 + (g - 0.5) * x, 0.5 + (b - 0.5) * x)
                else:
                    colour = (x, x, x)
                grad.add_color_stop_rgb(x, *colour)
            cr.set_source(grad)
            cr.rectangle(lab_w, y, bw, bar_h)
            cr.fill()
            # dim what is not selected: sample the key along the bar
            n = 96
            for k in range(n):
                x = (k + 0.5) / n
                if i == 0:
                    d = abs((x * 360.0 - p["hue"] + 180.0) % 360.0 - 180.0)
                    key = float(grading._band(np.array(d), -1.0, p["hue_width"] / 2, p["hue_soft"]))
                elif i == 1:
                    key = float(grading._band(np.array(x * 100), p["sat_low"], p["sat_high"], p["sat_soft"]))
                else:
                    key = float(grading._band(np.array(x * 100), p["luma_low"], p["luma_high"], p["luma_soft"]))
                if not on:
                    key = 1.0
                if p["invert"]:
                    key = 1.0 - key
                if key < 0.999:
                    cr.set_source_rgba(0.06, 0.07, 0.09, 0.78 * (1.0 - key))
                    cr.rectangle(lab_w + k / n * bw, y, bw / n + 0.6, bar_h)
                    cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.BG4))
            cr.set_line_width(1)
            cr.rectangle(lab_w + .5, y + .5, bw - 1, bar_h - 1)
            cr.stroke()
