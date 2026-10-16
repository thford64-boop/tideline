"""Export: render a sequence to a file with ``melt`` in a separate process."""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

from .builder import Builder


@dataclass
class Format:
    key: str
    label: str
    ext: str
    video: dict = field(default_factory=dict)     # avformat consumer properties
    audio: dict = field(default_factory=dict)
    crf: tuple | None = None                      # (high, medium, low) quality values
    crf_key: str = "crf"
    audio_only: bool = False
    image_sequence: bool = False
    note: str = ""


FORMATS = [
    Format("h264", "H.264 (MP4)", ".mp4",
           {"f": "mp4", "vcodec": "libx264", "preset": "medium", "pix_fmt": "yuv420p",
            "movflags": "+faststart"},
           {"acodec": "aac", "ab": "192k"}, crf=(17, 21, 26)),
    Format("hevc", "H.265 / HEVC (MP4)", ".mp4",
           {"f": "mp4", "vcodec": "libx265", "preset": "medium", "pix_fmt": "yuv420p", "vtag": "hvc1",
            "movflags": "+faststart"},
           {"acodec": "aac", "ab": "192k"}, crf=(19, 24, 29)),
    Format("av1", "AV1 (MP4)", ".mp4",
           {"f": "mp4", "vcodec": "libsvtav1", "preset": "8", "pix_fmt": "yuv420p",
            "movflags": "+faststart"},
           {"acodec": "libopus", "ab": "160k"}, crf=(24, 32, 40)),
    Format("vp9", "VP9 (WebM)", ".webm",
           {"f": "webm", "vcodec": "libvpx-vp9", "vb": "0", "pix_fmt": "yuv420p", "row-mt": "1",
            "cpu-used": "3"},
           {"acodec": "libopus", "ab": "160k"}, crf=(24, 32, 40)),
    Format("prores", "ProRes 422 HQ (MOV)", ".mov",
           {"f": "mov", "vcodec": "prores_ks", "vprofile": "3", "pix_fmt": "yuv422p10le",
            "vendor": "apl0"},
           {"acodec": "pcm_s24le"}, note="Intermediate codec, large files"),
    Format("dnxhr", "DNxHR HQ (MOV)", ".mov",
           {"f": "mov", "vcodec": "dnxhd", "vprofile": "dnxhr_hq", "pix_fmt": "yuv422p"},
           {"acodec": "pcm_s24le"}, note="Intermediate codec, large files"),
    Format("gif", "Animated GIF", ".gif", {"f": "gif", "pix_fmt": "rgb8"}, {"an": "1"}),
    Format("png", "PNG Sequence", ".png", {"vcodec": "png", "pix_fmt": "rgb24"}, {"an": "1"},
           image_sequence=True),
    Format("wav", "WAV Audio", ".wav", {}, {"f": "wav", "acodec": "pcm_s24le"}, audio_only=True),
    Format("flac", "FLAC Audio", ".flac", {}, {"f": "flac", "acodec": "flac"}, audio_only=True),
    Format("mp3", "MP3 Audio", ".mp3", {}, {"f": "mp3", "acodec": "libmp3lame", "ab": "256k"},
           audio_only=True),
    Format("opus", "Opus Audio (OGG)", ".ogg", {}, {"f": "ogg", "acodec": "libopus", "ab": "160k"},
           audio_only=True),
]
FORMAT_BY_KEY = {f.key: f for f in FORMATS}
QUALITIES = [("high", "High"), ("medium", "Medium"), ("low", "Low (smaller file)")]


@dataclass
class ExportSettings:
    format: str = "h264"
    path: str = ""
    quality: str = "high"
    width: int = 0               # 0 = match sequence
    height: int = 0
    range_in: int | None = None  # frames; None = whole sequence
    range_out: int | None = None # exclusive
    audio: bool = True
    audio_bitrate: str = ""
    video_bitrate: str = ""      # e.g. "12M"; overrides quality when set
    threads: int = 0


class ExportJob:
    """One render. Progress callbacks arrive on the worker thread; wrap them
    with ``GLib.idle_add`` when touching the UI."""

    def __init__(self, project, seq, settings, on_progress=None, on_done=None):
        self.project = project
        self.seq = seq
        self.settings = settings
        self.on_progress = on_progress
        self.on_done = on_done
        self.process = None
        self.cancelled = False
        self.error = ""
        self.percent = 0
        self.started = 0.0
        self._tmp = None

    def command(self, xml_path, length):
        s, fmt = self.settings, FORMAT_BY_KEY[self.settings.format]
        scaled = bool(s.width and s.height and (s.width, s.height) != (self.seq.width, self.seq.height))
        # A scaled export wraps the project in a "consumer" producer so the
        # sequence still renders at its own size and only the result is scaled.
        cmd = ["melt", "-progress2", ("consumer:" if scaled else "") + xml_path]
        if s.range_in is not None or s.range_out is not None:
            a = max(0, s.range_in or 0)
            b = min(length, s.range_out if s.range_out is not None else length)
            cmd += [f"in={a}", f"out={max(a, b - 1)}"]
        out = s.path
        if fmt.image_sequence:
            base, ext = os.path.splitext(out)
            out = f"{base}_%05d{ext or fmt.ext}"
        cmd += ["-consumer", "avformat:" + out]
        props = {"real_time": str(-(s.threads or max(1, min(4, (os.cpu_count() or 2) // 2)))),
                 "progressive": "1", "ar": str(self.seq.sample_rate), "channels": "2"}
        if scaled:
            props.update(width=str(s.width), height=str(s.height), sample_aspect_num="1",
                         sample_aspect_den="1")
        if fmt.audio_only:
            props["vn"] = "1"
            props["video_off"] = "1"
        else:
            props.update(fmt.video)
            if s.video_bitrate:
                props["vb"] = s.video_bitrate
            elif fmt.crf:
                props[fmt.crf_key] = str(fmt.crf[["high", "medium", "low"].index(s.quality)])
        if s.audio or fmt.audio_only:
            props.update(fmt.audio)
            if s.audio_bitrate and "ab" in fmt.audio:
                props["ab"] = s.audio_bitrate
        else:
            props["an"] = "1"
            props["audio_off"] = "1"
        cmd += [f"{k}={v}" for k, v in props.items()]
        return cmd

    def start(self):
        if not shutil.which("melt"):
            self.error = "The 'melt' command (part of the mlt package) was not found."
            if self.on_done:
                self.on_done(self)
            return
        self._tmp = tempfile.mkdtemp(prefix="tideline-export-")
        xml_path = os.path.join(self._tmp, "sequence.mlt")
        length = Builder(self.project).write_xml(self.seq, xml_path)
        cmd = self.command(xml_path, length)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        self.started = time.time()
        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, env=env, errors="replace")
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        tail = []
        pat = re.compile(r"percentage:\s*(\d+)")
        for line in self.process.stderr:
            m = pat.search(line)
            if m:
                pct = int(m.group(1))
                if pct != self.percent:
                    self.percent = pct
                    if self.on_progress:
                        self.on_progress(self)
            elif line.strip():
                tail.append(line.strip())
                del tail[:-8]
        rc = self.process.wait()
        if self.cancelled:
            self.error = "Cancelled"
            for p in (self.settings.path,):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        elif rc != 0:
            self.error = "\n".join(tail) or f"melt exited with status {rc}"
        else:
            self.percent = 100
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self.on_done:
            self.on_done(self)

    def cancel(self):
        self.cancelled = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def eta(self):
        if self.percent <= 0:
            return None
        elapsed = time.time() - self.started
        return elapsed * (100 - self.percent) / self.percent
