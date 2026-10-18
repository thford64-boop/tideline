"""The RGB Curves editor in Effect Controls: master, red, green and blue
curves over the histogram of the Program monitor's picture."""

import cairo
import numpy as np
from gi.repository import Gdk, Gtk

from .. import curves as cv
from .. import scopes
from . import theme
from .widgets import icon_button, label

CHANNEL_COLOURS = {"master": (0.92, 0.94, 0.97), "red": (1.0, 0.36, 0.33), "green": (0.38, 0.88, 0.42),
                   "blue": (0.38, 0.58, 1.0)}
HIT = 8              # pointer distance (px) that picks a point
PAD = 8


class CurveEditor(Gtk.Box):
    """``on_change(value, final)`` gets the whole {channel: points} value."""

    def __init__(self, app, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.app = app
        self.on_change = on_change
        self.value = cv.default()
        self.channel = "master"
        self.selected = None          # index of the selected point in the current channel
        self._drag = None             # (index, start x, start y) while a point is dragged
        self._hist = None             # (frame identity, {channel: counts})
        self.add_css_class("curve-editor")

        bar = Gtk.Box(spacing=2)
        first = None
        self.buttons = {}
        for key, text in cv.CHANNELS:
            btn = Gtk.ToggleButton(label=text, focusable=False)
            btn.add_css_class("curve-channel")
            btn.add_css_class("channel-" + key)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(key == self.channel)
            btn.connect("toggled", lambda b, k=key: b.get_active() and self._pick(k))
            self.buttons[key] = btn
            bar.append(btn)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        bar.append(icon_button("reset", "Reset this channel to a straight line", lambda b: self.reset_channel(),
                               size=13, css="kf-button"))
        self.append(bar)

        self.area = Gtk.DrawingArea(hexpand=True)
        self.area.set_size_request(-1, 230)
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
        self.append(label("Click to add a point, drag to move it; double-click or right-click removes it.",
                          css="faint", wrap=True))

    # -- value -------------------------------------------------------------
    def set_value(self, value):
        value = cv.clean_set(value)
        if value != self.value and self._drag is None:
            self.value = value
            if self.selected is not None and self.selected >= len(self.points):
                self.selected = None
        self.area.queue_draw()

    @property
    def points(self):
        return self.value[self.channel]

    def _set_points(self, pts, final):
        self.value = dict(self.value, **{self.channel: pts})
        self.area.queue_draw()
        self.on_change(cv.clean_set(self.value), final)

    def _pick(self, key):
        self.channel = key
        self.selected = None
        self.area.queue_draw()

    def reset_channel(self):
        self.selected = None
        self._set_points([list(p) for p in cv.IDENTITY], True)

    # -- geometry ----------------------------------------------------------
    def _box(self):
        w, h = self.area.get_width(), self.area.get_height()
        size = max(10, min(w, h) - 2 * PAD)
        return (w - size) / 2, PAD, size

    def _to_screen(self, x, y):
        bx, by, s = self._box()
        return bx + x * s, by + (1 - y) * s

    def _to_curve(self, px, py):
        bx, by, s = self._box()
        return min(1.0, max(0.0, (px - bx) / s)), min(1.0, max(0.0, 1 - (py - by) / s))

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
        button = gesture.get_current_button()
        hit = self._hit(px, py)
        if hit is not None and (button == 3 or n == 2):
            self._remove(hit)

    def _remove(self, index):
        pts = cv.remove_point(self.points, index)
        if len(pts) != len(self.points):
            self.selected = None
            self._set_points(pts, True)

    def _begin(self, gesture, px, py):
        if gesture.get_current_button() != 1:
            return
        hit = self._hit(px, py)
        added = False
        if hit is None:
            x, y = self._to_curve(px, py)
            pts, hit = cv.add_point(self.points, x, y)
            if hit < 0:
                return
            self._set_points(pts, False)
            added = True
        self.selected = hit
        self._drag = [hit, px, py, added]
        self.area.queue_draw()

    def _update(self, gesture, dx, dy):
        if self._drag is None or self._drag[0] >= len(self.points) or abs(dx) + abs(dy) < 1:
            return
        index, px, py, _ = self._drag
        x, y = self._to_curve(px + dx, py + dy)
        self._drag[3] = True
        self._set_points(cv.move_point(self.points, index, x, y), False)

    def _end(self, gesture, dx, dy):
        if self._drag is None:
            return
        changed = self._drag[3]
        self._drag = None
        if changed:
            self._set_points(self.points, True)

    def _key(self, ctrl, keyval, code, state):
        if self.selected is None:
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
    def _histogram(self):
        frame = self.app.frames.get("program")
        if not frame or frame[0] is None:
            return None
        if self._hist is None or self._hist[0] is not frame[0]:
            img = scopes.frame_array(*frame)
            self._hist = (frame[0], scopes.histogram(img))
        counts = self._hist[1]
        return counts["luma" if self.channel == "master" else self.channel]

    def _draw(self, area, cr, w, h):
        bx, by, s = self._box()
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(bx, by, s, s)
        cr.fill()
        colour = CHANNEL_COLOURS[self.channel]

        counts = self._histogram()
        if counts is not None and counts.max() > 0:
            heights = np.sqrt(counts / counts.max())
            cr.move_to(bx, by + s)
            for i, v in enumerate(heights):
                cr.line_to(bx + (i + 0.5) / 256 * s, by + s - v * s * 0.9)
            cr.line_to(bx + s, by + s)
            cr.close_path()
            cr.set_source_rgba(*colour, 0.12)
            cr.fill()

        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb(theme.BG3))
        for i in range(1, 4):
            cr.move_to(bx + s * i / 4 + .5, by)
            cr.line_to(bx + s * i / 4 + .5, by + s)
            cr.move_to(bx, by + s * i / 4 + .5)
            cr.line_to(bx + s, by + s * i / 4 + .5)
        cr.stroke()
        cr.set_source_rgba(*theme.rgb(theme.BG4))
        cr.rectangle(bx + .5, by + .5, s - 1, s - 1)
        cr.stroke()
        cr.set_dash([3, 3])
        cr.move_to(bx, by + s)
        cr.line_to(bx + s, by)
        cr.stroke()
        cr.set_dash([])

        # the other channels, faintly, when they are not straight
        for key, _ in cv.CHANNELS:
            if key != self.channel and not cv.is_identity(self.value[key]):
                self._curve(cr, self.value[key], CHANNEL_COLOURS[key], 0.35, 1.0)
        self._curve(cr, self.points, colour, 1.0, 1.8)

        for i, (x, y) in enumerate(self.points):
            sx, sy = self._to_screen(x, y)
            cr.arc(sx, sy, 4.5 if i == self.selected else 3.5, 0, 6.2832)
            if i == self.selected:
                cr.set_source_rgba(*colour, 1.0)
                cr.fill()
            else:
                cr.set_source_rgba(*theme.rgb(theme.BG0))
                cr.fill_preserve()
                cr.set_source_rgba(*colour, 1.0)
                cr.set_line_width(1.5)
                cr.stroke()
        if self.selected is not None and self.selected < len(self.points):
            x, y = self.points[self.selected]
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10)
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM))
            cr.move_to(bx + 5, by + 13)
            cr.show_text(f"In {round(x * 255)}  Out {round(y * 255)}")

    def _curve(self, cr, points, colour, alpha, width):
        bx, by, s = self._box()
        samples = cv.table(points, max(32, int(s / 2)))
        n = len(samples)
        for i, v in enumerate(samples):
            px, py = bx + i / (n - 1) * s, by + (1 - v) * s
            if i == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.set_source_rgba(*colour, alpha)
        cr.set_line_width(width)
        cr.stroke()
