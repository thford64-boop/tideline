"""Builds MLT graphs from the data model.

A sequence becomes a tractor: a black base track, one playlist per video track
composited in order, and one playlist per audio track mixed in. Transitions
between adjacent clips become small nested tractors spliced into the playlist.

The graph is rebuilt from scratch after every structural edit; the expensive
part (opening media files) is avoided by caching producers between builds.
Parameter tweaks skip the rebuild and update the live filters instead.

A builder made with ``proxies=True`` (the editor's preview) reads each video
item's proxy file instead of the original when it has one; exports and
exported frames use a plain builder and so always render the originals.
"""

import os
from xml.sax.saxutils import escape

from .. import proxies as proxymod
from .. import timeremap
from .effects import EFFECTS, TRANSITIONS, fade_gain, gain_to_db
from .mltlib import color_to_mlt, init, make_profile, mlt

UNLIMITED = 10 * 3600 * 60       # frames given to stills and generators
MAX_NEST_DEPTH = 6


class Graph:
    """A built graph plus everything needed to keep it alive and update it."""

    def __init__(self, seq, profile):
        self.seq_id = seq.id
        self.profile = profile
        self.tractor = None
        self.length = 1
        self.filters = {}        # effect id -> [(EffectDef, [mlt filters], ctx)]
        self.layouts = {}        # effect id -> [(EffectDef, cut ctx, segments)]
        self.tracks = {}         # audio track id -> (playlist, volume, pan left, pan right)
        self.keep = []           # python references to MLT objects in the graph
        self.missing = set()     # effect/transition types whose MLT service is absent

    def update_effect(self, effect):
        """Push changed parameters to the live filters. False if not in graph."""
        entries = self.filters.get(effect.id)
        if not entries:
            return False
        # Keyframes on a parameter MLT cannot animate decide how many filters
        # the effect needs; when that changes the graph has to be rebuilt.
        if any(edef.segments(effect, ctx) != segs for edef, ctx, segs in self.layouts.get(effect.id, ())):
            return False
        for edef, filters, ctx in entries:
            edef.apply(filters, effect, ctx)
        return True

    def update_audio_tracks(self, seq):
        """Push track volume, pan, mute and solo to the live graph."""
        solo = any(t.solo for t in seq.audio_tracks)
        for track in seq.audio_tracks:
            entry = self.tracks.get(track.id)
            if entry is None:
                return False
            _apply_track_audio(track, solo, *entry)
        return True


class ProducerCache:
    def __init__(self):
        self.items = {}          # key -> [producer, generation, extra refs]
        self.generation = 0

    def begin(self):
        self.generation += 1

    def get(self, key, factory):
        item = self.items.get(key)
        if item is None:
            producer, refs = factory()
            item = self.items[key] = [producer, self.generation, refs]
        item[1] = self.generation
        return item[0]

    def end(self):
        """Drop producers that the last two builds did not use."""
        stale = [k for k, v in self.items.items() if v[1] < self.generation - 1]
        for k in stale:
            del self.items[k]

    def clear(self):
        self.items.clear()


class Builder:
    def __init__(self, project, proxies=False):
        init()
        self.project = project
        self.proxies = proxies
        self.cache = ProducerCache()
        self._profiles = {}      # sequence id -> (signature, profile)

    # -- profiles --------------------------------------------------------
    def profile_for(self, seq):
        sig = (seq.width, seq.height, seq.fps_num, seq.fps_den)
        cur = self._profiles.get(seq.id)
        if cur is None or cur[0] != sig:
            cur = (sig, make_profile(seq.width, seq.height, seq.fps_num, seq.fps_den))
            self._profiles[seq.id] = cur
        return cur[1]

    # -- public ----------------------------------------------------------
    def build(self, seq):
        self.cache.begin()
        profile = self.profile_for(seq)
        graph = Graph(seq, profile)
        self._build_into(graph, seq, profile, 0, (seq.id,))
        self.cache.end()
        return graph

    def build_media(self, media, seq=None):
        """Producer for previewing a single project item in the source monitor.
        Returns (profile, producer, length, refs)."""
        fps_num, fps_den = (seq.fps_num, seq.fps_den) if seq is not None else (30, 1)
        if media.kind == "sequence":
            inner = self.project.sequences.get(media.sequence_id)
            builder = Builder(self.project, self.proxies)
            graph = builder.build(inner)
            return graph.profile, graph.tractor, graph.length, [graph, builder]
        if seq is None and media.fps:
            fps_num, fps_den = _rational_fps(media.fps)
        w, h = media.width or (seq.width if seq else 1920), media.height or (seq.height if seq else 1080)
        if media.kind in ("audio", "tone") or not media.has_video:
            w, h = 640, 360
        profile = make_profile(w + (w % 2), h + (h % 2), fps_num, fps_den)
        producer, refs = self._make_producer(media, profile, 1.0, {"width": profile.width(),
                                                                  "height": profile.height()}, 0, ())
        if media.unlimited:
            length = int(round(5.0 * fps_num / fps_den))
            producer.set("length", length)
            producer.set("out", length - 1)
        else:
            length = producer.get_length()
        return profile, producer, max(1, length), refs + [profile]

    def source_path(self, media):
        """(file to read for a media item, True when it is a proxy). A proxy
        also stands in for an original that is offline."""
        if self.proxies and media.kind == "video":
            proxy = proxymod.find(self.project, media)
            if proxy:
                return proxy, True
        return media.path, False

    # -- sequence --------------------------------------------------------
    def _build_into(self, graph, seq, profile, depth, chain):
        ctx = {"width": profile.width(), "height": profile.height(), "fps": seq.fps}
        tractor = mlt.Tractor(profile)
        length = max(1, seq.end())
        bg = mlt.Producer(profile, "color:#ff000000" if depth == 0 else "color:#00000000")
        bg.set("length", length)
        bg.set("out", length - 1)
        tractor.set_track(bg, 0)
        graph.keep += [tractor, bg]
        index = 1
        for track in seq.video_tracks:
            pl = self._build_track(graph, seq, track, profile, ctx, depth, chain)
            pl.set("hide", 3 if track.hidden else 2)
            tractor.set_track(pl, index)
            blend = mlt.Transition(profile, "frei0r.cairoblend")
            if not blend.is_valid():
                blend = mlt.Transition(profile, "qtblend")
            tractor.plant_transition(blend, 0, index)
            graph.keep += [pl, blend]
            index += 1
        solo = any(t.solo for t in seq.audio_tracks)
        for track in seq.audio_tracks:
            pl = self._build_track(graph, seq, track, profile, ctx, depth, chain)
            vol = mlt.Filter(profile, "volume")
            pans = []
            for mask in (1, 2):
                pan = mlt.Filter(profile, "volume")
                pan.set("channel_mask", mask)
                pans.append(pan)
            for f in [vol] + pans:
                pl.attach(f)
            _apply_track_audio(track, solo, pl, vol, pans[0], pans[1])
            if depth == 0:
                graph.tracks[track.id] = (pl, vol, pans[0], pans[1])
            graph.keep += [vol] + pans
            tractor.set_track(pl, index)
            mix = mlt.Transition(profile, "mix")
            mix.set("always_active", 1)
            mix.set("sum", 1)
            tractor.plant_transition(mix, 0, index)
            graph.keep += [pl, mix]
            index += 1
        if depth == 0:
            level = mlt.Filter(profile, "audiolevel")
            if level.is_valid():
                level.set("iec_scale", 0)
                tractor.attach(level)
                graph.keep.append(level)
            graph.tractor = tractor
            graph.length = length
        return tractor, length

    # -- tracks ----------------------------------------------------------
    def _build_track(self, graph, seq, track, profile, ctx, depth, chain):
        pl = mlt.Playlist(profile)
        clips = sorted(track.clips, key=lambda c: c.start)
        lane = {c.id: i % 2 for i, c in enumerate(clips)}
        live = {c.id: c for c in clips if c.enabled and c.media_id in self.project.media}
        head = {cid: 0 for cid in live}     # frames at the clip head owned by a transition
        tail = {cid: 0 for cid in live}
        mixes = []
        for tr in track.transitions:
            tdef = TRANSITIONS.get(tr.type)
            if tdef is None or tdef.kind != track.kind:
                continue
            a, b = live.get(tr.clip_a), live.get(tr.clip_b)
            pre, post = tr.split()
            if a is not None and b is not None and a.end == b.start:
                room_a = self._handle_after(seq, a)
                room_b = self._handle_before(seq, b)
                pre = min(pre, room_b, a.duration - head[a.id] - tail[a.id])
                post = min(post, room_a, b.duration - head[b.id] - tail[b.id])
                if pre + post < 1:
                    continue
                tail[a.id] += pre
                head[b.id] += post
                mixes.append((a.end - pre, a.end + post, tr, tdef, a, b))
            elif b is not None and tr.clip_a is None:
                n = min(tr.duration, b.duration - head[b.id] - tail[b.id])
                if n >= 1:
                    head[b.id] += n
                    mixes.append((b.start, b.start + n, tr, tdef, None, b))
            elif a is not None and tr.clip_b is None:
                n = min(tr.duration, a.duration - head[a.id] - tail[a.id])
                if n >= 1:
                    tail[a.id] += n
                    mixes.append((a.end - n, a.end, tr, tdef, a, None))
        make = lambda clip, s, e, fade=None: self._cut(graph, seq, track, clip, lane[clip.id], s, e,
                                                       profile, ctx, depth, chain, fade)
        segments = []
        for c in live.values():
            s, e = c.start + head[c.id], c.end - tail[c.id]
            if e > s:
                segments.append((s, e, make(c, s, e)))
        for s, e, tr, tdef, a, b in mixes:
            prod = self._mix(graph, track, profile, tr, tdef, a, b, s, e, make)
            if prod is not None:
                segments.append((s, e, prod))
        pos = 0
        for s, e, prod in sorted(segments, key=lambda x: x[0]):
            if prod is None or s < pos:
                continue
            if s > pos:
                pl.blank(s - pos - 1)
            pl.append(prod)
            graph.keep.append(prod)
            pos = e
        return pl

    def _handle_after(self, seq, clip):
        total = self.project.clip_frames(clip, seq)
        return UNLIMITED if total is None else max(0, total - clip.in_point - clip.duration)

    def _handle_before(self, seq, clip):
        total = self.project.clip_frames(clip, seq)
        return UNLIMITED if total is None else max(0, clip.in_point)

    # -- clips -----------------------------------------------------------
    def _cut(self, graph, seq, track, clip, lane, s, e, profile, ctx, depth, chain, fade=None):
        """Producer for timeline range [s, e) of a clip, with its effects."""
        media = self.project.media[clip.media_id]
        speed = clip.speed * (-1 if clip.reverse else 1)
        path, _ = self.source_path(media)
        key = (seq.id, profile.width(), profile.height(), profile.frame_rate_num(),
               profile.frame_rate_den(), media.id, path, round(speed, 6), track.id, lane,
               self._revision(media))
        remap = timeremap.remap_of(clip) if media.kind in timeremap.MEDIA_KINDS else None
        if remap is not None and path and os.path.exists(path):
            key += ("remap", clip.id, remap.signature)
            parent = self.cache.get(key, lambda: self._make_remapped(media, profile, seq, clip, remap))
        else:
            parent = self.cache.get(key, lambda: self._make_producer(media, profile, speed, ctx, depth, chain))
        if parent is None or not parent.is_valid():
            return None
        src_in = max(0, clip.in_point + (s - clip.start))
        src_out = src_in + (e - s) - 1
        cut = parent.cut(src_in, src_out)
        graph.keep.append(cut)
        # Generated motion is baked over the frames this cut can show.
        last = clip.in_point + clip.duration - 1
        ctx = dict(ctx, clip=(clip.in_point, last), range=(min(clip.in_point, src_in), max(last, src_out)),
                   cut=(src_in, src_out))
        for effect in sorted(clip.effects, key=lambda x: getattr(EFFECTS.get(x.type), "stage", 1)):
            edef = EFFECTS.get(effect.type)
            if edef is None or not edef.fits(track.kind) or not edef.services:
                continue
            segments = edef.segments(effect, ctx)
            graph.layouts.setdefault(effect.id, []).append((edef, ctx, segments))
            for segment in segments:
                filters = edef.create(profile)
                if not filters:
                    graph.missing.add(edef.label)
                    break
                ectx = ctx if segment is None else dict(ctx, segment=segment)
                edef.apply(filters, effect, ectx)
                for f in filters:
                    if segment is not None and segment[1] is not None:
                        f.set("in", segment[0])
                        f.set("out", segment[1])
                    cut.attach(f)
                graph.filters.setdefault(effect.id, []).append((edef, filters, ectx))
        if fade is not None:
            self._attach_fade(graph, cut, track, profile, fade, src_in, src_out)
        return cut

    def _attach_fade(self, graph, cut, track, profile, fade, src_in, src_out):
        """Fade a cut in or out over its whole length. ``fade`` is
        (curve, fade_in) for audio or ("alpha", fade_in) for video."""
        curve, fade_in = fade
        n = src_out - src_in + 1
        if curve == "alpha":
            f = mlt.Filter(profile, "brightness")
            a, b = (0, 1) if fade_in else (1, 0)
            f.set("alpha", f"{src_in}={a};{src_out}={b}")
            f.set("level", 1.0)
        else:
            f = mlt.Filter(profile, "volume")
            pts = []
            for i in range(n):
                x = (i + 0.5) / n
                pts.append(f"{src_in + i}={gain_to_db(fade_gain(curve, x, fade_in)):.2f}")
            f.set("level", ";".join(pts))
        cut.attach(f)
        graph.keep.append(f)

    def _revision(self, media):
        if media.kind == "sequence":
            inner = self.project.sequences.get(media.sequence_id)
            return getattr(inner, "_rev", 0) if inner is not None else -1
        if media.kind in ("color", "bars", "tone"):
            return repr(sorted(media.params.items()))
        return 0

    def _make_remapped(self, media, profile, seq, clip, remap):
        """A media file behind MLT's timeremap link, playing the clip's speed
        curve. Returns (producer, refs)."""
        total = self.project.media_frames(media, seq, clip.speed)
        tmap = timeremap.time_map(remap, seq.fps, abs(clip.speed), total, clip.reverse)
        out = timeremap.MAX_FRAMES - 1
        path, proxy = self.source_path(media)
        xml = (f'<?xml version="1.0" encoding="utf-8"?><mlt><chain id="remap" out="{out}">'
               f'<property name="resource">{escape(path)}</property>'
               f'<property name="length">{out + 1}</property>'
               '<link id="timeremap"><property name="mlt_service">timeremap</property>'
               f'<property name="time_map">{tmap}</property>'
               f'<property name="image_mode">{"blend" if remap.blend else "nearest"}</property>'
               '<property name="pitch">1</property></link></chain></mlt>')
        p = mlt.Producer(profile, "xml-string", xml)
        if not p.is_valid():
            return self._make_producer(media, profile, clip.speed * (-1 if clip.reverse else 1),
                                       {}, 0, ())
        refs = [profile]
        if media.kind == "video" and not proxy and _needs_yuv_matrix_fix(media):
            fix = mlt.Filter(profile, "gamma")
            fix.set("gamma", 1.0)
            p.attach(fix)
            refs.append(fix)
        return p, refs

    def _make_producer(self, media, profile, speed, ctx, depth, chain):
        """Create the parent producer for a media item. Returns (producer, refs)."""
        refs = [profile]
        kind = media.kind
        if kind in ("video", "audio", "image"):
            path, proxy = self.source_path(media)
            if not path or not os.path.exists(path) or (media.offline and not proxy):
                p = mlt.Producer(profile, "color:#ff40161c")
                p.set("length", UNLIMITED)
                p.set("out", UNLIMITED - 1)
                return p, refs
            if kind != "image" and abs(speed - 1.0) > 1e-9:
                p = mlt.Producer(profile, f"timewarp:{speed:.6f}:{path}")
                if p.is_valid():
                    p.set("warp_pitch", 1 if 0.5 <= abs(speed) <= 2.0 else 0)
            else:
                p = mlt.Producer(profile, path)
            if p.is_valid() and kind == "video" and not proxy and _needs_yuv_matrix_fix(media):
                # MLT mis-converts BT.601 sources when they are scaled straight
                # to RGB (any preview-scaled or transformed frame). A no-op
                # filter that works in YUV forces the correct matrix first.
                fix = mlt.Filter(profile, "gamma")
                fix.set("gamma", 1.0)
                p.attach(fix)
                refs.append(fix)
            if not p.is_valid():
                p = mlt.Producer(profile, "color:#ff40161c")
                p.set("length", UNLIMITED)
                p.set("out", UNLIMITED - 1)
            elif kind == "image":
                p.set("length", UNLIMITED)
                p.set("out", UNLIMITED - 1)
                p.set("ttl", 1)
            return p, refs
        if kind == "sequence":
            inner = self.project.sequences.get(media.sequence_id)
            if inner is None or depth >= MAX_NEST_DEPTH or inner.id in chain:
                p = mlt.Producer(profile, "color:#00000000")
                p.set("length", UNLIMITED)
                p.set("out", UNLIMITED - 1)
                return p, refs
            sub = Graph(inner, profile)
            tractor, _ = self._build_into(sub, inner, profile, depth + 1, chain + (inner.id,))
            return tractor, refs + [sub]
        if kind == "tone":
            p = mlt.Producer(profile, "tone")
            p.set("frequency", float(media.params.get("frequency", 1000.0)))
            p.set("level", float(media.params.get("level", -12.0)))
        elif kind == "bars":
            p = mlt.Producer(profile, "frei0r.test_pat_B")
            if p.is_valid():
                p.set("0", 0.5)
            else:
                p = mlt.Producer(profile, "color:#ff808080")
        elif kind == "title":
            p = mlt.Producer(profile, "color:#00000000")
        else:
            p = mlt.Producer(profile, "color:" + color_to_mlt(media.params.get("color", "#000000ff")))
        p.set("length", UNLIMITED)
        p.set("out", UNLIMITED - 1)
        return p, refs

    # -- transitions -----------------------------------------------------
    def _mix(self, graph, track, profile, tr, tdef, a, b, s, e, make):
        n = e - s
        if track.kind == "audio":
            if a is None:
                return make(b, s, e, (tr.type, True))
            if b is None:
                return make(a, s, e, (tr.type, False))
            ta, tb = make(a, s, e, (tr.type, False)), make(b, s, e, (tr.type, True))
            if ta is None or tb is None:
                return ta or tb
            t = mlt.Tractor(profile)
            t.set_track(ta, 0)
            t.set_track(tb, 1)
            mix = mlt.Transition(profile, "mix")
            mix.set("always_active", 1)
            mix.set("sum", 1)
            t.plant_transition(mix, 0, 1)
            graph.keep += [t, mix]
            return t
        # video
        if a is None or b is None:
            # A transition with nothing on the other side fades to/from
            # transparency so lower tracks show through.
            return make(b, s, e, ("alpha", True)) if a is None else make(a, s, e, ("alpha", False))
        if tdef.mode == "dip":
            h = max(1, n // 2)
            pa, pb = make(a, s, s + h), (make(b, s + h, e) if n - h > 0 else None)
            col = mlt.Producer(profile, "color:" + color_to_mlt(tr.params.get("color", tdef.color)))
            col.set("length", n)
            col.set("out", n - 1)
            la, lb = mlt.Playlist(profile), mlt.Playlist(profile)
            if pa is not None:
                la.append(pa)
            lb.blank(h - 1)
            if pb is not None:
                lb.append(pb)
            t = mlt.Tractor(profile)
            t.set_track(la, 0)
            t.set_track(col, 1)
            t.set_track(lb, 2)
            l1 = mlt.Transition(profile, "luma")
            l1.set("in", 0)
            l1.set("out", h - 1)
            t.plant_transition(l1, 0, 1)
            l2 = mlt.Transition(profile, "luma")
            l2.set("in", h)
            l2.set("out", n - 1)
            t.plant_transition(l2, 1, 2)
            graph.keep += [t, col, la, lb, l1, l2]
            return t
        ta, tb = make(a, s, e), make(b, s, e)
        if ta is None or tb is None:
            return ta or tb
        t = mlt.Tractor(profile)
        t.set_track(ta, 0)
        t.set_track(tb, 1)
        if tdef.mode == "frei0r":
            x = mlt.Transition(profile, tdef.resource)
            if x.is_valid():
                x.set("0", f"0=0;{max(1, n - 1)}=1")
            else:
                graph.missing.add(tdef.label)
                x = mlt.Transition(profile, "luma")
        else:
            x = mlt.Transition(profile, "luma")
            if tdef.resource:
                x.set("resource", tdef.resource)
                x.set("softness", float(tr.params.get("softness", 0.05)))
                if tdef.reverse != bool(tr.params.get("reverse", False)):
                    x.set("invert", 1)
                    x.set("reverse", 1)
        x.set("in", 0)
        x.set("out", n - 1)
        t.plant_transition(x, 0, 1)
        graph.keep += [t, x]
        return t

    # -- export ----------------------------------------------------------
    def write_xml(self, seq, path):
        """Serialise a freshly built graph of ``seq`` to MLT XML for rendering."""
        graph = self.build(seq)
        consumer = mlt.Consumer(graph.profile, "xml", path)
        consumer.set("no_meta", 1)
        consumer.connect(graph.tractor)
        consumer.run()
        return graph.length


def _apply_track_audio(track, any_solo, playlist, volume, pan_left, pan_right):
    muted = track.hidden or (any_solo and not track.solo)
    playlist.set("hide", 3 if muted else 1)
    volume.set("level", float(track.volume))
    volume.set("disable", 0 if track.volume else 1)
    b = max(-100.0, min(100.0, track.pan)) / 100.0
    pan_left.set("level", gain_to_db(1.0 if b <= 0 else 1.0 - b))
    pan_right.set("level", gain_to_db(1.0 if b >= 0 else 1.0 + b))
    for f in (pan_left, pan_right):
        f.set("disable", 0 if b else 1)


def _needs_yuv_matrix_fix(media):
    """True when MLT will treat the file as BT.601 (tagged so, or untagged
    and small), which needs converting to the BT.709 working space."""
    tag = (getattr(media, "colorspace", "") or "").lower()
    if tag in ("smpte170m", "bt470bg", "smpte240m"):
        return True
    if tag in ("", "unknown", "unspecified"):
        return 0 < media.width * media.height <= 750000
    return False


def _rational_fps(fps):
    for num, den in ((24000, 1001), (30000, 1001), (60000, 1001), (120000, 1001)):
        if abs(fps - num / den) < 0.005:
            return num, den
    if abs(fps - round(fps)) < 0.005:
        return int(round(fps)), 1
    return int(round(fps * 1000)), 1000
