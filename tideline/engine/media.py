"""Media probing, thumbnails and audio waveform peaks.

Everything slow runs in FFmpeg subprocesses on a small thread pool, with
results cached on disk under ``$XDG_CACHE_HOME/tideline``.
"""

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..model import LABEL_AUDIO, LABEL_VIDEO, MediaItem

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".exr", ".tga",
             ".jxl", ".avif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mxf", ".mts", ".m2ts", ".mpg",
             ".mpeg", ".wmv", ".flv", ".ogv", ".ts", ".3gp", ".gif"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".aif", ".aiff", ".wma", ".ac3"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT

PEAKS_PER_SECOND = 100
_PEAK_RATE = 8000

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "tideline")
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tideline-media")


class ProbeError(Exception):
    pass


def _fraction(text):
    try:
        num, _, den = (text or "0").partition("/")
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


def probe(path):
    """Inspect a file with ffprobe and return a MediaItem (not yet in a project)."""
    name = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if not os.path.isfile(path):
        raise ProbeError(f"{name}: file not found")
    if ext == ".svg":
        return MediaItem(name=name, kind="image", path=path, has_video=True, label=LABEL_VIDEO)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"{name}: could not run ffprobe ({exc})") from exc
    if out.returncode != 0:
        raise ProbeError(f"{name}: not a supported media file")
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise ProbeError(f"{name}: no video or audio streams")
    duration = float(fmt.get("duration") or 0) or max(
        (float(s.get("duration") or 0) for s in streams), default=0.0)
    item = MediaItem(name=name, kind="video", path=path, duration=duration)
    if video is not None:
        item.has_video = True
        item.width = int(video.get("width") or 0)
        item.height = int(video.get("height") or 0)
        rotation = 0
        for sd in video.get("side_data_list", []):
            rotation = int(sd.get("rotation", rotation) or 0)
        if abs(rotation) in (90, 270):
            item.width, item.height = item.height, item.width
        item.fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
        item.video_codec = video.get("codec_name", "")
        item.colorspace = video.get("color_space", "") or ""
        frames = video.get("nb_frames")
        fmt_name = fmt.get("format_name", "")
        still = ext in IMAGE_EXT or fmt_name.endswith("_pipe") or fmt_name == "image2" \
            or (audio is None and frames in ("1", 1) and ext not in VIDEO_EXT)
        if still:
            item.kind, item.duration, item.fps = "image", 0.0, 0.0
    if audio is not None:
        item.has_audio = True
        item.audio_codec = audio.get("codec_name", "")
        item.channels = int(audio.get("channels") or 0)
        item.sample_rate = int(audio.get("sample_rate") or 0)
    if video is None:
        item.kind = "audio"
    item.label = LABEL_AUDIO if item.kind == "audio" else LABEL_VIDEO
    if item.kind != "image" and item.duration <= 0:
        raise ProbeError(f"{name}: could not determine duration")
    return item


def _cache_path(kind, key, ext):
    d = os.path.join(CACHE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(key.encode()).hexdigest()[:20] + ext)


def _file_key(path):
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return path


def thumbnail_png(path, seconds, height, is_image=False):
    """PNG bytes of a frame scaled to ``height`` (cached). None on failure."""
    cache = _cache_path("thumbs", f"{_file_key(path)}|{seconds:.2f}|{height}", ".png")
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            return fh.read()
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if not is_image:
        cmd += ["-ss", f"{max(0.0, seconds):.3f}"]
    cmd += ["-i", path, "-frames:v", "1", "-vf", f"scale=-2:{height}", "-f", "image2pipe",
            "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    try:
        with open(cache, "wb") as fh:
            fh.write(out.stdout)
    except OSError:
        pass
    return out.stdout


def waveform_peaks(path):
    """uint8 array of absolute peaks, PEAKS_PER_SECOND per second (cached)."""
    cache = _cache_path("peaks", _file_key(path), ".npy")
    if os.path.exists(cache):
        try:
            return np.load(cache)
        except (OSError, ValueError):
            pass
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(_PEAK_RATE),
           "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    samples = np.frombuffer(out.stdout, dtype=np.int16)
    block = _PEAK_RATE // PEAKS_PER_SECOND
    n = len(samples) // block
    if n == 0:
        return None
    peaks = np.abs(samples[:n * block].astype(np.int32)).reshape(n, block).max(axis=1)
    peaks = np.minimum(255, peaks * 255 // 32768).astype(np.uint8)
    try:
        np.save(cache, peaks)
    except OSError:
        pass
    return peaks


class MediaCache:
    """In-memory thumbnails and waveforms, loaded asynchronously.

    ``notify`` is called on the main loop whenever something new has arrived,
    so views can redraw.
    """

    def __init__(self, notify=None):
        self.notify = notify
        self._thumbs = {}        # key -> texture | None (pending) | False (failed)
        self._png = {}           # key -> PNG bytes, for cairo surfaces
        self._surfaces = {}
        self._peaks = {}         # path -> ndarray | None (pending) | False (failed)

    def thumbnail(self, media, seconds=None, height=90):
        """A Gdk.Texture for a media item, or None while loading/unavailable."""
        if media is None or not media.path or not media.has_video or media.offline:
            return None
        if seconds is None:
            seconds = 0.0 if media.kind == "image" else min(1.0, media.duration * 0.1)
        seconds = round(seconds, 1)
        key = (media.path, seconds, height)
        if key in self._thumbs:
            return self._thumbs[key] or None
        self._thumbs[key] = None
        _pool.submit(self._load_thumb, key, media.kind == "image")
        return None

    def _load_thumb(self, key, is_image):
        from gi.repository import GLib
        data = thumbnail_png(key[0], key[1], key[2], is_image)
        GLib.idle_add(self._thumb_done, key, data)

    def _thumb_done(self, key, data):
        from gi.repository import Gdk, GLib
        tex = False
        if data:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                tex = False
        self._thumbs[key] = tex
        if tex:
            self._png[key] = data
        if tex and self.notify:
            self.notify()
        return False

    def thumbnail_surface(self, media, seconds=None, height=54):
        """Like ``thumbnail`` but as a cairo surface for custom drawing."""
        if self.thumbnail(media, seconds, height) is None:
            return None
        if seconds is None:
            seconds = 0.0 if media.kind == "image" else min(1.0, media.duration * 0.1)
        key = (media.path, round(seconds, 1), height)
        surf = self._surfaces.get(key)
        if surf is None and key in self._png:
            import io

            import cairo
            try:
                surf = cairo.ImageSurface.create_from_png(io.BytesIO(self._png[key]))
            except (cairo.Error, OSError):
                surf = False
            self._surfaces[key] = surf
        return surf or None

    def peaks(self, media):
        """Waveform peaks for a media item, or None while loading/unavailable."""
        if media is None or not media.path or not media.has_audio or media.offline:
            return None
        if media.path in self._peaks:
            p = self._peaks[media.path]
            return p if isinstance(p, np.ndarray) else None
        self._peaks[media.path] = None
        _pool.submit(self._load_peaks, media.path)
        return None

    def _load_peaks(self, path):
        from gi.repository import GLib
        data = waveform_peaks(path)
        GLib.idle_add(self._peaks_done, path, data)

    def _peaks_done(self, path, data):
        self._peaks[path] = data if data is not None else False
        if data is not None and self.notify:
            self.notify()
        return False

    def forget(self, path):
        self._peaks.pop(path, None)
        for key in [k for k in self._thumbs if k[0] == path]:
            del self._thumbs[key]
            self._png.pop(key, None)
            self._surfaces.pop(key, None)
