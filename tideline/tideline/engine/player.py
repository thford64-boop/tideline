"""Real-time playback of an MLT producer into the GTK main loop.

MLT's ``sdl2_audio`` consumer owns audio output and frame timing. Each time it
is ready to show a frame it fires ``consumer-frame-show`` on one of its own
threads; the listener copies the picture out and hands it to the main loop.

Every call that can wait on a consumer thread (start, stop, purge) goes through
ctypes so the GIL is released while waiting, otherwise the consumer thread
could block forever trying to enter the frame listener.
"""

import ctypes
import threading
import traceback

from gi.repository import GLib

from . import mltlib
from .mltlib import lib, mlt


class Player:
    _active = None               # the player currently holding the audio device

    def __init__(self, name="player"):
        mltlib.init()
        self.name = name
        self.on_frame = None     # callable(bytes, width, height, position)
        self.on_state = None     # callable(playing: bool)
        self.on_levels = None    # callable(left_db, right_db, playing)
        self.position = 0
        self.length = 1
        self.speed = 0.0
        self.scale = 0.5         # preview resolution factor
        self.scaling = "bilinear"   # MLT rescale method for the preview picture
        self.threads = 1         # render threads (MLT parallel consumer)
        self.drop_frames = True  # skip frames to keep up with real time
        self.volume = 1.0
        self.loop = False
        self.loop_range = None   # (in, out) frames for loop playback
        self._producer = None
        self._profile = None
        self._consumer = None
        self._preview_profile = None
        self._size = (0, 0)
        self._refs = None
        self._lock = threading.Lock()
        self._pending = None
        self._scheduled = False
        self._listener = mltlib.LISTENER(self._frame_show)
        self._event = None

    # -- graph -----------------------------------------------------------
    @property
    def has_producer(self):
        return self._producer is not None

    @property
    def playing(self):
        return self.speed != 0.0

    @property
    def fps(self):
        return self._profile.fps() if self._profile is not None else 30.0

    def set_producer(self, producer, profile, length=None, position=0, refs=None, resume=False):
        """Swap in a new producer. ``refs`` keeps its Python owners alive."""
        was_playing = self.speed if resume else 0.0
        self._stop_consumer()
        old = (self._producer, self._refs)
        self._producer = producer
        self._refs = refs
        new_profile = profile is not self._profile
        self._profile = profile
        with self._lock:
            self._pending = None
        if producer is None:
            self.length, self.position, self.speed = 1, 0, 0.0
            del old
            return
        self.length = max(1, int(length if length is not None else producer.get_length()))
        self.position = max(0, min(int(position), self.length - 1))
        if new_profile or self._consumer is None or self._stale_consumer():
            self._make_consumer()
        producer.set_speed(0)
        producer.seek(self.position)
        self._consumer.connect(producer)
        self.speed = 0.0
        del old
        if Player._active is self or Player._active is None:
            self._start_consumer()
            if was_playing:
                self.play(was_playing)

    def _stale_consumer(self):
        return self._size != self._preview_size()

    def _preview_size(self):
        w = max(64, int(round(self._profile.width() * self.scale / 2)) * 2)
        h = max(36, int(round(self._profile.height() * self.scale / 2)) * 2)
        return w, h

    def _make_consumer(self):
        self._stop_consumer()
        p = self._profile
        w, h = self._preview_size()
        pp = mltlib.make_profile(w, h, p.frame_rate_num(), p.frame_rate_den())
        pp.set_display_aspect(p.display_aspect_num(), p.display_aspect_den())
        consumer = mlt.Consumer(pp, "sdl2_audio")
        if not consumer.is_valid():
            raise RuntimeError("MLT's sdl2_audio consumer is unavailable (install sdl2 / sdl2-compat).")
        # real_time: N render threads; negative waits for every frame
        # instead of dropping late ones.
        real_time = self.threads if self.drop_frames else -self.threads
        for k, v in (("real_time", real_time), ("rescale", self.scaling), ("mlt_image_format", "rgb"),
                     ("buffer", 5 + self.threads), ("prefill", 1), ("scrub_audio", 0), ("channels", 2),
                     ("frequency", 48000), ("progressive", 1), ("terminate_on_pause", 0),
                     ("volume", float(self.volume))):
            consumer.set(k, v)
        self._event = lib.mlt_events_listen(mltlib.ptr(consumer.get_properties()), None,
                                            b"consumer-frame-show", self._listener)
        self._consumer, self._preview_profile, self._size = consumer, pp, (w, h)

    def _cptr(self):
        return mltlib.ptr(self._consumer.get_consumer())

    def _start_consumer(self):
        if self._consumer is not None and self._producer is not None:
            if lib.mlt_consumer_is_stopped(self._cptr()):
                lib.mlt_consumer_start(self._cptr())
            Player._active = self

    def _stop_consumer(self):
        if self._consumer is not None and not lib.mlt_consumer_is_stopped(self._cptr()):
            lib.mlt_consumer_stop(self._cptr())

    def activate(self):
        """Take the audio device from the other monitor's player."""
        other = Player._active
        if other is not None and other is not self:
            other.suspend()
        if self._producer is not None:
            self._start_consumer()

    def suspend(self):
        if self.speed != 0.0:
            self._set_speed(0.0)
        self._stop_consumer()
        if Player._active is self:
            Player._active = None

    def set_scale(self, scale):
        if abs(scale - self.scale) < 1e-6:
            return
        self.scale = scale
        self._reconfigure()

    def configure(self, scale=None, scaling=None, threads=None, drop_frames=None):
        """Change playback quality; the consumer is recreated only if needed."""
        old = (self.scale, self.scaling, self.threads, self.drop_frames)
        if scale is not None:
            self.scale = scale
        if scaling is not None:
            self.scaling = scaling
        if threads is not None:
            self.threads = max(1, int(threads))
        if drop_frames is not None:
            self.drop_frames = bool(drop_frames)
        if (self.scale, self.scaling, self.threads, self.drop_frames) != old:
            self._reconfigure()

    def _reconfigure(self):
        if self._producer is not None:
            active = Player._active is self
            self._make_consumer()
            self._consumer.connect(self._producer)
            if active:
                self._start_consumer()
                self.refresh()

    def set_volume(self, volume):
        self.volume = max(0.0, min(2.0, volume))
        if self._consumer is not None:
            self._consumer.set("volume", float(self.volume))

    # -- transport -------------------------------------------------------
    def _set_speed(self, speed):
        changed = (speed != 0.0) != (self.speed != 0.0)
        self.speed = speed
        if self._producer is not None:
            self._producer.set_speed(speed)
        if changed and self.on_state:
            self.on_state(speed != 0.0)

    def play(self, speed=1.0):
        if self._producer is None:
            return
        self.activate()
        if speed > 0 and self.position >= self.length - 1:
            self.seek(self.loop_range[0] if self.loop and self.loop_range else 0)
        elif speed < 0 and self.position <= 0:
            return
        self._set_speed(speed)
        lib.mlt_consumer_purge(self._cptr())
        self.refresh()

    def pause(self):
        if self._producer is None or self.speed == 0.0:
            return
        self._set_speed(0.0)
        lib.mlt_consumer_purge(self._cptr())
        self._producer.seek(self.position)
        self.refresh()

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play(1.0)

    def shuttle(self, direction):
        """J/L style shuttle: repeated presses step through 1x, 2x, 4x, 8x."""
        steps = (1.0, 2.0, 4.0, 8.0)
        cur = self.speed
        if cur * direction > 0 and abs(cur) in steps:
            nxt = steps[min(len(steps) - 1, steps.index(abs(cur)) + 1)] * direction
        else:
            nxt = 1.0 * direction
        self.play(nxt)

    def seek(self, position):
        if self._producer is None:
            return
        position = max(0, min(int(position), self.length - 1))
        self.position = position
        self.activate()
        self._producer.seek(position)
        lib.mlt_consumer_purge(self._cptr())
        self.refresh()

    def refresh(self):
        """Ask the consumer to render the current frame again."""
        if self._consumer is not None and Player._active is self:
            lib.mlt_properties_set_int(mltlib.ptr(self._consumer.get_properties()), b"refresh", 1)

    def close(self):
        self._stop_consumer()
        if Player._active is self:
            Player._active = None
        self._consumer = None
        self._producer = None
        self._refs = None

    # -- frame delivery ---------------------------------------------------
    def _frame_show(self, owner, data, event):
        # Runs on an MLT consumer thread.
        try:
            frame = event.u.p
            if not frame:
                return
            w, h = self._size
            cw, ch = ctypes.c_int(w), ctypes.c_int(h)
            fmt = ctypes.c_int(mltlib.IMAGE_RGB)
            buf = ctypes.POINTER(ctypes.c_uint8)()
            if lib.mlt_frame_get_image(frame, ctypes.byref(buf), ctypes.byref(fmt),
                                       ctypes.byref(cw), ctypes.byref(ch), 0) or not buf:
                return
            if fmt.value != mltlib.IMAGE_RGB:
                return
            pixels = ctypes.string_at(buf, cw.value * ch.value * 3)
            pos = lib.mlt_frame_get_position(frame)
            left = lib.mlt_properties_get_double(frame, b"meta.media.audio_level.0")
            right = lib.mlt_properties_get_double(frame, b"meta.media.audio_level.1")
            with self._lock:
                self._pending = (pixels, cw.value, ch.value, pos, left, right)
                schedule = not self._scheduled
                self._scheduled = True
            if schedule:
                GLib.idle_add(self._deliver, priority=GLib.PRIORITY_DEFAULT)
        except Exception:
            traceback.print_exc()

    def _deliver(self):
        with self._lock:
            item, self._pending = self._pending, None
            self._scheduled = False
        if item is None or self._producer is None:
            return False
        pixels, w, h, pos, left, right = item
        if self.speed != 0.0:
            self.position = pos
        if self.on_frame:
            self.on_frame(pixels, w, h, pos)
        if self.on_levels:
            self.on_levels(left, right, self.speed != 0.0)
        if self.speed > 0:
            end = self.length - 1
            if self.loop and self.loop_range:
                lo, hi = self.loop_range
                if pos >= min(hi, self.length) - 1:
                    self.seek(lo)
                    return False
            if pos >= end:
                if self.loop:
                    self.seek(0)
                else:
                    self.pause()
        elif self.speed < 0 and pos <= 0:
            self.pause()
        return False
