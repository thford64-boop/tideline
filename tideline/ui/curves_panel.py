"""Keyframe Curves: a large graph editor for the selected clip's animated
parameters. The Value graph is the same editable curve Effect Controls shows
(drag keyframes and their Bezier handles, right-click for curves and graph
presets), with room to work; the Speed graph shows how fast the value
changes, so eases and overshoots can be judged at a glance."""

from gi.repository import Gtk

from .. import keyframes as kf
from ..engine.effects import EFFECTS
from . import theme
from .effect_controls import KeyframeActions
from .graph import PAD_T, KeyframeGraph, _diamond, _num, _text
from .widgets import label, spacer


class _Row:
    """What a KeyframeGraph needs to know about the parameter it plots."""

    def __init__(self, panel, clip, effect, pdef):
        self.panel, self.app = panel, panel.app
        self.clip, self.effect, self.pdef = clip, effect, pdef
        self.graph = None


class CurveGraph(KeyframeGraph):
    """KeyframeGraph sized to fill the panel, with a Speed view."""

    def __init__(self, row):
        super().__init__(row)
        self.speed_view = False
        self.set_vexpand(True)
        self.set_content_height(160)
        self.set_margin_start(4)
        self.set_margin_top(4)

    def _speeds(self):
        lo, hi = self.time_range()
        fps = self.app.seq.fps if self.app.seq else 30.0
        n = max(2, int(self._plot()[2]))
        out = []
        for i in range(n + 1):
            t = lo + (hi - lo) * i / n
            out.append((t, kf.speed(self.keys(), t, fps) or 0.0))
        return out

    def value_range(self):
        if not self.speed_view:
            return super().value_range()
        top = max((v for _, v in self._speeds()), default=0.0)
        return -top * 0.08 - 1e-6, top * 1.15 + 1.0

    def press(self, x, y):
        if not self.speed_view:
            return super().press(x, y)
        # The speed graph is for looking: pick a keyframe by its time, or scrub.
        self.drag = {"kind": "scrub", "what": None, "x": x, "y": y, "range": self.value_range(),
                     "moved": False, "orig": []}
        near = [i for i, k in enumerate(self.keys()) if abs(self.x_of(k[0]) - x) <= 7]
        if near:
            self.selected = near[0]
        else:
            self._scrub(x)
        self.queue_draw()

    def add_key_at(self, x, y):
        if not self.speed_view:
            super().add_key_at(x, y)

    def _draw(self, area, cr, w, h):
        if not self.speed_view:
            return super()._draw(area, cr, w, h)
        px, py, pw, ph = self._plot()
        kfs = self.keys()
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        lo_v, hi_v = self.value_range()
        cr.set_line_width(1)
        for v in (lo_v + (hi_v - lo_v) * i / 4 for i in range(5)):
            y = round(self.y_of(v)) + 0.5
            cr.set_source_rgba(*theme.rgb(theme.BG3, 0.8))
            cr.move_to(px, y)
            cr.line_to(px + pw, y)
            cr.stroke()
            _text(cr, _num(max(0.0, v)), px - 5, y + 3, theme.TEXT_FAINT, right=True)
        speeds = self._speeds()
        cr.set_source_rgba(*theme.rgb(theme.WARN))
        cr.set_line_width(1.6)
        for j, (t, v) in enumerate(speeds):
            (cr.line_to if j else cr.move_to)(self.x_of(t), self.y_of(v))
        cr.stroke()
        fps = self.app.seq.fps if self.app.seq else 30.0
        for i, k in enumerate(kfs):
            x = self.x_of(k[0])
            y = self.y_of(kf.speed(kfs, k[0], fps) or 0.0)
            _diamond(cr, x, y, 5)
            cr.set_source_rgba(*theme.rgb(theme.SELECT if i == self.selected else theme.WARN))
            cr.fill()
        x = round(self.x_of(self.app.clip_time(self.clip))) + 0.5
        cr.set_source_rgba(*theme.rgb(theme.PLAYHEAD))
        cr.move_to(x, py - 4)
        cr.line_to(x, py + ph + 4)
        cr.stroke()
        if kfs:
            now = kf.speed(kfs, self.app.clip_time(self.clip), fps) or 0.0
            unit = self.pdef.unit or "units"
            _text(cr, f"speed {_num(now)} {unit}/s", px + pw - 2, PAD_T - 3, theme.TEXT_DIM, right=True)


class CurvesPanel(KeyframeActions, Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.params = []                  # [(effect, pdef)] animated on the clip
        self.clip = None
        self.graph = None
        self.chosen = None                # (effect id, parameter name) last picked
        self._busy = False
        self.header = label("", css="dim", ellipsize=True)
        self.header.set_margin_start(8)
        self.header.set_margin_top(5)
        self.append(self.header)
        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=6, margin_top=2, margin_bottom=2)
        self.pick = Gtk.DropDown.new_from_strings([])
        self.pick.set_tooltip_text("Animated parameter to edit")
        self.pick.connect("notify::selected", self._picked)
        self.value_btn = Gtk.ToggleButton(label="Value", active=True)
        self.speed_btn = Gtk.ToggleButton(label="Speed", group=self.value_btn)
        self.value_btn.set_tooltip_text("Value graph: drag keyframes and their curve handles")
        self.speed_btn.set_tooltip_text("Speed graph: how fast the value changes (units per second)")
        self.speed_btn.connect("toggled", self._view_changed)
        for b in (self.value_btn, self.speed_btn):
            b.add_css_class("flat")
            b.set_focusable(False)
        bar.append(self.pick)
        bar.append(spacer())
        self.ease_buttons = []
        for key, text in kf.EASES:
            btn = Gtk.Button(label=text)
            btn.add_css_class("flat")
            btn.set_focusable(False)
            btn.set_tooltip_text(f"{text} the selected keyframe (or the one under the playhead)")
            btn.connect("clicked", lambda b, k=key: self.ease(k))
            self.ease_buttons.append(btn)
            bar.append(btn)
        bar.append(self.value_btn)
        bar.append(self.speed_btn)
        self.append(bar)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.append(self.body)
        self.empty = label("Select a clip with keyframes: its animated parameters show here.",
                           css="faint", xalign=0.5, vexpand=True)
        self.install_keyframe_actions()
        for ev in ("selection", "sequence", "project", "effects"):
            app.on(ev, self.sync)
        app.on("playhead", self._redraw)
        self.sync()

    # -- contents --------------------------------------------------------
    def sync(self, *_):
        app = self.app
        pairs = [c for t, c in app.selected_clips()]
        clip = pairs[0] if len(pairs) == 1 else None
        params = []
        if clip is not None:
            for effect in clip.effects:
                edef = EFFECTS.get(effect.type)
                if edef is None:
                    continue
                for pdef in edef.params:
                    if effect.keyframes.get(pdef.name):
                        params.append((effect, pdef))
        names = [f"{EFFECTS[e.type].label} ▸ {p.label}" for e, p in params]
        same = clip is self.clip and [(e.id, p.name) for e, p in params] == \
            [(e.id, p.name) for e, p in self.params]
        self.clip, self.params = clip, params
        media = app.project.media.get(clip.media_id) if clip is not None else None
        self.header.set_text("No clip selected" if clip is None else
                             f"{app.seq.name}  ›  {clip.name or (media.name if media else '')}")
        if same and self.graph is not None:
            return self._redraw()
        self._busy = True
        self.pick.set_model(Gtk.StringList.new(names))
        index = next((i for i, (e, p) in enumerate(params) if (e.id, p.name) == self.chosen), 0)
        if params:
            self.pick.set_selected(index)
        self._busy = False
        self.pick.set_sensitive(bool(params))
        for b in self.ease_buttons:
            b.set_sensitive(bool(params))
        self._show(index if params else None)

    def _show(self, index):
        child = self.body.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.body.remove(child)
            child = nxt
        self.graph = None
        if index is None or index >= len(self.params):
            self.body.append(self.empty)
            return
        effect, pdef = self.params[index]
        row = _Row(self, self.clip, effect, pdef)
        self.graph = row.graph = CurveGraph(row)
        self.graph.speed_view = self.speed_btn.get_active() and self.graph.kind == "number"
        self.body.append(self.graph)

    def _picked(self, *_):
        if self._busy:
            return
        i = self.pick.get_selected()
        if 0 <= i < len(self.params):
            e, p = self.params[i]
            self.chosen = (e.id, p.name)
            self._show(i)

    def _view_changed(self, *_):
        if self.graph is not None:
            self.graph.speed_view = self.speed_btn.get_active() and self.graph.kind == "number"
            self.graph.queue_draw()

    def _redraw(self, *_):
        if self.graph is not None:
            self.graph.queue_draw()

    # -- easing ------------------------------------------------------------
    def ease(self, how):
        """Ease the selected keyframe, else the one under the playhead."""
        g = self.graph
        if g is None:
            return
        kfs = g.keys()
        i = g.selected
        if i is None or i >= len(kfs):
            i = kf.find(kfs, self.app.clip_time(g.clip))
        if i < 0:
            return self.app.status("Select a keyframe in the graph, or put the playhead on one.", warn=True)
        if g.pdef.hold or g.kind not in ("number", "color"):
            return self.app.status("This parameter jumps from one keyframe to the next: it has no curve.",
                                   warn=True)
        g.selected = i
        label_ = dict(kf.EASES)[how]
        self.app.set_keyframes(g.effect, g.pdef, kf.ease_key(kfs, i, how), label=f"Keyframe {label_}")
