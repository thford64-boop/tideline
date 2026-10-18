"""Multi-camera editing: lining the cameras up and building the
multi-camera source sequence.

A multi-camera source is an ordinary sequence marked ``multicam``: each video
track holds one camera (an "angle") and the audio tracks hold their sound,
each tagged with the camera it came from. A clip of it in another sequence
shows one angle (``Clip.angle``); cutting between cameras is splitting that
clip and giving the parts other angles. Cameras are synced by their sound
(the loudness changes of each recording, cross-correlated), by their In
points, or by their first frames. No GTK; ffmpeg reads the sound.
"""

import subprocess

import numpy as np

from .edits import Editor
from .model import Sequence

SYNC_MODES = [("audio", "Audio"), ("in", "In points"), ("start", "First frames")]
ENVELOPE_RATE = 400          # loudness samples a second used to sync by sound
_DECODE_RATE = 8000


class SyncError(Exception):
    """Shown to the user as is."""


def audio_tracks(seq, angle, mode):
    """The audio tracks of a multi-camera sequence heard with camera
    ``angle`` showing, under its audio ``mode``."""
    if mode == "all":
        return list(seq.audio_tracks)
    if mode == "follow":
        return [t for t in seq.audio_tracks if t.angle == angle]
    try:
        index = int(mode)
    except (TypeError, ValueError):
        index = 0
    return [seq.audio_tracks[index]] if 0 <= index < len(seq.audio_tracks) else []


def audio_choices(seq):
    """(mode, label) pairs for a multi-camera sequence's audio menu."""
    out = [(str(i), t.name or f"Audio {i + 1}") for i, t in enumerate(seq.audio_tracks)]
    return out + [("follow", "Follow the camera"), ("all", "Mix all cameras")]


def angle_names(seq):
    return [t.name or f"Camera {i + 1}" for i, t in enumerate(seq.video_tracks)]


# -- syncing by sound --------------------------------------------------------------
def envelope(path, rate=ENVELOPE_RATE, seconds=None):
    """Onsets of a recording's sound: how much louder it gets, ``rate`` times
    a second. Claps, words and hits stand out the same way in every
    camera's recording of them, whatever the microphone."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1",
           "-ar", str(_DECODE_RATE), "-f", "s16le"]
    if seconds:
        cmd += ["-t", f"{float(seconds):.3f}"]
    try:
        raw = subprocess.run(cmd + ["-"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SyncError(f"Could not read the sound of {path}: {exc}") from None
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    step = _DECODE_RATE // rate
    n = len(x) // step
    if n < 4:
        return np.zeros(0, dtype=np.float32)
    rms = np.sqrt((x[:n * step].reshape(n, step) ** 2).mean(axis=1))
    level = np.log1p(rms * 200.0)
    return np.maximum(0.0, np.diff(level, prepend=level[:1])).astype(np.float32)


def find_offset(ref, other, rate=ENVELOPE_RATE):
    """(seconds ``other`` started after ``ref`` — negative if before, how
    sure, 0..1) from their onset envelopes."""
    if len(ref) < 4 or len(other) < 4 or ref.std() == 0 or other.std() == 0:
        return 0.0, 0.0
    a = (ref - ref.mean()) / ref.std()
    b = (other - other.mean()) / other.std()
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    # corr[k] lines b's sample 0 up with a's sample k; negative k wrap to the end.
    lags = np.concatenate([np.arange(0, len(a)), np.arange(-len(b) + 1, 0)])
    values = np.concatenate([corr[:len(a)], corr[size - len(b) + 1:]])
    best = int(np.argmax(values))
    peak = values[best]
    guard = int(rate * 0.25)
    rest = np.concatenate([values[:max(0, best - guard)], values[best + guard + 1:]])
    second = rest.max() if len(rest) else 0.0
    sure = float(np.clip(1.0 - max(0.0, second) / peak, 0.0, 1.0)) if peak > 0 else 0.0
    return float(lags[best]) / rate, sure


def offsets(items, mode="audio", progress=None, cancelled=lambda: False):
    """Where each item starts (seconds, the earliest at 0) once lined up,
    and the names of items that could not be synced (left at the start).
    ``progress(fraction, text)``."""
    if mode == "start":
        return [0.0] * len(items), []
    if mode == "in":
        ins = [float(m.mark_in or 0.0) for m in items]
        top = max(ins)
        return [top - v for v in ins], []
    starts, failed, ref = [None] * len(items), [], None
    for i, m in enumerate(items):
        if cancelled():
            raise SyncError("Cancelled")
        if progress:
            progress(i / max(1, len(items)), f"Listening to {m.name}")
        if not (m.has_audio and m.path):
            failed.append(m.name)
            continue
        env = envelope(m.path)
        if ref is None:
            ref, starts[i] = env, 0.0
            continue
        shift, sure = find_offset(ref, env)
        if sure < 0.05:
            failed.append(m.name)
            continue
        starts[i] = shift
    if ref is None:
        raise SyncError("None of the clips has sound to sync by. Sync by In points or first frames instead.")
    low = min(s for s in starts if s is not None)
    if progress:
        progress(1.0, "Lined up")
    return [0.0 if s is None else s - low for s in starts], failed


# -- building ------------------------------------------------------------------
def build(project, items, starts, name, audio=None):
    """A multi-camera sequence of ``items`` (each starting ``starts[i]``
    seconds in): one video track per camera, one audio track per recording
    with sound. It takes its frame size and rate from the first camera."""
    from .engine.builder import _rational_fps
    cams = [m for m in items if m.has_video]
    if not cams:
        raise SyncError("A multi-camera sequence needs at least one clip with video.")
    first = cams[0]
    num, den = _rational_fps(first.fps) if first.fps else (30, 1)
    w, h = (first.width or 1920), (first.height or 1080)
    seq = Sequence(name=name, width=w + (w % 2), height=h + (h % 2), fps_num=num, fps_den=den, multicam=True)
    ed = Editor(project, seq)
    camera = 0
    for m, start in zip(items, starts):
        v, a = ed.clips_from_media(m)
        t = int(round(start * seq.fps))
        angle = None
        if v is not None:
            track = seq.add_track("video")
            track.name = m.name
            v.start = t
            track.clips.append(v)
            angle, camera = camera, camera + 1
        if a is not None:
            track = seq.add_track("audio")
            track.name = m.name
            track.angle = angle
            a.start = t
            track.clips.append(a)
        elif v is not None:
            v.link_id = None
    if not seq.audio_tracks:
        seq.add_track("audio")
    if audio is None:
        # A recorder without a picture usually has the best sound.
        external = next((i for i, t in enumerate(seq.audio_tracks) if t.angle is None and t.clips), None)
        audio = str(external if external is not None else 0)
    seq.multicam_audio = audio
    return seq
