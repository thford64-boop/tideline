"""Project data model: media, sequences, tracks, clips, effects.

Everything here is plain Python and JSON-serialisable; the MLT graph is derived
from it (see ``engine.builder``) and never the other way round.

Time is counted in sequence frames. A clip's ``in_point`` is the frame of its
own producer shown at the clip's first frame; for a speed-changed or reversed
clip that producer is the retimed one, so ``in_point + duration`` never exceeds
``media length / speed``.
"""

import copy
import json
import os
import uuid
from dataclasses import dataclass, field, fields

from . import PROJECT_FORMAT
from . import keyframes as kf
from . import timeremap

# Clip label colours (name, hex).
LABELS = [
    ("Sky", "#3b79a8"),
    ("Moss", "#3c8a67"),
    ("Coral", "#c25e4e"),
    ("Amber", "#b98427"),
    ("Plum", "#8659a6"),
    ("Lagoon", "#23968d"),
    ("Rose", "#b3527a"),
    ("Slate", "#5f6c7a"),
]
LABEL_VIDEO, LABEL_AUDIO, LABEL_GENERATED, LABEL_NESTED = 0, 1, 4, 5

GENERATED_KINDS = ("color", "title", "bars", "tone")
STILL_SECONDS = 5.0

# Frame shapes offered in the project and sequence settings (label, w, h).
ASPECTS = [
    ("16:9  Widescreen", 16, 9), ("9:16  Vertical", 9, 16), ("1:1  Square", 1, 1),
    ("4:5  Portrait", 4, 5), ("4:3  Standard", 4, 3), ("3:4  Portrait", 3, 4),
    ("3:2  Photo", 3, 2), ("2:3  Photo Portrait", 2, 3), ("21:9  Ultrawide", 21, 9),
    ("2.39:1  Cinema", 239, 100),
]
RESOLUTIONS = [480, 720, 1080, 1440, 2160]      # length of the frame's short side
DEFAULT_SETTINGS = {"width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1, "sample_rate": 48000}


def size_for_aspect(aspect_w, aspect_h, short):
    """Even frame size of that shape whose short side is ``short`` pixels."""
    even = lambda v: max(16, int(round(v / 2.0)) * 2)
    if aspect_w >= aspect_h:
        return even(short * aspect_w / aspect_h), even(short)
    return even(short), even(short * aspect_h / aspect_w)


def aspect_index(width, height):
    """Index into ``ASPECTS`` of a frame size's shape, or -1 for any other."""
    for i, (_, w, h) in enumerate(ASPECTS):
        if abs(width / max(1, height) - w / h) < 0.012:
            return i
    return -1


def aspect_name(width, height):
    i = aspect_index(width, height)
    return ASPECTS[i][0].split()[0] if i >= 0 else f"{width / max(1, height):.3g}:1"


def new_id():
    return uuid.uuid4().hex[:10]


def _from_dict(cls, data, **nested):
    names = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in names}
    for key, conv in nested.items():
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = conv(kwargs[key])
    return cls(**kwargs)


@dataclass
class Effect:
    type: str
    id: str = field(default_factory=new_id)
    enabled: bool = True
    params: dict = field(default_factory=dict)
    keyframes: dict = field(default_factory=dict)
    collapsed: bool = False

    def value(self, name, t=None, default=None):
        kfs = self.keyframes.get(name)
        if kfs and t is not None:
            return kf.evaluate(kfs, t)
        if kfs:
            return kfs[0][1]
        return self.params.get(name, default)

    def animated(self, name):
        return bool(self.keyframes.get(name))

    def copy(self, fresh_id=True):
        e = copy.deepcopy(self)
        if fresh_id:
            e.id = new_id()
        return e

    def to_dict(self):
        return {"type": self.type, "id": self.id, "enabled": self.enabled,
                "params": copy.deepcopy(self.params),
                "keyframes": copy.deepcopy(self.keyframes), "collapsed": self.collapsed}

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


@dataclass
class Clip:
    media_id: str
    start: int = 0
    duration: int = 1
    in_point: int = 0
    id: str = field(default_factory=new_id)
    name: str = ""
    speed: float = 1.0
    reverse: bool = False
    enabled: bool = True
    link_id: str | None = None
    label: int | None = None
    angle: int | None = None         # camera shown, for a clip of a multi-camera sequence
    effects: list = field(default_factory=list)

    @property
    def end(self):
        return self.start + self.duration

    def source_frame(self, t):
        """Producer frame shown at timeline frame ``t``."""
        return self.in_point + (t - self.start)

    def effect(self, type_):
        for e in self.effects:
            if e.type == type_:
                return e
        return None

    def effect_by_id(self, effect_id):
        for e in self.effects:
            if e.id == effect_id:
                return e
        return None

    def copy(self, fresh_ids=True):
        """Deep copy; with ``fresh_ids`` the clip and its effects get new ids."""
        c = copy.copy(self)
        c.effects = [e.copy(fresh_ids) for e in self.effects]
        if fresh_ids:
            c.id = new_id()
        return c

    def to_dict(self):
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "effects"}
        d["effects"] = [e.to_dict() for e in self.effects]
        return d

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d, effects=lambda v: [Effect.from_dict(e) for e in v])


@dataclass
class Transition:
    """A transition at a clip edge. ``clip_a`` is the outgoing clip, ``clip_b``
    the incoming one; either may be None for a fade to/from nothing."""
    type: str
    duration: int = 30
    clip_a: str | None = None
    clip_b: str | None = None
    align: str = "center"            # center | start | end (relative to the cut)
    id: str = field(default_factory=new_id)
    params: dict = field(default_factory=dict)

    def split(self):
        """(frames before the cut, frames after the cut)."""
        if self.clip_a is None:
            return 0, self.duration
        if self.clip_b is None:
            return self.duration, 0
        if self.align == "start":
            return 0, self.duration
        if self.align == "end":
            return self.duration, 0
        pre = self.duration // 2
        return pre, self.duration - pre

    def to_dict(self):
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


@dataclass
class Track:
    kind: str                        # "video" | "audio"
    name: str = ""
    id: str = field(default_factory=new_id)
    locked: bool = False
    hidden: bool = False             # video: eye off, audio: mute
    solo: bool = False
    target: bool = True
    sync_lock: bool = True
    height: int = 54
    captions: bool = False           # video track holding caption clips (one title per caption)
    angle: int | None = None         # multi-camera audio track: the camera (video track) it belongs to
    volume: float = 0.0              # dB, audio tracks
    pan: float = 0.0                 # -100..100, audio tracks
    clips: list = field(default_factory=list)
    transitions: list = field(default_factory=list)

    def sort(self):
        self.clips.sort(key=lambda c: c.start)

    def clip_at(self, t):
        for c in self.clips:
            if c.start <= t < c.end:
                return c
        return None

    def clip(self, clip_id):
        for c in self.clips:
            if c.id == clip_id:
                return c
        return None

    def prev_clip(self, clip):
        best = None
        for c in self.clips:
            if c is not clip and c.end <= clip.start and (best is None or c.end > best.end):
                best = c
        return best

    def next_clip(self, clip):
        best = None
        for c in self.clips:
            if c is not clip and c.start >= clip.end and (best is None or c.start < best.start):
                best = c
        return best

    def end(self):
        return max((c.end for c in self.clips), default=0)

    def transitions_for(self, clip_id):
        return [t for t in self.transitions if clip_id in (t.clip_a, t.clip_b)]

    def working_copy(self):
        """Copy whose clips and transitions can be edited without touching the
        original. Effects are shared: edit operations replace, never mutate."""
        t = copy.copy(self)
        t.clips = [copy.copy(c) for c in self.clips]
        t.transitions = [copy.copy(x) for x in self.transitions]
        return t

    def to_dict(self):
        d = {f.name: getattr(self, f.name) for f in fields(self)
             if f.name not in ("clips", "transitions")}
        d["clips"] = [c.to_dict() for c in self.clips]
        d["transitions"] = [t.to_dict() for t in self.transitions]
        return d

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d, clips=lambda v: [Clip.from_dict(c) for c in v],
                          transitions=lambda v: [Transition.from_dict(t) for t in v])


@dataclass
class Marker:
    t: int
    name: str = ""
    comment: str = ""
    color: int = 3
    id: str = field(default_factory=new_id)

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


@dataclass
class Sequence:
    name: str = "Sequence 01"
    id: str = field(default_factory=new_id)
    width: int = 1920
    height: int = 1080
    fps_num: int = 30
    fps_den: int = 1
    sample_rate: int = 48000
    video_tracks: list = field(default_factory=list)   # index 0 is V1 (bottom)
    audio_tracks: list = field(default_factory=list)   # index 0 is A1 (top)
    markers: list = field(default_factory=list)
    in_point: int | None = None
    out_point: int | None = None                        # exclusive
    caption_style: dict = field(default_factory=dict)   # how new captions look (see App.caption_style)
    multicam: bool = False           # a multi-camera source: each video track is one camera
    multicam_audio: str = "0"        # what it sounds like: "follow", "all", or an audio track index
    playhead: int = 0
    px_per_frame: float = 4.0
    scroll_x: float = 0.0

    @property
    def fps(self):
        return self.fps_num / self.fps_den

    def tracks(self):
        return self.video_tracks + self.audio_tracks

    def tracks_of(self, kind):
        return self.video_tracks if kind == "video" else self.audio_tracks

    def track(self, track_id):
        for t in self.tracks():
            if t.id == track_id:
                return t
        return None

    def track_label(self, track):
        lst = self.tracks_of(track.kind)
        return ("V" if track.kind == "video" else "A") + str(lst.index(track) + 1)

    def find_clip(self, clip_id):
        for t in self.tracks():
            c = t.clip(clip_id)
            if c is not None:
                return t, c
        return None, None

    def all_clips(self):
        for t in self.tracks():
            for c in t.clips:
                yield t, c

    def end(self):
        return max((t.end() for t in self.tracks()), default=0)

    def add_track(self, kind, index=None):
        lst = self.tracks_of(kind)
        track = Track(kind=kind)
        lst.insert(len(lst) if index is None else index, track)
        return track

    def edit_points(self):
        pts = {0}
        for _, c in self.all_clips():
            pts.add(c.start)
            pts.add(c.end)
        return sorted(pts)

    def snapshot_tracks(self):
        return ([t.working_copy() for t in self.video_tracks],
                [t.working_copy() for t in self.audio_tracks])

    def restore_tracks(self, snap):
        self.video_tracks = [t.working_copy() for t in snap[0]]
        self.audio_tracks = [t.working_copy() for t in snap[1]]

    def to_dict(self):
        d = {f.name: getattr(self, f.name) for f in fields(self)
             if f.name not in ("video_tracks", "audio_tracks", "markers")}
        d["video_tracks"] = [t.to_dict() for t in self.video_tracks]
        d["audio_tracks"] = [t.to_dict() for t in self.audio_tracks]
        d["markers"] = [m.to_dict() for m in self.markers]
        return d

    @classmethod
    def from_dict(cls, d):
        tracks = lambda v: [Track.from_dict(t) for t in v]
        return _from_dict(cls, d, video_tracks=tracks, audio_tracks=tracks,
                          markers=lambda v: [Marker.from_dict(m) for m in v])

    @classmethod
    def create(cls, name, width=1920, height=1080, fps_num=30, fps_den=1,
               sample_rate=48000, video_tracks=3, audio_tracks=3):
        seq = cls(name=name, width=width, height=height, fps_num=fps_num,
                  fps_den=fps_den, sample_rate=sample_rate)
        for _ in range(video_tracks):
            seq.add_track("video")
        for _ in range(audio_tracks):
            seq.add_track("audio")
        return seq


@dataclass
class MediaItem:
    """An entry in the project panel: a file, a generated item, a nested
    sequence, or a bin (folder)."""
    name: str
    kind: str                        # video | audio | image | sequence | color | title | bars | tone | bin
    id: str = field(default_factory=new_id)
    path: str | None = None
    parent: str | None = None        # containing bin id
    duration: float = 0.0            # seconds; 0 for stills and generators
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    colorspace: str = ""             # stream colour matrix tag as reported by ffprobe
    channels: int = 0
    sample_rate: int = 0
    label: int = 0
    sequence_id: str | None = None
    params: dict = field(default_factory=dict)
    mark_in: float | None = None     # seconds, source monitor marks
    mark_out: float | None = None
    offline: bool = False

    @property
    def is_bin(self):
        return self.kind == "bin"

    @property
    def unlimited(self):
        """True when the item has no natural length (stills, generators)."""
        return self.kind in ("image",) + GENERATED_KINDS

    def to_dict(self):
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


class Project:
    def __init__(self, name="Untitled"):
        self.name = name
        self.path = None
        self.media = {}              # id -> MediaItem (insertion ordered)
        self.sequences = {}          # id -> Sequence
        self.active_sequence_id = None
        self.settings = dict(DEFAULT_SETTINGS)   # frame shape and rates new sequences start with
        # media id -> {"path": proxy file, "source": original's size|mtime}. Kept
        # out of ``to_dict`` so undo never forgets a proxy; saved with the file.
        self.proxies = {}
        self.dirty = False

    # -- media -----------------------------------------------------------
    def add_media(self, item):
        self.media[item.id] = item
        return item

    def children(self, bin_id=None):
        return [m for m in self.media.values() if m.parent == bin_id]

    def media_for_path(self, path):
        for m in self.media.values():
            if m.path == path:
                return m
        return None

    def media_in_use(self, media_id):
        return any(c.media_id == media_id
                   for s in self.sequences.values() for _, c in s.all_clips())

    def remove_media(self, media_id):
        """Remove an item (and a bin's contents). Returns removed ids."""
        removed = []
        stack = [media_id]
        while stack:
            mid = stack.pop()
            item = self.media.pop(mid, None)
            if item is None:
                continue
            removed.append(mid)
            stack.extend(m.id for m in self.children(mid))
            if item.kind == "sequence" and item.sequence_id in self.sequences:
                del self.sequences[item.sequence_id]
                if self.active_sequence_id == item.sequence_id:
                    self.active_sequence_id = next(iter(self.sequences), None)
        for seq in self.sequences.values():
            for track in seq.tracks():
                track.clips = [c for c in track.clips if c.media_id not in removed]
        return removed

    # -- sequences -------------------------------------------------------
    @property
    def sequence(self):
        return self.sequences.get(self.active_sequence_id)

    def add_sequence(self, seq, parent=None):
        self.sequences[seq.id] = seq
        item = MediaItem(name=seq.name, kind="sequence", sequence_id=seq.id,
                         has_video=True, has_audio=True, width=seq.width,
                         height=seq.height, fps=seq.fps, label=LABEL_NESTED, parent=parent)
        self.add_media(item)
        if self.active_sequence_id is None:
            self.active_sequence_id = seq.id
        return item

    def media_for_sequence(self, seq_id):
        for m in self.media.values():
            if m.kind == "sequence" and m.sequence_id == seq_id:
                return m
        return None

    def unique_name(self, base):
        names = {m.name for m in self.media.values()}
        if base not in names:
            return base
        n = 2
        while f"{base} {n:02d}" in names:
            n += 1
        return f"{base} {n:02d}"

    def multicam_of(self, media):
        """The multi-camera sequence behind a project item, or None."""
        if media is None or media.kind != "sequence":
            return None
        inner = self.sequences.get(media.sequence_id)
        return inner if inner is not None and inner.multicam else None

    def media_frames(self, media, seq, speed=1.0):
        """Frames available from a media item in ``seq``'s timebase, or None
        when unlimited."""
        if media is None or media.unlimited:
            return None
        if media.kind == "sequence":
            inner = self.sequences.get(media.sequence_id)
            n = inner.end() if inner else 0
            if inner and inner.fps != seq.fps:
                n = int(n * seq.fps / inner.fps)
        else:
            n = int(media.duration * seq.fps + 1e-6)
        return max(1, int(n / abs(speed or 1.0)))

    def clip_frames(self, clip, seq, speed=None):
        """Frames of a clip's own timebase that show media (``in_point +
        duration`` may not pass it), counting its time remapping; None when
        unlimited."""
        media = self.media.get(clip.media_id)
        total = self.media_frames(media, seq, speed or clip.speed)
        if total is None or speed is not None or media.kind not in timeremap.MEDIA_KINDS:
            return total
        remap = timeremap.remap_of(clip)
        return total if remap is None else max(1, remap.length(total))

    # -- persistence -----------------------------------------------------
    def to_dict(self):
        return {
            "format": PROJECT_FORMAT,
            "name": self.name,
            "settings": dict(self.settings),
            "media": [m.to_dict() for m in self.media.values()],
            "sequences": [s.to_dict() for s in self.sequences.values()],
            "active_sequence_id": self.active_sequence_id,
        }

    def load_dict(self, d):
        self.name = d.get("name", "Untitled")
        saved = d.get("settings") if isinstance(d.get("settings"), dict) else {}
        self.settings = {k: int(saved.get(k) or v) for k, v in DEFAULT_SETTINGS.items()}
        self.media = {m["id"]: MediaItem.from_dict(m) for m in d.get("media", [])}
        self.sequences = {s["id"]: Sequence.from_dict(s) for s in d.get("sequences", [])}
        self.active_sequence_id = d.get("active_sequence_id")
        if self.active_sequence_id not in self.sequences:
            self.active_sequence_id = next(iter(self.sequences), None)

    def file_dict(self, base):
        """``to_dict`` plus what only the saved file holds: proxies, and
        paths relative to ``base`` (the project's folder)."""
        data = self.to_dict()
        proxies = {}
        for mid, entry in self.proxies.items():
            if mid in self.media and entry.get("path"):
                proxies[mid] = dict(entry)
                try:
                    proxies[mid]["relpath"] = os.path.relpath(entry["path"], base)
                except ValueError:
                    pass
        if proxies:
            data["proxies"] = proxies
        for m in data["media"]:
            if m.get("path"):
                # Store a relative path next to the absolute one so a project
                # folder can be moved together with its media.
                try:
                    m["relpath"] = os.path.relpath(m["path"], base)
                except ValueError:
                    pass
        return data

    def save(self, path=None):
        path = path or self.path
        data = self.file_dict(os.path.dirname(os.path.abspath(path)))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.dirty = False

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("format", 0) > PROJECT_FORMAT:
            raise ValueError("This project was saved by a newer version of the application.")
        base = os.path.dirname(os.path.abspath(path))
        for m in data.get("media", []):
            rel = m.pop("relpath", None)
            if m.get("path") and not os.path.exists(m["path"]) and rel:
                candidate = os.path.normpath(os.path.join(base, rel))
                if os.path.exists(candidate):
                    m["path"] = candidate
        proj = cls()
        proj.load_dict(data)
        for mid, entry in (data.get("proxies") or {}).items():
            if not isinstance(entry, dict) or mid not in proj.media or not entry.get("path"):
                continue
            rel = entry.pop("relpath", None)
            if not os.path.exists(entry["path"]) and rel:
                candidate = os.path.normpath(os.path.join(base, rel))
                if os.path.exists(candidate):
                    entry["path"] = candidate
            proj.proxies[mid] = {"path": entry["path"], "source": str(entry.get("source", ""))}
        proj.path = path
        proj.name = os.path.splitext(os.path.basename(path))[0]
        for m in proj.media.values():
            m.offline = bool(m.path) and not os.path.exists(m.path)
        return proj
