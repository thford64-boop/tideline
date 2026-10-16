"""Original vector icons, drawn with cairo on a 24-unit grid.

Icons are stroked in the widget's foreground colour, so they follow hover,
checked and disabled states without separate assets.
"""

import math

from gi.repository import Gtk


def _line(cr, *pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)


def _poly(cr, *pts):
    _line(cr, *pts)
    cr.close_path()


def _arrow(cr, x0, y0, x1, y1, head=3.5):
    ang = math.atan2(y1 - y0, x1 - x0)
    cr.move_to(x0, y0)
    cr.line_to(x1, y1)
    for d in (2.6, -2.6):
        cr.move_to(x1, y1)
        cr.line_to(x1 - head * math.cos(ang + d / 5.2 * 1.1), y1 - head * math.sin(ang + d / 5.2 * 1.1))


def _select(cr):
    _poly(cr, (6, 3), (6, 19), (10.2, 15), (13.2, 21), (15.6, 19.8), (12.7, 14), (18, 14))
    cr.fill()


def _track_fwd(cr):
    _line(cr, (4, 5), (4, 19))
    cr.stroke()
    _poly(cr, (8, 6), (14, 12), (8, 18))
    cr.fill()
    _poly(cr, (14, 6), (20, 12), (14, 18))
    cr.fill()


def _ripple(cr):
    _line(cr, (12, 4), (8, 4), (8, 20), (12, 20))
    cr.stroke()
    _arrow(cr, 13, 12, 21, 12)
    cr.stroke()
    _line(cr, (3, 8), (3, 16))
    cr.stroke()


def _roll(cr):
    _line(cr, (9, 4), (12, 4), (12, 20), (9, 20))
    cr.stroke()
    _arrow(cr, 9, 12, 3, 12)
    _arrow(cr, 15, 12, 21, 12)
    cr.stroke()


def _rate(cr):
    _arrow(cr, 9, 12, 3, 12)
    _arrow(cr, 15, 12, 21, 12)
    cr.stroke()
    cr.arc(12, 12, 4.2, 0, 2 * math.pi)
    cr.stroke()
    _line(cr, (12, 9.5), (12, 12), (14, 13))
    cr.stroke()


def _razor(cr):
    _poly(cr, (4, 14), (14, 4), (20, 10), (10, 20))
    cr.stroke()
    _line(cr, (9, 9), (15, 15))
    cr.stroke()
    _line(cr, (4, 20), (7, 17))
    cr.stroke()


def _slip(cr):
    _line(cr, (7, 5), (4, 5), (4, 19), (7, 19))
    _line(cr, (17, 5), (20, 5), (20, 19), (17, 19))
    cr.stroke()
    _arrow(cr, 12, 12, 7.5, 12, 3)
    _arrow(cr, 12, 12, 16.5, 12, 3)
    cr.stroke()


def _slide(cr):
    cr.rectangle(8, 7, 8, 10)
    cr.stroke()
    _arrow(cr, 7, 12, 2.5, 12, 3)
    _arrow(cr, 17, 12, 21.5, 12, 3)
    cr.stroke()


def _pen(cr):
    _poly(cr, (12, 3), (17, 12), (14, 19), (10, 19), (7, 12))
    cr.stroke()
    _line(cr, (12, 3), (12, 12))
    cr.stroke()
    cr.arc(12, 13, 1.3, 0, 2 * math.pi)
    cr.fill()
    _line(cr, (9, 22), (15, 22))
    cr.stroke()


def _hand(cr):
    cr.move_to(8, 13)
    cr.line_to(8, 6.5)
    cr.curve_to(8, 4.5, 10.5, 4.5, 10.5, 6.5)
    cr.line_to(10.5, 5)
    cr.curve_to(10.5, 3, 13, 3, 13, 5)
    cr.line_to(13, 6)
    cr.curve_to(13, 4, 15.5, 4, 15.5, 6)
    cr.line_to(15.5, 8)
    cr.curve_to(15.5, 6.3, 18, 6.3, 18, 8.2)
    cr.line_to(18, 15)
    cr.curve_to(18, 19, 15.5, 21, 12.5, 21)
    cr.curve_to(9.5, 21, 8.5, 19.5, 5.5, 14.5)
    cr.curve_to(4.5, 12.6, 6.8, 11.5, 8, 13)
    cr.stroke()


def _zoom(cr):
    cr.arc(10.5, 10.5, 6, 0, 2 * math.pi)
    cr.stroke()
    _line(cr, (15, 15), (20.5, 20.5))
    cr.stroke()


def _type(cr):
    _line(cr, (5, 7), (5, 4.5), (19, 4.5), (19, 7))
    _line(cr, (12, 4.5), (12, 19.5))
    _line(cr, (9, 19.5), (15, 19.5))
    cr.stroke()


def _play(cr):
    _poly(cr, (7, 4.5), (19.5, 12), (7, 19.5))
    cr.fill()


def _pause(cr):
    cr.rectangle(6.5, 5, 4, 14)
    cr.rectangle(13.5, 5, 4, 14)
    cr.fill()


def _step_back(cr):
    _poly(cr, (16, 6), (8, 12), (16, 18))
    cr.fill()
    cr.rectangle(5.5, 6, 2, 12)
    cr.fill()


def _step_fwd(cr):
    _poly(cr, (8, 6), (16, 12), (8, 18))
    cr.fill()
    cr.rectangle(16.5, 6, 2, 12)
    cr.fill()


def _mark_in(cr):
    _line(cr, (14, 4), (8, 4), (8, 20), (14, 20))
    cr.stroke()
    _poly(cr, (12, 9), (16.5, 12), (12, 15))
    cr.fill()


def _mark_out(cr):
    _line(cr, (10, 4), (16, 4), (16, 20), (10, 20))
    cr.stroke()
    _poly(cr, (12, 9), (7.5, 12), (12, 15))
    cr.fill()


def _goto_in(cr):
    _line(cr, (9, 5), (5.5, 5), (5.5, 19), (9, 19))
    cr.stroke()
    _arrow(cr, 20, 12, 9.5, 12)
    cr.stroke()


def _goto_out(cr):
    _line(cr, (15, 5), (18.5, 5), (18.5, 19), (15, 19))
    cr.stroke()
    _arrow(cr, 4, 12, 14.5, 12)
    cr.stroke()


def _insert(cr):
    cr.rectangle(3, 5, 6, 5)
    cr.rectangle(15, 5, 6, 5)
    cr.stroke()
    cr.rectangle(9.5, 13, 5, 6)
    cr.fill()
    _arrow(cr, 12, 12.5, 12, 6, 3)
    cr.stroke()


def _overwrite(cr):
    cr.rectangle(3, 5, 18, 5)
    cr.stroke()
    cr.rectangle(8.5, 5, 7, 5)
    cr.fill()
    _arrow(cr, 12, 20, 12, 12.5, 3)
    cr.stroke()


def _lift(cr):
    cr.rectangle(3, 14, 5, 5)
    cr.rectangle(16, 14, 5, 5)
    cr.stroke()
    cr.rectangle(9.5, 4, 5, 5)
    cr.fill()
    _arrow(cr, 12, 18, 12, 11, 3)
    cr.stroke()


def _extract(cr):
    cr.rectangle(3, 14, 5, 5)
    cr.rectangle(16, 14, 5, 5)
    cr.stroke()
    cr.rectangle(9.5, 4, 5, 5)
    cr.fill()
    _arrow(cr, 4, 11, 8.5, 11, 2.5)
    _arrow(cr, 20, 11, 15.5, 11, 2.5)
    cr.stroke()


def _camera(cr):
    cr.rectangle(3.5, 7.5, 17, 11.5)
    cr.stroke()
    _line(cr, (8.5, 7.5), (10, 5), (14, 5), (15.5, 7.5))
    cr.stroke()
    cr.arc(12, 13.2, 3.2, 0, 2 * math.pi)
    cr.stroke()


def _loop(cr):
    cr.arc(12, 12, 6.5, math.radians(-50), math.radians(200))
    cr.stroke()
    _poly(cr, (17.5, 3.5), (18, 9), (13, 7.5))
    cr.fill()


def _magnet(cr):
    cr.move_to(6, 4)
    cr.line_to(6, 12)
    cr.curve_to(6, 20, 18, 20, 18, 12)
    cr.line_to(18, 4)
    cr.line_to(14, 4)
    cr.line_to(14, 12)
    cr.curve_to(14, 14.6, 10, 14.6, 10, 12)
    cr.line_to(10, 4)
    cr.close_path()
    cr.stroke()
    _line(cr, (6, 7.5), (10, 7.5))
    _line(cr, (14, 7.5), (18, 7.5))
    cr.stroke()


def _link(cr):
    cr.save()
    cr.translate(12, 12)
    cr.rotate(-math.pi / 4)
    for x in (-5.5, 5.5):
        cr.new_sub_path()
        cr.arc(x - 1.6, 0, 2.8, math.pi / 2, 3 * math.pi / 2)
        cr.arc(x + 1.6, 0, 2.8, -math.pi / 2, math.pi / 2)
        cr.close_path()
    cr.restore()
    cr.stroke()
    _line(cr, (10, 14), (14, 10))
    cr.stroke()


def _marker(cr):
    _poly(cr, (7, 4), (17, 4), (17, 14), (12, 20), (7, 14))
    cr.fill()


def _wrench(cr):
    cr.arc(12, 12, 3, 0, 2 * math.pi)
    cr.stroke()
    for i in range(8):
        a = i * math.pi / 4
        _line(cr, (12 + 5.2 * math.cos(a), 12 + 5.2 * math.sin(a)),
              (12 + 7.8 * math.cos(a), 12 + 7.8 * math.sin(a)))
    cr.stroke()


def _safe(cr):
    cr.rectangle(3.5, 5.5, 17, 13)
    cr.stroke()
    cr.rectangle(7, 8.5, 10, 7)
    cr.stroke()


def _plus(cr):
    _line(cr, (12, 5), (12, 19))
    _line(cr, (5, 12), (19, 12))
    cr.stroke()


def _minus(cr):
    _line(cr, (5, 12), (19, 12))
    cr.stroke()


def _close(cr):
    _line(cr, (7, 7), (17, 17))
    _line(cr, (17, 7), (7, 17))
    cr.stroke()


def _reset(cr):
    cr.arc(12, 12.5, 6, math.radians(-150), math.radians(120))
    cr.stroke()
    _poly(cr, (3.5, 6), (9.5, 6.5), (6, 11.5))
    cr.fill()


def _up(cr):
    _line(cr, (6, 15), (12, 9), (18, 15))
    cr.stroke()


def _down(cr):
    _line(cr, (6, 9), (12, 15), (18, 9))
    cr.stroke()


def _left(cr):
    _line(cr, (14.5, 6), (8.5, 12), (14.5, 18))
    cr.stroke()


def _right(cr):
    _line(cr, (9.5, 6), (15.5, 12), (9.5, 18))
    cr.stroke()


def _diamond(cr):
    _poly(cr, (12, 5), (19, 12), (12, 19), (5, 12))
    cr.fill()


def _diamond_o(cr):
    _poly(cr, (12, 5.5), (18.5, 12), (12, 18.5), (5.5, 12))
    cr.stroke()


def _stopwatch(cr):
    cr.arc(12, 13.5, 6.5, 0, 2 * math.pi)
    cr.stroke()
    _line(cr, (12, 13.5), (12, 9.5))
    _line(cr, (10, 4), (14, 4))
    _line(cr, (12, 4), (12, 7))
    cr.stroke()


def _list(cr):
    for y in (7, 12, 17):
        _line(cr, (4, y), (6, y))
        _line(cr, (9, y), (20, y))
    cr.stroke()


def _grid(cr):
    for x in (4.5, 13.5):
        for y in (4.5, 13.5):
            cr.rectangle(x, y, 6, 6)
    cr.stroke()


def _folder(cr):
    _poly(cr, (3.5, 6), (9.5, 6), (11.5, 8.5), (20.5, 8.5), (20.5, 18.5), (3.5, 18.5))
    cr.stroke()


def _new_item(cr):
    _poly(cr, (6, 3.5), (14, 3.5), (18, 7.5), (18, 20.5), (6, 20.5))
    cr.stroke()
    _line(cr, (12, 10), (12, 17))
    _line(cr, (8.5, 13.5), (15.5, 13.5))
    cr.stroke()


def _trash(cr):
    _line(cr, (5, 7), (19, 7))
    _line(cr, (9.5, 7), (9.5, 4.5), (14.5, 4.5), (14.5, 7))
    _poly(cr, (6.5, 7), (17.5, 7), (16.5, 20), (7.5, 20))
    cr.stroke()


def _import(cr):
    _line(cr, (4, 15), (4, 19.5), (20, 19.5), (20, 15))
    cr.stroke()
    _arrow(cr, 12, 4, 12, 15)
    cr.stroke()


def _film(cr):
    cr.rectangle(4, 5, 16, 14)
    cr.stroke()
    for y in (8, 12, 16):
        _line(cr, (4, y), (7, y))
        _line(cr, (17, y), (20, y))
    _line(cr, (7, 5), (7, 19))
    _line(cr, (17, 5), (17, 19))
    cr.stroke()


def _wave(cr):
    for x, h in ((4, 3), (7, 7), (10, 4), (13, 9), (16, 5), (19, 2)):
        _line(cr, (x, 12 - h), (x, 12 + h))
    cr.stroke()


def _image(cr):
    cr.rectangle(3.5, 5, 17, 14)
    cr.stroke()
    _line(cr, (3.5, 16), (9, 11), (13, 15), (16, 12.5), (20.5, 16.5))
    cr.stroke()
    cr.arc(15.5, 9, 1.4, 0, 2 * math.pi)
    cr.fill()


def _sequence(cr):
    cr.rectangle(3.5, 6, 11, 4)
    cr.rectangle(9.5, 10, 11, 4)
    cr.rectangle(5.5, 14, 9, 4)
    cr.stroke()


def _title(cr):
    cr.rectangle(3.5, 5, 17, 14)
    cr.stroke()
    _line(cr, (8, 9), (16, 9))
    _line(cr, (12, 9), (12, 16))
    cr.stroke()


def _swatch(cr):
    cr.rectangle(5, 5, 14, 14)
    cr.fill()


def _fx(cr):
    cr.select_font_face("Sans", 1, 1)
    cr.set_font_size(13)
    cr.move_to(4, 16.5)
    cr.show_text("fx")


def _search(cr):
    _zoom(cr)


def _lock(cr):
    cr.rectangle(6, 11, 12, 9)
    cr.fill()
    cr.arc(12, 11, 3.6, math.pi, 2 * math.pi)
    cr.stroke()


def _unlock(cr):
    cr.rectangle(6, 11, 12, 9)
    cr.stroke()
    cr.arc(12, 9.5, 3.6, math.pi, 1.75 * math.pi)
    cr.stroke()


def _eye(cr):
    cr.move_to(3, 12)
    cr.curve_to(7, 5.5, 17, 5.5, 21, 12)
    cr.curve_to(17, 18.5, 7, 18.5, 3, 12)
    cr.stroke()
    cr.arc(12, 12, 2.6, 0, 2 * math.pi)
    cr.fill()


def _eye_off(cr):
    cr.move_to(3, 12)
    cr.curve_to(7, 5.5, 17, 5.5, 21, 12)
    cr.curve_to(17, 18.5, 7, 18.5, 3, 12)
    cr.stroke()
    _line(cr, (5, 19), (19, 5))
    cr.stroke()
def _curve(cr):
    _line(cr, (4, 4), (4, 20), (20, 20))
    cr.stroke()
    cr.move_to(4, 20)
    cr.curve_to(12, 20, 11, 6, 20, 6)
    cr.stroke()


def _fullscreen(cr):
    for (ax, ay), (bx, by), (cx, cy) in (((4, 9), (4, 4), (9, 4)), ((15, 4), (20, 4), (20, 9)),
                                         ((20, 15), (20, 20), (15, 20)), ((9, 20), (4, 20), (4, 15))):
        _line(cr, (ax, ay), (bx, by), (cx, cy))
    cr.stroke()


def _restore(cr):
    for (ax, ay), (bx, by), (cx, cy) in (((4, 9), (9, 9), (9, 4)), ((15, 4), (15, 9), (20, 9)),
                                         ((20, 15), (15, 15), (15, 20)), ((9, 20), (9, 15), (4, 15))):
        _line(cr, (ax, ay), (bx, by), (cx, cy))
    cr.stroke()


ICONS = {
    "select": _select, "track_fwd": _track_fwd, "ripple": _ripple, "roll": _roll, "rate": _rate,
    "razor": _razor, "slip": _slip, "slide": _slide, "pen": _pen, "hand": _hand, "zoom": _zoom,
    "type": _type, "play": _play, "pause": _pause, "step_back": _step_back, "step_fwd": _step_fwd,
    "mark_in": _mark_in, "mark_out": _mark_out, "goto_in": _goto_in, "goto_out": _goto_out,
    "insert": _insert, "overwrite": _overwrite, "lift": _lift, "extract": _extract,
    "camera": _camera, "loop": _loop, "magnet": _magnet, "link": _link, "marker": _marker,
    "wrench": _wrench, "safe": _safe, "plus": _plus, "minus": _minus, "close": _close,
    "reset": _reset, "up": _up, "down": _down, "left": _left, "right": _right,
    "diamond": _diamond, "diamond_o": _diamond_o, "stopwatch": _stopwatch, "list": _list,
    "grid": _grid, "folder": _folder, "new_item": _new_item, "trash": _trash, "import": _import,
    "film": _film, "wave": _wave, "image": _image, "sequence": _sequence, "title": _title,
    "swatch": _swatch, "fx": _fx, "search": _search, "lock": _lock, "unlock": _unlock,
    "eye": _eye, "eye_off": _eye_off, "curve": _curve, "fullscreen": _fullscreen, "restore": _restore,
}

MEDIA_ICONS = {"video": "film", "audio": "wave", "image": "image", "sequence": "sequence",
               "title": "title", "color": "swatch", "bars": "swatch", "tone": "wave", "bin": "folder"}


def draw(cr, name, x, y, size, color):
    """Draw icon ``name`` in a size x size box at (x, y). ``color`` is rgba."""
    fn = ICONS.get(name)
    if fn is None:
        return
    cr.save()
    cr.translate(x, y)
    cr.scale(size / 24.0, size / 24.0)
    cr.set_source_rgba(*color)
    cr.set_line_width(1.7)
    cr.set_line_cap(1)
    cr.set_line_join(1)
    cr.new_path()
    fn(cr)
    cr.restore()


class Icon(Gtk.DrawingArea):
    """An icon widget that takes its colour from CSS."""

    def __init__(self, name, size=16):
        super().__init__()
        self.name = name
        self.size = size
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_name_(self, name):
        if name != self.name:
            self.name = name
            self.queue_draw()

    def _draw(self, area, cr, w, h):
        c = self.get_color()
        draw(cr, self.name, (w - self.size) / 2, (h - self.size) / 2, self.size,
             (c.red, c.green, c.blue, c.alpha))
