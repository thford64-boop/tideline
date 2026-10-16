"""Timeline edit operations.

All operations work on the plain data model and never touch MLT, so they can be
unit-tested headless and replayed for live drag previews. They mutate clips and
tracks in place but always *replace* a clip's effect list instead of mutating
effects, which lets drag previews use cheap shallow snapshots.
"""

from . import keyframes as kf
from .model import STILL_SECONDS, Clip, Effect, Transition, new_id

INF = float("inf")
MIN_SPEED, MAX_SPEED = 0.01, 100.0

VIDEO_TRANSITIONS = ("cross_dissolve", "dip_black", "dip_white", "wipe_left", "wipe_right",
                     "wipe_up", "wipe_down", "barn_door", "iris", "clock_wipe", "push_left",
                     "push_right", "slide_left", "slide_right")
AUDIO_TRANSITIONS = ("constant_power", "constant_gain", "exponential_fade")


class EditError(Exception):
    """An edit that cannot be performed; the message is shown to the user."""


def default_effects(kind, media=None):
    if kind == "audio":
        return [Effect("volume"), Effect("pan")]
    effects = []
    if media is not None and media.kind == "title":
        effects.append(Effect("text", params=dict(media.params)))
    effects += [Effect("motion"), Effect("opacity")]
    return effects


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Editor:
    def __init__(self, project, seq, linked=True):
        self.project = project
        self.seq = seq
        self.linked = linked

    # -- queries ---------------------------------------------------------
    def media(self, clip):
        return self.project.media.get(clip.media_id)

    def clip_len(self, clip, speed=None):
        """Frames available in the clip's own timebase; None if unlimited."""
        return self.project.media_frames(self.media(clip), self.seq, speed or clip.speed)

    def items(self, clip_ids):
        out = []
        for cid in clip_ids:
            track, clip = self.seq.find_clip(cid)
            if clip is not None:
                out.append((track, clip))
        return out

    def partners(self, clip):
        """Clips linked to ``clip`` (excluding itself)."""
        if not clip.link_id:
            return []
        return [(t, c) for t, c in self.seq.all_clips()
                if c.link_id == clip.link_id and c is not clip]

    def expand(self, clip_ids):
        """Clip ids plus the ids of everything linked to them."""
        ids = list(dict.fromkeys(clip_ids))
        if not self.linked:
            return ids
        seen = set(ids)
        for _, clip in self.items(list(ids)):
            for _, p in self.partners(clip):
                if p.id not in seen:
                    seen.add(p.id)
                    ids.append(p.id)
        return ids

    def _free(self, track):
        if track.locked:
            raise EditError(f"Track {self.seq.track_label(track)} is locked.")

    def _ripple_tracks(self):
        return [t for t in self.seq.tracks() if not t.locked and t.sync_lock]

    def target_track(self, kind):
        """Lowest targeted, unlocked track of a kind (falls back to any unlocked)."""
        tracks = self.seq.tracks_of(kind)
        for t in tracks:
            if t.target and not t.locked:
                return t
        for t in tracks:
            if not t.locked:
                return t
        return None

    def targeted(self):
        return [t for t in self.seq.tracks() if t.target and not t.locked]

    # -- primitives ------------------------------------------------------
    def _split_clip(self, track, clip, t, link_map=None):
        right = clip.copy(fresh_ids=True)
        right.start = t
        right.in_point = clip.in_point + (t - clip.start)
        right.duration = clip.end - t
        clip.duration = t - clip.start
        if clip.link_id:
            right.link_id = (link_map.setdefault(clip.link_id, new_id())
                             if link_map is not None else None)
        for tr in track.transitions:
            if tr.clip_a == clip.id:
                tr.clip_a = right.id
        track.clips.append(right)
        track.sort()
        return right

    def clear_range(self, track, a, b, link_map=None):
        """Remove everything in [a, b) on a track, splitting clips that straddle."""
        if b <= a:
            return
        for c in list(track.clips):
            if c.end <= a or c.start >= b:
                continue
            if c.start < a and c.end > b:
                right = self._split_clip(track, c, a, link_map)
                delta = b - right.start
                right.start, right.in_point, right.duration = b, right.in_point + delta, right.duration - delta
            elif c.start < a:
                c.duration = a - c.start
            elif c.end > b:
                delta = b - c.start
                c.start, c.in_point, c.duration = b, c.in_point + delta, c.duration - delta
            else:
                track.clips.remove(c)
        track.sort()

    def shift_from(self, t, delta, tracks=None, skip=()):
        for track in (self._ripple_tracks() if tracks is None else tracks):
            for c in track.clips:
                if c.start >= t and c.id not in skip:
                    c.start += delta
            track.sort()
        if delta:
            for m in self.seq.markers:
                if m.t >= t:
                    m.t = max(0, m.t + delta)

    def _room_before(self, t, tracks=None, skip=()):
        """How far clips starting at or after ``t`` can move left."""
        room = INF
        for track in (self._ripple_tracks() if tracks is None else tracks):
            after = [c for c in track.clips if c.start >= t and c.id not in skip]
            if not after:
                continue
            before = max((c.end for c in track.clips if c.start < t), default=0)
            room = min(room, max(0, min(c.start for c in after) - before))
        return room

    def insert_gap(self, t, n, tracks=None, link_map=None):
        """Open ``n`` frames at ``t``, splitting clips that straddle it."""
        tracks = self._ripple_tracks() if tracks is None else tracks
        link_map = {} if link_map is None else link_map
        for track in tracks:
            c = track.clip_at(t)
            if c is not None and c.start < t:
                self._split_clip(track, c, t, link_map)
        self.shift_from(t, n, tracks)

    # -- adding media ----------------------------------------------------
    def clips_from_media(self, media, src_in=None, src_out=None):
        """Build the (video, audio) clip pair for a project item."""
        fps = self.seq.fps
        total = self.project.media_frames(media, self.seq)
        if total is None:
            in_point = 0
            dur = int(src_out - src_in) if src_in is not None and src_out is not None \
                else int(round(STILL_SECONDS * fps))
        else:
            in_point = _clamp(int(src_in or 0), 0, total - 1)
            out = _clamp(int(src_out if src_out is not None else total), in_point + 1, total)
            dur = out - in_point
        dur = max(1, dur)
        want_v = media.has_video
        want_a = media.has_audio
        link = new_id() if want_v and want_a else None
        v = a = None
        if want_v:
            v = Clip(media_id=media.id, duration=dur, in_point=in_point, name=media.name,
                     link_id=link, effects=default_effects("video", media))
        if want_a:
            a = Clip(media_id=media.id, duration=dur, in_point=in_point, name=media.name,
                     link_id=link, effects=default_effects("audio", media))
        return v, a

    def place(self, media, t, v_track=None, a_track=None, src_in=None, src_out=None,
              insert=False, video=True, audio=True):
        """Insert or overwrite a project item at ``t``. Returns the new clips."""
        v, a = self.clips_from_media(media, src_in, src_out)
        if not video:
            v = None
        if not audio:
            a = None
        if v is not None and a is not None:
            pass
        else:
            for c in (v, a):
                if c is not None:
                    c.link_id = None
        placed = []
        for clip, kind, track in ((v, "video", v_track), (a, "audio", a_track)):
            if clip is None:
                continue
            if track is None:
                track = self.target_track(kind) or self.seq.add_track(kind)
            self._free(track)
            clip.start = max(0, int(t))
            placed.append((track, clip))
        if not placed:
            raise EditError("Nothing to place: the item has no usable video or audio.")
        dur = placed[0][1].duration
        if insert:
            self.insert_gap(max(0, int(t)), dur)
        for track, clip in placed:
            self.clear_range(track, clip.start, clip.end)
            track.clips.append(clip)
            track.sort()
        self.validate()
        return [c for _, c in placed]

    # -- cutting and removing --------------------------------------------
    def split(self, t, clip_ids=None, all_tracks=False):
        """Razor at ``t``. Returns the number of clips cut."""
        link_map, count = {}, 0
        if clip_ids:
            pairs = self.items(self.expand(clip_ids))
        else:
            tracks = [x for x in self.seq.tracks() if not x.locked] if all_tracks else self.targeted()
            pairs = [(tr, tr.clip_at(t)) for tr in tracks]
        for track, clip in pairs:
            if clip is None or track.locked or not (clip.start < t < clip.end):
                continue
            self._split_clip(track, clip, t, link_map)
            count += 1
        return count

    def delete(self, clip_ids, ripple=False):
        pairs = self.items(self.expand(clip_ids))
        if not pairs:
            return False
        for track, _ in pairs:
            self._free(track)
        ranges = sorted((c.start, c.end) for _, c in pairs)
        for track, c in pairs:
            track.clips.remove(c)
        if ripple:
            merged = []
            for a, b in ranges:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            for a, b in reversed(merged):
                d = min(b - a, self._room_before(b))
                if d > 0 and d != INF:
                    self.shift_from(b, -int(d))
        self.validate()
        return True

    def gap_at(self, track, t):
        """(start, end) of the gap containing ``t`` on a track, or None."""
        if track.clip_at(t) is not None:
            return None
        a = max((c.end for c in track.clips if c.end <= t), default=0)
        later = [c.start for c in track.clips if c.start > t]
        if not later:
            return None
        return a, min(later)

    def ripple_delete_gap(self, track, t):
        gap = self.gap_at(track, t)
        if gap is None:
            return False
        a, b = gap
        d = min(b - a, self._room_before(b))
        if d <= 0 or d == INF:
            raise EditError("The gap cannot be closed: clips on other tracks are in the way.")
        self.shift_from(b, -int(d))
        self.validate()
        return True

    def lift(self, a, b, tracks=None):
        if b <= a:
            return False
        link_map = {}
        for track in (self.targeted() if tracks is None else tracks):
            self.clear_range(track, a, b, link_map)
        self.validate()
        return True

    def extract(self, a, b):
        if b <= a:
            return False
        tracks = [t for t in self.seq.tracks() if not t.locked and (t.target or t.sync_lock)]
        link_map = {}
        for track in tracks:
            self.clear_range(track, a, b, link_map)
        self.shift_from(b, -(b - a), tracks)
        self.validate()
        return True

    def ripple_trim_to(self, t, side):
        """Ripple-trim from the previous ("prev") or to the next ("next") edit
        point on targeted tracks to ``t``. Returns the removed (start, end)."""
        pts = set()
        for track in self.targeted():
            for c in track.clips:
                pts.update((c.start, c.end))
        if side == "prev":
            before = [p for p in pts if p < t]
            if before and self.extract(max(before), t):
                return max(before), t
            return False
        after = [p for p in pts if p > t]
        if after and self.extract(t, min(after)):
            return t, min(after)
        return False

    # -- moving ----------------------------------------------------------
    def move(self, clip_ids, dt, dtrack=0, kind=None, insert=False, duplicate=False):
        """Move (or duplicate) clips by ``dt`` frames and ``dtrack`` tracks.

        ``kind`` restricts the track change to video or audio clips, which is
        what dragging one half of a linked pair does. Returns the ids of the
        clips at their new position.
        """
        pairs = self.items(clip_ids)
        if not pairs:
            return []
        dt = int(max(dt, -min(c.start for _, c in pairs)))
        shift = {}
        for k in ("video", "audio"):
            idx = [self.seq.tracks_of(k).index(t) for t, _ in pairs if t.kind == k]
            if not idx:
                continue
            d = dtrack if kind in (None, k) else 0
            shift[k] = _clamp(d, -min(idx), len(self.seq.tracks_of(k)) - 1 - max(idx))
        plan = []
        for track, c in pairs:
            lst = self.seq.tracks_of(track.kind)
            dest = lst[lst.index(track) + shift[track.kind]]
            self._free(dest)
            if not duplicate:
                self._free(track)
            plan.append((track, dest, c))
        link_map, moved = {}, []
        for track, dest, c in plan:
            if duplicate:
                nc = c.copy(fresh_ids=True)
                if c.link_id:
                    nc.link_id = link_map.setdefault(c.link_id, new_id())
            else:
                track.clips.remove(c)
                nc = c
                if dest is not track:
                    for tr in [x for x in track.transitions if nc.id in (x.clip_a, x.clip_b)]:
                        track.transitions.remove(tr)
                        dest.transitions.append(tr)
            nc.start += dt
            moved.append((dest, nc))
        if insert:
            t0 = min(c.start for _, c in moved)
            self.insert_gap(t0, max(c.end for _, c in moved) - t0)
        for dest, nc in moved:
            self.clear_range(dest, nc.start, nc.end)
        for dest, nc in moved:
            dest.clips.append(nc)
            dest.sort()
        self.validate()
        return [c.id for _, c in moved]

    def nudge(self, clip_ids, dt):
        return self.move(self.expand(clip_ids), dt)

    # -- trimming --------------------------------------------------------
    def _edge_group(self, clip_id, edge):
        track, clip = self.seq.find_clip(clip_id)
        if clip is None:
            return []
        at = clip.start if edge == "start" else clip.end
        group = [(track, clip)]
        if self.linked:
            for t, p in self.partners(clip):
                if (p.start if edge == "start" else p.end) == at:
                    group.append((t, p))
        return group

    def trim(self, clip_id, edge, new_t, ripple=False):
        """Trim a clip edge to ``new_t``. Returns the frames actually moved."""
        group = self._edge_group(clip_id, edge)
        if not group:
            return 0
        clip = group[0][1]
        old = clip.start if edge == "start" else clip.end
        lo, hi = -INF, INF
        for track, c in group:
            self._free(track)
            total = self.clip_len(c)
            if edge == "start":
                hi = min(hi, c.duration - 1)
                if total is not None:
                    lo = max(lo, -c.in_point)
                if not ripple:
                    prev = track.prev_clip(c)
                    lo = max(lo, (prev.end if prev else 0) - c.start)
            else:
                lo = max(lo, -(c.duration - 1))
                if total is not None:
                    hi = min(hi, total - c.in_point - c.duration)
                if not ripple:
                    nxt = track.next_clip(c)
                    if nxt is not None:
                        hi = min(hi, nxt.start - c.end)
        delta = int(_clamp(new_t - old, lo, hi))
        if ripple:
            ids = {c.id for _, c in group}
            point = max(c.end for _, c in group)
            shift = delta if edge == "end" else -delta
            if shift < 0:
                others = [t for t in self._ripple_tracks() if all(t is not g for g, _ in group)]
                room = self._room_before(point, others)
                if -shift > room:
                    shift = -int(room)
                    delta = shift if edge == "end" else -shift
        for track, c in group:
            if edge == "start":
                if not ripple:
                    c.start += delta
                c.in_point = max(0, c.in_point + delta)
                c.duration -= delta
            else:
                c.duration += delta
        if ripple and shift:
            self.shift_from(point, shift, skip=ids)
        if delta:
            self.validate()
        return delta

    def _cut_at(self, track, clip, edge):
        """(left, right) clips meeting at a clip edge, or None when there is a gap."""
        if edge == "end":
            nxt = track.next_clip(clip)
            return (clip, nxt) if nxt is not None and nxt.start == clip.end else None
        prev = track.prev_clip(clip)
        return (prev, clip) if prev is not None and prev.end == clip.start else None

    def roll(self, clip_id, edge, new_t):
        """Move the cut at a clip edge, lengthening one clip and shortening the other."""
        group = self._edge_group(clip_id, edge)
        cuts = []
        for track, c in group:
            cut = self._cut_at(track, c, edge)
            if cut is not None:
                self._free(track)
                cuts.append(cut)
        if not cuts:
            return self.trim(clip_id, edge, new_t)
        old = cuts[0][1].start
        lo, hi = -INF, INF
        for left, right in cuts:
            lo = max(lo, -(left.duration - 1))
            hi = min(hi, right.duration - 1)
            total_l, total_r = self.clip_len(left), self.clip_len(right)
            if total_l is not None:
                hi = min(hi, total_l - left.in_point - left.duration)
            if total_r is not None:
                lo = max(lo, -right.in_point)
        delta = int(_clamp(new_t - old, lo, hi))
        for left, right in cuts:
            left.duration += delta
            right.start += delta
            right.in_point = max(0, right.in_point + delta)
            right.duration -= delta
        if delta:
            self.validate()
        return delta

    def slip(self, clip_ids, d):
        """Shift the source range of clips by ``d`` frames without moving them."""
        pairs = [(t, c) for t, c in self.items(self.expand(clip_ids)) if self.clip_len(c) is not None]
        if not pairs:
            return 0
        lo, hi = -INF, INF
        for track, c in pairs:
            self._free(track)
            lo = max(lo, -c.in_point)
            hi = min(hi, self.clip_len(c) - c.duration - c.in_point)
        d = int(_clamp(d, lo, hi))
        for _, c in pairs:
            c.in_point += d
        return d

    def slide(self, clip_id, d):
        """Move a clip between its neighbours, trimming them to make room."""
        track, clip = self.seq.find_clip(clip_id)
        if clip is None:
            return 0
        group = [(track, clip)]
        if self.linked:
            group += [(t, p) for t, p in self.partners(clip)
                      if p.start == clip.start and p.end == clip.end]
        lo, hi, plan = -INF, INF, []
        for tr, c in group:
            self._free(tr)
            prev, nxt = tr.prev_clip(c), tr.next_clip(c)
            adj_prev = prev if prev is not None and prev.end == c.start else None
            adj_next = nxt if nxt is not None and nxt.start == c.end else None
            if adj_prev is not None:
                lo = max(lo, -(adj_prev.duration - 1))
                total = self.clip_len(adj_prev)
                if total is not None:
                    hi = min(hi, total - adj_prev.in_point - adj_prev.duration)
            else:
                lo = max(lo, (prev.end if prev else 0) - c.start)
            if adj_next is not None:
                hi = min(hi, adj_next.duration - 1)
                if self.clip_len(adj_next) is not None:
                    lo = max(lo, -adj_next.in_point)
            elif nxt is not None:
                hi = min(hi, nxt.start - c.end)
            plan.append((c, adj_prev, adj_next))
        d = int(_clamp(d, lo, hi))
        for c, prev, nxt in plan:
            c.start += d
            if prev is not None:
                prev.duration += d
            if nxt is not None:
                nxt.start += d
                nxt.in_point = max(0, nxt.in_point + d)
                nxt.duration -= d
        if d:
            self.validate()
        return d

    # -- speed -----------------------------------------------------------
    def _retime(self, c, speed, duration=None):
        ratio = c.speed / speed
        c.in_point = int(round(c.in_point * ratio))
        c.duration = max(1, int(duration if duration is not None else round(c.duration * ratio)))
        effects = []
        for e in c.effects:
            if e.keyframes:
                e = e.copy(fresh_id=False)
                e.keyframes = {k: kf.scale_times(v, ratio) for k, v in e.keyframes.items()}
            effects.append(e)
        c.effects = effects
        c.speed = speed
        total = self.clip_len(c)
        if total is not None:
            c.in_point = _clamp(c.in_point, 0, total - 1)
            c.duration = max(1, min(c.duration, total - c.in_point))

    def rate_stretch(self, clip_id, edge, new_t):
        """Change a clip's duration by changing its speed. Returns the new speed."""
        group = self._edge_group(clip_id, edge)
        if not group:
            return None
        clip = group[0][1]
        if any(self.clip_len(c) is None for _, c in group):
            self.trim(clip_id, edge, new_t)
            return clip.speed
        lo, hi = 1, INF
        for track, c in group:
            self._free(track)
            lo = max(lo, c.duration * c.speed / MAX_SPEED)
            hi = min(hi, c.duration * c.speed / MIN_SPEED)
            if edge == "end":
                nxt = track.next_clip(c)
                if nxt is not None:
                    hi = min(hi, nxt.start - c.start)
            else:
                prev = track.prev_clip(c)
                hi = min(hi, c.end - (prev.end if prev else 0))
        want = (new_t - clip.start) if edge == "end" else (clip.end - new_t)
        dur = int(_clamp(want, max(1, int(lo + 0.999)), hi))
        for _, c in group:
            end = c.end
            self._retime(c, c.speed * c.duration / dur, dur)
            if edge == "start":
                c.start = end - c.duration
        return clip.speed

    def set_speed(self, clip_ids, speed, reverse=None, ripple=False):
        """Set playback speed (1.0 = 100%) and optionally direction."""
        speed = _clamp(float(speed), MIN_SPEED, MAX_SPEED)
        pairs = [(t, c) for t, c in self.items(self.expand(clip_ids)) if self.clip_len(c) is not None]
        if not pairs:
            raise EditError("Speed can only be changed on clips made from media files.")
        for track, c in sorted(pairs, key=lambda p: -p[1].start):
            self._free(track)
            old_end = c.end
            if abs(c.speed - speed) > 1e-9:
                self._retime(c, speed)
            if reverse is not None and reverse != c.reverse:
                total = self.clip_len(c)
                c.in_point = max(0, total - (c.in_point + c.duration))
                effects = []
                for e in c.effects:
                    if e.keyframes:
                        e = e.copy(fresh_id=False)
                        e.keyframes = {k: kf.scale_times(v, -1, total - 1) for k, v in e.keyframes.items()}
                    effects.append(e)
                c.effects = effects
                c.reverse = reverse
            if ripple:
                if c.end != old_end:
                    delta = c.end - old_end
                    if delta < 0:
                        others = [t for t in self._ripple_tracks() if t is not track]
                        delta = -int(min(-delta, self._room_before(old_end, others)))
                    self.shift_from(old_end, delta, skip={c.id})
            else:
                nxt = track.next_clip(c)
                later = [x.start for x in track.clips if x is not c and x.start >= c.start]
                if later and c.end > min(later):
                    c.duration = max(1, min(later) - c.start)
        self.validate()
        return True

    # -- transitions -----------------------------------------------------
    def add_transition(self, clip_id, edge, type_, duration, align=None):
        track, clip = self.seq.find_clip(clip_id)
        if clip is None:
            raise EditError("Select a clip to apply the transition to.")
        self._free(track)
        want = "video" if type_ in VIDEO_TRANSITIONS else "audio"
        if track.kind != want:
            raise EditError(f"This is {'a video' if want == 'video' else 'an audio'} transition; "
                            f"apply it to {'a video' if want == 'video' else 'an audio'} clip.")
        cut = self._cut_at(track, clip, edge)
        a, b = cut if cut else ((None, clip) if edge == "start" else (clip, None))
        track.transitions = [t for t in track.transitions
                             if not ((a and t.clip_a == a.id) or (b and t.clip_b == b.id))]
        duration = max(2, int(duration))
        for c in (a, b):
            if c is not None:
                duration = min(duration, c.duration)
        if a is not None and b is not None and align is None:
            tail = self.clip_len(a)
            tail = INF if tail is None else tail - a.in_point - a.duration
            head = INF if self.clip_len(b) is None else b.in_point
            pre, post = duration // 2, duration - duration // 2
            if post <= tail and pre <= head:
                align = "center"
            elif tail < post and head >= duration:
                align = "end"
            elif head < pre and tail >= duration:
                align = "start"
            else:
                align = "center"
        tr = Transition(type=type_, duration=duration, clip_a=a.id if a else None,
                        clip_b=b.id if b else None, align=align or "center")
        track.transitions.append(tr)
        return tr

    def remove_transition(self, transition_id):
        for track in self.seq.tracks():
            for tr in track.transitions:
                if tr.id == transition_id:
                    self._free(track)
                    track.transitions.remove(tr)
                    return True
        return False

    def find_transition(self, transition_id):
        for track in self.seq.tracks():
            for tr in track.transitions:
                if tr.id == transition_id:
                    return track, tr
        return None, None

    def transition_span(self, track, tr):
        """(start, end) of a transition on the timeline, or None if dangling."""
        a = track.clip(tr.clip_a) if tr.clip_a else None
        b = track.clip(tr.clip_b) if tr.clip_b else None
        if a is None and b is None:
            return None
        cut = a.end if a is not None else b.start
        pre, post = tr.split()
        return cut - pre, cut + post

    def validate(self):
        """Repair transitions after an edit: drop dangling ones, detach sides
        that are no longer adjacent, and clamp durations."""
        for track in self.seq.tracks():
            keep, edges = [], set()
            for tr in track.transitions:
                a = track.clip(tr.clip_a) if tr.clip_a else None
                b = track.clip(tr.clip_b) if tr.clip_b else None
                if a is None:
                    tr.clip_a = None
                if b is None:
                    tr.clip_b = None
                if a is not None and b is not None and a.end != b.start:
                    tr.clip_b, b = None, None
                if a is None and b is None:
                    continue
                key = ("a", a.id) if a is not None else ("b", b.id)
                other = ("b", b.id) if (a is not None and b is not None) else None
                if key in edges or (other and other in edges):
                    continue
                edges.add(key)
                if other:
                    edges.add(other)
                limit = min(c.duration for c in (a, b) if c is not None)
                tr.duration = max(1, min(tr.duration, limit))
                keep.append(tr)
            track.transitions = keep

    # -- links -----------------------------------------------------------
    def link(self, clip_ids):
        pairs = self.items(clip_ids)
        if len(pairs) < 2:
            raise EditError("Select at least two clips to link.")
        lid = new_id()
        for _, c in pairs:
            c.link_id = lid
        return True

    def unlink(self, clip_ids):
        pairs = self.items(self.expand(clip_ids))
        for _, c in pairs:
            c.link_id = None
        return bool(pairs)

    # -- clipboard -------------------------------------------------------
    def copy_items(self, clip_ids):
        pairs = self.items(self.expand(clip_ids))
        if not pairs:
            return []
        t0 = min(c.start for _, c in pairs)
        out = []
        for track, c in pairs:
            d = c.to_dict()
            out.append({"clip": d, "kind": track.kind, "offset": c.start - t0,
                        "index": self.seq.tracks_of(track.kind).index(track)})
        return out

    def paste(self, items, t, insert=False):
        if not items:
            return []
        link_map, plan = {}, []
        for it in items:
            lst = self.seq.tracks_of(it["kind"])
            if not lst:
                continue
            track = lst[min(it["index"], len(lst) - 1)]
            self._free(track)
            c = Clip.from_dict(it["clip"]).copy(fresh_ids=True)
            if c.link_id:
                c.link_id = link_map.setdefault(c.link_id, new_id())
            c.start = int(t) + it["offset"]
            plan.append((track, c))
        if not plan:
            return []
        if insert:
            self.insert_gap(int(t), max(c.end for _, c in plan) - int(t))
        for track, c in plan:
            self.clear_range(track, c.start, c.end)
        for track, c in plan:
            track.clips.append(c)
            track.sort()
        self.validate()
        return [c.id for _, c in plan]

    # -- tracks ----------------------------------------------------------
    def select_forward(self, t, track=None):
        """Ids of every clip starting at or after ``t`` (optionally on one track)."""
        tracks = [track] if track is not None else self.seq.tracks()
        return [c.id for tr in tracks for c in tr.clips if c.end > t and not tr.locked]

    def delete_track(self, track):
        lst = self.seq.tracks_of(track.kind)
        if len(lst) <= 1:
            raise EditError("A sequence needs at least one track of each kind.")
        lst.remove(track)
        return True
