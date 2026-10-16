"""The timeline: a custom-drawn canvas with track headers, ruler, clips,
transitions, keyframe rubber bands and all the editing tools."""

import copy

import cairo
import numpy as np
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango, PangoCairo

from .. import keyframes as kf
from .. import timecode as tc
from ..edits import EditError
from ..engine.effects import EFFECTS, TRANSITIONS
from ..engine.media import PEAKS_PER_SECOND
from ..model import LABELS
from . import icons, theme
from .widgets import TimecodeField, icon_button, popup_menu, spacer

HEADER_W = 152
RULER_H = 28
DIVIDER_H = 5
EDGE_PX = 7
SNAP_PX = 9
NAME_H = 16
MAX_PPF = 40.0

DRAG_LABELS = {"move": "Move", "trim": "Trim", "ripple": "Ripple Trim", "roll": "Rolling Edit",
               "rate": "Rate Stretch", "slip": "Slip", "slide": "Slide"}
CURSORS = {"hand": "grab", "zoom": "zoom-in", "razor": "crosshair", "slip": "ew-resize",
           "slide": "ew-resize", "pen": "crosshair", "type": "text"}


def _rounded(cr, x, y, w, h, r=3.0):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


class TimelineView(Gtk.DrawingArea):
    def __init__(self, app, panel):
        super().__init__(hexpand=True, vexpand=True)
        self.app = app
        self.panel = panel
        self.scroll_y = 0.0
        self.drag = None
        self.snap_line = None
        self.marquee = None
        self.ghosts = []             # [(track, start, end)] drop preview
        self.hover_x = None
        self.hover_hit = None
        self.tip = None              # text shown next to the pointer while dragging
        self.show_thumbs = True
        self.show_waves = True
        self._cache = None
        self._dirty = True
        self._size = (0, 0)
        self._ptr = (0.0, 0.0)
        self.set_focusable(True)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._press)
        drag.connect("drag-update", self._motion_drag)
        drag.connect("drag-end", self._release)
        self.add_controller(drag)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._clicked)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        motion.connect("leave", self._leave)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([GObject.TYPE_STRING, Gdk.FileList.__gtype__])
        drop.connect("motion", self._dnd_motion)
        drop.connect("leave", self._dnd_leave)
        drop.connect("drop", self._dnd_drop)
        self.add_controller(drop)

        for ev in ("sequence", "selection", "thumbs", "options", "marks", "project", "effects"):
            app.on(ev, self.invalidate)
        app.on("playhead", self._playhead_moved)
        app.on("tool", self._tool_changed)
        app.on("zoom", self.zoom_by)
        app.on("view_fit", self.fit)

    # -- geometry --------------------------------------------------------
    @property
    def seq(self):
        return self.app.seq

    def invalidate(self, *_):
        self._dirty = True
        self.queue_draw()

    def x_of(self, frame):
        s = self.seq
        return HEADER_W + (frame - s.scroll_x) * s.px_per_frame

    def frame_at(self, x):
        s = self.seq
        return max(0, int(round(s.scroll_x + (x - HEADER_W) / s.px_per_frame)))

    def rows(self):
        """[(track, y, height)] top to bottom, plus the divider's y."""
        s = self.seq
        vt, at = list(reversed(s.video_tracks)), s.audio_tracks
        content = sum(t.height for t in vt) + DIVIDER_H + sum(t.height for t in at)
        avail = self.get_height() - RULER_H
        if content <= avail:
            self.scroll_y = 0.0
            y = RULER_H + (avail - content) / 2
        else:
            self.scroll_y = max(0.0, min(self.scroll_y, content - avail))
            y = RULER_H - self.scroll_y
        rows = []
        for t in vt:
            rows.append((t, y, t.height))
            y += t.height
        divider = y
        y += DIVIDER_H
        for t in at:
            rows.append((t, y, t.height))
            y += t.height
        return rows, divider

    def row_of(self, track):
        for t, y, h in self.rows()[0]:
            if t is track or t.id == track.id:
                return y, h
        return None

    def visible_frames(self):
        return max(1.0, (self.get_width() - HEADER_W) / self.seq.px_per_frame)

    def min_ppf(self):
        return max(0.002, (self.get_width() - HEADER_W - 40) / max(self.seq.end() * 4.0, self.seq.fps * 3600))

    # -- view ------------------------------------------------------------
    def zoom_by(self, factor, anchor_x=None):
        s = self.seq
        if s is None:
            return
        w = self.get_width()
        if anchor_x is None:
            px = self.x_of(s.playhead)
            anchor_x = px if HEADER_W <= px <= w else (HEADER_W + w) / 2
        f = s.scroll_x + (anchor_x - HEADER_W) / s.px_per_frame
        s.px_per_frame = max(self.min_ppf(), min(MAX_PPF, s.px_per_frame * factor))
        s.scroll_x = max(0.0, f - (anchor_x - HEADER_W) / s.px_per_frame)
        self.view_changed()

    def fit(self, *_):
        s = self.seq
        if s is None or self.get_width() <= HEADER_W + 60:
            return
        span = max(s.end(), int(s.fps * 5))
        s.px_per_frame = max(self.min_ppf(), min(MAX_PPF, (self.get_width() - HEADER_W - 60) / span))
        s.scroll_x = 0.0
        self.view_changed()

    def view_changed(self):
        self.invalidate()
        self.panel.sync_scrollbar()

    def _playhead_moved(self):
        s = self.seq
        if s is None:
            return
        w = self.get_width()
        if w > HEADER_W + 40 and not (self.drag and self.drag["kind"] == "scrub"):
            x = self.x_of(s.playhead)
            vis = self.visible_frames()
            if x > w - 12:
                s.scroll_x = max(0.0, s.playhead - vis * (0.05 if self.app.program.playing else 0.5))
                self.view_changed()
            elif x < HEADER_W:
                s.scroll_x = max(0.0, s.playhead - vis * (0.9 if self.app.program.playing else 0.5))
                self.view_changed()
        self.queue_draw()

    def _tool_changed(self):
        self.hover_x = None
        self.set_cursor(Gdk.Cursor.new_from_name(CURSORS.get(self.app.tool, "default")))
        self.queue_draw()

    # -- hit testing -----------------------------------------------------
    def hit(self, x, y):
        s = self.seq
        if s is None:
            return {"zone": "empty"}
        if y < RULER_H:
            if x < HEADER_W:
                return {"zone": "corner"}
            for m in s.markers:
                if abs(self.x_of(m.t) - x) <= 5 and y < 14:
                    return {"zone": "ruler", "marker": m}
            return {"zone": "ruler"}
        rows, _ = self.rows()
        for track, ty, th in rows:
            if not (ty <= y < ty + th):
                continue
            if x < HEADER_W:
                return {"zone": "header", "track": track, "button": self._header_button(track, x)}
            ed = self.app.editor()
            band = min(20, th - 6)
            if ty + 2 <= y <= ty + 2 + band:
                for tr in track.transitions:
                    span = ed.transition_span(track, tr)
                    if span and self.x_of(span[0]) <= x < self.x_of(span[1]):
                        return {"zone": "transition", "track": track, "transition": tr}
            best = None
            for c in track.clips:
                x0, x1 = self.x_of(c.start), self.x_of(c.end)
                if x0 <= x < x1:
                    grip = min(EDGE_PX, (x1 - x0) / 3)
                    edge = "start" if x - x0 <= grip else "end" if x1 - x <= grip else None
                    return {"zone": "clip", "track": track, "clip": c, "edge": edge, "y": ty, "h": th}
                for edge, ex in (("end", x1), ("start", x0)):
                    d = abs(x - ex)
                    if d <= EDGE_PX and (best is None or d < best[0]):
                        best = (d, c, edge)
            if best is not None:
                return {"zone": "clip", "track": track, "clip": best[1], "edge": best[2], "y": ty, "h": th,
                        "outside": True}
            return {"zone": "track", "track": track, "frame": self.frame_at(x)}
        return {"zone": "empty"}

    def _header_button(self, track, x):
        if x < 30:
            return "target"
        if 31 <= x < 50:
            return "lock"
        if track.kind == "video":
            return "eye" if x >= HEADER_W - 26 else None
        if x >= HEADER_W - 24:
            return "solo"
        if x >= HEADER_W - 46:
            return "mute"
        return None

    # -- snapping --------------------------------------------------------
    def _snap_points(self, exclude=()):
        s = self.seq
        pts = {0, s.playhead}
        for v in (s.in_point, s.out_point):
            if v is not None:
                pts.add(v)
        pts.update(m.t for m in s.markers)
        for _, c in s.all_clips():
            if c.id not in exclude:
                pts.add(c.start)
                pts.add(c.end)
        return pts

    def _snap(self, t, pts, force=False):
        self.snap_line = None
        if not (self.app.snap or force) or not pts:
            return t
        best = min(pts, key=lambda p: abs(p - t))
        if abs(best - t) * self.seq.px_per_frame <= SNAP_PX:
            self.snap_line = best
            return best
        return t

    def _snap_delta(self, edges, df, pts):
        """Adjust a move of ``df`` frames so one of ``edges`` lands on a snap point."""
        self.snap_line = None
        if not self.app.snap or not pts:
            return df
        best = None
        for e in edges:
            target = e + df
            p = min(pts, key=lambda q: abs(q - target))
            d = p - target
            if abs(d) * self.seq.px_per_frame <= SNAP_PX and (best is None or abs(d) < abs(best[0])):
                best = (d, p)
        if best is not None:
            self.snap_line = best[1]
            return df + best[0]
        return df

    # -- drawing ---------------------------------------------------------
    def _draw(self, area, cr, w, h):
        s = self.seq
        if s is None:
            cr.set_source_rgba(*theme.rgb(theme.BG1))
            cr.paint()
            self._text(cr, "Import media (Ctrl+I), then drag a clip here to create a sequence",
                       0, h / 2 - 10, w, theme.TEXT_FAINT, size=13, align=Pango.Alignment.CENTER)
            return
        if self._dirty or self._size != (w, h) or self._cache is None:
            # The static layer paints every pixel, so a same-sized surface is reused.
            if self._cache is None or self._size != (w, h):
                self._cache = cr.get_target().create_similar(cairo.CONTENT_COLOR, w, h)
                self._size = (w, h)
            c2 = cairo.Context(self._cache)
            self._draw_static(c2, w, h)
            self._dirty = False
        cr.set_source_surface(self._cache, 0, 0)
        cr.paint()
        self._draw_overlay(cr, w, h)

    def _text(self, cr, text, x, y, width, color, size=11, bold=False, align=None, mono=False):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription(("Monospace " if mono else "Sans ") + ("Bold " if bold else "") + f"{size}px")
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        if width > 0:
            layout.set_width(int(width * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        if align is not None:
            layout.set_alignment(align)
        cr.set_source_rgba(*(theme.rgb(color) if isinstance(color, str) else color))
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    def _draw_static(self, cr, w, h):
        s = self.seq
        app = self.app
        rows, divider = self.rows()
        cr.set_source_rgba(*theme.rgb(theme.BG1))
        cr.paint()
        # lanes
        for track, y, th in rows:
            cr.set_source_rgba(*theme.rgb("#141920" if track.kind == "video" else "#12171c"))
            cr.rectangle(HEADER_W, y, w - HEADER_W, th)
            cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.LINE))
            cr.rectangle(HEADER_W, y + th - 1, w - HEADER_W, 1)
            cr.fill()
            if track.locked:
                cr.save()
                cr.rectangle(HEADER_W, y, w - HEADER_W, th)
                cr.clip()
                cr.set_source_rgba(1, 1, 1, 0.035)
                cr.set_line_width(4)
                for x in range(HEADER_W - int(th), w, 14):
                    cr.move_to(x, y + th)
                    cr.line_to(x + th, y)
                cr.stroke()
                cr.restore()
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(0, divider, w, DIVIDER_H)
        cr.fill()
        # in/out range across tracks
        if s.in_point is not None or s.out_point is not None:
            xa = self.x_of(s.in_point or 0)
            xb = self.x_of(s.out_point if s.out_point is not None else max(s.end(), s.playhead))
            cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.07))
            cr.rectangle(max(HEADER_W, xa), RULER_H, max(0, xb - max(HEADER_W, xa)), h - RULER_H)
            cr.fill()
        # clips
        cr.save()
        cr.rectangle(HEADER_W, RULER_H, w - HEADER_W, h - RULER_H)
        cr.clip()
        selected = set(app.selection)
        ed = app.editor()
        for track, y, th in rows:
            if y + th < RULER_H or y > h:
                continue
            for c in track.clips:
                self._draw_clip(cr, track, c, y, th, c.id in selected, w)
            for tr in track.transitions:
                span = ed.transition_span(track, tr)
                if span:
                    self._draw_transition(cr, tr, span, y, th, tr.id == app.selected_transition)
            if app.selected_gap and app.selected_gap[0] == track.id:
                gap = ed.gap_at(track, app.selected_gap[1])
                if gap:
                    cr.set_source_rgba(*theme.rgb(theme.SELECT, 0.16))
                    cr.rectangle(self.x_of(gap[0]), y + 1, (gap[1] - gap[0]) * s.px_per_frame, th - 3)
                    cr.fill()
        cr.restore()
        self._draw_ruler(cr, w)
        for track, y, th in rows:
            if y + th > RULER_H:
                self._draw_header(cr, track, max(y, RULER_H - th), y, th)
        # corner
        cr.set_source_rgba(*theme.rgb(theme.BG2))
        cr.rectangle(0, 0, HEADER_W, RULER_H)
        cr.fill()
        cr.set_source_rgba(*theme.rgb(theme.LINE))
        cr.rectangle(HEADER_W - 1, 0, 1, h)
        cr.rectangle(0, RULER_H - 1, w, 1)
        cr.fill()
        self._text(cr, f"{s.width}×{s.height}  {s.fps:.3g} fps", 8, 7, HEADER_W - 12, theme.TEXT_FAINT, size=10)

    def _draw_ruler(self, cr, w):
        s = self.seq
        cr.set_source_rgba(*theme.rgb(theme.BG2))
        cr.rectangle(HEADER_W, 0, w - HEADER_W, RULER_H)
        cr.fill()
        ppf = s.px_per_frame
        base = tc.timebase(s.fps_num, s.fps_den)
        steps = [1, 2, 5, 10] + [base * k for k in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200,
                                                    18000, 36000)]
        step = next((k for k in steps if k * ppf >= 86), steps[-1])
        minor = step // 5 if step % 5 == 0 and step >= 5 else step // 2 if step % 2 == 0 else step
        if minor * ppf < 5:
            minor = step
        if s.in_point is not None or s.out_point is not None:
            xa = self.x_of(s.in_point or 0)
            xb = self.x_of(s.out_point if s.out_point is not None else max(s.end(), s.playhead))
            cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.32))
            cr.rectangle(max(HEADER_W, xa), RULER_H - 9, max(0, xb - max(HEADER_W, xa)), 8)
            cr.fill()
        first = int(s.scroll_x // minor) * minor
        last = int(s.scroll_x + (w - HEADER_W) / ppf) + minor
        cr.set_line_width(1)
        f = first
        while f <= last:
            x = int(self.x_of(f)) + 0.5
            if x >= HEADER_W:
                major = f % step == 0
                cr.set_source_rgba(*theme.rgb(theme.TEXT_FAINT if major else theme.BG4))
                cr.move_to(x, RULER_H - (10 if major else 5))
                cr.line_to(x, RULER_H - 1)
                cr.stroke()
                if major:
                    self._text(cr, tc.frames_to_tc(f, s.fps_num, s.fps_den), x + 4, 3, 0, theme.TEXT_DIM,
                               size=10, mono=True)
            f += minor
        for m in s.markers:
            x = self.x_of(m.t)
            if x >= HEADER_W - 5:
                cr.set_source_rgba(*theme.rgb(theme.MARKER_COLORS[m.color % len(theme.MARKER_COLORS)]))
                cr.move_to(x - 4.5, 1)
                cr.line_to(x + 4.5, 1)
                cr.line_to(x + 4.5, 7)
                cr.line_to(x, 12)
                cr.line_to(x - 4.5, 7)
                cr.close_path()
                cr.fill()
                if m.id == self.app.selected_marker:
                    cr.set_source_rgba(*theme.rgb(theme.SELECT))
                    cr.rectangle(x - 5.5, 0.5, 11, 12)
                    cr.stroke()

    def _draw_header(self, cr, track, clip_y, y, th):
        s = self.seq
        cr.save()
        cr.rectangle(0, max(RULER_H, y), HEADER_W, th)
        cr.clip()
        cr.set_source_rgba(*theme.rgb(theme.BG2))
        cr.rectangle(0, y, HEADER_W, th)
        cr.fill()
        cr.set_source_rgba(*theme.rgb(theme.LINE))
        cr.rectangle(0, y + th - 1, HEADER_W, 1)
        cr.fill()
        cy = y + th / 2
        _rounded(cr, 4, cy - 9, 24, 18, 3)
        cr.set_source_rgba(*theme.rgb(theme.ACCENT_DIM if track.target else theme.BG3))
        cr.fill()
        self._text(cr, s.track_label(track), 4, cy - 7, 24, "#ffffff" if track.target else theme.TEXT_FAINT,
                   size=10, bold=True, align=Pango.Alignment.CENTER)
        icons.draw(cr, "lock" if track.locked else "unlock", 33, cy - 7, 14,
                   theme.rgb(theme.WARN if track.locked else theme.TEXT_FAINT))
        name = track.name or (("Video " if track.kind == "video" else "Audio ") + s.track_label(track)[1:])
        right = 30 if track.kind == "video" else 50
        self._text(cr, name, 53, cy - 7, HEADER_W - 53 - right, theme.TEXT_DIM, size=11)
        if track.kind == "video":
            icons.draw(cr, "eye_off" if track.hidden else "eye", HEADER_W - 23, cy - 8, 16,
                       theme.rgb(theme.DANGER if track.hidden else theme.TEXT_DIM))
        else:
            for label, x, on, color in (("M", HEADER_W - 44, track.hidden, theme.WARN),
                                        ("S", HEADER_W - 22, track.solo, theme.ACCENT)):
                _rounded(cr, x, cy - 8, 18, 16, 3)
                cr.set_source_rgba(*theme.rgb(color if on else theme.BG3))
                cr.fill()
                self._text(cr, label, x, cy - 6.5, 18, "#10141a" if on else theme.TEXT_DIM, size=10, bold=True,
                           align=Pango.Alignment.CENTER)
        cr.restore()

    def _clip_color(self, clip):
        media = self.app.project.media.get(clip.media_id)
        idx = clip.label if clip.label is not None else (media.label if media else 7)
        return LABELS[idx % len(LABELS)][1]

    def _draw_clip(self, cr, track, clip, y, th, selected, w, ghost=False):
        s = self.seq
        x0, x1 = self.x_of(clip.start), self.x_of(clip.end)
        if x1 < HEADER_W or x0 > w:
            return
        media = self.app.project.media.get(clip.media_id)
        color = self._clip_color(clip)
        bx, by, bw, bh = x0 + 0.5, y + 1.5, max(1.0, x1 - x0 - 1), th - 4
        _rounded(cr, bx, by, bw, bh)
        if not clip.enabled:
            cr.set_source_rgba(0.21, 0.23, 0.26, 0.7 if ghost else 1)
        else:
            cr.set_source_rgba(*theme.shade(color, 0.78 if selected else 0.56), 0.6 if ghost else 1)
        cr.fill()
        vx0, vx1 = max(x0, HEADER_W), min(x1, w)
        cr.save()
        _rounded(cr, bx, by, bw, bh)
        cr.clip()
        body_y, body_h = by + NAME_H, bh - NAME_H
        if not ghost and body_h >= 14 and clip.enabled and media is not None:
            if track.kind == "video" and self.show_thumbs and bw >= 40:
                secs = clip.in_point * abs(clip.speed) / s.fps if not media.unlimited else 0.0
                surf = self.app.media_cache.thumbnail_surface(media, None if media.kind == "image" else secs)
                if surf is not None:
                    k = body_h / surf.get_height()
                    cr.save()
                    cr.translate(bx + 1, body_y)
                    cr.scale(k, k)
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint_with_alpha(0.92)
                    cr.restore()
            elif track.kind == "audio" and self.show_waves:
                self._draw_wave(cr, clip, media, vx0, vx1, body_y, body_h, color)
        # name bar
        cr.set_source_rgba(*theme.shade(color, 1.08 if selected else 0.86), 0.7 if ghost else 1)
        cr.rectangle(bx, by, bw, NAME_H)
        cr.fill()
        tx = max(bx, HEADER_W) + 4
        user_fx = any(not EFFECTS[e.type].intrinsic and e.type != "text" for e in clip.effects if e.type in EFFECTS) \
            or any(e.params or e.keyframes for e in clip.effects if e.type in EFFECTS and EFFECTS[e.type].intrinsic)
        if user_fx and bw > 30:
            _rounded(cr, tx - 1, by + 2, 15, 12, 2)
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.fill()
            self._text(cr, "fx", tx, by + 1.5, 14, theme.WARN, size=9, bold=True, align=Pango.Alignment.CENTER)
            tx += 18
        name = clip.name or (media.name if media else "Missing media")
        if abs(clip.speed - 1.0) > 1e-6 or clip.reverse:
            name += f"  [{'-' if clip.reverse else ''}{clip.speed * 100:.4g}%]"
        if x1 - tx > 14:
            self._text(cr, name, tx, by + 1, x1 - tx - 4, (1, 1, 1, 0.95 if clip.enabled else 0.45), size=10.5)
        if media is None or media.offline:
            cr.set_source_rgba(*theme.rgb(theme.DANGER, 0.35))
            cr.rectangle(bx, body_y, bw, body_h)
            cr.fill()
        if not ghost and th >= 46 and clip.enabled:
            self._draw_band(cr, track, clip, y, th, vx0, vx1)
        cr.restore()
        _rounded(cr, bx, by, bw, bh)
        if selected:
            cr.set_source_rgba(*theme.rgb(theme.SELECT))
            cr.set_line_width(1.6)
        else:
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.set_line_width(1)
        cr.stroke()

    def _draw_wave(self, cr, clip, media, xa, xb, y, h, color):
        peaks = self.app.media_cache.peaks(media)
        if peaks is None or len(peaks) == 0 or xb - xa < 2:
            return
        s = self.seq
        xs = np.arange(int(xa), int(xb) + 2)
        local = clip.in_point + (s.scroll_x + (xs - HEADER_W) / s.px_per_frame - clip.start)
        if clip.reverse:
            total = self.app.project.media_frames(media, s, clip.speed) or 1
            local = total - 1 - local
        idx = np.clip((local * abs(clip.speed) / s.fps * PEAKS_PER_SECOND).astype(np.int64), 0, len(peaks) - 1)
        lo, hi = np.minimum(idx[:-1], idx[1:]), np.maximum(idx[:-1], idx[1:])
        if int((hi - lo).max()) > 1 and not clip.reverse:
            # Several peaks per pixel: take the maximum of each pixel's span.
            ends = np.append(lo, min(int(hi[-1]) + 1, len(peaks) - 1))
            vals = np.maximum.reduceat(peaks, ends)[:-1]
        else:
            vals = peaks[lo]
        amp = np.maximum(0.6, vals.astype(np.float32) / 255.0 * (h / 2 - 1))
        mid = y + h / 2
        cr.set_source_rgba(*theme.shade(color, 1.75), 0.9)
        cr.set_line_width(1)
        for x, a in zip(xs[:-1].tolist(), amp.tolist()):
            cr.move_to(x + 0.5, mid - a)
            cr.line_to(x + 0.5, mid + a)
        cr.stroke()

    # rubber bands: opacity on video clips, level on audio clips
    def _band_info(self, track, clip):
        if track.kind == "video":
            e = clip.effect("opacity")
            return (e, EFFECTS["opacity"].param("opacity"), 0.0, 100.0) if e else None
        e = clip.effect("volume")
        return (e, EFFECTS["volume"].param("level"), -60.0, 15.0) if e else None

    def _band_y(self, y, th, norm):
        top, bottom = y + NAME_H + 5, y + th - 6
        return bottom - max(0.0, min(1.0, norm)) * (bottom - top)

    def _band_value(self, y, th, py, lo, hi):
        top, bottom = y + NAME_H + 5, y + th - 6
        return lo + max(0.0, min(1.0, (bottom - py) / max(1.0, bottom - top))) * (hi - lo)

    def _draw_band(self, cr, track, clip, y, th, xa, xb):
        info = self._band_info(track, clip)
        if info is None or xb - xa < 6:
            return
        effect, pdef, lo, hi = info
        kfs = effect.keyframes.get(pdef.name)
        norm = lambda v: (v - lo) / (hi - lo)
        cr.set_line_width(1.2)
        cr.set_source_rgba(1, 1, 1, 0.75 if kfs else 0.4)
        if not kfs:
            yy = self._band_y(y, th, norm(effect.params.get(pdef.name, pdef.default)))
            cr.move_to(xa, yy)
            cr.line_to(xb, yy)
            cr.stroke()
            return
        step = max(1, int(3 / self.seq.px_per_frame))
        f0, f1 = self.frame_at(xa), self.frame_at(xb)
        first = True
        for f in list(range(f0, f1 + 1, step)) + [f1]:
            px = self.x_of(f)
            yy = self._band_y(y, th, norm(kf.evaluate(kfs, clip.source_frame(f))))
            if first:
                cr.move_to(px, yy)
                first = False
            else:
                cr.line_to(px, yy)
        cr.stroke()
        for k in kfs:
            px = self.x_of(clip.start + (k[0] - clip.in_point))
            if xa - 5 <= px <= xb + 5:
                yy = self._band_y(y, th, norm(k[1]))
                cr.move_to(px, yy - 4)
                cr.line_to(px + 4, yy)
                cr.line_to(px, yy + 4)
                cr.line_to(px - 4, yy)
                cr.close_path()
                cr.set_source_rgba(*theme.rgb(theme.ACCENT))
                cr.fill_preserve()
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.stroke()

    def _draw_transition(self, cr, tr, span, y, th, selected):
        x0, x1 = self.x_of(span[0]), self.x_of(span[1])
        band = min(20, th - 6)
        _rounded(cr, x0 + 0.5, y + 2.5, max(2.0, x1 - x0 - 1), band, 2)
        cr.set_source_rgba(*(theme.rgb(theme.ACCENT, 0.95) if selected else (0.74, 0.79, 0.86, 0.94)))
        cr.fill_preserve()
        cr.set_source_rgba(*(theme.rgb(theme.SELECT) if selected else (0, 0, 0, 0.65)))
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgba(0, 0, 0, 0.3)
        cr.move_to(x0 + 2, y + 2 + band - 2)
        cr.line_to(x1 - 2, y + 4)
        cr.stroke()
        if x1 - x0 > 70:
            tdef = TRANSITIONS.get(tr.type)
            self._text(cr, tdef.label if tdef else tr.type, x0 + 5, y + 4, x1 - x0 - 10, "#0c0f13", size=10)

    def _draw_overlay(self, cr, w, h):
        s = self.seq
        for track, start, end in self.ghosts:
            row = self.row_of(track)
            if row:
                x0, x1 = max(HEADER_W, self.x_of(start)), self.x_of(end)
                cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.3))
                _rounded(cr, x0, row[0] + 2, max(2, x1 - x0), row[1] - 4)
                cr.fill_preserve()
                cr.set_source_rgba(*theme.rgb(theme.ACCENT))
                cr.set_line_width(1.2)
                cr.stroke()
        if self.app.tool == "razor" and self.hover_x is not None and self.hover_x > HEADER_W:
            cr.set_source_rgba(*theme.rgb(theme.DANGER, 0.9))
            cr.set_line_width(1)
            x = int(self.hover_x) + 0.5
            cr.move_to(x, RULER_H)
            cr.line_to(x, h)
            cr.stroke()
        if self.snap_line is not None:
            x = int(self.x_of(self.snap_line)) + 0.5
            if x >= HEADER_W:
                cr.set_source_rgba(*theme.rgb(theme.SELECT, 0.9))
                cr.set_line_width(1)
                cr.set_dash([3, 3])
                cr.move_to(x, RULER_H)
                cr.line_to(x, h)
                cr.stroke()
                cr.set_dash([])
        if self.marquee:
            x0, y0, x1, y1 = self.marquee
            cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.14))
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.fill_preserve()
            cr.set_source_rgba(*theme.rgb(theme.ACCENT, 0.8))
            cr.set_line_width(1)
            cr.stroke()
        x = int(self.x_of(s.playhead)) + 0.5
        if x >= HEADER_W - 1:
            cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
            cr.set_line_width(1.3)
            cr.move_to(x, 14)
            cr.line_to(x, h)
            cr.stroke()
            cr.move_to(x - 6, 12)
            cr.line_to(x + 6, 12)
            cr.line_to(x + 6, 19)
            cr.line_to(x, 26)
            cr.line_to(x - 6, 19)
            cr.close_path()
            cr.fill()
        if self.tip:
            px, py = self._ptr
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription("Monospace 11px"))
            layout.set_text(self.tip, -1)
            tw, thh = layout.get_pixel_size()
            bx, by = min(w - tw - 12, px + 14), max(RULER_H + 2, py - 26)
            _rounded(cr, bx, by, tw + 10, thh + 6, 3)
            cr.set_source_rgba(*theme.rgb(theme.BG0, 0.94))
            cr.fill()
            cr.set_source_rgba(*theme.rgb(theme.TEXT))
            cr.move_to(bx + 5, by + 3)
            PangoCairo.show_layout(cr, layout)

    # -- pointer ---------------------------------------------------------
    def _mods(self, gesture):
        st = gesture.get_current_event_state()
        return (bool(st & Gdk.ModifierType.SHIFT_MASK), bool(st & Gdk.ModifierType.CONTROL_MASK),
                bool(st & Gdk.ModifierType.ALT_MASK))

    def _hover(self, ctrl, x, y):
        self._ptr = (x, y)
        if self.seq is None or self.drag:
            return
        tool = self.app.tool
        hit = self.hit(x, y)
        self.hover_hit = hit
        cursor = CURSORS.get(tool, "default")
        if hit["zone"] == "clip" and hit.get("edge") and tool in ("select", "ripple", "roll", "rate"):
            cursor = "col-resize" if tool == "roll" else ("w-resize" if hit["edge"] == "start" else "e-resize")
        elif hit["zone"] in ("ruler", "header", "corner"):
            cursor = "default"
        self.set_cursor(Gdk.Cursor.new_from_name(cursor))
        if tool == "razor":
            pts = self._snap_points()
            f = self._snap(self.frame_at(x), pts)
            self.snap_line = None
            self.hover_x = self.x_of(f) if x > HEADER_W and y > RULER_H else None
            self.queue_draw()

    def _leave(self, ctrl):
        if self.hover_x is not None:
            self.hover_x = None
            self.queue_draw()

    def _scroll(self, ctrl, dx, dy):
        s = self.seq
        if s is None:
            return False
        st = ctrl.get_current_event_state()
        if st & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            self.zoom_by(1.18 ** (-dy), self._ptr[0] if self._ptr[0] > HEADER_W else None)
        elif st & Gdk.ModifierType.SHIFT_MASK:
            self.scroll_y += dy * 40
            self.invalidate()
        else:
            s.scroll_x = max(0.0, s.scroll_x + (dx + dy) * 70 / s.px_per_frame)
            self.view_changed()
        return True

    def _scrub(self, x, snap):
        f = self.frame_at(x)
        if snap:
            f = self._snap(f, self._snap_points() - {self.seq.playhead}, force=True)
            self.snap_line = None
        self.app.set_playhead(f)

    def _header_click(self, hit):
        track, button = hit["track"], hit["button"]
        if button is None:
            return
        if button == "target":
            track.target = not track.target
        elif button == "lock":
            track.locked = not track.locked
        elif button in ("eye", "mute"):
            track.hidden = not track.hidden
        elif button == "solo":
            track.solo = not track.solo
        self.app.commit("Track " + button.capitalize(), rebuild=button in ("eye", "mute", "solo"))

    def _press(self, gesture, x, y):
        app, s = self.app, self.seq
        self.drag = None
        self._ptr = (x, y)
        app.focus = "timeline"
        if app.window:
            app.window.show_panel("timeline")
        self.grab_focus()
        if s is None:
            return
        shift, ctrl, alt = self._mods(gesture)
        hit = self.hit(x, y)
        zone, tool = hit["zone"], app.tool
        if zone == "ruler":
            m = hit.get("marker")
            app.selected_marker = m.id if m else None
            self.drag = {"kind": "scrub", "x": x, "y": y}
            self._scrub(x, shift)
            self.invalidate()
            return
        if zone == "header":
            return self._header_click(hit)
        if zone == "corner":
            return
        if tool == "hand":
            self.drag = {"kind": "hand", "x": x, "y": y, "sx": s.scroll_x, "sy": self.scroll_y}
            return
        if tool == "zoom":
            self.drag = {"kind": "zoom", "x": x, "y": y, "alt": alt}
            return
        if tool == "razor":
            if zone == "clip":
                t = self._snap(self.frame_at(x), self._snap_points())
                self.snap_line = None
                cid = hit["clip"].id
                app.edit("Razor", lambda ed: ed.split(t, None if shift else [cid], all_tracks=shift))
            return
        if zone == "transition":
            app.select_transition(hit["transition"].id)
            if app.window:
                app.window.show_panel("effect_controls")
            return
        if tool == "track_fwd" and zone in ("clip", "track"):
            f = self.frame_at(x)
            ids = app.editor().select_forward(f, hit["track"] if shift else None)
            app.select(ids, expand=False)
            if not ids:
                return
            first = min((c for c in (s.find_clip(i)[1] for i in ids)), key=lambda c: c.start)
            hit = dict(hit, zone="clip", clip=first, edge=None)
            zone = "clip"
        if zone == "clip":
            clip, edge, track = hit["clip"], hit.get("edge"), hit["track"]
            if tool == "pen" or (tool == "select" and ctrl and not edge and hit.get("h", 0) >= 46):
                return self._band_press(hit, x, y)
            kind = None
            if tool in ("select", "track_fwd"):
                kind = ("ripple" if ctrl else "trim") if (edge and tool == "select") else "move"
            elif tool in ("ripple", "roll", "rate"):
                kind = tool if edge else None
            elif tool in ("slip", "slide"):
                kind = tool
            if hit.get("outside") and kind == "move":
                kind = None
            was_selected = clip.id in app.selection
            if kind == "move" and shift and tool == "select":
                app.select([clip.id], toggle=True, expand=not alt)
                return
            if not was_selected:
                app.select([clip.id], expand=not alt)
            if kind is None or track.locked:
                return
            self.drag = {"kind": kind, "clip": clip.id, "edge": edge, "x": x, "y": y, "f0": self.frame_at(x),
                         "base": s.snapshot_tracks(), "markers": [copy.copy(m) for m in s.markers],
                         "moved": False, "applied": False, "track_kind": track.kind,
                         "track_index": s.tracks_of(track.kind).index(track), "alt": alt,
                         "was_selected": was_selected, "edge_t": clip.start if edge == "start" else clip.end,
                         "start": clip.start, "ids": list(app.selection)}
            return
        # empty space
        if zone == "track" and tool == "select" and not shift:
            if app.editor().gap_at(hit["track"], hit["frame"]):
                app.select_gap(hit["track"], hit["frame"])
            else:
                app.clear_selection()
        elif not shift:
            app.clear_selection()
        if tool == "select":
            self.drag = {"kind": "marquee", "x": x, "y": y, "base": list(app.selection) if shift else []}

    def _track_delta(self, d, y):
        s = self.seq
        lst = s.tracks_of(d["track_kind"])
        for track, ty, th in self.rows()[0]:
            if ty <= y < ty + th and track.kind == d["track_kind"]:
                return lst.index(track) - d["track_index"]
        return d.get("dtrack", 0)

    def _preview(self, d, dx, y, final=False, insert=False):
        """Re-apply the drag to a fresh copy of the tracks. Returns True if
        the edit changed something."""
        app, s = self.app, self.seq
        s.restore_tracks(d["base"])
        s.markers = [copy.copy(m) for m in d["markers"]]
        ed = app.editor()
        df = int(round(dx / s.px_per_frame))
        kind, cid = d["kind"], d["clip"]
        ids = d["ids"]
        pts = self._snap_points(exclude=set(ids))
        changed = False
        try:
            if kind == "move":
                edges = []
                for i in ids:
                    c = s.find_clip(i)[1]
                    if c is not None:
                        edges += [c.start, c.end]
                df = self._snap_delta(edges, df, pts)
                d["dtrack"] = self._track_delta(d, y)
                new = ed.move(ids, df, d["dtrack"], kind=d["track_kind"], insert=insert, duplicate=d["alt"])
                changed = bool(new) and (df != 0 or d["dtrack"] != 0 or d["alt"])
                d["new_ids"] = new
                self.tip = ("%+d  " % df) + tc.frames_to_tc(abs(df), s.fps_num, s.fps_den)
            elif kind in ("trim", "ripple"):
                t = self._snap(d["edge_t"] + df, pts)
                delta = ed.trim(cid, d["edge"], t, ripple=kind == "ripple")
                changed = delta != 0
                c = s.find_clip(cid)[1]
                self.tip = ("%+d  dur " % delta) + tc.frames_to_tc(c.duration, s.fps_num, s.fps_den)
            elif kind == "roll":
                delta = ed.roll(cid, d["edge"], self._snap(d["edge_t"] + df, pts))
                changed = delta != 0
                self.tip = "%+d" % delta
            elif kind == "rate":
                speed = ed.rate_stretch(cid, d["edge"], self._snap(d["edge_t"] + df, pts))
                changed = speed is not None
                self.tip = f"{(speed or 1) * 100:.1f}%"
            elif kind == "slip":
                delta = ed.slip([cid], -df)
                changed = delta != 0
                self.tip = "slip %+d" % delta
            elif kind == "slide":
                delta = ed.slide(cid, self._snap(d["start"] + df, pts) - d["start"])
                changed = delta != 0
                self.tip = "slide %+d" % delta
        except EditError as exc:
            s.restore_tracks(d["base"])
            self.tip = str(exc)
            changed = False
        d["applied"] = changed
        return changed

    def _motion_drag(self, gesture, dx, dy):
        d = self.drag
        if d is None or self.seq is None:
            return
        s = self.seq
        x, y = d["x"] + dx, d["y"] + dy
        self._ptr = (x, y)
        kind = d["kind"]
        if kind == "scrub":
            return self._scrub(x, self._mods(gesture)[0])
        if kind == "hand":
            s.scroll_x = max(0.0, d["sx"] - dx / s.px_per_frame)
            self.scroll_y = d["sy"] - dy
            return self.view_changed()
        if kind == "zoom":
            self.marquee = (d["x"], RULER_H, x, self.get_height())
            return self.queue_draw()
        if kind == "marquee":
            self.marquee = (d["x"], d["y"], x, y)
            xa, xb = sorted((d["x"], x))
            ya, yb = sorted((d["y"], y))
            ids = list(d["base"])
            for track, ty, th in self.rows()[0]:
                if ty + th < ya or ty > yb or track.locked:
                    continue
                for c in track.clips:
                    if self.x_of(c.end) >= xa and self.x_of(c.start) <= xb and c.id not in ids:
                        ids.append(c.id)
            if ids != self.app.selection:
                self.app.select(ids, expand=False)
            return self.queue_draw()
        if kind == "kf":
            return self._band_drag(d, x, y, False)
        if not d["moved"] and abs(dx) < 4 and abs(dy) < 4:
            return
        d["moved"] = True
        self._preview(d, dx, y)
        self.invalidate()

    def _release(self, gesture, dx, dy):
        d, self.drag = self.drag, None
        self.snap_line, self.tip = None, None
        marquee, self.marquee = self.marquee, None
        if d is None or self.seq is None:
            return
        app, s = self.app, self.seq
        shift, ctrl, alt = self._mods(gesture)
        kind = d["kind"]
        x, y = d["x"] + dx, d["y"] + dy
        if kind in ("scrub", "hand", "marquee"):
            return self.queue_draw()
        if kind == "zoom":
            if marquee and abs(dx) > 8:
                fa, fb = sorted((self.frame_at(d["x"]), self.frame_at(x)))
                s.px_per_frame = max(self.min_ppf(), min(MAX_PPF, (self.get_width() - HEADER_W) / max(1, fb - fa)))
                s.scroll_x = float(fa)
                self.view_changed()
            else:
                self.zoom_by(1 / 1.6 if (d["alt"] or alt) else 1.6, d["x"])
            return
        if kind == "kf":
            return self._band_drag(d, x, y, True)
        if not d["moved"]:
            if (kind == "move" and app.tool == "select" and d["was_selected"] and len(app.selection) > 1
                    and not shift):
                app.select([d["clip"]], expand=not alt)
            return
        changed = self._preview(d, dx, y, final=True, insert=ctrl and kind == "move")
        if not changed:
            s.restore_tracks(d["base"])
            s.markers = d["markers"]
            return self.invalidate()
        label = DRAG_LABELS[kind]
        if kind == "move":
            label = "Duplicate" if d["alt"] else ("Insert Move" if ctrl else "Move")
            if d.get("new_ids"):
                app.selection = list(d["new_ids"])
        app.commit(label)
        app.emit("selection")

    def cancel_drag(self):
        d, self.drag = self.drag, None
        self.snap_line, self.tip, self.marquee = None, None, None
        if d and "base" in d and self.seq is not None:
            self.seq.restore_tracks(d["base"])
            self.seq.markers = d["markers"]
        self.invalidate()
        return d is not None

    def _clicked(self, gesture, n_press, x, y):
        app, s = self.app, self.seq
        if s is None:
            return
        button = gesture.get_current_button()
        hit = self.hit(x, y)
        if button == 3:
            return self._context_menu(hit, x, y)
        if button != 1 or n_press != 2:
            return
        self.drag = None
        zone = hit["zone"]
        if zone == "clip" and app.tool in ("select", "track_fwd"):
            clip = hit["clip"]
            media = app.project.media.get(clip.media_id)
            if media is None:
                return
            if media.kind == "sequence":
                app.set_active_sequence(media.sequence_id)
            elif media.kind == "title":
                app.window.show_panel("effect_controls")
            else:
                total = app.project.media_frames(media, s, clip.speed)
                local = clip.in_point
                if clip.reverse and total:
                    local = total - 1 - local
                app.open_source(media, int(local * abs(clip.speed)) if total else None)
        elif zone == "ruler" and hit.get("marker"):
            app.window.edit_marker(hit["marker"])
        elif zone == "header" and hit["button"] is None:
            track = hit["track"]
            from .widgets import ask_text

            def rename(text):
                track.name = text
                app.commit("Rename Track", rebuild=False)

            ask_text(app.window, "Rename Track", "Track name", track.name or s.track_label(track), rename)
        elif zone == "transition":
            app.window.show_panel("effect_controls")

    def _context_menu(self, hit, x, y):
        app = self.app
        zone = hit["zone"]
        menu = Gio.Menu()

        def section(*items):
            sec = Gio.Menu()
            for label, action in items:
                sec.append(label, action)
            menu.append_section(None, sec)

        if zone == "clip":
            clip = hit["clip"]
            if clip.id not in app.selection:
                app.select([clip.id])
            section(("Cut", "app.cut"), ("Copy", "app.copy"), ("Clear", "app.delete"),
                    ("Ripple Delete", "app.ripple_delete"))
            section(("Enable / Disable", "app.toggle_enable"), ("Link / Unlink", "app.link"),
                    ("Rename…", "app.rename"), ("Speed/Duration…", "app.speed"),
                    ("Remove Effects", "app.remove_effects"))
            labels = Gio.Menu()
            for i, (name, _) in enumerate(LABELS):
                labels.append(name, f"app.set_label({i})")
            sec = Gio.Menu()
            sec.append_submenu("Label", labels)
            menu.append_section(None, sec)
            section(("Apply Default Transitions", "app.default_transitions"),
                    ("Reveal in Project", "app.reveal"), ("Match Frame", "app.match_frame"))
        elif zone == "transition":
            app.select_transition(hit["transition"].id)
            section(("Remove Transition", "app.delete"))
        elif zone == "track":
            if app.editor().gap_at(hit["track"], hit["frame"]):
                app.select_gap(hit["track"], hit["frame"])
                section(("Ripple Delete Gap", "app.ripple_delete"))
            section(("Paste", "app.paste"), ("Paste Insert", "app.paste_insert"))
            section(("Add Edit", "app.add_edit"), ("Add Marker", "app.add_marker"))
        elif zone == "header":
            tid = hit["track"].id
            section(("Add Video Track", "app.add_video_track"), ("Add Audio Track", "app.add_audio_track"),
                    ("Delete This Track", f"app.delete_track('{tid}')"),
                    ("Delete Empty Tracks", "app.delete_empty_tracks"))
        elif zone == "ruler":
            if hit.get("marker"):
                app.selected_marker = hit["marker"].id
                section(("Edit Marker…", "app.add_marker"), ("Clear Marker", "app.clear_marker"))
            section(("Add Marker", "app.add_marker"), ("Clear All Markers", "app.clear_all_markers"))
            section(("Mark In", "app.mark_in"), ("Mark Out", "app.mark_out"),
                    ("Clear In and Out", "app.clear_in_out"))
        else:
            section(("Paste", "app.paste"), ("Add Video Track", "app.add_video_track"),
                    ("Add Audio Track", "app.add_audio_track"))
        popup_menu(self, menu, x, y)

    # -- keyframe rubber band --------------------------------------------
    def _band_press(self, hit, x, y):
        app, s = self.app, self.seq
        clip, track = hit["clip"], hit["track"]
        info = self._band_info(track, clip)
        if info is None or track.locked:
            return
        effect, pdef, lo, hi = info
        if clip.id not in app.selection:
            app.select([clip.id], expand=False)
        ty, th = hit["y"], hit["h"]
        kfs = [list(k) for k in effect.keyframes.get(pdef.name) or []]
        grabbed = None
        for i, k in enumerate(kfs):
            kx = self.x_of(clip.start + (k[0] - clip.in_point))
            ky = self._band_y(ty, th, (k[1] - lo) / (hi - lo))
            if abs(kx - x) <= 6 and abs(ky - y) <= 6:
                grabbed = i
                break
        if grabbed is None:
            t = clip.source_frame(max(clip.start, min(clip.end - 1, self.frame_at(x))))
            if not kfs:
                value = effect.params.get(pdef.name, pdef.default)
            else:
                value = self._band_value(ty, th, y, lo, hi)
            kfs = kf.set_key(kfs, t, round(float(value), 2))
            grabbed = kf.find(kfs, t)
            effect.keyframes = dict(effect.keyframes, **{pdef.name: kfs})
            app.effect_changed(effect, False)
        self.drag = {"kind": "kf", "x": x, "y": y, "clip": clip, "effect": effect, "pdef": pdef, "lo": lo,
                     "hi": hi, "index": grabbed, "ty": ty, "th": th, "orig": [list(k) for k in kfs]}

    def _band_drag(self, d, x, y, final):
        clip, effect, pdef = d["clip"], d["effect"], d["pdef"]
        kfs = [list(k) for k in d["orig"]]
        i = d["index"]
        t = clip.source_frame(max(clip.start, min(clip.end - 1, self.frame_at(x))))
        lo_t = kfs[i - 1][0] + 1 if i > 0 else None
        hi_t = kfs[i + 1][0] - 1 if i + 1 < len(kfs) else None
        if lo_t is not None:
            t = max(lo_t, t)
        if hi_t is not None:
            t = min(hi_t, t)
        value = round(float(self._band_value(d["ty"], d["th"], y, d["lo"], d["hi"])), 1)
        kfs[i][0], kfs[i][1] = t, value
        effect.keyframes = dict(effect.keyframes, **{pdef.name: kfs})
        self.tip = f"{value:.1f} {pdef.unit}"
        self.app.effect_changed(effect, final, f"Keyframe {pdef.label}")
        if final:
            self.tip = None
            self.app.emit("selection")
        self.invalidate()

    # -- drag and drop ---------------------------------------------------
    def _drop_tracks(self, y):
        """(video track, audio track) for a drop at height y."""
        s = self.seq
        row = next((t for t, ty, th in self.rows()[0] if ty <= y < ty + th), None)
        if row is None:
            return None, None
        idx = s.tracks_of(row.kind).index(row)
        other = s.audio_tracks if row.kind == "video" else s.video_tracks
        mate = other[min(idx, len(other) - 1)] if other else None
        return (row, mate) if row.kind == "video" else (mate, row)

    def _dnd_motion(self, target, x, y):
        self._ptr = (x, y)
        payload = self.app.drag_payload
        s = self.seq
        self.ghosts = []
        if s is not None and payload and payload[0] in ("media", "source"):
            vt, at = self._drop_tracks(y)
            ed = self.app.editor()
            t = self._snap(self.frame_at(x), self._snap_points())
            for mid in payload[1]:
                m = self.app.project.media.get(mid)
                if m is None or m.is_bin or (m.kind == "sequence" and m.sequence_id == s.id):
                    continue
                a, b = self.app._marks_in_seq(m, s)
                v, au = ed.clips_from_media(m, a, b)
                dur = (v or au).duration
                if v is not None and vt is not None:
                    self.ghosts.append((vt, t, t + dur))
                if au is not None and at is not None:
                    self.ghosts.append((at, t, t + dur))
                t += dur
        self.queue_draw()
        return Gdk.DragAction.COPY

    def _dnd_leave(self, target):
        self.ghosts, self.snap_line = [], None
        self.queue_draw()

    def _dnd_drop(self, target, value, x, y):
        app = self.app
        self.ghosts = []
        ctrl = bool(target.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)
        s = self.seq
        t = self._snap(self.frame_at(x), self._snap_points()) if s is not None else 0
        self.snap_line = None
        vt, at = self._drop_tracks(y) if s is not None else (None, None)
        if isinstance(value, Gdk.FileList):
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            app.import_paths(paths, then=lambda items: app.drop_media(items, t, vt, at, insert=ctrl))
            return True
        text = str(value)
        if not text.startswith("tideline:"):
            return False
        _, kind, arg = text.split(":", 2)
        if kind in ("media", "source"):
            items = [app.project.media[i] for i in arg.split(",") if i in app.project.media]
            app.drop_media(items, t, vt, at, insert=ctrl)
            app.focus = "timeline"
        elif s is not None:
            hit = self.hit(x, y)
            if hit["zone"] != "clip":
                app.status("Drop the effect onto a clip.", warn=True)
                return False
            clip = hit["clip"]
            if kind == "effect":
                app.select([clip.id], expand=False)
                app.apply_effect(arg, [clip.id])
            elif kind == "preset":
                app.select([clip.id])
                app.apply_preset(arg)
            elif kind == "transition":
                edge = "start" if abs(x - self.x_of(clip.start)) < abs(x - self.x_of(clip.end)) else "end"
                app.apply_transition(arg, clip.id, edge)
        self.invalidate()
        return True


class TimelinePanel(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        bar = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
        self.tc = TimecodeField(app.fps)
        self.tc.connect("seek", lambda w, f: app.set_playhead(f))
        self.tc.set_tooltip_text("Playhead position — click to type, drag to scrub")
        bar.append(self.tc)
        self.snap_btn = icon_button("magnet", "Snap in Timeline (S)", lambda b: self._toggle("snap", b), toggle=True)
        self.link_btn = icon_button("link", "Linked Selection", lambda b: self._toggle("linked", b), toggle=True)
        bar.append(self.snap_btn)
        bar.append(self.link_btn)
        bar.append(icon_button("marker", "Add Marker (M)", lambda b: app.do("add_marker")))
        self.thumb_btn = icon_button("film", "Show video thumbnails", self._toggle_view, toggle=True)
        self.wave_btn = icon_button("wave", "Show audio waveforms", self._toggle_view, toggle=True)
        bar.append(self.thumb_btn)
        bar.append(self.wave_btn)
        bar.append(spacer())
        self.seq_names = Gtk.StringList()
        self.seq_drop = Gtk.DropDown(model=self.seq_names)
        self.seq_drop.add_css_class("flat")
        self.seq_drop.set_focusable(False)
        self.seq_drop.set_tooltip_text("Open sequences")
        self.seq_drop.connect("notify::selected", self._pick_sequence)
        self._seq_ids = []
        bar.append(self.seq_drop)
        self.append(bar)

        self.view = TimelineView(app, self)
        self.append(self.view)

        bottom = Gtk.Box(spacing=4, margin_start=HEADER_W, margin_end=4)
        self.adj = Gtk.Adjustment()
        self._adj_handler = self.adj.connect("value-changed", self._scrolled)
        self.scrollbar = Gtk.Scrollbar(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj, hexpand=True)
        bottom.append(self.scrollbar)
        bottom.append(icon_button("minus", "Zoom out (-)", lambda b: self.view.zoom_by(1 / 1.4), size=13))
        self.zoom = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.zoom.set_size_request(110, -1)
        self.zoom.set_draw_value(False)
        self.zoom.set_focusable(False)
        self._zoom_handler = self.zoom.connect("value-changed", self._zoomed)
        bottom.append(self.zoom)
        bottom.append(icon_button("plus", "Zoom in (=)", lambda b: self.view.zoom_by(1.4), size=13))
        self.append(bottom)

        self.thumb_btn.set_active(True)
        self.wave_btn.set_active(True)
        for ev in ("sequence", "project", "media"):
            app.on(ev, self.refresh)
        app.on("playhead", lambda: self.tc.set_frame(app.seq.playhead if app.seq else 0))
        app.on("options", self._options)
        self.view.connect("resize", lambda *_: GLib.idle_add(self.sync_scrollbar))
        self._options()
        self.refresh()

    def _toggle(self, what, btn):
        if getattr(self.app, what) != btn.get_active():
            setattr(self.app, what, btn.get_active())
            self.app.emit("options")

    def _toggle_view(self, btn):
        self.view.show_thumbs = self.thumb_btn.get_active()
        self.view.show_waves = self.wave_btn.get_active()
        self.view.invalidate()

    def _options(self):
        self.snap_btn.set_active(self.app.snap)
        self.link_btn.set_active(self.app.linked)

    def refresh(self, *_):
        app = self.app
        seqs = list(app.project.sequences.values())
        ids = [s.id for s in seqs]
        names = [s.name for s in seqs]
        current = [self.seq_names.get_string(i) for i in range(self.seq_names.get_n_items())]
        self.seq_drop.handler_block_by_func(self._pick_sequence)
        if ids != self._seq_ids or names != current:
            self._seq_ids = ids
            self.seq_names.splice(0, self.seq_names.get_n_items(), names)
        if app.project.active_sequence_id in ids:
            self.seq_drop.set_selected(ids.index(app.project.active_sequence_id))
        self.seq_drop.handler_unblock_by_func(self._pick_sequence)
        self.seq_drop.set_visible(bool(ids))
        self.tc.set_frame(app.seq.playhead if app.seq else 0)
        self.sync_scrollbar()

    def _pick_sequence(self, drop, _p):
        i = drop.get_selected()
        if 0 <= i < len(self._seq_ids):
            self.app.set_active_sequence(self._seq_ids[i])

    def sync_scrollbar(self):
        s = self.app.seq
        if s is None or self.view.get_width() <= HEADER_W:
            return False
        vis = self.view.visible_frames()
        upper = max(s.end(), s.playhead, s.scroll_x + vis * 0.5) + vis
        self.adj.handler_block(self._adj_handler)
        self.adj.configure(min(s.scroll_x, upper - vis), 0, upper, vis / 20, vis * 0.9, vis)
        self.adj.handler_unblock(self._adj_handler)
        lo, hi = np.log(self.view.min_ppf()), np.log(MAX_PPF)
        self.zoom.handler_block(self._zoom_handler)
        self.zoom.set_value(100 * (np.log(s.px_per_frame) - lo) / max(1e-9, hi - lo))
        self.zoom.handler_unblock(self._zoom_handler)
        return False

    def _scrolled(self, adj):
        s = self.app.seq
        if s is not None:
            s.scroll_x = adj.get_value()
            self.view.invalidate()

    def _zoomed(self, scale):
        s = self.app.seq
        if s is None:
            return
        lo, hi = np.log(self.view.min_ppf()), np.log(MAX_PPF)
        target = float(np.exp(lo + (hi - lo) * scale.get_value() / 100))
        self.view.zoom_by(target / s.px_per_frame)
