"""Time remapping: a clip's speed as a keyframed curve.

The Time Remap effect has one keyframable parameter, Speed, in percent of the
clip's own speed (Clip ▸ Speed/Duration still sets that base speed). Like any
other keyframes, its keys sit in the clip's own frames, so they stay with the
picture when the clip is trimmed or moved.

The picture shown at clip frame ``p`` is the running sum of the speed curve,
pinned at the effect's anchor frame (the clip's first frame when remapping was
switched on): ``r(anchor) = anchor`` and ``r(p + 1) = r(p) + speed(p) / 100``.
At a constant 100 % ``r(p) = p`` and the clip plays exactly as it did before.
Speeds may be 0 (a freeze) or negative (the clip runs backwards).

``r`` is handed to MLT's ``timeremap`` link as a time map. Outside the
keyframed span the speed is constant, so the map only needs a point per frame
between the first and the last keyframe; MLT interpolates the straight parts.
"""

import math

from . import keyframes as kf

EFFECT = "time_remap"
MEDIA_KINDS = ("video", "audio")       # only media files can be remapped
SPEED_RANGE = (-1000.0, 1000.0)
MAX_FRAMES = 10 * 3600 * 60            # cap for a clip that never runs out (a final freeze)


def effect_of(clip):
    """The clip's Time Remap effect when it is switched on, else None."""
    for e in clip.effects:
        if e.type == EFFECT:
            return e if e.enabled else None
    return None


class Remap:
    """The frame mapping of one Time Remap effect, in the clip's frames."""

    def __init__(self, effect):
        keys = effect.keyframes.get("speed") or []
        self.keys = [list(k) for k in keys]
        self.anchor = int(effect.params.get("anchor", 0) or 0)
        self.blend = bool(effect.params.get("blend", False))
        if self.keys:
            self.k0, self.kn = int(self.keys[0][0]), int(self.keys[-1][0])
            self.first, self.last = float(self.keys[0][1]), float(self.keys[-1][1])
            # cum[i]: sum of the speed over frames k0 .. k0 + i - 1
            cum, total = [0.0], 0.0
            for t in range(self.k0, self.kn):
                total += float(kf.evaluate(self.keys, t))
                cum.append(total)
            self.cum = cum
        else:
            speed = float(effect.params.get("speed", 100.0))
            self.k0 = self.kn = 0
            self.first = self.last = speed
            self.cum = [0.0]
        self._f_anchor = self._sum(self.anchor)

    @property
    def signature(self):
        return (self.anchor, self.blend, repr(self.keys) if self.keys else self.first)

    def _sum(self, p):
        """Sum of the speed (in %) over frames k0 .. p - 1, negative before k0."""
        if p <= self.k0:
            return (p - self.k0) * self.first
        if p >= self.kn:
            return self.cum[-1] + (p - self.kn) * self.last
        return self.cum[p - self.k0]

    def position(self, p):
        """Frame of the clip's unremapped producer shown at clip frame ``p``."""
        return self.anchor + (self._sum(p) - self._f_anchor) / 100.0

    def speed(self, p):
        return float(kf.evaluate(self.keys, p)) if self.keys else self.first

    def map_points(self, limit=MAX_FRAMES):
        """Frames at which ``position`` must be sampled for linear
        interpolation between them to be exact everywhere in 0 .. limit."""
        pts = {0, max(1, limit - 1)}
        if self.keys:
            pts.update(range(max(0, self.k0), max(0, self.kn) + 1))
        return sorted(pts)

    def length(self, total, limit=MAX_FRAMES):
        """Clip frames that stay inside a producer of ``total`` frames: the
        first frame from the anchor on whose picture lies past the end."""
        if total is None:
            return None
        end = total - 1
        p = max(0, self.anchor)
        if self.position(p) > end:
            return p
        stop = max(p, self.kn)
        while p < stop:
            p += 1
            if self.position(p) > end:
                return p
        rate = self.last / 100.0
        if rate <= 1e-9:
            return limit
        over = (end - self.position(p)) / rate
        return int(min(limit, p + math.floor(over) + 1))


def remap_of(clip):
    e = effect_of(clip)
    return Remap(e) if e is not None else None


def time_map(remap, fps, step, total=None, reverse=False, limit=MAX_FRAMES):
    """MLT ``time_map`` for the ``timeremap`` link: clip frame -> seconds of
    the source file. ``step`` is source frames per clip frame (the clip's own
    speed); a reversed clip counts back from the last of ``total`` frames."""
    def seconds(p):
        r = remap.position(p)
        if reverse and total is not None:
            r = total - 1 - r
        return r * step / fps

    points = [(p, seconds(p)) for p in remap.map_points(limit)]
    # MLT interpolates between the points, so where the curve runs past the
    # start of the file a point is added at the crossing before clamping.
    out = []
    for i, (p, v) in enumerate(points):
        if i and (v < 0) != (points[i - 1][1] < 0):
            q, u = points[i - 1]
            cross = int(round(q + (0 - u) * (p - q) / (v - u)))
            if q < cross < p:
                out.append((cross, seconds(cross)))
        out.append((p, v))
    return ";".join(f"{p}={max(0.0, v):.5f}" for p, v in out)
