"""Source and Program monitors: video display, scrub bar, transport."""

import math

import cairo
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk

from ..engine.effects import EFFECTS
from ..settings import PREVIEW_SCALES
from . import theme
from .widgets import TimecodeField, icon_button, set_button_icon


class VideoView(Gtk.Widget):
    """Shows the current frame letterboxed, plus optional overlays."""

    def __init__(self, hint=""):
        super().__init__(hexpand=True, vexpand=True)
        self.texture = None
        self.hint = hint
        self.safe_margins = False
        self.overlay = None          # callable(cr, x, y, w, h)
        self.background = None       # colour around the picture; None = the theme's
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_frame(self, pixels, w, h):
        if pixels is None:
            self.texture = None
        else:
            self.texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(pixels), w * 3)
        self.queue_draw()

    def frame_rect(self):
        """(x, y, w, h) of the picture inside the widget."""
        W, H = self.get_width(), self.get_height()
        if self.texture is None or W <= 0 or H <= 0:
            return 0.0, 0.0, float(W), float(H)
        tw, th = self.texture.get_width(), self.texture.get_height()
        scale = min((W - 8) / tw, (H - 8) / th)
        w, h = tw * scale, th * scale
        return (W - w) / 2, (H - h) / 2, w, h

    def do_measure(self, orientation, for_size):
        return (120, 480, -1, -1) if orientation == Gtk.Orientation.HORIZONTAL else (68, 270, -1, -1)

    def do_snapshot(self, snapshot):
        W, H = self.get_width(), self.get_height()
        full = Graphene.Rect().init(0, 0, W, H)
        bg = Gdk.RGBA()
        bg.parse(self.background or theme.BG0)
        snapshot.append_color(bg, full)
        x, y, w, h = self.frame_rect()
        if self.texture is not None:
            snapshot.append_scaled_texture(self.texture, Gsk.ScalingFilter.LINEAR,
                                           Graphene.Rect().init(x, y, w, h))
        if self.texture is None or self.safe_margins or self.overlay:
            cr = snapshot.append_cairo(full)
            if self.texture is None:
                cr.set_source_rgba(*theme.rgb(theme.TEXT_FAINT))
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
                cr.set_font_size(13)
                ext = cr.text_extents(self.hint)
                cr.move_to((W - ext.width) / 2, H / 2)
                cr.show_text(self.hint)
                return
            if self.safe_margins:
                cr.set_line_width(1)
                cr.set_source_rgba(1, 1, 1, 0.55)
                for inset in (0.05, 0.1):
                    cr.rectangle(x + w * inset + .5, y + h * inset + .5, w * (1 - 2 * inset), h * (1 - 2 * inset))
                cr.stroke()
                cr.move_to(x + w / 2 - 8, y + h / 2)
                cr.line_to(x + w / 2 + 8, y + h / 2)
                cr.move_to(x + w / 2, y + h / 2 - 8)
                cr.line_to(x + w / 2, y + h / 2 + 8)
                cr.stroke()
            if self.overlay:
                self.overlay(cr, x, y, w, h)


class ScrubBar(Gtk.DrawingArea):
    """Mini time ruler under a monitor: playhead, in/out range, markers."""

    def __init__(self, on_seek):
        super().__init__(hexpand=True)
        self.set_content_height(20)
        self.on_seek = on_seek
        self.length, self.position, self.marks, self.markers = 1, 0, (None, None), []
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, x, y: self._seek(x))
        drag.connect("drag-update", lambda g, dx, dy: self._seek(g.get_start_point()[1] + dx))
        self.add_controller(drag)

    def update(self, length, position, marks, markers=()):
        state = (max(1, length), position, marks, tuple(markers))
        if state != (self.length, self.position, self.marks, tuple(self.markers)):
            self.length, self.position, self.marks, self.markers = state
            self.queue_draw()

    def _x(self, frame, w):
        return 6 + (w - 12) * frame / max(1, self.length)

    def _seek(self, x):
        w = self.get_width()
        f = int(round((x - 6) / max(1, w - 12) * self.length))
        self.on_seek(max(0, min(self.length - 1, f)))

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(*theme.rgb(theme.BG2))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.set_source_rgba(*theme.rgb(theme.BG4))
        cr.rectangle(6, h / 2 - 1, w - 12, 2)
        cr.fill()
        a, b = self.marks
        if a is not None or b is not None:
            xa = self._x(a if a is not None else 0, w)
            xb = self._x(b if b is not None else self.length, w)
            cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.35))
            cr.rectangle(xa, 4, max(2, xb - xa), h - 8)
            cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.ACCENT))
            for x, d in ((xa, 3), (xb, -3)):
                cr.move_to(x, 3)
                cr.line_to(x, h - 3)
                cr.move_to(x, 3)
                cr.line_to(x + d, 3)
                cr.move_to(x, h - 3)
                cr.line_to(x + d, h - 3)
            cr.stroke()
        for t, color in self.markers:
            x = self._x(t, w)
            cr.set_source_rgba(*theme.rgb(color))
            cr.move_to(x - 3, 1)
            cr.line_to(x + 3, 1)
            cr.line_to(x, 6)
            cr.close_path()
            cr.fill()
        x = self._x(self.position, w)
        cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
        cr.rectangle(x - 1, 0, 2, h)
        cr.fill()
        cr.move_to(x - 5, 0)
        cr.line_to(x + 5, 0)
        cr.line_to(x, 7)
        cr.close_path()
        cr.fill()


RESOLUTIONS = PREVIEW_SCALES


class MonitorPanel(Gtk.Box):
    def __init__(self, app, kind):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.kind = kind
        self.player = app.source if kind == "source" else app.program
        hint = ("Double-click a clip in the Project panel to open it here" if kind == "source"
                else "Drag media into the timeline to start a sequence")
        self.view = VideoView(hint)
        self.append(self.view)
        self.scrub = ScrubBar(self._seek)
        self.append(self.scrub)

        info = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=3)
        fps = (lambda: self._fps())
        self.tc = TimecodeField(fps)
        self.tc.connect("seek", lambda w, f: self._seek(f))
        self.tc.set_tooltip_text("Current time — click to type, drag to scrub")
        self.duration = TimecodeField(fps, dim=True, editable=False)
        self.duration.set_tooltip_text("Duration (In to Out when marked)")
        self.res = Gtk.DropDown.new_from_strings([r[0] for r in RESOLUTIONS])
        self.res.add_css_class("flat")
        self._sync_resolution()
        self.res.set_tooltip_text("Playback resolution (default in Edit ▸ Preferences)")
        self.res.set_focusable(False)
        self.res.connect("notify::selected", lambda d, p: self.player.set_scale(RESOLUTIONS[d.get_selected()][1]))
        app.on("playback_prefs", self._sync_resolution)
        self.name = Gtk.Label(xalign=0.5, hexpand=True, ellipsize=3)
        self.name.add_css_class("dim")
        info.append(self.tc)
        info.append(self.name)
        info.append(self.res)
        info.append(self.duration)
        self.append(info)

        bar = Gtk.Box(spacing=1, halign=Gtk.Align.CENTER)
        bar.add_css_class("transport")
        cmds = [("mark_in", "mark_in", "Mark In (I)"), ("mark_out", "mark_out", "Mark Out (O)"),
                ("goto_in", "goto_in", "Go to In (Shift+I)"), ("step_back", "step_back", "Step Back (Left)"),
                ("play", "play_toggle", "Play / Stop (Space)"), ("step_fwd", "step_fwd", "Step Forward (Right)"),
                ("goto_out", "goto_out", "Go to Out (Shift+O)")]
        if kind == "source":
            cmds += [("insert", "insert", "Insert (,)"), ("overwrite", "overwrite", "Overwrite (.)")]
        else:
            cmds += [("lift", "lift", "Lift (;)"), ("extract", "extract", "Extract (')"),
                     ("camera", "export_frame", "Export Frame (Ctrl+Shift+E)"),
                     ("popout", "popout_monitor", "Pop-out Preview: show the program on another display "
                                                  "(Ctrl+Shift+F11)")]
        for icon, cmd, tip in cmds:
            btn = icon_button(icon, tip, lambda b, c=cmd: self._command(c), size=17)
            if icon == "play":
                btn.add_css_class("play")
                self.play_button = btn
            bar.append(btn)
        self.safe_button = icon_button("safe", "Safe margins", self._toggle_safe, toggle=True, size=17)
        bar.append(self.safe_button)
        self.proxy_button = icon_button("proxy", "Use proxies: play the small proxy copies instead of the "
                                        "original media where they exist (Ctrl+Alt+P). Exports always use "
                                        "the originals.", self._toggle_proxies, toggle=True, size=17)
        self.proxy_button.set_active(app.prefs["use_proxies"])
        bar.append(self.proxy_button)
        app.on("proxies", self._sync_proxies)
        self.append(bar)

        app.on("frame", self._frame)
        app.on("playstate", self._playstate)
        if kind == "source":
            app.on("source", self.refresh)
            app.on("source_pos", self.refresh)
            self.player.on_frame = self._source_frame
            src = Gtk.DragSource(actions=Gdk.DragAction.COPY)
            src.connect("prepare", self._drag_prepare)
            src.connect("drag-end", lambda *_: setattr(app, "drag_payload", None))
            self.view.add_controller(src)
        else:
            for ev in ("playhead", "marks", "sequence", "project"):
                app.on(ev, self.refresh)
            for ev in ("selection", "effects", "playhead", "tool"):
                app.on(ev, self.view.queue_draw)
            app.on("safe_margins", lambda: self.safe_button.set_active(not self.safe_button.get_active()))
            self.view.overlay = self._overlay
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._xf_begin)
            drag.connect("drag-update", self._xf_update)
            drag.connect("drag-end", self._xf_end)
            self.view.add_controller(drag)
            self._xf = None
        self.refresh()

    # -- state -----------------------------------------------------------
    def _sync_resolution(self):
        scales = [r[1] for r in RESOLUTIONS]
        best = min(range(len(scales)), key=lambda i: abs(scales[i] - self.player.scale))
        if self.res.get_selected() != best:
            self.res.set_selected(best)

    def _fps(self):
        if self.kind == "program":
            return self.app.fps()
        seq = self.app.seq
        if seq is not None:
            return seq.fps_num, seq.fps_den
        return int(round(self.player.fps * 1000)), 1000

    def _command(self, cmd):
        self.app.focus = self.kind
        if self.app.window:
            self.app.window.show_panel(self.kind)
        self.app.do(cmd)

    def _seek(self, frame):
        self.app.focus = self.kind
        if self.kind == "source":
            self.player.seek(frame)
            self.refresh()
        else:
            self.app.set_playhead(frame)

    def _toggle_proxies(self, btn):
        self.app.set_use_proxies(btn.get_active())

    def _sync_proxies(self):
        if self.proxy_button.get_active() != self.app.prefs["use_proxies"]:
            self.proxy_button.set_active(self.app.prefs["use_proxies"])

    def _toggle_safe(self, btn):
        self.view.safe_margins = btn.get_active()
        self.view.queue_draw()

    def _frame(self, kind, pixels, w, h):
        if kind == self.kind:
            self.view.set_frame(pixels, w, h)

    def _source_frame(self, pixels, w, h, pos):
        self.app.emit("frame", "source", pixels, w, h)
        if self.player.playing:
            self.refresh()

    def _playstate(self):
        set_button_icon(self.play_button, "pause" if self.player.playing else "play")

    def refresh(self, *_):
        app = self.app
        if self.kind == "source":
            m = app.source_media
            if m is None:
                self.scrub.update(1, 0, (None, None))
                self.tc.set_frame(0)
                self.duration.set_frame(0)
                self.name.set_text("")
                return
            a, b = app.source_marks()
            marked = m.mark_in is not None or m.mark_out is not None
            self.scrub.update(self.player.length, self.player.position,
                              (a if m.mark_in is not None else None, b if m.mark_out is not None else None))
            self.tc.set_frame(self.player.position)
            self.duration.set_frame(b - a if marked else self.player.length)
            self.name.set_text(m.name)
        else:
            seq = app.seq
            if seq is None:
                self.scrub.update(1, 0, (None, None))
                self.tc.set_frame(0)
                self.duration.set_frame(0)
                self.name.set_text("")
                return
            end = max(1, seq.end())
            self.scrub.update(end, seq.playhead, (seq.in_point, seq.out_point),
                              [(m.t, theme.MARKER_COLORS[m.color % len(theme.MARKER_COLORS)]) for m in seq.markers])
            self.tc.set_frame(seq.playhead)
            if seq.in_point is not None or seq.out_point is not None:
                self.duration.set_frame((seq.out_point if seq.out_point is not None else end) - (seq.in_point or 0))
            else:
                self.duration.set_frame(seq.end())
            self.name.set_text(seq.name)

    def _drag_prepare(self, source, x, y):
        m = self.app.source_media
        if m is None:
            return None
        self.app.drag_payload = ("source", [m.id])
        return Gdk.ContentProvider.new_for_value(GObject.Value(str, "tideline:source:" + m.id))

    # -- direct manipulation of the selected clip (program monitor) --------
    def _target(self):
        """(clip, motion effect) when exactly one visible video clip is selected."""
        app = self.app
        seq = app.seq
        if seq is None or app.tool not in ("select", "type"):
            return None
        pairs = [(t, c) for t, c in app.selected_clips() if t.kind == "video"]
        if len(pairs) != 1:
            return None
        clip = pairs[0][1]
        if not (clip.start <= seq.playhead < clip.end):
            return None
        motion = clip.effect("motion")
        return (clip, motion) if motion is not None else None

    def _box(self, clip, motion):
        """Clip rectangle in sequence pixels: (cx, cy, w, h, rotation)."""
        app = self.app
        seq = app.seq
        edef = EFFECTS["motion"]
        get = lambda n: app.param_value(clip, motion, edef.param(n))
        cx, cy = get("position")
        sh = get("scale")
        sw = sh if motion.params.get("uniform", True) else get("scale_w")
        return cx, cy, seq.width * sw / 100.0, seq.height * sh / 100.0, get("rotation")

    def _overlay(self, cr, x, y, w, h):
        target = self._target()
        if target is None:
            return
        seq = self.app.seq
        k = w / seq.width
        cx, cy, bw, bh, rot = self._box(*target)
        cr.save()
        cr.translate(x + cx * k, y + cy * k)
        cr.rotate(math.radians(rot))
        cr.set_line_width(1.2)
        cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.95))
        cr.rectangle(-bw * k / 2, -bh * k / 2, bw * k, bh * k)
        cr.stroke()
        for sx in (-1, 1):
            for sy in (-1, 1):
                cr.rectangle(sx * bw * k / 2 - 4, sy * bh * k / 2 - 4, 8, 8)
        cr.fill()
        cr.arc(0, 0, 4, 0, 2 * math.pi)
        cr.move_to(-8, 0)
        cr.line_to(8, 0)
        cr.move_to(0, -8)
        cr.line_to(0, 8)
        cr.stroke()
        cr.restore()

    def _to_seq(self, px, py):
        x, y, w, h = self.view.frame_rect()
        seq = self.app.seq
        k = seq.width / w if w else 1.0
        return (px - x) * k, (py - y) * k

    def _xf_begin(self, gesture, px, py):
        app = self.app
        self._xf = None
        seq = app.seq
        if seq is None:
            return
        app.focus = "program"
        sx, sy = self._to_seq(px, py)
        if app.tool == "type":
            app.cmd_new_title(offset=[round(sx), round(sy)])
            app.set_tool("select")
            return
        if app.tool != "select":
            return
        target = self._target()
        if target is not None:
            cx, cy, bw, bh, rot = self._box(*target)
            r = math.radians(-rot)
            lx = (sx - cx) * math.cos(r) - (sy - cy) * math.sin(r)
            ly = (sx - cx) * math.sin(r) + (sy - cy) * math.cos(r)
            grab = 12 * seq.width / max(1.0, self.view.frame_rect()[2])
            near_corner = abs(abs(lx) - bw / 2) < grab and abs(abs(ly) - bh / 2) < grab
            inside = abs(lx) <= bw / 2 and abs(ly) <= bh / 2
            if near_corner or inside:
                clip, motion = target
                self._xf = {"mode": "scale" if near_corner else "move", "clip": clip, "motion": motion,
                            "pos": [cx, cy], "scale": app.param_value(clip, motion, EFFECTS["motion"].param("scale")),
                            "start": (sx, sy), "dist": max(1.0, math.hypot(sx - cx, sy - cy)), "moved": False}
                return
        # Click on the picture: select the topmost video clip under the pointer.
        for track in reversed(seq.video_tracks):
            c = track.clip_at(seq.playhead)
            if c is None or track.hidden or not c.enabled:
                continue
            m = c.effect("motion")
            if m is None:
                continue
            cx, cy, bw, bh, _ = self._box(c, m)
            if abs(sx - cx) <= bw / 2 and abs(sy - cy) <= bh / 2:
                app.select([c.id], expand=False)
                return
        app.clear_selection()

    def _xf_apply(self, dx, dy, final):
        xf = self._xf
        app, seq = self.app, self.app.seq
        k = seq.width / max(1.0, self.view.frame_rect()[2])
        edef = EFFECTS["motion"]
        if xf["mode"] == "move":
            pos = [round(xf["pos"][0] + dx * k, 1), round(xf["pos"][1] + dy * k, 1)]
            app.set_param(xf["clip"], xf["motion"], edef.param("position"), pos, final)
        else:
            sx, sy = xf["start"][0] + dx * k, xf["start"][1] + dy * k
            d = math.hypot(sx - xf["pos"][0], sy - xf["pos"][1])
            scale = round(max(1.0, xf["scale"] * d / xf["dist"]), 1)
            app.set_param(xf["clip"], xf["motion"], edef.param("scale"), scale, final)

    def _xf_update(self, gesture, dx, dy):
        if self._xf and (self._xf["moved"] or abs(dx) + abs(dy) > 2):
            self._xf["moved"] = True
            self._xf_apply(dx, dy, False)

    def _xf_end(self, gesture, dx, dy):
        if self._xf and self._xf["moved"]:
            self._xf_apply(dx, dy, True)
        self._xf = None
