"""Scopes panel: waveform, RGB parade, vectorscope and histogram of the
Program (or Source) monitor's picture, updated as it plays."""

import math

import cairo
import numpy as np
from gi.repository import GLib, Gtk

from .. import scopes as sc
from . import theme
from .widgets import label

FEEDS = [("program", "Program"), ("source", "Source")]
MAX_RATE = 15            # analyses per second while playing
VECTOR_SIZE = 256
LABEL_W = 26             # room for the scale on the left of a waveform


def _surface(argb):
    """A cairo surface over a (h, w, 4) uint8 array; keep the array alive."""
    h, w, _ = argb.shape
    return cairo.ImageSurface.create_for_data(memoryview(np.ascontiguousarray(argb)), cairo.FORMAT_ARGB32, w, h,
                                              w * 4)


class ScopesPanel(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        prefs = app.prefs
        self.images = {}          # scope -> (cairo surface, backing array) or data
        self.info = {}
        self._frame_id = None
        self._last = 0
        self._timer = 0

        bar = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
        self.mode = self._dropdown(sc.MODES, prefs["scope_mode"], "scope_mode")
        self.mode.set_tooltip_text("Which scope to show")
        self.kind = self._dropdown(sc.WAVEFORM_KINDS, prefs["scope_waveform"], "scope_waveform")
        self.kind.set_tooltip_text("What the waveform plots: luma, the red, green and blue channels over "
                                   "each other, or luma with the two colour-difference signals")
        self.feed = self._dropdown(FEEDS, prefs["scope_feed"], "scope_feed")
        self.feed.set_tooltip_text("Which monitor's picture is measured")
        for w in (self.mode, self.kind, self.feed):
            bar.append(w)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(label("Brightness", css="faint"))
        self.gain = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.25, 3.0, 0.05)
        self.gain.set_value(prefs["scope_gain"])
        self.gain.set_draw_value(False)
        self.gain.set_size_request(90, -1)
        self.gain.set_focusable(False)
        self.gain.set_tooltip_text("Brightness of the traces")
        self.gain.connect("value-changed", self._gain_changed)
        bar.append(self.gain)
        self.append(bar)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        self.append(self.area)
        app.on("frame", self._frame)
        self.connect("map", lambda *_: self.analyse(force=True))
        self._sync_kind()

    def _dropdown(self, items, current, pref):
        drop = Gtk.DropDown.new_from_strings([text for _, text in items])
        drop.set_focusable(False)
        keys = [k for k, _ in items]
        drop.set_selected(keys.index(current) if current in keys else 0)

        def changed(d, _p):
            self.app.prefs[pref] = keys[d.get_selected()]
            self.app.prefs.save()
            self._sync_kind()
            self.analyse(force=True)

        drop.connect("notify::selected", changed)
        return drop

    def _sync_kind(self):
        self.kind.set_visible(self.app.prefs["scope_mode"] in ("waveform", "all"))

    def _gain_changed(self, scale):
        self.app.prefs["scope_gain"] = round(scale.get_value(), 2)
        self.analyse(force=True)

    # -- analysis ----------------------------------------------------------
    def _frame(self, kind, pixels, w, h):
        if kind != self.app.prefs["scope_feed"] or not self.get_mapped():
            return
        now = GLib.get_monotonic_time()
        wait = 1_000_000 // MAX_RATE - (now - self._last)
        if wait <= 0:
            self.analyse()
        elif not self._timer:
            self._timer = GLib.timeout_add(max(1, wait // 1000), self._deferred)

    def _deferred(self):
        self._timer = 0
        self.analyse()
        return False

    def analyse(self, force=False):
        """Measure the latest frame of the chosen monitor and redraw."""
        if not self.get_mapped():
            return
        frame = self.app.frames.get(self.app.prefs["scope_feed"])
        if not frame or frame[0] is None:
            self.images, self.info, self._frame_id = {}, {}, None
            self.area.queue_draw()
            return
        if frame[0] is self._frame_id and not force:
            return
        self._frame_id = frame[0]
        self._last = GLib.get_monotonic_time()
        img = sc.frame_array(*frame)
        mode = self.app.prefs["scope_mode"]
        gain = self.app.prefs["scope_gain"]
        wanted = ("waveform", "parade", "vectorscope", "histogram") if mode == "all" else (mode,)
        images = {}
        h, w = img.shape[:2]
        if "waveform" in wanted:
            layers = [(sc.spread(sc.intensity(d, gain, h * 0.2)), c)
                      for d, c in sc.waveform(img, self.app.prefs["scope_waveform"])]
            arr = sc.to_argb(layers, sc.LEVELS, w)
            images["waveform"] = (_surface(arr), arr)
        if "parade" in wanted:
            arrs = []
            for d, c in sc.parade(img):
                arr = sc.to_argb([(sc.spread(sc.intensity(d, gain, h * 0.2)), c)], sc.LEVELS, w)
                arrs.append((_surface(arr), arr))
            images["parade"] = arrs
        if "vectorscope" in wanted:
            count, colour = sc.vectorscope(img, VECTOR_SIZE)
            amount = sc.intensity(count, gain, h * w * 0.004)
            # Each bin shows the colour of its pixels, lifted so dark colours stay visible.
            tint = colour / 255.0
            tint = 0.35 + 0.65 * tint / np.maximum(tint.max(axis=-1, keepdims=True), 1e-3)
            arr = sc.argb_from_rgb(tint, amount)
            images["vectorscope"] = (_surface(arr), arr)
        if "histogram" in wanted:
            images["histogram"] = sc.histogram(img)
            self.info["clipping"] = sc.clipping(img)
        self.images = images
        self.area.queue_draw()

    # -- drawing -----------------------------------------------------------
    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.paint()
        if not self.images:
            self._centre_text(cr, w, h, "No picture to measure")
            return
        mode = self.app.prefs["scope_mode"]
        if mode == "all":
            hw, hh = w / 2, h / 2
            cells = [("waveform", 0, 0), ("vectorscope", hw, 0), ("parade", 0, hh), ("histogram", hw, hh)]
            for name, x, y in cells:
                self._scope(cr, name, x + 4, y + 4, hw - 8, hh - 8)
        else:
            self._scope(cr, mode, 6, 6, w - 12, h - 12)

    def _scope(self, cr, name, x, y, w, h):
        if w < 40 or h < 40 or name not in self.images:
            return
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        if name == "waveform":
            self._waveform(cr, [self.images["waveform"]], x, y, w, h)
        elif name == "parade":
            self._waveform(cr, self.images["parade"], x, y, w, h)
        elif name == "vectorscope":
            self._vectorscope(cr, x, y, w, h)
        elif name == "histogram":
            self._histogram(cr, x, y, w, h)
        cr.restore()

    def _text(self, cr, x, y, text, size=9, colour=None):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        cr.set_source_rgba(*(colour or theme.rgb(theme.TEXT_FAINT)))
        cr.move_to(x, y)
        cr.show_text(text)

    def _centre_text(self, cr, w, h, text):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        ext = cr.text_extents(text)
        cr.set_source_rgba(*theme.rgb(theme.TEXT_FAINT))
        cr.move_to((w - ext.width) / 2, h / 2)
        cr.show_text(text)

    def _waveform(self, cr, images, x, y, w, h):
        gx, gw = x + LABEL_W, w - LABEL_W
        top, gh = y + 6, h - 12
        cr.set_source_rgba(0, 0, 0, 1)
        cr.rectangle(gx, top, gw, gh)
        cr.fill()
        cr.set_line_width(1)
        for pct in (0, 25, 50, 75, 100):
            ly = round(top + gh * (1 - pct / 100)) + .5
            cr.set_source_rgba(*theme.rgb(theme.BG4 if pct in (0, 100) else theme.BG3))
            cr.move_to(gx, ly)
            cr.line_to(gx + gw, ly)
            cr.stroke()
            self._text(cr, x, ly + 3, str(pct))
        gap = 4 if len(images) > 1 else 0
        cell = (gw - gap * (len(images) - 1)) / len(images)
        for i, (surface, _arr) in enumerate(images):
            cr.save()
            cx = gx + i * (cell + gap)
            cr.translate(cx, top)
            cr.scale(cell / surface.get_width(), gh / surface.get_height())
            cr.set_source_surface(surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_BEST)
            cr.set_operator(cairo.OPERATOR_ADD)
            cr.paint()
            cr.restore()

    def _vectorscope(self, cr, x, y, w, h):
        size = min(w, h) - 4
        cx, cy = x + w / 2, y + h / 2
        r = size / 2
        cr.set_source_rgba(0, 0, 0, 1)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb(theme.BG4))
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(*theme.rgb(theme.BG3))
        cr.move_to(cx - r, cy)
        cr.line_to(cx + r, cy)
        cr.move_to(cx, cy - r)
        cr.line_to(cx, cy + r)
        cr.stroke()
        # skin tone line
        a = math.radians(sc.SKIN_ANGLE)
        cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD, 0.55))
        cr.move_to(cx, cy)
        cr.line_to(cx + math.cos(a) * r, cy - math.sin(a) * r)
        cr.stroke()
        surface, _arr = self.images["vectorscope"]
        cr.save()
        # The grid spans Cb and Cr of -0.5..0.5, i.e. the circle's diameter.
        cr.translate(cx - r, cy - r)
        cr.scale(size / surface.get_width(), size / surface.get_height())
        cr.set_source_surface(surface, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BEST)
        cr.set_operator(cairo.OPERATOR_ADD)
        cr.paint()
        cr.restore()
        # 75 % colour bar targets
        cr.set_line_width(1)
        for name, rgb in sc.BAR_TARGETS:
            cb, cr_ = sc.rgb_to_cbcr(*rgb)
            tx, ty = cx + cb * size, cy - cr_ * size
            cr.set_source_rgba(*theme.rgb(theme.TEXT_DIM, 0.8))
            cr.rectangle(tx - 5, ty - 5, 10, 10)
            cr.stroke()
            ox, oy = (tx - cx), (ty - cy)
            n = math.hypot(ox, oy) or 1
            self._text(cr, tx + ox / n * 11 - 5, ty + oy / n * 11 + 3, name)

    def _histogram(self, cr, x, y, w, h):
        counts = self.images["histogram"]
        top, gh = y + 4, h - 22
        cr.set_source_rgba(0, 0, 0, 1)
        cr.rectangle(x, top, w, gh)
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb(theme.BG3))
        for i in range(1, 4):
            lx = round(x + w * i / 4) + .5
            cr.move_to(lx, top)
            cr.line_to(lx, top + gh)
        cr.stroke()
        peak = max(float(counts[k].max()) for k in ("red", "green", "blue", "luma")) or 1.0
        gain = self.app.prefs["scope_gain"]

        def path(values):
            vals = np.clip(np.sqrt(values / peak) * gain, 0, 1)
            cr.move_to(x, top + gh)
            for i, v in enumerate(vals):
                cr.line_to(x + (i + 0.5) / 256 * w, top + gh - v * gh)
            cr.line_to(x + w, top + gh)
            cr.close_path()

        cr.save()
        cr.set_operator(cairo.OPERATOR_ADD)
        for key, colour in (("red", (0.75, 0.18, 0.16)), ("green", (0.16, 0.65, 0.2)), ("blue", (0.2, 0.32, 0.85))):
            path(counts[key])
            cr.set_source_rgba(*colour, 0.85)
            cr.fill()
        cr.restore()
        path(counts["luma"])
        cr.set_source_rgba(0.92, 0.94, 0.97, 0.9)
        cr.set_line_width(1.2)
        cr.stroke()
        low, high = self.info.get("clipping", (0.0, 0.0))
        warn = theme.rgb(theme.WARN)
        self._text(cr, x, y + h - 4, f"Crushed {low * 100:.1f} %", 10, warn if low > 0.01 else None)
        text = f"Clipped {high * 100:.1f} %"
        cr.set_font_size(10)
        ext = cr.text_extents(text)
        self._text(cr, x + w - ext.width - 2, y + h - 4, text, 10, warn if high > 0.01 else None)
