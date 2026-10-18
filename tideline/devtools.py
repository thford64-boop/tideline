"""Developer helpers: drive the running app from a script and take
screenshots of its window without touching the desktop.

Run with ``TIDELINE_DEBUG_SCRIPT=path/to/script.py``. The script defines
``main(app)`` as a generator; each ``yield <milliseconds>`` lets the UI run.
"""

import os
import runpy
import traceback

from gi.repository import GLib, Gtk


def screenshot(widget, path):
    """Render a mapped widget (usually the window) to a PNG file."""
    w, h = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        # The paintable has no image while a relayout is pending.
        return False
    native = widget.get_native()
    texture = native.get_renderer().render_texture(node, None)
    texture.save_to_png(path)
    return True


def run_script(app, path):
    ns = runpy.run_path(path)
    gen = ns["main"](app)

    def step():
        try:
            delay = next(gen)
        except StopIteration:
            return False
        except Exception:
            traceback.print_exc()
            app.quit()
            return False
        GLib.timeout_add(int(delay or 1), step)
        return False

    GLib.timeout_add(400, step)


def maybe_run(app):
    path = os.environ.get("TIDELINE_DEBUG_SCRIPT")
    if path:
        run_script(app, path)
