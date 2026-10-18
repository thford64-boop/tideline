"""Proxy media: small, fast-decoding copies of large video files that the
editor plays and scrubs in place of the originals.

A proxy has the same length, frame rate and audio as its original, only a
smaller picture in an easy codec, so every edit made on it applies unchanged
to the original. The project remembers which proxy belongs to which item
(``Project.proxies``); only the preview uses them, and exports and exported
frames always read the original files.

Independent of GTK: jobs run FFmpeg on a worker thread and report through
plain callbacks, which the app forwards to the main loop.
"""

import hashlib
import os
import subprocess
import threading

SIZES = [(360, "360p"), (540, "540p"), (720, "720p"), (1080, "1080p")]
CODECS = [("mjpeg", "MJPEG (fastest to scrub)"), ("h264", "H.264 (smaller files)")]
LOCATIONS = [("project", "Next to the project"), ("cache", "Cache folder")]
AUTO = [(0, "Never"), (1440, "Larger than 1080p"), (2160, "4K and larger")]
SUFFIX = ".proxy.mov"
FOLDER = "Proxies"

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                         "tideline", "proxies")


def eligible(media):
    """True for items a proxy can stand in for: video files."""
    return media is not None and media.kind == "video" and media.has_video and bool(media.path)


def worth_it(media, size):
    """True when a proxy of ``size`` (short side) would be smaller than the original."""
    return eligible(media) and min(media.width or 0, media.height or 0) > size


def signature(path):
    """Size and modification time of a file, to notice when it changes."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}|{int(st.st_mtime)}"


def find(project, media):
    """Path of the item's proxy, or None when it has none (or it is gone or
    out of date because the original changed)."""
    if media is None:
        return None
    entry = project.proxies.get(media.id)
    if not entry or not os.path.isfile(entry.get("path", "")):
        return None
    source = entry.get("source", "")
    if source and media.path and os.path.exists(media.path) and signature(media.path) != source:
        return None
    return entry["path"]


def folder_for(project, location="project"):
    """Where new proxies of ``project`` are written."""
    if location == "project" and project.path:
        return os.path.join(os.path.dirname(os.path.abspath(project.path)), FOLDER)
    return CACHE_DIR


def target_path(project, media, location="project"):
    stem = os.path.splitext(os.path.basename(media.path))[0][:60] or "proxy"
    digest = hashlib.sha1(f"{media.path}|{signature(media.path)}".encode()).hexdigest()[:10]
    return os.path.join(folder_for(project, location), f"{stem}-{digest}{SUFFIX}")


def attach(project, media, path):
    """Record ``path`` as the proxy of ``media``."""
    project.proxies[media.id] = {"path": os.path.abspath(path), "source": signature(media.path or "")}


def detach(project, media, delete_file=False):
    """Forget an item's proxy; with ``delete_file`` also remove it from disk
    when Tideline made it. Returns True if there was one."""
    entry = project.proxies.pop(media.id, None)
    if not entry:
        return False
    path = entry.get("path", "")
    if delete_file and path.endswith(SUFFIX):
        try:
            os.remove(path)
        except OSError:
            pass
    return True


def ffmpeg_command(src, dest, media, size=540, codec="mjpeg"):
    """The FFmpeg command that writes the proxy of ``media`` to ``dest``."""
    w, h = media.width or 16, media.height or 9
    scale = f"scale=-2:{size}" if w >= h else f"scale={size}:-2"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", "-i", src,
           "-map", "0:v:0", "-map", "0:a?", "-fps_mode", "passthrough",
           "-vf", f"{scale}:flags=bicubic:out_color_matrix=bt709",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    if codec == "h264":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "10", "-bf", "0",
                "-tune", "fastdecode", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p"]
    cmd += ["-c:a", "pcm_s16le", "-f", "mov", dest]
    return cmd


class Job:
    """One proxy to make."""

    def __init__(self, media, dest, size=540, codec="mjpeg"):
        self.media_id = media.id
        self.name = media.name
        self.src = media.path
        self.dest = dest
        self.duration = max(0.001, media.duration or 0.0)
        self.cmd = ffmpeg_command(self.src, dest + ".part", media, size, codec)
        self.progress = 0.0
        self.error = ""
        self.cancelled = False
        self._proc = None

    def run(self, on_progress=None):
        """Encode (blocking). True when the proxy was written."""
        os.makedirs(os.path.dirname(self.dest), exist_ok=True)
        part = self.dest + ".part"
        try:
            self._proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True)
        except OSError as exc:
            self.error = f"could not run ffmpeg ({exc})"
            return False
        with self._proc:
            for line in self._proc.stdout:
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and value.isdigit():
                    self.progress = min(1.0, int(value) / 1e6 / self.duration)
                    if on_progress:
                        on_progress(self)
            err = self._proc.stderr.read()
        code = self._proc.wait()
        if self.cancelled or code != 0:
            if not self.cancelled:
                self.error = (err.strip().splitlines() or [f"ffmpeg exited with {code}"])[-1]
            try:
                os.remove(part)
            except OSError:
                pass
            return False
        os.replace(part, self.dest)
        self.progress = 1.0
        return True

    def cancel(self):
        self.cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()


class Queue:
    """Makes proxies one at a time on a worker thread.

    ``on_progress(job, index, total)`` and ``on_done(job, ok)`` are called
    from that thread; ``index`` counts from 1 within the current batch."""

    def __init__(self, on_progress=None, on_done=None):
        self.on_progress = on_progress
        self.on_done = on_done
        self.pending = []
        self.current = None
        self._done_in_batch = 0
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def busy(self, media_id=None):
        with self._lock:
            jobs = self.pending + ([self.current] if self.current else [])
        return any(j.media_id == media_id for j in jobs) if media_id else bool(jobs)

    def state(self, media_id):
        """"creating" (with progress), "queued" or None."""
        with self._lock:
            if self.current is not None and self.current.media_id == media_id:
                return "creating"
            if any(j.media_id == media_id for j in self.pending):
                return "queued"
        return None

    def submit(self, jobs):
        """Queue jobs, skipping items already queued. Returns how many were added."""
        with self._lock:
            known = {j.media_id for j in self.pending}
            if self.current is not None:
                known.add(self.current.media_id)
            fresh = [j for j in jobs if j.media_id not in known]
            self.pending += fresh
            start = bool(fresh) and not self._running
            if start:
                self._running = True
                self._done_in_batch = 0
                self._thread = threading.Thread(target=self._work, daemon=True, name="tideline-proxies")
        if start:
            self._thread.start()
        return len(fresh)

    def cancel(self, media_id=None):
        """Cancel every job, or the ones for one item."""
        with self._lock:
            if media_id is None:
                self.pending = []
            else:
                self.pending = [j for j in self.pending if j.media_id != media_id]
            current = self.current
        if current is not None and media_id in (None, current.media_id):
            current.cancel()

    def wait(self, timeout=None):
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _work(self):
        while True:
            with self._lock:
                if not self.pending:
                    self.current = None
                    self._running = False
                    return
                job = self.current = self.pending.pop(0)
                index = self._done_in_batch + 1
                total = index + len(self.pending)
            report = (lambda j: self.on_progress(j, index, total)) if self.on_progress else None
            if report:
                report(job)
            ok = job.run(report)
            with self._lock:
                self._done_in_batch += 1
                self.current = None
            if self.on_done:
                self.on_done(job, ok)
