"""MLT access: the SWIG bindings for building graphs plus a few ctypes entry
points for the calls that must not hold the Python GIL (anything that waits on
an MLT thread) or that SWIG does not expose (event listeners, raw frame data).
"""

import ctypes
import ctypes.util
import os

# MLT's Qt-based services (text, crop, shadows) create a QApplication on first
# use; it must render off-screen instead of connecting to the display from an
# MLT worker thread.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import mlt7 as mlt  # noqa: E402

vp = ctypes.c_void_p
LOG_QUIET, LOG_FATAL, LOG_ERROR, LOG_WARNING, LOG_INFO = -8, 8, 16, 24, 32


class _EvU(ctypes.Union):
    _fields_ = [("i", ctypes.c_int), ("p", vp)]


class EventData(ctypes.Structure):
    _fields_ = [("u", _EvU)]


LISTENER = ctypes.CFUNCTYPE(None, vp, vp, EventData)

try:
    # The bindings above have loaded the library already; this finds that copy
    # (also inside an AppImage, where ldconfig would not know about it).
    lib = ctypes.CDLL("libmlt-7.so.7")
except OSError:
    lib = ctypes.CDLL(ctypes.util.find_library("mlt-7") or "libmlt-7.so.7")
lib.mlt_log_set_level.argtypes = [ctypes.c_int]
lib.mlt_events_listen.restype = vp
lib.mlt_events_listen.argtypes = [vp, vp, ctypes.c_char_p, LISTENER]
lib.mlt_frame_get_image.restype = ctypes.c_int
lib.mlt_frame_get_image.argtypes = [
    vp, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)), ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]
lib.mlt_frame_get_position.restype = ctypes.c_int
lib.mlt_frame_get_position.argtypes = [vp]
lib.mlt_properties_get_double.restype = ctypes.c_double
lib.mlt_properties_get_double.argtypes = [vp, ctypes.c_char_p]
lib.mlt_properties_set_int.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
for _fn in ("mlt_consumer_start", "mlt_consumer_stop", "mlt_consumer_purge",
            "mlt_consumer_is_stopped"):
    getattr(lib, _fn).argtypes = [vp]
    getattr(lib, _fn).restype = ctypes.c_int

IMAGE_RGB = mlt.mlt_image_rgb
IMAGE_RGBA = mlt.mlt_image_rgba

_initialised = False


def init():
    global _initialised
    if not _initialised:
        debug = bool(os.environ.get("TIDELINE_DEBUG"))
        # Loading the plugin repository reports every duplicate frei0r
        # registration; keep that noise out of the terminal.
        lib.mlt_log_set_level(LOG_INFO if debug else LOG_QUIET)
        mlt.Factory.init()
        lib.mlt_log_set_level(LOG_INFO if debug else LOG_ERROR)
        _initialised = True


def ptr(swig_pointer):
    """Raw address of a SWIG-wrapped MLT pointer."""
    return int(swig_pointer)


def make_profile(width, height, fps_num, fps_den=1, colorspace=709):
    def gcd(a, b):
        while b:
            a, b = b, a % b
        return a
    p = mlt.Profile()
    p.set_explicit(1)
    p.set_width(int(width))
    p.set_height(int(height))
    p.set_frame_rate(int(fps_num), int(fps_den))
    p.set_sample_aspect(1, 1)
    g = gcd(int(width), int(height)) or 1
    p.set_display_aspect(int(width) // g, int(height) // g)
    p.set_progressive(1)
    p.set_colorspace(colorspace)
    return p


_service_cache = {}


def has_service(kind, name):
    """True when an MLT filter/transition/producer of that name can be created."""
    key = (kind, name)
    if key not in _service_cache:
        init()
        prof = make_profile(320, 180, 25)
        try:
            if kind == "filter":
                ok = mlt.Filter(prof, name).is_valid()
            elif kind == "transition":
                ok = mlt.Transition(prof, name).is_valid()
            else:
                ok = mlt.Producer(prof, name).is_valid()
        except Exception:
            ok = False
        _service_cache[key] = bool(ok)
    return _service_cache[key]


def color_to_mlt(value, default="#ff000000"):
    """'#RRGGBB' or '#RRGGBBAA' -> MLT's '#AARRGGBB'."""
    if not isinstance(value, str) or not value.startswith("#"):
        return default
    h = value[1:]
    if len(h) == 6:
        return "#ff" + h.lower()
    if len(h) == 8:
        return "#" + (h[6:8] + h[0:6]).lower()
    return default
