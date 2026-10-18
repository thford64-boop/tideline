"""Caption Style: font, colours, outline, box, shadow, position and
animation of captions, for the selected captions or all of them."""

import cairo
from gi.repository import Gtk, Pango, PangoCairo

from ..engine.effects import TEXT_ANIMS, TEXT_ANIMS_OUT
from . import theme
from .dialogs import _Dialog, _spin
from .widgets import parse_rgba, rgba_to_hex

# Ready-made looks: only the keys they set change.
PRESETS = [
    ("Classic", {"fill": "#ffffffff", "background": "#000000b0", "stroke_width": 0, "bold": True, "shadow": False,
                 "anim_in": "none", "anim_out": "none"}),
    ("Outline", {"fill": "#ffffffff", "background": "#00000000", "stroke": "#000000ff", "stroke_width": 3,
                 "bold": True, "shadow": True, "anim_in": "none", "anim_out": "none"}),
    ("Yellow", {"fill": "#ffd43bff", "background": "#00000000", "stroke": "#000000ff", "stroke_width": 2,
                "bold": True, "shadow": True}),
    ("Bold Pop", {"fill": "#ffffffff", "background": "#00000000", "stroke": "#000000ff", "stroke_width": 5,
                  "bold": True, "shadow": False, "anim_in": "pop", "anim_out": "fade", "anim_time": 0.25,
                  "position": "middle"}),
    ("Minimal", {"fill": "#ffffffff", "background": "#00000000", "stroke_width": 0, "bold": False,
                 "shadow": True, "anim_in": "fade", "anim_out": "fade", "anim_time": 0.2}),
    ("Typewriter", {"fill": "#f2f2f2ff", "background": "#000000c8", "stroke_width": 0, "bold": False,
                    "font": "Monospace", "anim_in": "typewriter", "anim_out": "none", "anim_time": 1.0}),
]
POSITIONS = [("bottom", "Bottom"), ("middle", "Middle"), ("top", "Top")]
ALIGNS = [("left", "Left"), ("center", "Centre"), ("right", "Right")]


class CaptionPreview(Gtk.DrawingArea):
    """A sketch of a caption in the sequence's frame. Qt draws the real
    one, so letter shapes can differ a little; sizes and places match."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.set_content_width(400)
        self.set_content_height(226)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        seq = self.dialog.app.seq
        style = self.dialog.style
        fw, fh = seq.width, seq.height
        k = min(w / fw, h / fh)
        ox, oy = (w - fw * k) / 2, (h - fh * k) / 2
        cr.set_source_rgba(*theme.rgb(theme.BG0))
        cr.paint()
        # a stand-in picture: sky over ground
        grad = cairo.LinearGradient(0, oy, 0, oy + fh * k)
        grad.add_color_stop_rgb(0, 0.33, 0.47, 0.62)
        grad.add_color_stop_rgb(0.62, 0.72, 0.66, 0.55)
        grad.add_color_stop_rgb(0.63, 0.30, 0.38, 0.25)
        grad.add_color_stop_rgb(1, 0.18, 0.24, 0.16)
        cr.set_source(grad)
        cr.rectangle(ox, oy, fw * k, fh * k)
        cr.fill()
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription()
        desc.set_family(style["font"] or "Sans")
        desc.set_absolute_size(max(1, float(style["size"]) * k) * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if style["bold"] else Pango.Weight.NORMAL)
        desc.set_style(Pango.Style.ITALIC if style["italic"] else Pango.Style.NORMAL)
        layout.set_font_description(desc)
        layout.set_alignment({"left": Pango.Alignment.LEFT, "right": Pango.Alignment.RIGHT}.get(
            style["halign"], Pango.Alignment.CENTER))
        text = self.dialog.sample_text()
        layout.set_markup(f"<u>{text}</u>" if style["underline"] else text.replace("&", "&amp;"), -1)
        tw, th = (v / Pango.SCALE for v in layout.get_size())
        pad = float(style["padding"]) * k
        margin = float(style["margin"]) / 100.0
        bw, bh = tw + 2 * pad, th + 2 * pad
        x = {"left": fw * margin * k, "right": fw * k - bw - fw * margin * k}.get(style["halign"], (fw * k - bw) / 2)
        y = {"top": fh * margin * k, "middle": (fh * k - bh) / 2}.get(style["position"], fh * k - bh - fh * margin * k)
        x, y = ox + x, oy + y
        bg = parse_rgba(style["background"])
        if bg.alpha > 0:
            cr.set_source_rgba(bg.red, bg.green, bg.blue, bg.alpha)
            cr.rectangle(x, y, bw, bh)
            cr.fill()
        if style["shadow"]:
            sc = parse_rgba(style["shadow_color"])
            off = max(1.0, float(style["size"]) / 16) * k
            cr.move_to(x + pad + off, y + pad + off)
            cr.set_source_rgba(sc.red, sc.green, sc.blue, sc.alpha * 0.85)
            PangoCairo.show_layout(cr, layout)
        cr.move_to(x + pad, y + pad)
        PangoCairo.layout_path(cr, layout)
        width = float(style["stroke_width"]) * k
        if width > 0:
            ol = parse_rgba(style["stroke"])
            cr.set_source_rgba(ol.red, ol.green, ol.blue, ol.alpha)
            cr.set_line_width(width * 2)
            cr.set_line_join(1)
            cr.stroke_preserve()
        fill = parse_rgba(style["fill"])
        cr.set_source_rgba(fill.red, fill.green, fill.blue, fill.alpha)
        cr.fill()


class CaptionStyleDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "Caption Style", "Apply")
        seq = app.seq
        self.selected = app.caption_clips("selected")
        self.all = app.caption_clips("all")
        first = self.selected[0][1] if self.selected else (self.all[0][1] if self.all else None)
        self.style = app.caption_style_of(first) if first is not None else app.caption_style()
        self._busy = False

        self.preview = CaptionPreview(self)
        self.preview.set_valign(Gtk.Align.START)
        self.preview.set_margin_start(10)
        presets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                              column_spacing=4, row_spacing=4)
        for name, values in PRESETS:
            btn = Gtk.Button(label=name, focusable=False)
            btn.connect("clicked", lambda b, v=values: self._preset(v))
            presets.append(btn)
        self.add_row("Presets", presets)

        font_row = Gtk.Box(spacing=6)
        self.font = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title="Caption Font"), level=Gtk.FontLevel.FAMILY)
        self.font.set_font_desc(Pango.FontDescription(f"{self.style['font']} 12"))
        self.font.connect("notify::font-desc", lambda *_: self._changed())
        font_row.append(self.font)
        self.size = _spin(self.style["size"], 4, 800)
        self.size.set_width_chars(5)
        self.size.connect("value-changed", lambda *_: self._changed())
        font_row.append(self.size)
        font_row.append(Gtk.Label(label="px"))
        self.add_row("Font", font_row)
        looks = Gtk.Box(spacing=4)
        self.bold = Gtk.ToggleButton(label="Bold", active=self.style["bold"], focusable=False)
        self.italic = Gtk.ToggleButton(label="Italic", active=self.style["italic"], focusable=False)
        self.underline = Gtk.ToggleButton(label="Underline", active=self.style["underline"], focusable=False)
        for b in (self.bold, self.italic, self.underline):
            b.connect("toggled", lambda *_: self._changed())
            looks.append(b)
        self.add_row("", looks)

        self.fill = self._colour("fill", "Text Colour")
        self.add_row("Text", self.fill)
        outline = Gtk.Box(spacing=6)
        self.stroke = self._colour("stroke", "Outline Colour")
        outline.append(self.stroke)
        self.stroke_width = _spin(self.style["stroke_width"], 0, 20)
        self.stroke_width.set_width_chars(4)
        self.stroke_width.connect("value-changed", lambda *_: self._changed())
        outline.append(self.stroke_width)
        outline.append(Gtk.Label(label="px wide"))
        self.add_row("Outline", outline)
        box = Gtk.Box(spacing=6)
        self.background = self._colour("background", "Box Colour")
        box.append(self.background)
        self.padding = _spin(self.style["padding"], 0, 200)
        self.padding.set_width_chars(4)
        self.padding.connect("value-changed", lambda *_: self._changed())
        box.append(self.padding)
        box.append(Gtk.Label(label="px padding (clear colour: no box)"))
        self.add_row("Box", box)
        shadow = Gtk.Box(spacing=6)
        self.shadow = Gtk.Switch(active=self.style["shadow"], valign=Gtk.Align.CENTER)
        self.shadow.connect("notify::active", lambda *_: self._changed())
        shadow.append(self.shadow)
        self.shadow_color = self._colour("shadow_color", "Shadow Colour")
        shadow.append(self.shadow_color)
        self.add_row("Shadow", shadow)

        place = Gtk.Box(spacing=6)
        self.position = self._choice(POSITIONS, self.style["position"])
        self.halign = self._choice(ALIGNS, self.style["halign"])
        self.margin = _spin(self.style["margin"], 0, 45, 0.5, 1)
        self.margin.set_width_chars(5)
        self.margin.connect("value-changed", lambda *_: self._changed())
        for w in (self.position, self.halign, self.margin, Gtk.Label(label="% from the edge")):
            place.append(w)
        self.add_row("Position", place)

        anim = Gtk.Box(spacing=6)
        self.anim_in = self._choice(TEXT_ANIMS, self.style["anim_in"])
        self.anim_out = self._choice(TEXT_ANIMS_OUT, self.style["anim_out"])
        self.anim_time = _spin(self.style["anim_time"], 0.04, 10, 0.02, 2)
        self.anim_time.set_width_chars(5)
        self.anim_time.set_tooltip_text("How long each animation lasts; for Typewriter and Word by Word, the "
                                        "time the whole caption takes to appear")
        self.anim_time.connect("value-changed", lambda *_: self._changed())
        for w in (Gtk.Label(label="In"), self.anim_in, Gtk.Label(label="Out"), self.anim_out, self.anim_time,
                  Gtk.Label(label="s")):
            anim.append(w)
        self.add_row("Animation", anim)

        scope = Gtk.Box(spacing=10)
        self.scope_selected = Gtk.CheckButton(label=f"Selected captions ({len(self.selected)})")
        self.scope_all = Gtk.CheckButton(label=f"All captions in “{seq.name}” ({len(self.all)})",
                                         group=self.scope_selected)
        (self.scope_selected if self.selected else self.scope_all).set_active(True)
        self.scope_selected.set_sensitive(bool(self.selected))
        scope.append(self.scope_selected)
        scope.append(self.scope_all)
        self.add_row("Apply to", scope)
        self.make_default = Gtk.CheckButton(label="Use this style for new captions in this sequence",
                                            active=not self.selected)
        self.scope_all.connect("toggled", lambda b: self.make_default.set_active(b.get_active()))
        self.grid.attach(self.make_default, 1, self._row, 1, 1)
        self._row += 1
        # The preview sits beside the settings, so the dialog fits on short screens.
        self.grid.attach(self.preview, 2, 0, 1, self._row)

    # -- pieces ------------------------------------------------------------
    def _colour(self, key, title):
        btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=True, title=title), focusable=False)
        btn.set_rgba(parse_rgba(self.style[key]))
        btn.connect("notify::rgba", lambda *_: self._changed())
        return btn

    def _choice(self, items, current):
        drop = Gtk.DropDown.new_from_strings([text for _, text in items])
        drop.items = items
        drop.set_selected(next((i for i, (k, _) in enumerate(items) if k == current), 0))
        drop.set_focusable(False)
        drop.connect("notify::selected", lambda *_: self._changed())
        return drop

    def sample_text(self):
        clips = self.selected or self.all
        text = clips[0][1].effect("text").value("text", None, "") if clips else ""
        return (text or "The quick brown fox\njumps over the lazy dog").strip()

    # -- state -------------------------------------------------------------
    def _read(self):
        desc = self.font.get_font_desc()
        pick = lambda drop: drop.items[max(0, drop.get_selected())][0]
        return dict(self.style, font=(desc.get_family() if desc else None) or "Sans",
                    size=int(self.size.get_value()), bold=self.bold.get_active(), italic=self.italic.get_active(),
                    underline=self.underline.get_active(), fill=rgba_to_hex(self.fill.get_rgba()),
                    stroke=rgba_to_hex(self.stroke.get_rgba()), stroke_width=int(self.stroke_width.get_value()),
                    background=rgba_to_hex(self.background.get_rgba()), padding=int(self.padding.get_value()),
                    shadow=self.shadow.get_active(), shadow_color=rgba_to_hex(self.shadow_color.get_rgba()),
                    position=pick(self.position), halign=pick(self.halign),
                    margin=round(self.margin.get_value(), 1), anim_in=pick(self.anim_in),
                    anim_out=pick(self.anim_out), anim_time=round(self.anim_time.get_value(), 2))

    def _changed(self):
        if self._busy:
            return
        self.style = self._read()
        self.preview.queue_draw()

    def _preset(self, values):
        style = dict(self.style, **values)
        self._busy = True
        try:
            self.font.set_font_desc(Pango.FontDescription(f"{style['font']} 12"))
            for attr in ("bold", "italic", "underline"):
                getattr(self, attr).set_active(bool(style[attr]))
            for attr in ("fill", "stroke", "background", "shadow_color"):
                getattr(self, attr).set_rgba(parse_rgba(style[attr]))
            self.stroke_width.set_value(style["stroke_width"])
            self.shadow.set_active(bool(style["shadow"]))
            for attr in ("position", "halign", "anim_in", "anim_out"):
                drop = getattr(self, attr)
                drop.set_selected(next((i for i, (k, _) in enumerate(drop.items) if k == style[attr]), 0))
            self.anim_time.set_value(style["anim_time"])
        finally:
            self._busy = False
        self._changed()

    def accept(self):
        scope = "selected" if self.scope_selected.get_active() else "all"
        self.app.apply_caption_style(self._read(), scope, self.make_default.get_active())
        self.close()
