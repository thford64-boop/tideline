"""The Cutout window: click what to keep, see the result live.

    left-click     keep what is under the pointer
    right-click    remove what is under the pointer (also Alt+click)
    left-drag      draw a box around something to keep
    scroll         after a single click: take a smaller or bigger part
    Ctrl+Z         undo the last click; Esc cancels; Enter applies

The AI runs in another process (``cutout_core.Session``); every request is
answered on a background thread and handed back to the main loop, so the
window never freezes while SAM thinks.
"""

import copy
import math
import os

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

OBJECT_COLOURS = [(0.24, 0.78, 0.45), (0.30, 0.62, 1.0), (1.0, 0.72, 0.20), (0.85, 0.45, 1.0),
                  (0.25, 0.85, 0.85), (1.0, 0.50, 0.55)]
VIEWS = [("highlight", "Highlight"), ("cutout", "Cut-out"), ("original", "Original")]
CSS = b"""
.cutout-scrub trough { background: alpha(#ffffff, 0.16); min-height: 6px; border-radius: 3px; }
.cutout-scrub highlight { background: alpha(#3ec776, 0.55); border-radius: 3px; }
.cutout-scrub slider { min-width: 14px; min-height: 14px; margin: -5px; background: #e8e8e8; }
.cutout-scrub mark indicator { color: #3ec776; min-height: 8px; min-width: 2px; }
"""
_css_added = []


def _add_css():
    if _css_added:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _css_added.append(provider)


def _checker():
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 16, 16)
    cr = cairo.Context(surf)
    cr.set_source_rgb(0.78, 0.78, 0.78)
    cr.paint()
    cr.set_source_rgb(0.55, 0.55, 0.55)
    cr.rectangle(0, 0, 8, 8)
    cr.rectangle(8, 8, 8, 8)
    cr.fill()
    pat = cairo.SurfacePattern(surf)
    pat.set_extend(cairo.EXTEND_REPEAT)
    return pat


def fmt_time(seconds):
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:05.2f}"


class CutoutWindow(Gtk.Window):
    """``source``: {"path", "kind" ("image"|"video"), "name", "fps", "first",
    "last", "frame"} with frame numbers of the source file.
    ``on_apply(prompts)`` gets {"objects": [...], "edges": {...}}."""

    def __init__(self, app, session, source, prompts, edges, on_apply, on_close=None):
        super().__init__(title=f"Remove Background — {source['name']}", application=app,
                         transient_for=getattr(app, "window", None), modal=False)
        self.session = session
        self.source = source
        self.on_apply = on_apply
        self.on_close_cb = on_close
        self.video = source["kind"] == "video"
        self.frame = int(source.get("frame") or 0) if self.video else 0
        prompts = copy.deepcopy(prompts or {})
        self.objects = prompts.get("objects") or [{"frames": {}}]
        self.current = len(self.objects) - 1
        self.edges = dict(edges or {})
        self.edges.update(prompts.get("edges") or {})
        self.history = []
        self.view = "highlight"
        self.image = None             # cairo surface of the frame
        self.mask = None              # cairo surface, alpha = removed
        self.img_size = (1, 1)        # full size of the picture in pixels
        self.loaded_frame = None
        self.closed = False
        self.busy = "Starting the AI…"
        self.drag = None              # (x0, y0, x1, y1, button) while dragging
        self.picks = {}               # object index -> [size picked, choices] for lone clicks
        self.hover = None
        self._load_timer = 0
        self._req_load = None
        self.checker = _checker()

        self.set_default_size(*self._default_size())
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        # toolbar
        bar = Gtk.Box(spacing=6)
        root.append(bar)
        hint = Gtk.Label(xalign=0, hexpand=True)
        hint.set_markup("<b>Click</b> what to keep · <b>right-click</b> what to remove · "
                        "<b>drag</b> a box around it")
        hint.set_ellipsize(3)
        bar.append(hint)
        self.view_btns = {}
        group = None
        views = Gtk.Box(spacing=0)
        views.add_css_class("linked")
        for key, lab in VIEWS:
            b = Gtk.ToggleButton(label=lab)
            if group is not None:
                b.set_group(group)
            group = group or b
            b.set_active(key == self.view)
            b.connect("toggled", self._view_toggled, key)
            views.append(b)
            self.view_btns[key] = b
        bar.append(views)

        # canvas
        self.canvas = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.canvas.set_draw_func(self._draw)
        self.canvas.set_focusable(True)
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        drag = Gtk.GestureDrag(button=0)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.canvas.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                           | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", lambda c, dx, dy: self._resize(-1 if dy > 0 else 1) or True)
        self.canvas.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: self._set_hover((x, y)))
        motion.connect("leave", lambda c: self._set_hover(None))
        self.canvas.add_controller(motion)
        frame = Gtk.Frame()
        frame.set_child(self.canvas)
        root.append(frame)

        # video scrubber
        if self.video:
            row = Gtk.Box(spacing=8)
            first, last = int(source["first"]), int(source["last"])
            self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, first, max(first + 1, last), 1)
            self.scale.set_draw_value(False)
            self.scale.set_hexpand(True)
            self.scale.add_css_class("cutout-scrub")
            self.scale.set_tooltip_text("Frame to look at and click on (← → step one frame)")
            _add_css()
            self.scale.set_value(self.frame)
            self.scale.connect("value-changed", self._scrubbed)
            self.time_lbl = Gtk.Label(width_chars=20, xalign=1)
            for b, step in (("go-previous-symbolic", -1), ("go-next-symbolic", 1)):
                btn = Gtk.Button.new_from_icon_name(b)
                btn.set_tooltip_text("Previous frame" if step < 0 else "Next frame")
                btn.connect("clicked", lambda _b, s=step: self.scale.set_value(self.scale.get_value() + s))
                row.append(btn) if step < 0 else None
                if step > 0:
                    self._next_btn = btn
            row.append(self.scale)
            row.append(self._next_btn)
            row.append(self.time_lbl)
            root.append(row)
            self.frames_lbl = Gtk.Label(xalign=0)
            self.frames_lbl.add_css_class("dim-label")
            root.append(self.frames_lbl)

        # controls
        ctl = Gtk.Box(spacing=10)
        root.append(ctl)
        self.obj_lbl = Gtk.Label()
        new_obj = Gtk.Button(label="New Object")
        new_obj.set_tooltip_text("Keep another, separate thing as well (its own clicks and box)")
        new_obj.connect("clicked", lambda *_: self._new_object())
        undo = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        undo.set_tooltip_text("Undo the last click (Ctrl+Z)")
        undo.connect("clicked", lambda *_: self._undo())
        clear = Gtk.Button(label="Clear")
        clear.set_tooltip_text("Remove every click and box" + (" on this frame" if self.video else ""))
        clear.connect("clicked", lambda *_: self._clear())
        self.smaller = Gtk.Button(label="Smaller")
        self.smaller.set_tooltip_text("Keep a smaller part of what you clicked (scroll down)")
        self.smaller.connect("clicked", lambda *_: self._resize(-1))
        self.bigger = Gtk.Button(label="Bigger")
        self.bigger.set_tooltip_text("Keep a bigger part of what you clicked, e.g. the whole person (scroll up)")
        self.bigger.connect("clicked", lambda *_: self._resize(1))
        size_box = Gtk.Box(spacing=0)
        size_box.add_css_class("linked")
        size_box.append(self.smaller)
        size_box.append(self.bigger)
        for w in (self.obj_lbl, new_obj, undo, clear, size_box):
            ctl.append(w)
        ctl.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.refine_sw = self._switch(ctl, "Refine edges", "refine",
                                      "Lets the edge follow the picture's own edges for a cleaner outline")
        self.speck_sw = self._switch(ctl, "Remove specks", "despeckle",
                                     "Removes stray dots and fills pin-holes the AI leaves on busy texture")
        self.decon_sw = self._switch(ctl, "Fix edge colour", "decontaminate",
                                     "Removes the old background's colour fringe from soft edges (in the result)")
        self.grow = self._spin(ctl, "Grow", "grow", -60, 60, "Pixels to grow (or shrink, below 0) the outline")
        self.feather = self._spin(ctl, "Feather", "feather", 0, 60, "Pixels to soften the outline")

        # bottom row
        bottom = Gtk.Box(spacing=8)
        root.append(bottom)
        self.status = Gtk.Label(xalign=0, hexpand=True)
        self.status.set_ellipsize(3)
        bottom.append(self.status)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.apply_btn = Gtk.Button(label="Remove Background")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        bottom.append(cancel)
        bottom.append(self.apply_btn)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("close-request", self._closing)
        self._update_labels()
        self._set_status("Starting the AI… (the first time after Tideline starts takes a few seconds)")
        self._load_frame()

    # -- small widget helpers -------------------------------------------------
    def _switch(self, box, text, key, tip):
        row = Gtk.Box(spacing=4)
        lab = Gtk.Label(label=text)
        sw = Gtk.Switch(active=bool(self.edges.get(key, True)), valign=Gtk.Align.CENTER)
        sw.set_tooltip_text(tip)
        lab.set_tooltip_text(tip)
        sw.connect("notify::active", lambda w, _p: self._edge_changed(key, w.get_active()))
        row.append(lab)
        row.append(sw)
        box.append(row)
        return sw

    def _spin(self, box, text, key, lo, hi, tip):
        row = Gtk.Box(spacing=4)
        lab = Gtk.Label(label=text)
        spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
        spin.set_value(float(self.edges.get(key) or 0))
        spin.set_tooltip_text(tip)
        lab.set_tooltip_text(tip)
        spin.connect("value-changed", lambda w: self._edge_changed(key, int(w.get_value())))
        row.append(lab)
        row.append(spin)
        box.append(row)
        return spin

    def _default_size(self):
        display = Gdk.Display.get_default()
        mw, mh = 1600, 1000
        try:
            mons = display.get_monitors()
            if mons.get_n_items():
                geo = mons.get_item(0).get_geometry()
                mw, mh = int(geo.width * 0.8), int(geo.height * 0.85)
        except Exception:
            pass
        return min(1200, mw), min(900, mh)

    # -- state ------------------------------------------------------------------
    def _fp(self, create=False):
        frames = self.objects[self.current].setdefault("frames", {})
        key = str(self.frame)
        if create and key not in frames:
            frames[key] = {"points": [], "box": None}
        return frames.get(key)

    def _snapshot(self):
        self.history.append((copy.deepcopy(self.objects), self.current))
        del self.history[:-100]

    def _has_keep(self):
        return any(fp.get("box") or any(p[2] for p in fp.get("points") or [])
                   for o in self.objects for fp in (o.get("frames") or {}).values())

    def _prompts(self):
        objs = []
        for o in self.objects:
            frames = {k: fp for k, fp in (o.get("frames") or {}).items()
                      if fp and (fp.get("box") or fp.get("points"))}
            if frames:
                objs.append({"frames": frames})
        return {"objects": objs, "edges": dict(self.edges)}

    def _update_labels(self):
        n = len(self.objects)
        self.obj_lbl.set_markup(f"Object <b>{self.current + 1}</b> of {n}")
        self.apply_btn.set_sensitive(self._has_keep() and not self.closed)
        pick = self.picks.get(str(self.current))
        self.smaller.set_sensitive(bool(pick) and pick[0] > 0)
        self.bigger.set_sensitive(bool(pick) and pick[0] < pick[1] - 1)
        if self.video:
            t = self.frame / max(1e-6, self.source["fps"])
            self.time_lbl.set_text(f"{fmt_time(t)}  (frame {self.frame})")
            marked = sorted({int(k) for o in self.objects for k, fp in (o.get("frames") or {}).items()
                             if fp and (fp.get("box") or fp.get("points"))})
            if marked != getattr(self, "_marks", None):
                self._marks = marked
                self.scale.clear_marks()
                for f in marked:
                    self.scale.add_mark(f, Gtk.PositionType.BOTTOM, None)
            if marked:
                shown = ", ".join(str(f) for f in marked[:12]) + (" …" if len(marked) > 12 else "")
                self.frames_lbl.set_text(f"Clicks on frame {shown}. The AI follows what you picked through the "
                                         "whole clip; if it loses track, go to that frame and click again.")
            else:
                self.frames_lbl.set_text("Pick a frame where what you want to keep is clearly visible, then click it.")

    def _set_status(self, text):
        self.status.set_text(text)

    # -- talking to the AI -----------------------------------------------------------
    def _load_frame(self):
        args = {"path": self.source["path"]}
        if self.video:
            args["frame"] = self.frame
        frame = self.frame
        self.busy = "Analysing the frame…" if self.video else "Analysing the picture…"
        self.canvas.queue_draw()

        def done(reply):
            if self.closed or frame != self.frame:
                return
            if not reply.get("ok"):
                self.busy = None
                self._set_status("Couldn't load: " + str(reply.get("error")))
                self.canvas.queue_draw()
                return
            try:
                self.image = cairo.ImageSurface.create_from_png(reply["preview"])
            except Exception as exc:
                self._set_status(f"Couldn't show the frame: {exc}")
                return
            self.img_size = (reply["w"], reply["h"])
            self.loaded_frame = frame
            self.mask = None
            self.busy = None
            self._predict()

        try:
            self.session.request("load", args, done)
        except Exception as exc:
            self.busy = None
            self._set_status(str(exc))

    def _predict(self):
        if self.loaded_frame != self.frame:
            return
        frame = self.frame
        if not any((o.get("frames") or {}).get(str(frame)) for o in self.objects):
            self.mask = None
            self.picks = {}
            self._set_status("Click what you want to keep." if not self._has_keep() else
                             "No clicks on this frame — the AI tracks the object here from your other clicks.")
            self.canvas.queue_draw()
            self._update_labels()
            return
        self.busy = "Thinking…"
        self.canvas.queue_draw()

        def done(reply):
            if self.closed or reply.get("superseded") or frame != self.frame:
                return
            self.busy = None
            if not reply.get("ok"):
                self._set_status("The AI couldn't do that: " + str(reply.get("error")))
                self.canvas.queue_draw()
                return
            try:
                self.mask = cairo.ImageSurface.create_from_png(reply["mask"])
            except Exception as exc:
                self._set_status(f"Couldn't show the mask: {exc}")
                return
            for p in (reply.get("mask"),):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.picks = reply.get("picks") or {}
            self.area = reply.get("area", 0)
            self._update_labels()
            area = reply.get("area", 0)
            if area <= 0:
                self._set_status("Nothing is kept yet — click what to keep.")
            elif str(self.current) in self.picks:
                self._set_status(f"Keeping {area * 100:.0f}% of the picture. Too much or too little? Scroll "
                                 "(or Smaller / Bigger) to change it, or click more parts.")
            else:
                self._set_status(f"Keeping {area * 100:.0f}% of the picture. Click more to add parts, "
                                 "right-click to take parts away.")
            self.canvas.queue_draw()

        self.session.request("predict", {"objects": self._prompts()["objects"], "frame": frame,
                                         "edges": self.edges}, done)
        self._update_labels()

    # -- edits ------------------------------------------------------------------------
    def _edge_changed(self, key, value):
        self.edges[key] = value
        if self.image is not None:
            self._predict()

    def _add_point(self, nx, ny, keep):
        self._snapshot()
        fp = self._fp(create=True)
        fp["points"].append([round(nx, 5), round(ny, 5), 1 if keep else 0])
        fp.pop("size", None)
        self._predict()

    def _set_box(self, x0, y0, x1, y1):
        self._snapshot()
        self._fp(create=True)["box"] = [round(min(x0, x1), 5), round(min(y0, y1), 5),
                                        round(max(x0, x1), 5), round(max(y0, y1), 5)]
        self._predict()

    def _resize(self, step):
        """Pick SAM's next smaller or bigger answer for a lone click."""
        pick = self.picks.get(str(self.current))
        fp = self._fp()
        if not pick or not fp:
            return
        size = min(pick[1] - 1, max(0, pick[0] + step))
        if size == pick[0]:
            return
        self._snapshot()
        fp["size"] = size
        self.picks[str(self.current)] = [size, pick[1]]
        self._predict()

    def _undo(self):
        if not self.history:
            return
        self.objects, self.current = self.history.pop()
        self._predict()

    def _clear(self):
        self._snapshot()
        if self.video:
            for o in self.objects:
                (o.get("frames") or {}).pop(str(self.frame), None)
        else:
            self.objects, self.current = [{"frames": {}}], 0
        self._predict()

    def _new_object(self):
        if not (self._fp() or {}).get("points") and not (self._fp() or {}).get("box") \
                and not self.objects[self.current].get("frames"):
            return                      # the current one is still empty
        self._snapshot()
        self.objects.append({"frames": {}})
        self.current = len(self.objects) - 1
        self._update_labels()
        self._set_status(f"Object {self.current + 1}: click or drag a box around the next thing to keep.")
        self.canvas.queue_draw()

    def _apply(self):
        if not self._has_keep():
            return
        prompts = self._prompts()
        self.closed = True
        self.on_apply(prompts)
        self.close()

    def _closing(self, *_):
        self.closed = True
        if self._load_timer:
            GLib.source_remove(self._load_timer)
            self._load_timer = 0
        if self.on_close_cb:
            self.on_close_cb()
        return False

    def _key(self, ctl, keyval, code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self._undo()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._apply()
            return True
        if self.video and keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and not ctrl:
            self.scale.set_value(self.scale.get_value() + (1 if keyval == Gdk.KEY_Right else -1))
            return True
        return False

    def _view_toggled(self, btn, key):
        if btn.get_active():
            self.view = key
            self.canvas.queue_draw()

    def _scrubbed(self, scale):
        self.frame = int(round(scale.get_value()))
        self._update_labels()
        if self._load_timer:
            GLib.source_remove(self._load_timer)

        def go():
            self._load_timer = 0
            self._load_frame()
            return False
        self._load_timer = GLib.timeout_add(250, go)

    # -- pointer ------------------------------------------------------------------------
    def _fit(self):
        if self.image is None:
            return None
        aw, ah = self.canvas.get_width(), self.canvas.get_height()
        iw, ih = self.image.get_width(), self.image.get_height()
        s = min(aw / iw, ah / ih)
        dw, dh = iw * s, ih * s
        return (aw - dw) / 2, (ah - dh) / 2, dw, dh

    def _norm(self, x, y):
        fit = self._fit()
        if fit is None:
            return None
        ox, oy, dw, dh = fit
        nx, ny = (x - ox) / dw, (y - oy) / dh
        if -0.02 <= nx <= 1.02 and -0.02 <= ny <= 1.02:
            return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))
        return None

    def _set_hover(self, pos):
        self.hover = pos
        self.canvas.queue_draw()

    def _drag_begin(self, gesture, x, y):
        self.canvas.grab_focus()
        button = gesture.get_current_button()
        self.drag = [x, y, x, y, button, False]

    def _drag_update(self, gesture, dx, dy):
        if self.drag is None:
            return
        self.drag[2], self.drag[3] = self.drag[0] + dx, self.drag[1] + dy
        if math.hypot(dx, dy) > 6 and self.drag[4] == 1:
            self.drag[5] = True                        # it's a box
        self.canvas.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        d, self.drag = self.drag, None
        if d is None or self.image is None or self.loaded_frame != self.frame:
            self.canvas.queue_draw()
            return
        x0, y0, x1, y1, button, is_box = d
        state = gesture.get_current_event_state()
        if is_box:
            a, b = self._norm(x0, y0), self._norm(x1, y1)
            if a and b and abs(a[0] - b[0]) > 0.005 and abs(a[1] - b[1]) > 0.005:
                self._set_box(a[0], a[1], b[0], b[1])
        else:
            p = self._norm(x0, y0)
            if p:
                remove = button == 3 or bool(state & Gdk.ModifierType.ALT_MASK)
                if button in (1, 3):
                    self._add_point(p[0], p[1], not remove)
        self.canvas.queue_draw()

    # -- drawing --------------------------------------------------------------------------
    def _draw(self, area, cr, w, h):
        cr.set_source_rgb(0.11, 0.11, 0.12)
        cr.paint()
        fit = self._fit()
        if fit is not None:
            ox, oy, dw, dh = fit
            iw, ih = self.image.get_width(), self.image.get_height()
            s = dw / iw
            cr.save()
            cr.translate(ox, oy)
            cr.scale(s, s)
            if self.view == "cutout" and self.mask is not None:
                cr.set_source(self.checker)
                cr.rectangle(0, 0, iw, ih)
                cr.fill()
                # the picture where it is kept: punch the removed part out
                cr.push_group()
                cr.set_source_surface(self.image, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_GOOD)
                cr.paint()
                cr.set_operator(cairo.OPERATOR_DEST_OUT)
                cr.mask_surface(self.mask, 0, 0)
                cr.pop_group_to_source()
                cr.paint()
            else:
                cr.set_source_surface(self.image, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_GOOD)
                cr.paint()
                if self.view == "highlight" and self.mask is not None:
                    cr.set_source_rgba(0.02, 0.02, 0.06, 0.8)
                    cr.mask_surface(self.mask, 0, 0)
            cr.restore()
            if self.view != "original":
                self._draw_prompts(cr, ox, oy, dw, dh)
        if self.drag is not None and self.drag[5]:
            x0, y0, x1, y1 = self.drag[:4]
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.stroke()
            cr.set_dash([])
        if self.busy:
            self._badge(cr, self.busy, 12, 12)

    def _draw_prompts(self, cr, ox, oy, dw, dh):
        for i, obj in enumerate(self.objects):
            fp = (obj.get("frames") or {}).get(str(self.frame))
            if not fp:
                continue
            col = OBJECT_COLOURS[i % len(OBJECT_COLOURS)]
            faint = 1.0 if i == self.current else 0.55
            box = fp.get("box")
            if box:
                cr.set_source_rgba(*col, 0.95 * faint)
                cr.set_line_width(2)
                cr.set_dash([8, 4])
                cr.rectangle(ox + box[0] * dw, oy + box[1] * dh, (box[2] - box[0]) * dw, (box[3] - box[1]) * dh)
                cr.stroke()
                cr.set_dash([])
            for x, y, lab in fp.get("points") or []:
                px, py = ox + x * dw, oy + y * dh
                cr.arc(px, py, 8, 0, 2 * math.pi)
                cr.set_source_rgba(*(col if lab else (0.92, 0.22, 0.22)), faint)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, faint)
                cr.set_line_width(2)
                cr.stroke()
                cr.move_to(px - 4, py)
                cr.line_to(px + 4, py)
                if lab:
                    cr.move_to(px, py - 4)
                    cr.line_to(px, py + 4)
                cr.stroke()

    def _badge(self, cr, text, x, y):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        ext = cr.text_extents(text)
        cr.set_source_rgba(0, 0, 0, 0.7)
        cr.rectangle(x, y, ext.x_advance + 20, 26)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(x + 10, y + 18)
        cr.show_text(text)
