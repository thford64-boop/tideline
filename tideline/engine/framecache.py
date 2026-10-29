"""Pre-rendered preview frames kept in RAM.

A frame is stored under the *signature* of what it shows (a hash of the
sequence, the media it uses and the preview settings) and its position, so
an edit never shows a stale picture: it changes the signature, and undoing
it brings the old frames back. The least recently used frames go first when
the budget (Preferences ▸ Frame cache) is full.

Frames get here two ways: every frame the Program monitor shows is kept, and
``Renderer`` renders a range ahead of time on a thread of its own (Sequence ▸
Render In to Out). While the frames ahead of the playhead are cached, the
player stops rendering pictures and plays them from here (see Player).
"""

import ctypes
import hashlib
import json
import threading
from collections import OrderedDict

from . import mltlib
from .mltlib import lib

MB = 1024 * 1024
SIZES = [(0, "Off"), (512, "512 MB"), (1024, "1 GB"), (2048, "2 GB"), (4096, "4 GB"), (8192, "8 GB")]

lib.mlt_service_get_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
lib.mlt_service_get_frame.restype = ctypes.c_int
lib.mlt_producer_seek.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.mlt_frame_close.argtypes = [ctypes.c_void_p]
lib.mlt_properties_set.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]


# What the project stores that never changes a picture: the view, marks,
# names, colour labels and the sound mix. Leaving it out of the signature
# keeps rendered frames through scrolling, marking, renaming and mixing.
_VIEW_ONLY = {
    "sequence": {"name", "markers", "in_point", "out_point", "playhead", "px_per_frame", "scroll_x",
                 "multicam_audio"},
    "track": {"name", "locked", "target", "sync_lock", "height", "volume", "pan", "solo"},
    "clip": {"name", "label", "link_id"},
    "effect": {"collapsed"},
    "media": {"name", "parent", "label", "mark_in", "mark_out"},
}


def _picture_only(kind, d):
    out = {k: v for k, v in d.items() if k not in _VIEW_ONLY[kind]}
    if kind == "sequence":
        for side in ("video_tracks", "audio_tracks"):
            out[side] = [_picture_only("track", t) for t in d.get(side) or []]
    elif kind == "track":
        out["clips"] = [_picture_only("clip", c) for c in d.get("clips") or []]
    elif kind == "clip":
        out["effects"] = [_picture_only("effect", e) for e in d.get("effects") or []]
    return out


def signature(project, seq, extra=()):
    """Hash of everything that decides a sequence's picture."""
    d = project.to_dict()
    data = {"seq": seq.id, "sequences": [_picture_only("sequence", v) for v in d.get("sequences") or []],
            "media": [_picture_only("media", v) for v in d.get("media") or []],
            "settings": d.get("settings"), "extra": list(extra)}
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class FrameCache:
    """LRU store of RGB frames: (signature, position) -> (pixels, w, h)."""

    def __init__(self, budget_mb=0):
        self.budget = int(budget_mb) * MB
        self.used = 0
        self.version = 0             # bumped on every change, for views that draw coverage
        self._frames = OrderedDict()
        self._lock = threading.Lock()
        self._ranges = (None, [])

    @property
    def enabled(self):
        return self.budget > 0

    def set_budget(self, budget_mb):
        with self._lock:
            self.budget = int(budget_mb) * MB
            self._trim()

    def get(self, sig, pos):
        with self._lock:
            item = self._frames.get((sig, pos))
            if item is not None:
                self._frames.move_to_end((sig, pos))
            return item

    def has(self, sig, pos):
        return (sig, pos) in self._frames

    def put(self, sig, pos, pixels, w, h):
        if not self.budget or sig is None or len(pixels) > self.budget:
            return
        key = (sig, pos)
        with self._lock:
            old = self._frames.pop(key, None)
            if old is not None:
                self.used -= len(old[0])
            self._frames[key] = (pixels, w, h)
            self.used += len(pixels)
            self._trim()
            self.version += 1

    def _trim(self):
        while self.used > self.budget and self._frames:
            _, (pixels, _, _) = self._frames.popitem(last=False)
            self.used -= len(pixels)
            self.version += 1

    def clear(self):
        with self._lock:
            self._frames.clear()
            self.used = 0
            self.version += 1

    def run_ahead(self, sig, pos, limit):
        """How many frames from ``pos`` on are cached, up to ``limit``."""
        n = 0
        while n < limit and (sig, pos + n) in self._frames:
            n += 1
        return n

    def ranges(self, sig):
        """Cached frames of a signature as sorted [(first, last + 1)] runs."""
        if sig is None:
            return []
        if self._ranges[0] == (sig, self.version):
            return self._ranges[1]
        with self._lock:
            frames = sorted(p for s, p in self._frames if s == sig)
        out = []
        for p in frames:
            if out and out[-1][1] == p:
                out[-1][1] = p + 1
            else:
                out.append([p, p + 1])
        out = [tuple(r) for r in out]
        self._ranges = ((sig, self.version), out)
        return out

    def frames_of(self, sig):
        return sum(b - a for a, b in self.ranges(sig))


class Renderer:
    """Renders frames ``first`` to ``last`` (inclusive) of a built graph into
    a FrameCache on a thread of its own, the way the Program monitor would.

    ``progress(done, total)`` and ``finished(cancelled)`` are called on that
    thread; the caller hands them to the main loop.
    """

    def __init__(self, graph, cache, sig, first, last, size, rescale, progress=None, finished=None):
        self.graph, self.cache, self.sig = graph, cache, sig
        self.first, self.last = int(first), int(last)
        self.size, self.rescale = size, rescale
        self.progress, self.finished = progress, finished
        self.cancelled = False
        self.done = 0
        self.thread = None

    @property
    def total(self):
        return max(0, self.last - self.first + 1)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="tideline-prerender", daemon=True)
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout)

    def render(self, pos):
        """RGB bytes of one frame (the calls release the GIL while MLT works)."""
        w, h = self.size
        service = mltlib.ptr(self.graph.tractor.get_service())
        producer = mltlib.ptr(self.graph.tractor.get_producer())
        lib.mlt_producer_seek(producer, int(pos))
        frame = ctypes.c_void_p()
        if lib.mlt_service_get_frame(service, ctypes.byref(frame), 0) or not frame:
            return None
        try:
            lib.mlt_properties_set(frame, b"consumer.rescale", self.rescale.encode())
            lib.mlt_properties_set(frame, b"consumer.progressive", b"1")
            cw, ch = ctypes.c_int(w), ctypes.c_int(h)
            fmt = ctypes.c_int(mltlib.IMAGE_RGB)
            buf = ctypes.POINTER(ctypes.c_uint8)()
            if lib.mlt_frame_get_image(frame, ctypes.byref(buf), ctypes.byref(fmt), ctypes.byref(cw),
                                       ctypes.byref(ch), 0) or not buf or fmt.value != mltlib.IMAGE_RGB:
                return None
            return ctypes.string_at(buf, cw.value * ch.value * 3), cw.value, ch.value
        finally:
            lib.mlt_frame_close(frame)

    def _run(self):
        try:
            for pos in range(self.first, self.last + 1):
                if self.cancelled:
                    break
                if not self.cache.has(self.sig, pos):
                    out = self.render(pos)
                    if out is not None:
                        self.cache.put(self.sig, pos, *out)
                self.done += 1
                if self.progress:
                    self.progress(self.done, self.total)
                # A full cache would throw away the start of the range again.
                if self.cache.used + self.size[0] * self.size[1] * 3 > self.cache.budget and \
                        self.cache.run_ahead(self.sig, self.first, self.done) < self.done:
                    break
        finally:
            if self.finished:
                self.finished(self.cancelled)
