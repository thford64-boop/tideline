"""Headless tests for timecode, keyframes and timeline edit operations."""

import json
import os
import tempfile
import unittest

from tideline import keyframes as kf
from tideline import procedural as proc
from tideline import timecode as tc
from tideline import timeremap
from tideline.edits import AUDIO_TRANSITIONS, VIDEO_TRANSITIONS, EditError, Editor
from tideline import presets
from tideline import settings as st
from tideline.engine.effects import EFFECTS
from tideline.model import (ASPECTS, Effect, MediaItem, Project, Sequence, aspect_index, aspect_name,
                            size_for_aspect)


def make_project():
    p = Project()
    seq = Sequence.create("Seq", 1280, 720, 30, 1)
    p.add_sequence(seq)
    a = p.add_media(MediaItem(name="a.mp4", kind="video", path="/x/a.mp4", duration=10.0,
                              has_video=True, has_audio=True, width=1280, height=720, fps=30))
    b = p.add_media(MediaItem(name="b.mp4", kind="video", path="/x/b.mp4", duration=10.0,
                              has_video=True, has_audio=True, width=1280, height=720, fps=30))
    still = p.add_media(MediaItem(name="s.png", kind="image", path="/x/s.png", has_video=True))
    music = p.add_media(MediaItem(name="m.wav", kind="audio", path="/x/m.wav", duration=60.0,
                                  has_audio=True))
    return p, seq, a, b, still, music


def layout(track):
    return [(c.start, c.end, c.in_point) for c in track.clips]


class Timecode(unittest.TestCase):
    def test_non_drop(self):
        self.assertEqual(tc.frames_to_tc(0, 30), "00:00:00:00")
        self.assertEqual(tc.frames_to_tc(30 * 3661 + 12, 30), "01:01:01:12")
        self.assertEqual(tc.frames_to_tc(24 * 60, 24000, 1001), "00:01:00:00")

    def test_drop_frame(self):
        self.assertEqual(tc.frames_to_tc(1800, 30000, 1001), "00;01;00;02")
        self.assertEqual(tc.frames_to_tc(17982, 30000, 1001), "00;10;00;00")
        self.assertEqual(tc.frames_to_tc(107892, 30000, 1001), "01;00;00;00")

    def test_roundtrip(self):
        for num, den in ((30, 1), (25, 1), (24000, 1001), (30000, 1001), (60000, 1001)):
            for f in (0, 1, 29, 30, 1799, 1800, 17981, 17982, 54321, 107892, 250000):
                text = tc.frames_to_tc(f, num, den)
                self.assertEqual(tc.tc_to_frames(text, num, den), (f, False), (num, den, f, text))

    def test_parse_shorthand(self):
        self.assertEqual(tc.tc_to_frames("1015", 30), (10 * 30 + 15, False))
        self.assertEqual(tc.tc_to_frames("+10", 30), (10, True))
        self.assertEqual(tc.tc_to_frames("-1:00", 30), (-30, True))
        self.assertEqual(tc.tc_to_frames("1:00:00:00", 25), (90000, False))
        self.assertIsNone(tc.tc_to_frames("abc", 30))


class Keyframes(unittest.TestCase):
    def test_evaluate(self):
        k = [[0, 0.0, "linear"], [10, 100.0, "hold"], [20, 50.0, "linear"]]
        self.assertEqual(kf.evaluate(k, -5), 0.0)
        self.assertEqual(kf.evaluate(k, 5), 50.0)
        self.assertEqual(kf.evaluate(k, 15), 100.0)
        self.assertEqual(kf.evaluate(k, 99), 50.0)
        self.assertEqual(kf.evaluate([[0, [0, 0], "linear"], [10, [10, 20], "linear"]], 5), [5, 10])

    def test_ease_is_monotonic_and_hits_ends(self):
        for interp in ("ease_in", "ease_out", "ease"):
            k = [[0, 0.0, interp], [20, 1.0, "linear"]]
            vals = [kf.evaluate(k, t) for t in range(21)]
            self.assertEqual(vals[0], 0.0)
            self.assertAlmostEqual(vals[-1], 1.0)
            self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))

    def test_set_remove(self):
        k = kf.set_key([], 10, 1.0)
        k = kf.set_key(k, 0, 0.0)
        k = kf.set_key(k, 10, 2.0)
        self.assertEqual([x[:2] for x in k], [[0, 0.0], [10, 2.0]])
        self.assertEqual(kf.neighbours(k, 5), (0, 10))
        self.assertEqual(kf.remove_key(k, 0), [[10, 2.0, "linear"]])

    def test_to_mlt(self):
        k = [[0, 0.0, "linear"], [10, 1.0, "hold"], [20, 0.5, "linear"]]
        self.assertEqual(kf.to_mlt(k), "0=0;10|=1;20=0.5")
        baked = kf.to_mlt([[0, 0.0, "ease"], [4, 1.0, "linear"]])
        self.assertEqual(len(baked.split(";")), 5)

    def test_sample_times(self):
        a = [[0, 0, "linear"], [10, 1, "linear"]]
        b = [[5, 0, "hold"], [8, 1, "linear"]]
        self.assertEqual(kf.sample_times(a, b), [0, 5, 7, 8, 10])

    def test_every_curve_starts_and_ends_on_its_keyframes(self):
        for interp, _ in kf.INTERPS:
            if interp == "hold":
                continue
            k = [[0, 10.0, interp], [40, 50.0, "linear"]]
            self.assertAlmostEqual(kf.evaluate(k, 0), 10.0, msg=interp)
            self.assertAlmostEqual(kf.evaluate(k, 40), 50.0, msg=interp)
            self.assertAlmostEqual(kf.ease(0.0, interp), 0.0, places=5, msg=interp)
            self.assertAlmostEqual(kf.ease(1.0, interp), 1.0, places=5, msg=interp)
        self.assertGreater(max(kf.ease(u / 50, "back_out") for u in range(51)), 1.05)     # overshoots
        self.assertLess(min(kf.ease(u / 50, "elastic_in") for u in range(51)), -0.05)

    def test_custom_bezier_curve(self):
        self.assertAlmostEqual(kf.bezier(0.3, [0.25, 0.25, 0.75, 0.75]), 0.3, places=4)   # a straight line
        slow_start = [[0, 0.0, "bezier", [0.9, 0.0, 1.0, 0.1]], [20, 100.0, "linear"]]
        self.assertLess(kf.evaluate(slow_start, 10), 15.0)
        over = [[0, 0.0, "bezier", [0.3, 1.8, 0.6, 1.0]], [20, 100.0, "linear"]]
        self.assertGreater(max(kf.evaluate(over, t) for t in range(21)), 105.0)
        self.assertEqual(len(kf.to_mlt(slow_start).split(";")), 21)                       # baked per frame
        # presets convert to a custom curve of nearly the same shape
        for interp in ("ease_in", "ease_out", "ease", "sine", "expo_out", "back_out"):
            curve = kf.curve_of([0, 0.0, interp])
            for u in (0.25, 0.5, 0.75):
                self.assertAlmostEqual(kf.bezier(u, curve), kf.ease(u, interp), delta=0.06, msg=interp)

    def test_custom_curve_survives_edits(self):
        k = [[0, 0.0, "bezier", [0.1, 0.2, 0.3, 0.4]], [20, 1.0, "linear"]]
        mid = kf.set_key(k, 10, 0.5)
        self.assertEqual(mid[1], [10, 0.5, "bezier", [0.1, 0.2, 0.3, 0.4]])               # inherits the segment's curve
        self.assertEqual(kf.set_key(k, 0, 5.0)[0], [0, 5.0, "bezier", [0.1, 0.2, 0.3, 0.4]])
        self.assertEqual(kf.set_key(k, 0, 5.0, "ease")[0], [0, 5.0, "ease"])
        self.assertEqual(kf.scale_times(k, 2)[0], [0, 0.0, "bezier", [0.1, 0.2, 0.3, 0.4]])


class Procedural(unittest.TestCase):
    def test_noise_is_repeatable_smooth_and_bounded(self):
        a = [proc.fractal(3, t / 10, 3) for t in range(400)]
        self.assertEqual(a, [proc.fractal(3, t / 10, 3) for t in range(400)])
        self.assertNotEqual(a, [proc.fractal(4, t / 10, 3) for t in range(400)])
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in a))
        self.assertGreater(max(a) - min(a), 0.8)
        self.assertLess(max(abs(y - x) for x, y in zip(a, a[1:])), 0.6)

    def test_waves(self):
        self.assertAlmostEqual(proc.wave("sine", 0.25), 1.0)
        self.assertAlmostEqual(proc.wave("triangle", 0.5), 1.0)
        self.assertAlmostEqual(proc.wave("triangle", 1.0), -1.0)
        self.assertEqual((proc.wave("square", 0.2), proc.wave("square", 0.7)), (1.0, -1.0))
        self.assertAlmostEqual(proc.wave("sawtooth", 2.75), 0.5)
        self.assertAlmostEqual(proc.wave("bounce", 0.5), 1.0)
        self.assertAlmostEqual(proc.cover_scale(1920, 1080, 0, 0, 0), 1.0)
        self.assertAlmostEqual(proc.cover_scale(1920, 1080, 96, 0, 0), 1.1)


class Edits(unittest.TestCase):
    def setUp(self):
        self.p, self.seq, self.a, self.b, self.still, self.music = make_project()
        self.ed = Editor(self.p, self.seq)
        self.v1, self.a1 = self.seq.video_tracks[0], self.seq.audio_tracks[0]

    def test_transition_lists_match_registry(self):
        from tideline.engine.effects import TRANSITIONS
        self.assertEqual({k for k, t in TRANSITIONS.items() if t.kind == "video"}, set(VIDEO_TRANSITIONS))
        self.assertEqual({k for k, t in TRANSITIONS.items() if t.kind == "audio"}, set(AUDIO_TRANSITIONS))

    def test_place_overwrite_creates_linked_pair(self):
        clips = self.ed.place(self.a, 0, src_in=30, src_out=130)
        self.assertEqual(len(clips), 2)
        self.assertEqual(layout(self.v1), [(0, 100, 30)])
        self.assertEqual(layout(self.a1), [(0, 100, 30)])
        self.assertEqual(clips[0].link_id, clips[1].link_id)
        self.assertEqual([e.type for e in clips[0].effects], ["motion", "opacity"])
        self.assertEqual([e.type for e in clips[1].effects], ["volume", "pan"])

    def test_overwrite_splits_underlying_clip(self):
        self.ed.place(self.a, 0)                      # 0..300
        self.ed.place(self.b, 100, src_in=0, src_out=50)
        self.assertEqual(layout(self.v1), [(0, 100, 0), (100, 150, 0), (150, 300, 150)])

    def test_insert_ripples_all_tracks(self):
        self.ed.place(self.a, 0)
        self.ed.place(self.music, 0, a_track=self.seq.audio_tracks[1], src_out=600)
        self.ed.place(self.b, 100, src_in=0, src_out=50, insert=True)
        self.assertEqual(layout(self.v1), [(0, 100, 0), (100, 150, 0), (150, 350, 100)])
        self.assertEqual(layout(self.seq.audio_tracks[1]), [(0, 100, 0), (150, 650, 100)])

    def test_still_gets_default_duration(self):
        (clip,) = self.ed.place(self.still, 0)
        self.assertEqual(clip.duration, 150)
        self.assertEqual(self.a1.clips, [])

    def test_split_relinks_halves(self):
        self.ed.place(self.a, 0)
        self.assertEqual(self.ed.split(120), 2)
        self.assertEqual(layout(self.v1), [(0, 120, 0), (120, 300, 120)])
        lv, rv = self.v1.clips
        la, ra = self.a1.clips
        self.assertEqual(lv.link_id, la.link_id)
        self.assertEqual(rv.link_id, ra.link_id)
        self.assertNotEqual(lv.link_id, rv.link_id)
        self.assertNotEqual(lv.effects[0].id, rv.effects[0].id)

    def test_delete_and_ripple_delete(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        first = self.v1.clips[0]
        self.ed.delete([first.id])
        self.assertEqual(layout(self.v1), [(100, 200, 0)])
        self.assertEqual(layout(self.a1), [(100, 200, 0)])
        self.assertTrue(self.ed.ripple_delete_gap(self.v1, 50))
        self.assertEqual(layout(self.v1), [(0, 100, 0)])
        self.assertEqual(layout(self.a1), [(0, 100, 0)])

    def test_ripple_delete_blocked_by_other_track(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        self.ed.place(self.music, 50, a_track=self.seq.audio_tracks[1], src_out=100, audio=True)
        self.ed.place(self.music, 160, a_track=self.seq.audio_tracks[1], src_out=40)
        self.ed.delete([self.v1.clips[0].id], ripple=True)
        # A2 has a clip ending at 150 and another starting at 160: only 10 frames of room.
        self.assertEqual(layout(self.v1), [(90, 190, 0)])
        self.assertEqual(layout(self.seq.audio_tracks[1]), [(50, 150, 0), (150, 190, 0)])

    def test_lift_and_extract(self):
        self.ed.place(self.a, 0)
        self.ed.lift(100, 150)
        self.assertEqual(layout(self.v1), [(0, 100, 0), (150, 300, 150)])
        self.ed.extract(100, 150)
        self.assertEqual(layout(self.v1), [(0, 100, 0), (100, 250, 150)])
        self.ed.extract(20, 40)
        self.assertEqual(layout(self.v1), [(0, 20, 0), (20, 80, 40), (80, 230, 150)])

    def test_trim_respects_media_and_neighbours(self):
        self.ed.place(self.a, 100, src_in=30, src_out=130)
        self.ed.place(self.b, 200, src_out=100)
        clip = self.v1.clips[0]
        self.assertEqual(self.ed.trim(clip.id, "start", 0), -30)       # only 30 frames of head
        self.assertEqual(layout(self.v1)[0], (70, 200, 0))
        self.assertEqual(layout(self.a1)[0], (70, 200, 0))             # linked audio follows
        self.assertEqual(self.ed.trim(clip.id, "end", 400), 0)         # neighbour in the way
        self.assertEqual(self.ed.trim(clip.id, "end", 150), -50)
        self.assertEqual(self.ed.trim(clip.id, "start", 500), 79)      # keeps one frame
        self.assertEqual(clip.duration, 1)

    def test_ripple_trim_shifts_followers(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        clip = self.v1.clips[0]
        self.assertEqual(self.ed.trim(clip.id, "end", 60, ripple=True), -40)
        self.assertEqual(layout(self.v1), [(0, 60, 0), (60, 160, 0)])
        self.assertEqual(layout(self.a1), [(0, 60, 0), (60, 160, 0)])
        self.assertEqual(self.ed.trim(self.v1.clips[1].id, "start", 80, ripple=True), 20)
        self.assertEqual(layout(self.v1), [(0, 60, 0), (60, 140, 20)])
        self.assertEqual(self.ed.trim(clip.id, "end", 90, ripple=True), 30)
        self.assertEqual(layout(self.v1), [(0, 90, 0), (90, 170, 20)])

    def test_roll(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_in=50, src_out=150)
        left = self.v1.clips[0]
        self.assertEqual(self.ed.roll(left.id, "end", 130), 30)
        self.assertEqual(layout(self.v1), [(0, 130, 0), (130, 200, 80)])
        self.assertEqual(self.ed.roll(left.id, "end", 0), -80)         # right clip has 80 frames of head
        self.assertEqual(layout(self.v1), [(0, 50, 0), (50, 200, 0)])

    def test_slip_and_slide(self):
        self.ed.place(self.a, 0, src_in=50, src_out=150)
        self.ed.place(self.b, 100, src_in=50, src_out=150)
        self.ed.place(self.a, 200, src_in=50, src_out=150)
        mid = self.v1.clips[1]
        self.assertEqual(self.ed.slip([mid.id], 500), 150)
        self.assertEqual(mid.in_point, 200)
        self.assertEqual(self.ed.slip([mid.id], -1000), -200)
        self.assertEqual(self.ed.slide(mid.id, 20), 20)
        self.assertEqual(layout(self.v1), [(0, 120, 50), (120, 220, 0), (220, 300, 70)])
        self.assertEqual(self.ed.slide(mid.id, -1000), -70)       # next clip runs out of head

    def test_rate_stretch_and_speed(self):
        self.ed.place(self.a, 0, src_in=0, src_out=100)
        clip = self.v1.clips[0]
        self.assertAlmostEqual(self.ed.rate_stretch(clip.id, "end", 200), 0.5)
        self.assertEqual((clip.duration, clip.in_point), (200, 0))
        self.assertEqual(self.a1.clips[0].duration, 200)
        self.ed.set_speed([clip.id], 2.0)
        self.assertEqual(clip.duration, 50)
        self.ed.set_speed([clip.id], 2.0, reverse=True)
        self.assertTrue(clip.reverse)
        self.assertEqual(clip.in_point, 150 - 50)
        with self.assertRaises(EditError):
            (s,) = self.ed.place(self.still, 400)
            self.ed.set_speed([s.id], 2.0)

    def test_speed_scales_keyframes(self):
        self.ed.place(self.a, 0, src_in=0, src_out=100)
        clip = self.v1.clips[0]
        original = clip.effects[1]
        original.keyframes["opacity"] = [[0, 0.0, "linear"], [50, 100.0, "linear"]]
        self.ed.set_speed([clip.id], 0.5)
        self.assertEqual(clip.effects[1].keyframes["opacity"][1][0], 100)
        self.assertEqual(original.keyframes["opacity"][1][0], 50)       # original untouched

    def test_move_overwrites_and_changes_track(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        a = self.v1.clips[0]
        ids = self.ed.move(self.ed.expand([a.id]), 150, 1, kind="video")
        self.assertEqual(layout(self.seq.video_tracks[1]), [(150, 250, 0)])
        self.assertEqual(layout(self.v1), [(100, 200, 0)])
        self.assertEqual(layout(self.a1), [(100, 150, 0), (150, 250, 0)])   # audio overwrote on A1
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.ed.move([self.v1.clips[0].id], -500), [self.v1.clips[0].id])
        self.assertEqual(self.v1.clips[0].start, 0)

    def test_move_insert_and_duplicate(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        b = self.v1.clips[1]
        self.ed.move(self.ed.expand([b.id]), -50, insert=True)
        self.assertEqual(layout(self.v1), [(0, 50, 0), (50, 150, 0), (150, 200, 50)])
        dup = self.ed.move([self.v1.clips[0].id], 300, duplicate=True)
        self.assertEqual(len(self.v1.clips), 4)
        self.assertNotIn(dup[0], [c.id for c in self.v1.clips[:3]])

    def test_locked_track_refuses(self):
        self.ed.place(self.a, 0)
        self.v1.locked = True
        with self.assertRaises(EditError):
            self.ed.delete([self.v1.clips[0].id])
        self.assertEqual(self.ed.split(50), 1)       # only the audio track is cut

    def test_transitions(self):
        self.ed.place(self.a, 0, src_in=30, src_out=130)
        self.ed.place(self.b, 100, src_in=30, src_out=130)
        a, b = self.v1.clips
        tr = self.ed.add_transition(a.id, "end", "cross_dissolve", 30)
        self.assertEqual((tr.clip_a, tr.clip_b, tr.align), (a.id, b.id, "center"))
        self.assertEqual(self.ed.transition_span(self.v1, tr), (85, 115))
        with self.assertRaises(EditError):
            self.ed.add_transition(a.id, "end", "constant_power", 30)
        # Replacing at the same cut keeps a single transition.
        self.ed.add_transition(b.id, "start", "dip_black", 20)
        self.assertEqual(len(self.v1.transitions), 1)
        # Splitting the outgoing clip keeps the transition on the cut.
        self.ed.split(50)
        self.assertEqual(self.v1.transitions[0].clip_a, self.v1.clips[1].id)
        # Moving the incoming clip away leaves a one-sided transition.
        self.ed.move([b.id], 100)
        self.assertEqual(self.v1.transitions[0].clip_b, None)
        self.ed.delete([self.v1.clips[1].id])
        self.assertEqual(self.v1.transitions, [])

    def test_transition_alignment_follows_handles(self):
        self.ed.place(self.a, 0, src_in=0, src_out=300)        # no tail handle
        self.ed.place(self.b, 300, src_in=60, src_out=160)
        tr = self.ed.add_transition(self.v1.clips[0].id, "end", "cross_dissolve", 30)
        self.assertEqual(tr.align, "end")
        self.assertEqual(self.ed.transition_span(self.v1, tr), (270, 300))

    def test_copy_paste(self):
        self.ed.place(self.a, 0, src_out=100)
        items = self.ed.copy_items([self.v1.clips[0].id])
        self.assertEqual(len(items), 2)
        ids = self.ed.paste(items, 200)
        self.assertEqual(layout(self.v1), [(0, 100, 0), (200, 300, 0)])
        v, a = (self.seq.find_clip(i)[1] for i in ids)
        self.assertEqual(v.link_id, a.link_id)
        self.assertNotEqual(v.link_id, self.v1.clips[0].link_id)
        self.ed.paste(items, 50, insert=True)
        self.assertEqual(layout(self.v1), [(0, 50, 0), (50, 150, 0), (150, 200, 50), (300, 400, 0)])

    def test_ripple_trim_to_playhead(self):
        self.ed.place(self.a, 0, src_out=100)
        self.ed.place(self.b, 100, src_out=100)
        self.ed.ripple_trim_to(130, "prev")
        self.assertEqual(layout(self.v1), [(0, 100, 0), (100, 170, 30)])
        self.ed.ripple_trim_to(50, "next")
        self.assertEqual(layout(self.v1), [(0, 50, 0), (50, 120, 30)])

    def test_snapshot_restore_is_isolated(self):
        self.ed.place(self.a, 0, src_out=100)
        snap = self.seq.snapshot_tracks()
        self.ed.trim(self.v1.clips[0].id, "end", 40)
        self.ed.split(20)
        self.seq.restore_tracks(snap)
        self.assertEqual(layout(self.seq.video_tracks[0]), [(0, 100, 0)])
        self.seq.restore_tracks(snap)
        Editor(self.p, self.seq).trim(self.seq.video_tracks[0].clips[0].id, "end", 10)
        self.seq.restore_tracks(snap)
        self.assertEqual(layout(self.seq.video_tracks[0]), [(0, 100, 0)])

    def test_project_roundtrip(self):
        import json
        self.ed.place(self.a, 0, src_out=100)
        self.ed.add_transition(self.v1.clips[0].id, "start", "cross_dissolve", 20)
        data = json.loads(json.dumps(self.p.to_dict()))
        q = Project()
        q.load_dict(data)
        self.assertEqual(q.to_dict(), self.p.to_dict())


class TimeRemapping(unittest.TestCase):
    def setUp(self):
        self.p, self.seq, self.a, _, self.still, _ = make_project()
        self.ed = Editor(self.p, self.seq)

    def remap(self, clip, speed=100.0, keys=None):
        e = Effect("time_remap", params={"speed": speed, "anchor": clip.in_point},
                   keyframes={"speed": keys} if keys else {})
        clip.effects = [e] + clip.effects
        return e

    def test_position_is_the_running_sum_of_speed(self):
        r = timeremap.Remap(Effect("time_remap", params={"speed": 100.0, "anchor": 40}))
        self.assertEqual([r.position(p) for p in (0, 40, 100)], [0, 40, 100])
        r = timeremap.Remap(Effect("time_remap", params={"anchor": 10},
                                   keyframes={"speed": [[10, 100.0, "linear"], [20, 300.0, "linear"]]}))
        self.assertEqual(r.position(10), 10)
        self.assertAlmostEqual(r.position(20), 10 + sum(100 + 20 * i for i in range(10)) / 100)
        self.assertAlmostEqual(r.position(25) - r.position(20), 15.0)        # 300 % after the last key
        self.assertAlmostEqual(r.position(5), 5.0)                           # 100 % before the first

    def test_length_follows_the_curve(self):
        (v, _) = self.ed.place(self.a, 0, src_in=0, src_out=100)              # 300 source frames
        self.remap(v, 200.0)
        self.assertEqual(self.p.clip_frames(v, self.seq), 150)
        self.remap(v, keys=[[0, 100.0, "linear"], [50, 0.0, "hold"]])
        self.assertEqual(self.p.clip_frames(v, self.seq), timeremap.MAX_FRAMES)  # ends in a freeze
        self.assertIsNone(self.p.clip_frames(self.ed.place(self.still, 0, v_track=self.seq.video_tracks[1])[0],
                                             self.seq))

    def test_speed_change_rescales_curve_and_anchor(self):
        (v, _) = self.ed.place(self.a, 0, src_in=60, src_out=160)
        e = self.remap(v, keys=[[60, 100.0, "linear"], [90, 50.0, "linear"]])
        self.ed.set_speed([v.id], 2.0)
        e = v.effect("time_remap")
        self.assertEqual(e.params["anchor"], 30)
        self.assertEqual([k[0] for k in e.keyframes["speed"]], [30, 45])

    def test_time_map_is_exact_and_clamped(self):
        r = timeremap.Remap(Effect("time_remap", params={"speed": -100.0, "anchor": 30}))
        text = timeremap.time_map(r, 30.0, 1.0, limit=100)
        points = dict(tuple(map(float, item.split("="))) for item in text.split(";"))
        self.assertEqual(points[0.0], 2.0)              # 60 source frames at 30 fps
        self.assertEqual(points[60.0], 0.0)             # crosses the file's first frame...
        self.assertEqual(points[99.0], 0.0)             # ...and holds it


class EverythingKeyframes(unittest.TestCase):
    def test_colours_blend_and_switches_hold(self):
        fade = [[0, "#ff0000ff", "linear"], [10, "#0000ff00", "linear"]]
        self.assertEqual(kf.evaluate(fade, 5), "#80008080")
        self.assertEqual(kf.evaluate(fade, 10), "#0000ff00")
        self.assertEqual(kf.color_rgba("#102030"), [16, 32, 48, 255])
        flag = [[0, False, "linear"], [10, True, "linear"]]
        self.assertIs(kf.evaluate(flag, 9), False)
        words = [[0, "one", "hold"], [10, "two", "hold"]]
        self.assertEqual((kf.evaluate(words, 9), kf.evaluate(words, 10)), ("one", "two"))
        self.assertEqual(kf.to_mlt(words, str), "0|=one;10=two")
        self.assertEqual(kf.to_mlt(fade, str), "0=#ff0000ff;10=#0000ff00")

    def test_every_parameter_can_be_keyframed(self):
        fixed = [(d.key, p.name) for d in EFFECTS.values() for p in d.params if not p.key]
        # Uniform Scale only links two rows; Frame Blending picks how the whole
        # remapped clip is drawn.
        self.assertEqual(fixed, [("motion", "uniform"), ("time_remap", "blend")])

    def test_segments_follow_stepped_keyframes(self):
        text, ctx = EFFECTS["text"], {"cut": (0, 99)}
        e = Effect("text")
        self.assertEqual(text.segments(e, ctx), [None])
        e.keyframes = {"offset": [[0, [0, 0], "linear"], [50, [9, 9], "linear"]],
                       "fill": [[0, "#ffffffff", "linear"], [50, "#000000ff", "linear"]]}
        self.assertEqual(text.segments(e, ctx), [None])             # MLT animates these itself
        e.keyframes = {"text": [[10, "a", "hold"], [40, "b", "hold"], [300, "c", "hold"]]}
        self.assertEqual(text.segments(e, ctx), [(0, 39), (40, 99)])
        self.assertEqual(text.segments(e, {"cut": (50, 99)}), [(50, None)])
        e.keyframes = {"size": [[10, 10, "linear"], [14, 50, "linear"]]}
        self.assertEqual(text.segments(e, ctx), [(0, 10), (11, 11), (12, 12), (13, 13), (14, 99)])
        e.keyframes = {"bold": [[0, False, "hold"], [1, True, "hold"]]}
        self.assertEqual(text.segments(e, ctx), [(0, None)])        # frame 0 cannot stand alone
        shake = Effect("shake", keyframes={"fit": [[0, True, "hold"], [30, False, "hold"]]})
        self.assertEqual(EFFECTS["shake"].segments(shake, ctx), [None])   # computed per frame instead


class Presets(unittest.TestCase):
    def setUp(self):
        self.p, self.seq, self.a, self.b, self.still, self.music = make_project()
        self.ed = Editor(self.p, self.seq)
        self.v, self.au = self.ed.place(self.a, 0, src_in=30, src_out=120)
        self.vt, self.at = self.seq.video_tracks[0], self.seq.audio_tracks[0]

    def test_capture_stores_times_from_the_clip_start(self):
        self.v.effect("opacity").keyframes = {"opacity": [[30, 0.0, "ease_out"], [60, 100.0, "linear"]]}
        self.v.effects.append(Effect("tint", params={"amount": 80.0}))
        self.au.effect("volume").params = {"level": -6.0}
        preset = presets.capture([(self.vt, self.v), (self.at, self.au)], self.seq, "Look")
        self.assertEqual([e["type"] for e in preset["effects"]], ["opacity", "tint", "volume"])
        self.assertEqual(preset["effects"][0]["keyframes"]["opacity"][0][:2], [0, 0.0])
        with self.assertRaises(presets.PresetError):
            presets.capture([(self.vt, Editor(self.p, self.seq).place(self.b, 200)[0])], self.seq, "Empty")

    def test_apply_rescales_time_and_position(self):
        raw = {"tideline_preset": 1, "name": "Slide", "fps": 60, "width": 1920, "height": 1080, "effects": [
            {"type": "motion", "keyframes": {"position": [[0, [0, 540], "expo_out"], [60, [960, 540], "linear"]]}},
            {"type": "tint", "params": {"amount": 40.0}},
            {"type": "gain", "params": {"level": 3.0}}]}
        preset = presets.parse(raw)
        effects = presets.apply(preset, self.vt, self.v, self.seq)       # 1280x720 at 30 fps, in point 30
        motion = next(e for e in effects if e.type == "motion")
        self.assertEqual(motion.id, self.v.effect("motion").id)
        self.assertEqual(motion.keyframes["position"], [[30, [0.0, 360.0], "expo_out"], [60, [640.0, 360.0], "linear"]])
        self.assertEqual([e.type for e in effects][-1], "tint")
        self.assertNotIn("gain", [e.type for e in effects])
        audio = presets.apply(preset, self.at, self.au, self.seq)
        self.assertEqual(audio[-1].type, "gain")
        self.assertEqual(presets.kinds(preset), {"video", "audio"})

    def test_untrusted_files_are_cleaned(self):
        raw = {"tideline_preset": 1, "effects": [
            {"type": "tint", "params": {"amount": "lots", "white": "#zzzzzz", "black": "#102030"},
             "keyframes": {"amount": [[0, 5000, "wobbly"], ["x"], [5, 20.0, "bezier"]], "bogus": [[0, 1, "linear"]]}},
            {"type": "mirror", "keyframes": {"mode": [[0, "vertical", "linear"], [9, "sideways", "hold"]]}},
            {"type": "does_not_exist"}, "junk"]}
        preset = presets.parse(raw)
        tint, mirror = preset["effects"]
        self.assertEqual(tint["params"], {"black": "#102030"})
        self.assertEqual(tint["keyframes"], {"amount": [[0, 100.0, "linear"], [5, 20.0, "bezier", kf.DEFAULT_BEZIER]]})
        self.assertEqual(mirror["keyframes"], {"mode": [[0, "vertical", "hold"]]})
        self.assertEqual(preset["unknown"], ["does_not_exist"])
        for bad in ({}, [], {"tideline_preset": 99, "effects": []}, {"tideline_preset": 1, "effects": [{"type": "x"}]}):
            with self.assertRaises(presets.PresetError):
                presets.parse(bad)

    def test_built_in_presets_load(self):
        names = [p["name"] for path, p, builtin in presets.library() if builtin]
        self.assertGreaterEqual(len(names), 5)
        self.assertIn("Fade In", names)

    def test_a_text_style_keeps_the_words(self):
        title = self.p.add_media(MediaItem(name="T", kind="title", has_video=True, params={"text": "Mine"}))
        (clip,) = self.ed.place(title, 300, v_track=self.vt)
        preset = presets.parse({"tideline_preset": 1, "effects": [
            {"type": "text", "params": {"text": "Theirs", "font": "Serif", "bold": True}}]})
        text = next(e for e in presets.apply(preset, self.vt, clip, self.seq) if e.type == "text")
        self.assertEqual((text.params["text"], text.params["font"], text.params["bold"]), ("Mine", "Serif", True))


class ProjectSettings(unittest.TestCase):
    def test_aspect_helpers(self):
        self.assertEqual(size_for_aspect(16, 9, 1080), (1920, 1080))
        self.assertEqual(size_for_aspect(9, 16, 1080), (1080, 1920))
        self.assertEqual(size_for_aspect(4, 5, 1080), (1080, 1350))
        self.assertEqual(size_for_aspect(1, 1, 720), (720, 720))
        for _, w, h in ASPECTS:
            size = size_for_aspect(w, h, 1080)
            self.assertEqual((size[0] % 2, size[1] % 2, min(size)), (0, 0, 1080))
            self.assertEqual(ASPECTS[aspect_index(*size)][1:], (w, h))
        self.assertEqual((aspect_name(1920, 1080), aspect_name(1080, 1920), aspect_name(1000, 700)),
                         ("16:9", "9:16", "1.43:1"))

    def test_settings_are_saved_with_the_project(self):
        p = Project()
        self.assertEqual((p.settings["width"], p.settings["height"]), (1920, 1080))
        p.settings.update(width=1080, height=1920, fps_num=60)
        q = Project()
        q.load_dict(json.loads(json.dumps(p.to_dict())))
        self.assertEqual(q.settings, p.settings)
        q.load_dict({"name": "old"})                                 # projects saved before the setting existed
        self.assertEqual(q.settings["width"], 1920)


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tideline-settings-")
        self.file = os.path.join(self.dir, "settings.json")

    def test_defaults_and_round_trip(self):
        s = st.Settings(self.file)
        self.assertFalse(s["setup_done"])
        self.assertEqual(st.matching_preset(s), "balanced")
        s.apply_preset("performance")
        s["layout"], s["setup_done"] = "audio", True
        s.save()
        t = st.Settings(self.file)
        self.assertEqual((t["layout"], t["setup_done"], t["preview_scale"]), ("audio", True, 0.25))
        self.assertEqual(st.matching_preset(t), "performance")
        t["ui_fps"] = 15
        self.assertEqual(st.matching_preset(t), "custom")

    def test_bad_values_fall_back(self):
        with open(self.file, "w") as fh:
            json.dump({"layout": 3, "recent": "nope", "ui_fps": 60, "unknown": 1}, fh)
        s = st.Settings(self.file)
        self.assertEqual((s["layout"], s["recent"], s["ui_fps"]), ("editing", [], 60))
        self.assertNotIn("unknown", s)

    def test_recent_projects(self):
        s = st.Settings(self.file)
        a, b = os.path.join(self.dir, "a.tideline"), os.path.join(self.dir, "b.tideline")
        open(a, "w").close()
        s.add_recent(a)
        s.add_recent(b)
        s.add_recent(a)
        self.assertEqual(s.recent_projects(), [(a, True), (b, False)])
        s.remove_recent(b)
        self.assertEqual(st.Settings(self.file)["recent"], [a])

    def test_render_threads(self):
        self.assertEqual(st.render_threads(2), 2)
        self.assertTrue(1 <= st.render_threads(0) <= 4)


if __name__ == "__main__":
    unittest.main()
