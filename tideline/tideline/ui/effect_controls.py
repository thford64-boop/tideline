"""Effect Controls: parameter editors and keyframing for the selected clip
or transition."""

import os

from gi.repository import Gio, GLib, Gtk, Pango

from .. import keyframes as kf
from .. import timecode as tc
from ..engine.effects import EFFECTS, TRANSITIONS
from .curve_editor import CurveEditor
from .grading_widgets import ColourWheel, HueCurveEditor, QualifierView
from .graph import KeyframeGraph, MotionPreview
from .widgets import (DragNumber, icon_button, label, open_files, parse_rgba, popup_menu, rgba_to_hex,
                      set_button_icon, spacer)

ALIGNMENTS = [("center", "Centre at Cut"), ("start", "Start at Cut"), ("end", "End at Cut")]
# Headings shown above a parameter, to break long effects into parts.
HEADINGS = {("text", "anim_in"): "Animation", ("secondary", "use_hue"): "Qualifier",
            ("secondary", "hue_shift"): "Correction", ("wheels", "contrast"): "Contrast & Saturation"}


def interp_menu():
    """Menu of keyframe curves; the common ones first, the rest in submenus."""
    menu, top, more = Gio.Menu(), Gio.Menu(), Gio.Menu()
    for title, items in kf.INTERP_GROUPS:
        target = top
        if title:
            target = Gio.Menu()
            more.append_submenu(title, target)
        for key, text in items:
            target.append(text, f"ec.interp('{key}')")
    menu.append_section(None, top)
    menu.append_section(None, more)
    return menu


class ParamRow:
    """One parameter: animation toggle, label, editor, keyframe navigation,
    and (when animated) a value graph that folds out underneath."""

    def __init__(self, panel, clip, effect, pdef):
        self.panel, self.app = panel, panel.app
        self.clip, self.effect, self.pdef = clip, effect, pdef
        self.updating = False
        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.graph = None
        self.kf_button = None
        if pdef.type == "wheel":
            # A wheel is a tile of its own; Effect Controls lays the tiles out in a row.
            self.watch, self.numbers, self.scale = None, [], None
            self.editor = ColourWheel(pdef.label, lambda value, final: self._set(value, final))
            self.container.append(self.editor)
            self.refresh()
            return
        self.widget = Gtk.Box(spacing=6)
        self.widget.add_css_class("fx-row")
        self.container.append(self.widget)
        animated = effect.animated(pdef.name)
        if pdef.key:
            self.watch = icon_button("stopwatch", "Toggle animation", None, toggle=True, size=14,
                                     css="kf-button")
            self.watch.set_active(animated)
            self.watch.connect("toggled", self._toggle_anim)
        else:
            self.watch = Gtk.Box()
            self.watch.set_size_request(18, -1)
        self.widget.append(self.watch)
        name = label(pdef.label, css="param-label", ellipsize=True)
        name.set_size_request(118, -1)
        self.widget.append(name)
        self.numbers, self.scale, self.editor = [], None, None
        self._build_editor()
        if pdef.key and animated:
            nav = Gtk.Box(spacing=0)
            nav.append(icon_button("left", "Previous keyframe", lambda b: self.app.goto_keyframe(
                clip, effect, pdef, -1), size=12, css="kf-button"))
            tip = "Add / remove keyframe" + ("" if pdef.hold else " (right-click for interpolation)")
            self.kf_button = icon_button("diamond_o", tip, lambda b: self.app.toggle_keyframe(clip, effect, pdef),
                                         size=13, css="kf-button")
            if not pdef.hold:
                click = Gtk.GestureClick(button=3)
                click.connect("pressed", self._interp_menu)
                self.kf_button.add_controller(click)
            nav.append(self.kf_button)
            nav.append(icon_button("right", "Next keyframe", lambda b: self.app.goto_keyframe(
                clip, effect, pdef, 1), size=12, css="kf-button"))
            key = (effect.id, pdef.name)
            show = icon_button("curve", "Show the keyframe graph", None, toggle=True, size=13, css="kf-button")
            show.set_active(key in panel.graphs_open)
            show.connect("toggled", lambda b: panel.toggle_graph(key, b.get_active()))
            nav.append(show)
            if key in panel.graphs_open:
                self.graph = KeyframeGraph(self)
                self.container.append(self.graph)
            self.widget.append(nav)
        self.refresh()

    # -- editors ---------------------------------------------------------
    def _build_editor(self):
        p = self.pdef
        box = self.widget
        if p.type in ("float", "int"):
            num = DragNumber(0.0, p.min, p.max, p.step, 0 if p.type == "int" else p.digits, p.unit)
            num.connect("changed", lambda w, v, final: self._set(int(round(v)) if p.type == "int" else v, final))
            self.numbers = [num]
            box.append(num)
            if p.min is not None and p.max is not None and p.slider:
                self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, p.min, p.max,
                                                      max(p.step, (p.max - p.min) / 1000))
                self.scale.set_draw_value(False)
                self.scale.set_hexpand(True)
                self.scale.set_focusable(False)
                self.scale.connect("value-changed", self._scale_changed)
                box.append(self.scale)
            else:
                box.append(spacer())
        elif p.type == "point":
            for i in range(2):
                num = DragNumber(0.0, None, None, 1.0, 1, "")
                num.connect("changed", lambda w, v, final, i=i: self._set_point(i, v, final))
                self.numbers.append(num)
                box.append(num)
            box.append(spacer())
        elif p.type == "bool":
            self.editor = Gtk.Switch(valign=Gtk.Align.CENTER, focusable=False)
            self.editor.connect("notify::active", lambda w, _p: self._set(w.get_active(), True))
            box.append(self.editor)
            box.append(spacer())
        elif p.type == "choice":
            self.editor = Gtk.DropDown.new_from_strings([c[1] for c in p.choices])
            self.editor.set_focusable(False)
            self.editor.connect("notify::selected", lambda w, _p: self._set(p.choices[w.get_selected()][0], True))
            box.append(self.editor)
            box.append(spacer())
        elif p.type == "color":
            self.editor = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=True, title=p.label))
            self.editor.set_focusable(False)
            self.editor.connect("notify::rgba", lambda w, _p: self._set(rgba_to_hex(w.get_rgba()), True))
            box.append(self.editor)
            box.append(spacer())
        elif p.type == "font":
            self.editor = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title="Font"), level=Gtk.FontLevel.FAMILY)
            self.editor.set_focusable(False)
            self.editor.connect("notify::font-desc", self._font_changed)
            box.append(self.editor)
            box.append(icon_button("plus", "Add a font file (.ttf, .otf) and use it here",
                                   lambda b: self.app.choose_fonts(lambda names: self._set(names[0], True)),
                                   size=13, css="kf-button"))
            box.append(spacer())
        elif p.type == "file":
            self.editor = Gtk.Button(label="Choose…", focusable=False)
            self.editor.connect("clicked", self._choose_file)
            box.append(self.editor)
            box.append(spacer())
        elif p.type in ("curves", "huecurves"):
            box.append(spacer())
            editor = CurveEditor if p.type == "curves" else HueCurveEditor
            self.editor = editor(self.app, lambda value, final: self._set(value, final))
            self.editor.set_margin_start(30)
            self.editor.set_margin_end(8)
            self.editor.set_margin_bottom(6)
            self.container.append(self.editor)
        elif p.type == "text":
            self.editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, hexpand=True, accepts_tab=False)
            self.editor.set_size_request(-1, 58)
            self.editor.get_buffer().connect("changed", self._text_changed)
            box.append(self.editor)

    def _value(self):
        return self.app.param_value(self.clip, self.effect, self.pdef)

    def _set(self, value, final):
        if not self.updating:
            self.app.set_param(self.clip, self.effect, self.pdef, value, final)

    def _set_point(self, index, v, final):
        if self.updating:
            return
        cur = list(self._value() or [0.0, 0.0])
        cur[index] = v
        self.app.set_param(self.clip, self.effect, self.pdef, cur, final)

    def _scale_changed(self, scale):
        if self.updating:
            return
        v = scale.get_value()
        self._set(int(round(v)) if self.pdef.type == "int" else round(v, 3), False)
        self.panel.commit_later(f"Change {self.pdef.label}")

    def _text_changed(self, buf):
        if self.updating:
            return
        self._set(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False), False)
        self.panel.commit_later("Edit Text")

    def _font_changed(self, button, _p):
        desc = button.get_font_desc()
        if desc is not None and not self.updating:
            self._set(desc.get_family() or "Sans", True)

    def _choose_file(self, button):
        open_files(self.app.window, self.pdef.label, lambda paths: self._set(paths[0], True), multiple=False)

    def _toggle_anim(self, button):
        if button.get_active():
            self.panel.graphs_open.add((self.effect.id, self.pdef.name))
        self.app.toggle_animation(self.clip, self.effect, self.pdef, button.get_active())

    def _interp_menu(self, gesture, n, x, y):
        self.panel.interp_target, self.panel.interp_time = self, None
        popup_menu(self.kf_button, interp_menu(), x, y)

    # -- refresh ---------------------------------------------------------
    def refresh(self):
        p, v = self.pdef, self._value()
        self.updating = True
        try:
            if p.type in ("float", "int"):
                self.numbers[0].set_value(float(v))
                if self.scale is not None and abs(self.scale.get_value() - float(v)) > 1e-6:
                    self.scale.set_value(float(v))
            elif p.type == "point":
                for num, comp in zip(self.numbers, v or [0.0, 0.0]):
                    num.set_value(float(comp))
            elif p.type == "bool":
                if self.editor.get_active() != bool(v):
                    self.editor.set_active(bool(v))
            elif p.type == "choice":
                keys = [c[0] for c in p.choices]
                idx = keys.index(v) if v in keys else 0
                if self.editor.get_selected() != idx:
                    self.editor.set_selected(idx)
            elif p.type == "color":
                rgba = parse_rgba(v)
                if rgba_to_hex(self.editor.get_rgba()) != rgba_to_hex(rgba):
                    self.editor.set_rgba(rgba)
            elif p.type == "font":
                cur = self.editor.get_font_desc()
                if cur is None or cur.get_family() != v:
                    self.editor.set_font_desc(Pango.FontDescription(f"{v} 12"))
            elif p.type == "file":
                self.editor.set_label(os.path.basename(v) if v else "Choose…")
            elif p.type in ("curves", "huecurves", "wheel"):
                self.editor.set_value(v)
            elif p.type == "text":
                buf = self.editor.get_buffer()
                if buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False) != (v or ""):
                    buf.set_text(v or "")
        finally:
            self.updating = False
        if self.kf_button is not None:
            on_key = kf.find(self.effect.keyframes.get(p.name), self.app.clip_time(self.clip)) >= 0
            set_button_icon(self.kf_button, "diamond" if on_key else "diamond_o")
            if on_key:
                self.kf_button.add_css_class("on")
            else:
                self.kf_button.remove_css_class("on")
        if self.graph is not None:
            self.graph.queue_draw()


class EffectControls(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.rows = []
        self.previews = []
        self.graphs_open = set()          # (effect id, parameter) whose value graph is shown
        self.signature = None
        self.interp_target = None
        self.interp_time = None           # keyframe a context menu acts on (None: at the playhead)
        self._commit_id = 0
        self._commit_label = ""
        self.header = label("", css="dim", ellipsize=True)
        self.header.set_margin_start(8)
        self.header.set_margin_top(5)
        self.header.set_margin_bottom(5)
        self.append(self.header)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.body)
        self.append(scroller)
        group = Gio.SimpleActionGroup()
        interp = Gio.SimpleAction.new("interp", GLib.VariantType.new("s"))
        interp.connect("activate", self._set_interp)
        group.add_action(interp)
        for name, fn in (("kf_delete", self._kf_delete), ("kf_add", self._kf_add)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            group.add_action(action)
        self.insert_action_group("ec", group)
        for ev in ("selection", "sequence", "project"):
            app.on(ev, self.sync)
        for ev in ("playhead", "effects"):
            app.on(ev, self.refresh_values)
        app.on("picker", self._picker_changed)
        self.sync()

    def _picker_changed(self):
        for view in self.previews:
            if isinstance(view, QualifierView):
                view.refresh()

    def commit_later(self, label_):
        """Coalesce a burst of live changes (slider drag, typing) into one undo step."""
        self._commit_label = label_
        if self._commit_id:
            GLib.source_remove(self._commit_id)
        self._commit_id = GLib.timeout_add(450, self._commit_now)

    def _commit_now(self):
        self._commit_id = 0
        self.app.commit(self._commit_label, rebuild=False)
        return False

    def _set_interp(self, action, param):
        row = self.interp_target
        if row is not None:
            self.app.set_interp(row.clip, row.effect, row.pdef, param.get_string(), self.interp_time)

    def _kf_delete(self, *_):
        row = self.interp_target
        if row is not None and self.interp_time is not None:
            if row.graph is not None:
                row.graph.selected = None
            self.app.remove_keyframe(row.clip, row.effect, row.pdef, self.interp_time)

    def _kf_add(self, *_):
        row = self.interp_target
        if row is not None and row.graph is not None:
            row.graph.add_pending()

    def toggle_graph(self, key, show):
        if show:
            self.graphs_open.add(key)
        else:
            self.graphs_open.discard(key)
        self.sync()

    def _target(self):
        app = self.app
        seq = app.seq
        if seq is None:
            return None
        if app.selected_transition:
            track, tr = app.editor().find_transition(app.selected_transition)
            if tr is not None:
                return "transition", track, tr
        pairs = app.selected_clips()
        if not pairs:
            return None
        # A linked video+audio pair shows the video part first, then the audio part.
        ids = {c.link_id for _, c in pairs}
        if len(pairs) == 1 or (len(ids) == 1 and None not in ids):
            return "clips", sorted(pairs, key=lambda p: 0 if p[0].kind == "video" else 1), None
        return "multi", pairs, None

    def _signature(self, target):
        if target is None:
            return None
        kind, a, b = target
        if kind == "transition":
            return ("t", b.id, b.type, b.clip_a, b.clip_b)
        if kind == "multi":
            return ("m", len(a))
        # Object identities are part of the signature: drags and undo replace
        # clip objects, and rows must never keep editing a stale one.
        return ("c", tuple(sorted(self.graphs_open))) + tuple(
            (c.id, id(c), tuple((e.id, id(e), e.type, e.collapsed,
                                 tuple(sorted(k for k, v in e.keyframes.items() if v)),
                                 e.params.get("uniform", True)) for e in c.effects)) for _, c in a)

    def sync(self, *_):
        target = self._target()
        sig = self._signature(target)
        if sig != self.signature:
            self.signature = sig
            self._rebuild(target)
        else:
            self.refresh_values()

    def refresh_values(self, *_):
        if not self.rows or not self.get_mapped():
            return
        app = self.app
        if app.program.playing and not app.prefs["live_values"]:
            return          # caught up when playback stops
        for row in self.rows + self.previews:
            row.refresh()

    def _clear(self):
        self.rows, self.previews = [], []
        child = self.body.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.body.remove(child)
            child = nxt

    def _rebuild(self, target):
        self._clear()
        app = self.app
        if target is None:
            self.header.set_text("No clip selected")
            self.body.append(label("Select a clip in the timeline to edit its effects.", css="hint", xalign=0.5,
                                   margin_top=30, wrap=True))
            return
        kind, a, b = target
        if kind == "multi":
            self.header.set_text(f"{len(a)} clips selected")
            self.body.append(label("Select a single clip to edit its effects.", css="hint", xalign=0.5,
                                   margin_top=30, wrap=True))
            return
        if kind == "transition":
            return self._build_transition(a, b)
        seq = app.seq
        clip = a[0][1]
        media = app.project.media.get(clip.media_id)
        self.header.set_text(f"{seq.name}  ›  {clip.name or (media.name if media else '')}")
        for track, c in a:
            title = label("Video" if track.kind == "video" else "Audio", css="faint", margin_start=8,
                          margin_top=4, margin_bottom=2)
            self.body.append(title)
            for effect in c.effects:
                edef = EFFECTS.get(effect.type)
                if edef is not None and edef.fits(track.kind):
                    self._build_effect(c, effect, edef)

    def _build_effect(self, clip, effect, edef):
        app = self.app
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        section.add_css_class("fx-section")
        head = Gtk.Box(spacing=3)
        head.add_css_class("fx-header")

        def collapse(_b):
            effect.collapsed = not effect.collapsed
            self.sync()

        head.append(icon_button("right" if effect.collapsed else "down", "", collapse, size=12, css="kf-button"))
        fx = Gtk.ToggleButton(label="fx", focusable=False, tooltip_text="Enable / bypass this effect")
        fx.add_css_class("flat")
        fx.add_css_class("fx-toggle")
        fx.set_active(effect.enabled)
        fx.connect("toggled", lambda b: app.toggle_effect(effect, b.get_active()))
        head.append(fx)
        head.append(label(edef.label))
        head.append(spacer())
        head.append(icon_button("reset", "Reset parameters", lambda b: app.reset_effect(effect), size=13,
                                css="kf-button"))
        if not edef.intrinsic:
            head.append(icon_button("up", "Move up", lambda b: app.move_effect(clip, effect, -1), size=12,
                                    css="kf-button"))
            head.append(icon_button("down", "Move down", lambda b: app.move_effect(clip, effect, 1), size=12,
                                    css="kf-button"))
            head.append(icon_button("close", "Remove effect", lambda b: app.remove_effect(clip, effect), size=12,
                                    css="kf-button"))
        section.append(head)
        if not effect.collapsed:
            if edef.panel == "secondary":
                view = QualifierView(app, clip, effect)
                self.previews.append(view)
                section.append(view)
            uniform = effect.params.get("uniform", True)
            wheels = None
            for pdef in edef.params:
                if effect.type == "motion" and pdef.name == "scale_w" and uniform:
                    continue
                heading = HEADINGS.get((effect.type, pdef.name))
                if heading:
                    section.append(label(heading, css="faint", margin_start=30, margin_top=6, margin_bottom=1))
                row = ParamRow(self, clip, effect, pdef)
                self.rows.append(row)
                if pdef.type == "wheel":
                    if wheels is None:
                        wheels = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                             max_children_per_line=4, min_children_per_line=1,
                                             column_spacing=6, row_spacing=6, margin_start=24, margin_end=8,
                                             margin_top=4, margin_bottom=4)
                        section.append(wheels)
                    wheels.append(row.container)
                    continue
                wheels = None
                section.append(row.container)
            if edef.preview is not None:
                preview = MotionPreview(app, clip, effect, edef)
                self.previews.append(preview)
                section.append(preview)
            if not edef.params:
                section.append(label("No parameters", css="faint", margin_start=30, margin_top=3, margin_bottom=3))
        self.body.append(section)

    def _build_transition(self, track, tr):
        app = self.app
        seq = app.seq
        tdef = TRANSITIONS.get(tr.type)
        self.header.set_text(f"Transition  ›  {tdef.label if tdef else tr.type}")
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        section.add_css_class("fx-section")

        def row(text, widget):
            box = Gtk.Box(spacing=6)
            box.add_css_class("fx-row")
            pad = Gtk.Box()
            pad.set_size_request(18, -1)
            box.append(pad)
            name = label(text, css="param-label")
            name.set_size_request(118, -1)
            box.append(name)
            box.append(widget)
            section.append(box)

        options = [t for t in TRANSITIONS.values() if t.kind == track.kind]
        types = Gtk.DropDown.new_from_strings([t.label for t in options])
        types.set_selected(next((i for i, t in enumerate(options) if t.key == tr.type), 0))
        types.set_focusable(False)
        types.connect("notify::selected", lambda w, _p: app.update_transition(tr, type=options[w.get_selected()].key))
        row("Type", types)
        dur = DragNumber(tr.duration, 1, 100000, 1.0, 0, "frames")
        dur.connect("changed", lambda w, v, final: app.update_transition(tr, final, duration=max(1, int(v))))
        box = Gtk.Box(spacing=8)
        box.append(dur)
        box.append(label(tc.frames_to_tc(tr.duration, seq.fps_num, seq.fps_den), css="dim"))
        row("Duration", box)
        if tr.clip_a and tr.clip_b:
            align = Gtk.DropDown.new_from_strings([a[1] for a in ALIGNMENTS])
            align.set_selected(next((i for i, a in enumerate(ALIGNMENTS) if a[0] == tr.align), 0))
            align.set_focusable(False)
            align.connect("notify::selected", lambda w, _p: app.update_transition(tr, align=ALIGNMENTS[w.get_selected()][0]))
            row("Alignment", align)
        if tdef is not None and tdef.mode == "luma" and tdef.resource:
            soft = DragNumber(tr.params.get("softness", 0.05) * 100, 0, 100, 1.0, 0, "%")
            soft.connect("changed", lambda w, v, final: app.update_transition(tr, final, softness=v / 100.0))
            row("Edge Softness", soft)
            rev = Gtk.Switch(valign=Gtk.Align.CENTER, active=bool(tr.params.get("reverse", False)), focusable=False)
            rev.connect("notify::active", lambda w, _p: app.update_transition(tr, reverse=w.get_active()))
            row("Reverse", rev)
        if tdef is not None and tdef.mode == "dip":
            col = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), focusable=False)
            col.set_rgba(parse_rgba(tr.params.get("color", tdef.color)))
            col.connect("notify::rgba", lambda w, _p: app.update_transition(tr, color=rgba_to_hex(w.get_rgba())))
            row("Colour", col)
        remove = Gtk.Button(label="Remove Transition", halign=Gtk.Align.START, margin_start=30, margin_top=8,
                            margin_bottom=8, focusable=False)
        remove.connect("clicked", lambda b: app.do("delete"))
        section.append(remove)
        self.body.append(section)
