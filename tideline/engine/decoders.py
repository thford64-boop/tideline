"""Decoders for the media work done outside MLT: probing files, thumbnails
and audio waveforms (also the sound multi-camera syncing listens to).

FFmpeg's command-line tools are always there and always the fallback. PyAV,
OpenCV and Decord can be chosen instead in Preferences when they are
installed, system-wide or into Tideline's own folder for them
(``user_site()``, where Preferences ▸ Install puts them). Each does what it
can and hands the rest to FFmpeg:

==========  =======  ==========  ==========
decoder     probe    thumbnails  waveforms
==========  =======  ==========  ==========
FFmpeg      yes      yes         yes
PyAV        yes      yes         yes
OpenCV      FFmpeg   yes         FFmpeg
Decord      FFmpeg   video only  yes
==========  =======  ==========  ==========

Playback and export always decode with MLT's own FFmpeg producer; that is
what keeps the preview identical to the export.
"""

import json
import os
import struct
import subprocess
import sys
import threading
import zlib
from collections import OrderedDict

import numpy as np

BACKENDS = [("ffmpeg", "FFmpeg"), ("pyav", "PyAV"), ("opencv", "OpenCV"), ("decord", "Decord")]
LABELS = dict(BACKENDS)
MODULES = {"pyav": "av", "opencv": "cv2", "decord": "decord"}
PIP_NAMES = {"pyav": "av", "opencv": "opencv-python-headless", "decord": "decord"}
CAN = {"ffmpeg": {"probe", "thumbnail", "audio"}, "pyav": {"probe", "thumbnail", "audio"},
       "opencv": {"thumbnail"}, "decord": {"thumbnail", "audio"}}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".exr", ".tga",
             ".jxl", ".avif", ".heic"}

current = "ffmpeg"
_found = {}
_lock = threading.Lock()


class DecodeError(Exception):
    pass


def user_site():
    """Folder that Tideline installs optional decoders into (one per Python
    version, since their compiled parts are tied to it)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "tideline", f"python{sys.version_info[0]}.{sys.version_info[1]}")


def _add_user_site():
    # Appended, so the system's own numpy (and anything else) keeps priority.
    site = user_site()
    if os.path.isdir(site) and site not in sys.path:
        sys.path.append(site)


_add_user_site()


def available(name):
    """True when the decoder can be used (its Python module imports)."""
    if name == "ffmpeg":
        return True
    if name not in _found:
        try:
            __import__(MODULES[name])
            _found[name] = True
        except Exception:
            _found[name] = False
    return _found[name]


def rescan():
    """Forget which decoders were found (after installing one)."""
    _add_user_site()
    import importlib
    importlib.invalidate_caches()
    _found.clear()


def set_backend(name):
    global current
    current = name if name in LABELS else "ffmpeg"


def active(job):
    """The decoder that does ``job`` ("probe", "thumbnail", "audio") now."""
    return current if current != "ffmpeg" and job in CAN[current] and available(current) else "ffmpeg"


def install_command(name):
    """Command that installs a decoder into ``user_site()``, or None when
    neither pip nor uv is there. numpy is left out: Tideline has its own."""
    import importlib.util
    import shutil
    args = ["install", "--upgrade", "--no-deps", "--target", user_site(), PIP_NAMES[name]]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"] + args
    if shutil.which("uv"):
        return ["uv", "pip"] + args[:1] + ["--python", sys.executable] + args[1:]
    return None


def install(name):
    """Install a decoder (blocking; run it on a thread). (ok, message)."""
    cmd = install_command(name)
    if cmd is None:
        return False, (f"Neither pip nor uv was found. Install it yourself with: "
                       f"pip install --target {user_site()} {PIP_NAMES[name]}")
    os.makedirs(user_site(), exist_ok=True)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not install {LABELS[name]}: {exc}"
    rescan()
    if out.returncode != 0 or not available(name):
        lines = (out.stderr or out.stdout or "").strip().splitlines()
        return False, f"Could not install {LABELS[name]}: " + (lines[-1] if lines else f"exit {out.returncode}")
    return True, f"{LABELS[name]} is installed."


def _run(job, path, *args):
    """Run ``job`` with the chosen decoder; FFmpeg when it cannot."""
    name = active(job)
    if name != "ffmpeg":
        try:
            out = _IMPL[name][job](path, *args)
            if out is not None:
                return out
        except Exception:
            pass
    return _IMPL["ffmpeg"][job](path, *args)


def probe_info(path):
    """ffprobe-style ``{"format": {...}, "streams": [...]}`` for a file."""
    return _run("probe", path)


def thumbnail_png(path, seconds, height, is_image=False):
    """PNG bytes of the frame at ``seconds`` scaled to ``height``, or None."""
    return _run("thumbnail", path, seconds, height, is_image)


def audio_mono(path, rate, seconds=None):
    """The file's sound mixed to mono at ``rate`` Hz as int16 samples, or None."""
    return _run("audio", path, rate, seconds)


# -- helpers -------------------------------------------------------------------
def encode_png(rgb):
    """PNG bytes of an (h, w, 3) uint8 array."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    raw = np.empty((h, w * 3 + 1), np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def _even_width(w, h, height):
    """Width for ``height`` keeping the shape, rounded to even like scale=-2."""
    return max(2, int(round(w * height / max(1, h) / 2)) * 2)


def _rate_text(rate):
    if not rate:
        return "0/0"
    return f"{rate.numerator}/{rate.denominator}" if hasattr(rate, "numerator") else str(rate)


# -- FFmpeg (command-line tools) ---------------------------------------------------
def _ff_probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecodeError(f"could not run ffprobe ({exc})") from exc
    if out.returncode != 0:
        raise DecodeError("not a supported media file")
    return json.loads(out.stdout or "{}")


def _ff_thumbnail(path, seconds, height, is_image):
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if not is_image:
        cmd += ["-ss", f"{max(0.0, seconds):.3f}"]
    cmd += ["-i", path, "-frames:v", "1", "-vf", f"scale=-2:{height}", "-f", "image2pipe",
            "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


def _ff_audio(path, rate, seconds):
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le"]
    if seconds:
        cmd += ["-t", f"{float(seconds):.3f}"]
    try:
        out = subprocess.run(cmd + ["-"], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return np.frombuffer(out.stdout, dtype=np.int16)


# -- PyAV ----------------------------------------------------------------------------
def _av_probe(path):
    import av
    with av.open(path) as c:
        streams = []
        for s in c.streams:
            cc = s.codec_context
            d = {"codec_type": s.type, "codec_name": getattr(cc, "name", "") or "",
                 "duration": float(s.duration * s.time_base) if s.duration and s.time_base else 0.0}
            if s.type == "video":
                d.update(width=cc.width, height=cc.height, avg_frame_rate=_rate_text(s.average_rate),
                         r_frame_rate=_rate_text(s.base_rate or s.guessed_rate),
                         nb_frames=str(s.frames) if s.frames else None,
                         disposition={"attached_pic": int(bool(s.disposition & av.stream.Disposition.attached_pic))}
                         if hasattr(av.stream, "Disposition") else {})
                space = getattr(cc, "colorspace", None)
                d["color_space"] = {1: "bt709", 5: "bt470bg", 6: "smpte170m", 9: "bt2020nc"}.get(
                    int(space) if space is not None else 0, "")
                rotation = _av_rotation(c, s)
                if rotation:
                    d["side_data_list"] = [{"rotation": rotation}]
            elif s.type == "audio":
                d.update(channels=cc.channels, sample_rate=str(cc.sample_rate))
            streams.append(d)
        fmt = {"format_name": c.format.name, "duration": c.duration / 1e6 if c.duration else 0.0}
    return {"format": fmt, "streams": streams}


def _av_rotation(container, stream):
    """Display rotation of a video stream (ffprobe's sign), from its first frame."""
    try:
        frame = next(container.decode(stream))
        return int(round(getattr(frame, "rotation", 0) or 0))
    except Exception:
        return 0
    finally:
        try:
            container.seek(0)
        except Exception:
            pass


def _av_thumbnail(path, seconds, height, is_image):
    import av
    with av.open(path) as c:
        vs = next((s for s in c.streams.video), None)
        if vs is None:
            return None
        vs.thread_type = "AUTO"
        if not is_image and seconds > 0 and vs.time_base:
            c.seek(int(seconds / vs.time_base), stream=vs, backward=True, any_frame=False)
        frame = None
        for f in c.decode(vs):
            frame = f
            if is_image or f.time is None or f.time >= seconds - 0.5 / float(vs.average_rate or 30):
                break
        if frame is None:
            return None
        rotation = int(round(getattr(frame, "rotation", 0) or 0)) % 360
        w, h = frame.width, frame.height
        turned = rotation in (90, 270)
        tw, th = (h, w) if turned else (w, h)
        out_w = _even_width(tw, th, height)
        img = frame.reformat(width=height if turned else out_w, height=out_w if turned else height,
                             format="rgb24", interpolation="BICUBIC").to_ndarray()
    if rotation:
        img = np.rot90(img, k=rotation // 90)
    return encode_png(img)


def _av_audio(path, rate, seconds):
    import av
    chunks = []
    with av.open(path) as c:
        if not c.streams.audio:
            return None
        stream = c.streams.audio[0]
        res = av.AudioResampler(format="s16", layout="mono", rate=rate)
        limit = int(seconds * rate) if seconds else None
        got = 0
        for frame in c.decode(stream):
            for out in res.resample(frame):
                a = out.to_ndarray().reshape(-1)
                chunks.append(a)
                got += len(a)
            if limit is not None and got >= limit:
                break
        for out in res.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return None
    samples = np.concatenate(chunks).astype(np.int16)
    return samples[:limit] if seconds else samples


# -- OpenCV --------------------------------------------------------------------------
def _cv_thumbnail(path, seconds, height, is_image):
    import cv2
    if is_image or os.path.splitext(path)[1].lower() in IMAGE_EXT:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
    else:
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                return None
            if seconds > 0:
                cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
            ok, img = cap.read()
            if not ok:
                return None
        finally:
            cap.release()
    if img is None:
        return None
    h, w = img.shape[:2]
    img = cv2.resize(img, (_even_width(w, h, height), height), interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", img)
    return png.tobytes() if ok else None


# -- Decord --------------------------------------------------------------------------
_readers = OrderedDict()       # (path, height) -> [VideoReader, lock]


def _decord_reader(path, height):
    import decord
    key = (path, height)
    with _lock:
        entry = _readers.get(key)
        if entry is None:
            probe = decord.VideoReader(path, num_threads=1)
            h, w = probe[0].shape[:2]
            del probe
            reader = decord.VideoReader(path, width=_even_width(w, h, height), height=height, num_threads=1)
            entry = _readers[key] = [reader, threading.Lock()]
            while len(_readers) > 4:
                _readers.popitem(last=False)
        _readers.move_to_end(key)
        return entry


def _decord_thumbnail(path, seconds, height, is_image):
    if is_image or os.path.splitext(path)[1].lower() in IMAGE_EXT:
        return None                     # Decord reads videos only
    reader, lock = _decord_reader(path, height)
    with lock:
        fps = reader.get_avg_fps() or 30.0
        i = max(0, min(len(reader) - 1, int(round(seconds * fps))))
        img = reader[i].asnumpy()
    return encode_png(img)


def _decord_audio(path, rate, seconds):
    import decord
    reader = decord.AudioReader(path, sample_rate=rate, mono=True)
    n = len(reader._array[0]) if hasattr(reader, "_array") else reader.shape[1]
    if seconds:
        n = min(n, int(seconds * rate))
    if n <= 0:
        return None
    x = reader[0:n].asnumpy().reshape(-1)
    return np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)


_IMPL = {
    "ffmpeg": {"probe": _ff_probe, "thumbnail": _ff_thumbnail, "audio": _ff_audio},
    "pyav": {"probe": _av_probe, "thumbnail": _av_thumbnail, "audio": _av_audio},
    "opencv": {"thumbnail": _cv_thumbnail},
    "decord": {"thumbnail": _decord_thumbnail, "audio": _decord_audio},
}
