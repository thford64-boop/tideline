"""Captions: speech-to-text, caption layout, and SRT / WebVTT files.

Captions live in the timeline as ordinary title clips on a video track marked
as a caption track, one clip per caption, so they render, export, trim and
style like any other title. This module is independent of GTK and MLT's
Python bindings: it renders the sequence's sound with ``melt``, hands it to a
speech recogniser and turns the result into timed captions.

Recognisers are external programs, found on ``PATH``:

* ``whisper-cli`` from whisper.cpp (Arch: ``whisper-cpp``) — fast on a CPU.
  Its models are downloaded once into ``~/.local/share/tideline/whisper``.
* ``whisper`` from OpenAI Whisper (Arch: ``python-openai-whisper``), which
  fetches its own models.

Nothing leaves the machine apart from that one-time model download.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass

MODELS = [
    # (name, label, approximate download in MB for whisper.cpp)
    ("tiny", "Tiny — fastest, rough", 75),
    ("base", "Base — fast, good for clear speech", 142),
    ("small", "Small — slower, accurate", 466),
    ("medium", "Medium — slow, very accurate", 1500),
    ("large-v3-turbo", "Large v3 Turbo — slowest, best", 1600),
]
LANGUAGES = [
    ("auto", "Detect automatically"), ("en", "English"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
    ("sv", "Swedish"), ("tr", "Turkish"), ("ru", "Russian"), ("uk", "Ukrainian"), ("ar", "Arabic"),
    ("hi", "Hindi"), ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"), ("id", "Indonesian"),
    ("vi", "Vietnamese"),
]
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin"
CPP_NAMES = ("whisper-cli", "whisper-cpp", "whisper.cpp")
MAX_CUE_SECONDS = 7.0
MERGE_GAP = 0.6             # seconds of silence that always start a new caption
MIN_CUE_SECONDS = 0.7


@dataclass
class Cue:
    start: float             # seconds
    end: float
    text: str


class CaptionError(Exception):
    """Shown to the user as is."""


# -- files ---------------------------------------------------------------------
_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _seconds(text):
    m = _TIME.search(text)
    if not m:
        raise ValueError(text)
    h, mnt, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000.0


def parse(text):
    """Cues from SRT or WebVTT text (the format is recognised by its content)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        a, _, b = lines[timing].partition("-->")
        try:
            start, end = _seconds(a), _seconds(b)
        except ValueError:
            continue
        body = "\n".join(lines[timing + 1:])
        body = re.sub(r"<[^>]+>", "", body)                  # <i>, <b>, VTT voice/timestamp tags
        body = re.sub(r"\{\\[^}]*\}", "", body).strip()       # SSA-style overrides some SRTs carry
        if body and end > start:
            cues.append(Cue(start, end, body))
    cues.sort(key=lambda c: c.start)
    return cues


def load(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            cues = parse(fh.read())
    except OSError as exc:
        raise CaptionError(f"Could not read {os.path.basename(path)}: {exc.strerror}") from None
    if not cues:
        raise CaptionError(f"{os.path.basename(path)} has no captions in SRT or WebVTT format.")
    return cues


def _stamp(t, sep):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues):
    return "\n".join(f"{i}\n{_stamp(c.start, ',')} --> {_stamp(c.end, ',')}\n{c.text}\n"
                     for i, c in enumerate(cues, 1))


def to_vtt(cues):
    return "WEBVTT\n\n" + "\n".join(f"{_stamp(c.start, '.')} --> {_stamp(c.end, '.')}\n{c.text}\n" for c in cues)


def save(cues, path):
    text = to_vtt(cues) if path.lower().endswith(".vtt") else to_srt(cues)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# -- layout --------------------------------------------------------------------
def wrap(text, max_chars):
    """Break text into lines of at most ``max_chars`` on word boundaries."""
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > max_chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def _balanced(text, max_chars, max_lines):
    """Lines for one caption, evened out so two lines have similar lengths."""
    lines = wrap(text, max_chars)
    if len(lines) == 2 and max_lines >= 2:
        words = text.split()
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if len(a) <= max_chars and len(b) <= max_chars:
                score = abs(len(a) - len(b))
                if best is None or score < best[0]:
                    best = (score, [a, b])
        if best:
            lines = best[1]
    return lines


def _split(cue, max_chars, max_lines):
    """Split a cue too long for one caption, sharing out its time by length."""
    words = cue.text.split()
    if not words:
        return []
    chunks, cur = [], []
    for word in words:
        trial = cur + [word]
        if cur and len(wrap(" ".join(trial), max_chars)) > max_lines:
            chunks.append(cur)
            cur = [word]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    total = sum(len(" ".join(c)) for c in chunks) or 1
    out, t = [], cue.start
    for c in chunks:
        text = " ".join(c)
        d = (cue.end - cue.start) * len(text) / total
        out.append(Cue(t, t + d, text))
        t += d
    out[-1].end = cue.end
    return out


def layout(cues, max_chars=42, max_lines=2):
    """Turn recogniser output into readable captions: long segments are split,
    short neighbours joined while they fit, and every caption is wrapped."""
    pieces = []
    for cue in cues:
        text = " ".join(cue.text.split())
        if text:
            pieces.extend(_split(Cue(cue.start, cue.end, text), max_chars, max_lines))
    merged = []
    for cue in pieces:
        if merged:
            last = merged[-1]
            joined = f"{last.text} {cue.text}"
            if (cue.start - last.end < MERGE_GAP and cue.end - last.start <= MAX_CUE_SECONDS
                    and len(wrap(joined, max_chars)) <= max_lines
                    and not last.text.endswith((".", "?", "!"))):
                last.text, last.end = joined, cue.end
                continue
        merged.append(Cue(cue.start, cue.end, cue.text))
    for i, cue in enumerate(merged):
        nxt = merged[i + 1].start if i + 1 < len(merged) else None
        if cue.end - cue.start < MIN_CUE_SECONDS:
            cue.end = cue.start + MIN_CUE_SECONDS
        if nxt is not None and cue.end > nxt:
            cue.end = max(cue.start + 0.04, nxt)
        cue.text = "\n".join(_balanced(cue.text, max_chars, max_lines))
    return merged


# -- style ---------------------------------------------------------------------
STYLE_KEYS = ("font", "size", "bold", "italic", "underline", "fill", "stroke", "stroke_width", "background",
              "padding", "halign", "position", "margin", "shadow", "shadow_color", "anim_in", "anim_out", "anim_time")
_TEXT_KEYS = ("font", "size", "bold", "italic", "underline", "fill", "stroke", "stroke_width", "background",
              "padding", "anim_in", "anim_out", "anim_time")


def default_style(width, height):
    """White bold text on a dark box near the bottom, sized to the frame."""
    size = max(16, int(round(min(width, height) / 18)))
    return {"font": "Sans", "size": size, "bold": True, "italic": False, "underline": False,
            "fill": "#ffffffff", "stroke": "#000000ff", "stroke_width": 0, "background": "#000000b0",
            "padding": max(4, size // 4), "halign": "center", "position": "bottom", "margin": 6.0,
            "shadow": False, "shadow_color": "#000000b4", "anim_in": "none", "anim_out": "none", "anim_time": 0.3}


def text_params(style, width, height):
    """Text effect parameters that draw a caption in ``style``: position and
    margin become an alignment and an offset in a ``width`` × ``height`` frame."""
    margin = float(style.get("margin", 6.0)) / 100.0
    position, halign = style.get("position", "bottom"), style.get("halign", "center")
    dy = {"bottom": -1.0, "top": 1.0}.get(position, 0.0) * round(height * margin)
    dx = {"left": 1.0, "right": -1.0}.get(halign, 0.0) * round(width * margin)
    params = {k: style[k] for k in _TEXT_KEYS if k in style}
    params.update(halign=halign, valign=position if position in ("top", "middle") else "bottom",
                  offset=[float(dx), float(dy)])
    return params


def style_of(text, shadow, base, width, height):
    """The style a title shows, as a style dict: ``text`` is its Text effect,
    ``shadow`` its Drop Shadow (or None), ``base`` fills in the rest."""
    style = dict(base)
    if text is None:
        return style
    for k in _TEXT_KEYS + ("halign",):
        style[k] = text.value(k, None, style.get(k))
    valign = text.value("valign", None, "bottom")
    style["position"] = valign if valign in ("top", "middle") else "bottom"
    off = text.value("offset", None, [0.0, 0.0]) or [0.0, 0.0]
    if style["position"] != "middle":
        style["margin"] = round(abs(float(off[1])) / height * 100.0, 1)
    style["shadow"] = shadow is not None and shadow.enabled
    if shadow is not None:
        style["shadow_color"] = shadow.value("color", None, style.get("shadow_color"))
    return style


def shadow_params(style):
    """Drop Shadow parameters in proportion to the caption's size."""
    size = float(style.get("size", 48))
    return {"color": style.get("shadow_color", "#000000b4"), "radius": round(max(2.0, size / 10), 1),
            "x": round(max(1.0, size / 16), 1), "y": round(max(1.0, size / 16), 1)}


# -- recognisers ---------------------------------------------------------------
def models_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "tideline", "whisper")


def cpp_model_path(name):
    """Where a whisper.cpp model is, or should be downloaded to."""
    fname = f"ggml-{name}.bin"
    for folder in (models_dir(), "/usr/share/whisper.cpp/models", "/usr/share/whisper-cpp/models",
                   "/usr/local/share/whisper.cpp/models"):
        path = os.path.join(folder, fname)
        if os.path.isfile(path):
            return path
    return os.path.join(models_dir(), fname)


@dataclass
class Engine:
    key: str                 # "cpp" | "openai"
    label: str
    path: str

    def model_ready(self, model):
        return self.key != "cpp" or os.path.isfile(cpp_model_path(model))


def engines():
    """The speech recognisers installed on this machine, best first."""
    found = []
    for name in CPP_NAMES:
        path = shutil.which(name)
        if path:
            found.append(Engine("cpp", "whisper.cpp", path))
            break
    path = shutil.which("whisper")
    if path:
        found.append(Engine("openai", "OpenAI Whisper", path))
    return found


INSTALL_HINT = ("Auto captions need a speech recogniser. On Arch: sudo pacman -S whisper-cpp "
                "(recommended) or python-openai-whisper. Other systems: install whisper.cpp "
                "so that 'whisper-cli' is on your PATH.")


def download_model(name, progress=None, cancelled=lambda: False):
    """Fetch a whisper.cpp model once. ``progress(fraction)``."""
    dest = cpp_model_path(name)
    if os.path.isfile(dest):
        return dest
    dest = os.path.join(models_dir(), f"ggml-{name}.bin")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(MODEL_URL.format(name), timeout=30) as resp, open(tmp, "wb") as fh:
            size = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if cancelled():
                    raise CaptionError("Cancelled")
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                got += len(block)
                if progress and size:
                    progress(got / size)
        if size and got != size:
            raise CaptionError("The model download was cut short; try again.")
        os.replace(tmp, dest)
    except OSError as exc:
        raise CaptionError(f"Could not download the '{name}' speech model: {exc}") from None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


# -- the job -------------------------------------------------------------------
@dataclass
class Request:
    xml_path: str            # MLT XML of the sequence (Builder.write_xml)
    range_in: int            # frames
    range_out: int           # exclusive
    fps: float
    engine: Engine
    model: str = "base"
    language: str = "auto"
    max_chars: int = 42
    max_lines: int = 2
    threads: int = 0


class TranscribeJob:
    """Render the sound, recognise speech, lay out captions — on a worker
    thread. ``on_progress(stage_text, fraction)`` and ``on_done(job)`` are
    called from that thread; ``job.cues`` (seconds from the range start) or
    ``job.error`` holds the result."""

    STAGES = (("Preparing audio", 0.0, 0.12), ("Downloading speech model", 0.12, 0.3),
              ("Recognising speech", 0.3, 1.0))

    def __init__(self, request, on_progress=None, on_done=None):
        self.req = request
        self.on_progress = on_progress
        self.on_done = on_done
        self.cues = []
        self.error = ""
        self.cancelled = False
        self.process = None
        self._tmp = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        p = self.process
        if p is not None and p.poll() is None:
            p.terminate()

    def _report(self, stage, fraction):
        if self.on_progress:
            name, lo, hi = self.STAGES[stage]
            self.on_progress(name, lo + (hi - lo) * max(0.0, min(1.0, fraction)))

    def _run(self):
        self._tmp = tempfile.mkdtemp(prefix="tideline-captions-")
        try:
            wav = self._render_audio()
            raw = self._recognise(wav)
            self.cues = layout(raw, self.req.max_chars, self.req.max_lines)
            if not self.cues:
                self.error = "No speech was recognised in this part of the sequence."
        except CaptionError as exc:
            self.error = str(exc)
        except Exception as exc:                     # noqa: BLE001 - reported to the user
            self.error = f"Captioning failed: {exc!r}"
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)
            if self.cancelled:
                self.error = "Cancelled"
            if self.on_done:
                self.on_done(self)

    def _run_tool(self, cmd, stage, pattern, scale=1.0):
        """Run a command, reporting progress parsed from its output."""
        if self.cancelled:
            raise CaptionError("Cancelled")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        env=env, errors="replace", bufsize=1)
        tail = []
        buf = ""
        while True:
            ch = self.process.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                line, buf = buf, ""
                m = pattern.search(line) if pattern else None
                if m:
                    self._report(stage, float(m.group(1)) / 100.0 * scale)
                elif line.strip():
                    tail.append(line.strip())
                    del tail[:-12]
            else:
                buf += ch
        rc = self.process.wait()
        self.process.stdout.close()
        if self.cancelled:
            raise CaptionError("Cancelled")
        if rc != 0:
            raise CaptionError("\n".join(tail[-6:]) or f"{os.path.basename(cmd[0])} exited with status {rc}")

    def _render_audio(self):
        req = self.req
        if not shutil.which("melt") or not shutil.which("ffmpeg"):
            raise CaptionError("Auto captions need the 'melt' (mlt) and 'ffmpeg' commands.")
        mix = os.path.join(self._tmp, "mix.wav")
        self._report(0, 0.0)
        self._run_tool(["melt", "-progress2", req.xml_path, f"in={req.range_in}",
                        f"out={max(req.range_in, req.range_out - 1)}", "-consumer", "avformat:" + mix,
                        "vn=1", "video_off=1", "acodec=pcm_s16le", "ar=48000", "channels=2",
                        "real_time=-2"], 0, re.compile(r"percentage:\s*(\d+)"), 0.85)
        wav = os.path.join(self._tmp, "speech.wav")
        self._run_tool(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", mix,
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], 0, None)
        self._report(0, 1.0)
        return wav

    def _recognise(self, wav):
        req = self.req
        threads = str(req.threads or max(1, min(8, os.cpu_count() or 2)))
        out = os.path.join(self._tmp, "speech")
        if req.engine.key == "cpp":
            model = cpp_model_path(req.model)
            if not os.path.isfile(model):
                model = download_model(req.model, lambda f: self._report(1, f), lambda: self.cancelled)
            self._report(2, 0.0)
            cmd = [req.engine.path, "-m", model, "-f", wav, "-t", threads, "-l", req.language,
                   "-osrt", "-of", out, "-ml", str(req.max_chars), "-sow", "-pp"]
            self._run_tool(cmd, 2, re.compile(r"progress\s*=\s*(\d+)%"))
            srt = out + ".srt"
        else:
            self._report(2, 0.0)
            cmd = [req.engine.path, wav, "--model", req.model, "--output_format", "srt", "--output_dir",
                   self._tmp, "--word_timestamps", "True", "--max_line_width", str(req.max_chars),
                   "--max_line_count", "1", "--fp16", "False", "--verbose", "False", "--threads", threads]
            if req.language != "auto":
                cmd += ["--language", req.language]
            self._run_tool(cmd, 2, re.compile(r"(\d+)%\|"))
            srt = os.path.join(self._tmp, "speech.srt")
        if not os.path.isfile(srt):
            raise CaptionError("The speech recogniser produced no captions file.")
        with open(srt, encoding="utf-8", errors="replace") as fh:
            cues = parse(fh.read())
        # Some recognisers mark silence or music with bracketed tags.
        return [c for c in cues if not re.fullmatch(r"\s*[\[(].*[\])]\s*", c.text)]
