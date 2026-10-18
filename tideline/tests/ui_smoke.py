"""End-to-end UI test, run inside the real application.

    TIDELINE_DEBUG_SCRIPT=tests/ui_smoke.py python -m tideline

It generates its own media, then drives the same handlers the mouse and
keyboard use (timeline tools, drag and drop, Effect Controls, monitors,
shortcuts, save/load) and checks the resulting project state. The process
exits non-zero if any check fails. It needs a display; nothing is clicked on
your behalf, so the window can sit on any workspace.
"""

import os
import shutil
import subprocess
import tempfile
import traceback

from gi.repository import Gdk, GdkPixbuf, GLib

import json

from tideline import keyframes as kf
from tideline import proxies
from tideline.engine import media as mediamod
from tideline.engine.player import Player
from tideline.ui.timeline import HEADER_W

TMP = tempfile.mkdtemp(prefix="tideline-ui-")
mediamod.CACHE_DIR = os.path.join(TMP, "cache")          # keep test thumbnails out of ~/.cache
proxies.CACHE_DIR = os.path.join(TMP, "proxies")
results = []


def make_media():
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    aac = ["-c:a", "aac", "-ac", "2"]
    tone = lambda f, d: ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={d}:sample_rate=48000"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12", *tone(440, 12),
                         *x264, *aac, f"{TMP}/a.mp4"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(ff + ["-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=10", *tone(880, 10),
                         *x264, *aac, f"{TMP}/b.mp4"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(ff + ["-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=24", "-t", "8", *x264,
                         f"{TMP}/c.mp4"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(ff + [*tone(220, 20), "-ac", "2", f"{TMP}/music.wav"], check=True, stdin=subprocess.DEVNULL)
    shutil.copy(f"{TMP}/c.mp4", f"{TMP}/c_moved.mp4")
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4", *tone(330, 4),
                         *x264, *aac, f"{TMP}/hd.mp4"], check=True, stdin=subprocess.DEVNULL)
    # Two "cameras" hearing the same claps; camera B started 1.5 s later.
    claps = "if(lt(mod(t*1000\\,1370)\\,30)+lt(abs(t-2.2)\\,0.02)+lt(abs(t-5.1)\\,0.03)\\,sin(2*PI*1000*t)\\,0)"
    subprocess.run(ff + ["-f", "lavfi", "-i", f"aevalsrc={claps}:s=48000:d=10", f"{TMP}/claps.wav"], check=True,
                   stdin=subprocess.DEVNULL)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0xc02020:size=320x180:rate=30:duration=8", "-i", f"{TMP}/claps.wav",
                         "-map", "0:v", "-map", "1:a", "-t", "8", *x264, *aac, f"{TMP}/camA.mp4"], check=True,
                   stdin=subprocess.DEVNULL)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0x2020c0:size=320x180:rate=30:duration=7", "-ss", "1.5",
                         "-i", f"{TMP}/claps.wav", "-map", "0:v", "-map", "1:a", "-t", "7", *x264, *aac,
                         f"{TMP}/camB.mp4"], check=True, stdin=subprocess.DEVNULL)


class Mods:
    """Stand-in for a GTK gesture or drop target: modifier state and button."""

    def __init__(self, shift=False, ctrl=False, alt=False, button=1):
        self.state = ((Gdk.ModifierType.SHIFT_MASK if shift else 0) | (Gdk.ModifierType.CONTROL_MASK if ctrl else 0)
                      | (Gdk.ModifierType.ALT_MASK if alt else 0))
        self.button = button

    def get_current_event_state(self):
        return Gdk.ModifierType(self.state)

    def get_current_button(self):
        return self.button


def app_effects():
    from tideline.engine.effects import EFFECTS
    return EFFECTS


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((f"   {detail}") if not cond and detail != "" else ""), flush=True)


def main(app):
    try:
        make_media()
        yield from run(app)
    except Exception:
        traceback.print_exc()
        results.append(False)
    failed = len(results) - sum(results)
    print(f"SUMMARY: {sum(results)} passed, {failed} failed", flush=True)
    shutil.rmtree(TMP, ignore_errors=True)
    app.exit_status = 1 if failed else 0
    app.project.dirty = False
    app.quit()


def run(app):
    yield 500
    done = []
    app.import_paths([f"{TMP}/{n}" for n in ("a.mp4", "b.mp4", "c.mp4", "music.wav")], then=done.append)
    while not done:
        yield 100
    ids = {m.name: m.id for m in app.project.media.values()}

    class Items:
        """Media objects are recreated by undo, so look them up by id each time."""

        def __getitem__(self, name):
            return app.project.media[ids[name]]

    items = Items()
    win, tl = app.window, app.window.timeline

    class Seq:
        """Undo, drags and project loads replace model objects; always read live ones."""

        def __getattr__(self, name):
            return getattr(app.seq, name)

    seq = Seq()
    v = lambda i: app.seq.video_tracks[i]
    a = lambda i: app.seq.audio_tracks[i]
    lay = lambda: {app.seq.track_label(t): [(c.start, c.end, c.in_point) for c in t.clips]
                   for t in app.seq.tracks() if t.clips}
    key = lambda k, state=0: win._key(None, k, 0, Gdk.ModifierType(state))
    CTRL = Gdk.ModifierType.CONTROL_MASK

    def row_y(track, frac=0.6):
        y, h = tl.row_of(track)
        return y + h * frac

    def click(x, y, **mods):
        g = Mods(**mods)
        tl._press(g, x, y)
        tl._release(g, 0, 0)

    def drag(x, y, dx, dy=0, steps=4, **mods):
        g = Mods(**mods)
        tl._press(g, x, y)
        for i in range(1, steps + 1):
            tl._motion_drag(g, dx * i / steps, dy * i / steps)
        tl._release(g, dx, dy)

    check("every command has an action", all(app.lookup_action(c) is not None for c in
                                             __import__("tideline.commands", fromlist=["BY_NAME"]).BY_NAME))

    # ---- drag and drop ---------------------------------------------------
    app.drag_payload = ("media", [items["a.mp4"].id])
    tl._dnd_motion(None, 400, 200)
    ok = tl._dnd_drop(Mods(), "tideline:media:" + items["a.mp4"].id, 400, 200)
    yield 500
    check("drop on an empty timeline creates a matching sequence",
          ok and app.seq is not None and (seq.width, seq.height, seq.fps) == (640, 360, 30.0) and seq.end() == 360)
    app.drop_media([items["b.mp4"]], 360)
    yield 300
    tl.fit()
    yield 300
    ppf = seq.px_per_frame
    check("clips carry linked audio", lay() == {"V1": [(0, 360, 0), (360, 660, 0)], "A1": [(0, 360, 0), (360, 660, 0)]},
          lay())

    # ---- selection tool --------------------------------------------------
    click(tl.x_of(100), row_y(v(0)))
    check("click selects a clip with its linked audio", len(app.selection) == 2)
    app.snap = False
    drag(tl.x_of(500), row_y(v(0)), 60 * ppf)
    yield 200
    check("dragging moves the linked pair", v(0).clips[1].start == 420 and a(0).clips[1].start == 420, lay())
    app.do("undo")
    yield 200
    check("undo restores the move", v(0).clips[1].start == 360, lay())
    app.do("redo")
    yield 200
    check("redo reapplies it", v(0).clips[1].start == 420, lay())
    app.snap = True
    drag(tl.x_of(500), row_y(v(0)), -57 * ppf)
    check("snapping pulls a clip onto its neighbour", v(0).clips[1].start == 360, lay())
    app.snap = False
    drag(tl.x_of(360) - 3, row_y(v(0)), -90 * ppf)
    check("dragging an edge trims clip and linked audio", v(0).clips[0].end == 270 and a(0).clips[0].end == 270, lay())
    drag(tl.x_of(270) - 3, row_y(v(0)), -30 * ppf, ctrl=True)
    check("Ctrl+drag ripple-trims", v(0).clips[0].end == 240 and v(0).clips[1].start == 330, lay())
    click(tl.x_of(280), row_y(v(0)))
    check("clicking a gap selects it", app.selected_gap is not None)
    app.do("ripple_delete")
    yield 200
    check("ripple delete closes the gap", v(0).clips[1].start == 240 and a(0).clips[1].start == 240, lay())

    # ---- other tools -----------------------------------------------------
    app.set_tool("roll")
    drag(tl.x_of(240) - 3, row_y(v(0)), 20 * ppf)
    check("rolling edit moves the cut", (v(0).clips[0].end, v(0).clips[1].start, v(0).clips[1].in_point) == (260, 260, 20),
          lay())
    app.set_tool("razor")
    click(tl.x_of(100), row_y(v(0)))
    yield 200
    check("razor cuts clip and linked audio", len(v(0).clips) == 3 and len(a(0).clips) == 3 and v(0).clips[0].end == 100,
          lay())
    app.set_tool("slip")
    before = v(0).clips[1].in_point
    drag(tl.x_of(180), row_y(v(0)), -25 * ppf)
    check("slip changes only the source range", v(0).clips[1].in_point == before + 25 and v(0).clips[1].start == 100)
    app.set_tool("slide")
    drag(tl.x_of(180), row_y(v(0)), 15 * ppf)
    check("slide moves a clip between its neighbours",
          (v(0).clips[0].end, v(0).clips[1].start, v(0).clips[2].start) == (115, 115, 275), lay())
    app.set_tool("rate")
    d0 = v(0).clips[2].duration
    drag(tl.x_of(v(0).clips[2].end) - 3, row_y(v(0)), d0 * ppf)
    check("rate stretch halves the speed", abs(v(0).clips[2].speed - 0.5) < 0.02 and v(0).clips[2].duration == 2 * d0)
    app.do("undo")
    yield 150
    app.set_tool("track_fwd")
    click(tl.x_of(120), row_y(v(0)))
    check("track select forward selects everything to the right", len(app.selection) == 4, len(app.selection))
    app.set_tool("select")
    app.clear_selection()
    g = Mods()
    y3 = tl.row_of(v(2))[0] + 5
    tl._press(g, tl.x_of(5), y3)
    tl._motion_drag(g, tl.x_of(90) - tl.x_of(5), row_y(v(0), 0.9) - y3)
    tl._release(g, 0, 0)
    check("marquee selects intersecting clips", len(app.selection) == 1, len(app.selection))
    drag(tl.x_of(50), row_y(v(0)), 300 * ppf, row_y(v(1)) - row_y(v(0)), alt=True)
    yield 200
    check("Alt+drag duplicates onto another track", len(v(1).clips) == 1 and len(v(0).clips) == 3, lay())
    app.do("undo")
    yield 150
    app.set_tool("pen")
    c = v(0).clips[2]
    click(tl.x_of(c.start + 20), row_y(v(0), 0.5))
    g = Mods()
    tl._press(g, tl.x_of(c.start + 80), row_y(v(0), 0.5))
    tl._motion_drag(g, 0, 14)
    tl._release(g, 0, 14)
    kfs = v(0).clips[2].effect("opacity").keyframes.get("opacity", [])
    check("pen tool adds opacity keyframes", len(kfs) == 2 and kfs[1][1] < kfs[0][1], kfs)
    app.set_tool("select")

    # ---- track headers and ruler ------------------------------------------
    tl._press(Mods(), HEADER_W - 40, row_y(a(0), 0.5))
    check("header M mutes the track", a(0).hidden is True)
    tl._press(Mods(), HEADER_W - 40, row_y(a(0), 0.5))
    tl._press(Mods(), 40, row_y(a(0), 0.5))
    check("header lock locks the track", a(0).locked is True)
    tl._press(Mods(), 40, row_y(a(0), 0.5))
    g = Mods()
    tl._press(g, tl.x_of(150), 10)
    tl._motion_drag(g, 30 * ppf, 0)
    tl._release(g, 30 * ppf, 0)
    check("dragging in the ruler scrubs", seq.playhead == 180, seq.playhead)

    # ---- keyboard ---------------------------------------------------------
    key(Gdk.KEY_i)
    app.set_playhead(220)
    key(Gdk.KEY_o)
    check("I and O mark the sequence", (seq.in_point, seq.out_point) == (180, 221))
    end = seq.end()
    key(Gdk.KEY_apostrophe)
    yield 200
    check("extract removes the marked range", seq.end() == end - 41, (end, seq.end()))
    key(Gdk.KEY_z, CTRL)
    yield 200
    check("Ctrl+Z undoes it", seq.end() == end)
    key(Gdk.KEY_c)
    check("C picks the razor", app.tool == "razor")
    key(Gdk.KEY_v)
    check("V picks the selection tool", app.tool == "select")
    app.set_playhead(50)
    key(Gdk.KEY_k, CTRL)
    yield 150
    check("Ctrl+K adds an edit at the playhead", any(c.end == 50 for c in v(0).clips), lay())
    key(Gdk.KEY_Down)
    check("Down jumps to the next edit", seq.playhead == 115, seq.playhead)
    key(Gdk.KEY_Left)
    check("Left steps back one frame", seq.playhead == 114)
    key(Gdk.KEY_m)
    yield 100
    check("M adds a marker", [m.t for m in seq.markers] == [114])
    key(Gdk.KEY_d)
    check("D selects the clip under the playhead", len(app.selection) >= 2)
    key(Gdk.KEY_c, CTRL)
    app.set_playhead(seq.end())
    end = seq.end()
    key(Gdk.KEY_v, CTRL)
    yield 200
    check("copy and paste append clips", seq.end() > end)
    key(Gdk.KEY_d, CTRL)
    yield 200
    check("Ctrl+D adds a video transition", len(v(0).transitions) >= 1)
    app.set_playhead(20)
    key(Gdk.KEY_space)
    yield 900
    check("Space starts playback", app.program.playing and seq.playhead > 25, seq.playhead)
    key(Gdk.KEY_space)
    yield 200
    check("Space stops playback", not app.program.playing)
    app.do("shuttle_fwd")
    app.do("shuttle_fwd")
    yield 600
    check("L twice plays at double speed", app.program.speed == 2.0)
    app.do("shuttle_stop")
    yield 200
    p0 = seq.playhead
    app.do("shuttle_back")
    yield 800
    check("J plays in reverse", seq.playhead < p0, (p0, seq.playhead))
    app.do("shuttle_stop")
    yield 200

    # ---- effects ----------------------------------------------------------
    y2 = row_y(v(1), 0.5)
    tl._dnd_drop(Mods(), "tideline:media:" + items["c.mp4"].id, tl.x_of(60), y2)
    yield 400
    pip_id = v(1).clips[0].id
    pip = lambda: app.seq.find_clip(pip_id)[1]
    check("drop places a clip on the track under the pointer", (pip().start, pip().duration) == (60, 240))
    tl._dnd_drop(Mods(), "tideline:effect:gaussian_blur", tl.x_of(30), row_y(v(0)))
    yield 300
    check("dropping an effect applies it", "gaussian_blur" in [e.type for e in v(0).clips[0].effects])
    app.select([pip_id])
    win.show_panel("effect_controls")
    yield 400
    ec = win.effect_controls
    row = lambda eff, name: next(r for r in ec.rows if r.effect.type == eff and r.pdef.name == name)
    row("motion", "scale").numbers[0].emit("changed", 50.0, True)
    yield 300
    check("Effect Controls edits scale", pip().effect("motion").params.get("scale") == 50.0)
    rect = app.graph.filters[pip().effect("motion").id][0][1][0].get("transition.rect")
    check("the live graph follows without a rebuild", rect.startswith("160.000 90.000 320.000 180.000"), rect)
    app.set_playhead(70)
    row("opacity", "opacity").watch.set_active(True)
    yield 300
    row("opacity", "opacity").numbers[0].emit("changed", 20.0, True)
    yield 200
    app.set_playhead(130)
    yield 100
    row("opacity", "opacity").numbers[0].emit("changed", 90.0, True)
    yield 300
    check("animating a parameter records keyframes at the playhead",
          pip().effect("opacity").keyframes.get("opacity") == [[10, 20.0, "linear"], [70, 90.0, "linear"]],
          pip().effect("opacity").keyframes)
    app.set_playhead(100)
    yield 200
    check("Effect Controls shows interpolated values", abs(row("opacity", "opacity").numbers[0].value - 55.0) < 0.6)
    r = row("opacity", "opacity")
    app.goto_keyframe(r.clip, r.effect, r.pdef, 1)
    check("keyframe navigation moves the playhead", seq.playhead == 130)

    # ---- value graph, custom curves, generated motion ----------------------
    opacity_keys = lambda: pip().effect("opacity").keyframes["opacity"]
    g = row("opacity", "opacity").graph
    check("animating a parameter opens its value graph", g is not None)
    yield 300
    g.press(g.x_of(70), g.y_of(90.0))
    g.drag_to(g.x_of(60), g.y_of(60.0))
    g.release()
    yield 200
    check("dragging a keyframe in the graph moves it in time and value",
          opacity_keys()[1][0] == 60 and abs(opacity_keys()[1][1] - 60.0) < 3, opacity_keys())
    g.selected = 0
    _seg, _end, _c, ht, hv = g._handles()[0]
    g.press(g.x_of(ht), g.y_of(hv))
    g.drag_to(g.x_of(10 + 0.8 * 50), g.y_of(20.0))
    g.release()
    yield 200
    first = opacity_keys()[0]
    check("dragging a curve handle makes a custom curve",
          first[2] == "bezier" and abs(first[3][0] - 0.8) < 0.05 and abs(first[3][1]) < 0.08, first)
    linear_mid = (first[1] + opacity_keys()[1][1]) / 2
    check("the custom curve changes the values in between", kf.evaluate(opacity_keys(), 35) < linear_mid - 5)
    alpha = app.graph.filters[pip().effect("opacity").id][0][1][0].get("alpha")
    check("the live graph plays the custom curve", alpha.count(";") >= 49, alpha[:80])
    g.add_key_at(g.x_of(40), g.y_of(50.0))
    yield 200
    check("double-clicking the graph adds a keyframe", [k[0] for k in opacity_keys()] == [10, 40, 60])
    ec.interp_target, ec.interp_time = row("opacity", "opacity"), 40
    ec.activate_action("ec.interp", GLib.Variant("s", "bounce_out"))
    check("a keyframe takes a curve preset from the graph menu", opacity_keys()[1][2] == "bounce_out")
    ec.activate_action("ec.kf_delete", None)
    yield 200
    check("a keyframe can be deleted from the graph menu", [k[0] for k in opacity_keys()] == [10, 60])
    app.apply_effect("shake", [pip_id])
    yield 400
    shake = lambda: pip().effect("shake")
    built = app.graph
    rect = lambda: app.graph.filters[shake().id][0][1][0].get("transition.rect")
    before = rect()
    check("Shake animates the picture on every frame", before.count(";") >= 239, before[:60])
    row("shake", "x").numbers[0].emit("changed", 200.0, True)
    yield 200
    check("Shake settings update the live graph without a rebuild", rect() != before and app.graph is built)
    series = dict(ec.previews[0].series(50)) if ec.previews else {}
    check("Effect Controls previews the generated motion", len(set(series.get("X", []))) > 10)
    app.remove_effect(pip(), shake())
    yield 300
    check("the window opens full screen", win.is_fullscreen())
    app.do("toggle_fullscreen")
    yield 600
    check("F11 leaves full screen", not win.is_fullscreen())
    app.do("toggle_fullscreen")
    yield 600

    # ---- program monitor --------------------------------------------------
    pm = win.program_monitor
    app.set_playhead(100)
    yield 400
    fx, fy, fw, fh = pm.view.frame_rect()
    k = fw / seq.width
    pm._xf_begin(None, fx + 320 * k, fy + 180 * k)
    pm._xf_update(None, 100 * k, -50 * k)
    pm._xf_end(None, 100 * k, -50 * k)
    yield 300
    pos = pip().effect("motion").params.get("position")
    check("dragging in the Program monitor moves the clip", pos and abs(pos[0] - 420) < 2 and abs(pos[1] - 130) < 2, pos)
    app.set_tool("type")
    pm._xf_begin(None, fx + 60 * k, fy + 40 * k)
    yield 500
    titles = [m for m in app.project.media.values() if m.kind == "title"]
    tclips = [c for _, c in app.seq.all_clips() if titles and c.media_id == titles[0].id]
    check("the type tool creates a title clip", len(titles) == 1 and len(tclips) == 1 and app.tool == "select")
    row("text", "text").editor.get_buffer().set_text("Hello")
    yield 700
    check("typing edits the title", app.seq.find_clip(tclips[0].id)[1].effect("text").params.get("text") == "Hello")

    # ---- keyframes and graphs for every kind of parameter --------------------
    from tideline import fonts, presets
    presets.user_dir = lambda: f"{TMP}/presets"                # keep the test out of the real libraries
    fonts.store_dir = lambda: f"{TMP}/fonts"
    title_id = tclips[0].id
    title = lambda: app.seq.find_clip(title_id)[1]
    text = lambda: title().effect("text")
    t0 = title().start
    check("every parameter row has an animation toggle, except the title's animations",
          [r.pdef.name for r in ec.rows if not hasattr(r.watch, "set_active")]
          == ["anim_in", "anim_out", "anim_time", "uniform"])
    app.set_playhead(t0)
    row("text", "fill").watch.set_active(True)
    yield 300
    app.set_playhead(t0 + 40)
    yield 100
    r = row("text", "fill")
    app.set_param(r.clip, r.effect, r.pdef, "#ff0000ff", True)
    yield 300
    fill = text().keyframes.get("fill")
    check("a colour takes keyframes", fill == [[0, "#ffffffff", "linear"], [40, "#ff0000ff", "linear"]], fill)
    g = row("text", "fill").graph
    check("a colour has a graph of its four channels", g is not None and g.kind == "color" and g._comps() == 4)
    yield 200
    k = g.keys()[1]
    g.press(g.x_of(k[0]), g.y_of(0.0))                         # the green channel of the second keyframe
    g.drag_to(g.x_of(k[0]), g.y_of(128.0))
    g.release()
    yield 200
    check("dragging a channel in the graph changes the colour", text().keyframes["fill"][1][1] == "#ff8000ff",
          text().keyframes["fill"])
    app.set_playhead(t0 + 20)
    yield 300
    alpha_mid = app.graph.filters[text().id][0][1][0].get("fgcolour")
    check("the live graph animates the colour", "=" in alpha_mid and len(app.graph.filters[text().id]) == 1, alpha_mid)
    app.set_playhead(t0)
    row("text", "halign").watch.set_active(True)
    yield 300
    g = row("text", "halign").graph
    check("a choice has a stepped graph", g is not None and g.kind == "steps" and g.levels == ["left", "center", "right"])
    yield 200
    g.add_key_at(g.x_of(60), g.y_of(2.0))
    yield 400
    halign = text().keyframes.get("halign")
    check("a choice steps between keyframes", [k[1:] for k in halign] == [["left", "hold"], ["right", "hold"]]
          and halign[1][0] == 60, halign)
    check("stepped keyframes split the effect into segments", len(app.graph.filters[text().id]) == 2,
          len(app.graph.filters[text().id]))
    row("text", "text").watch.set_active(True)
    yield 300
    app.set_playhead(t0 + 90)
    yield 100
    row("text", "text").editor.get_buffer().set_text("Goodbye")
    yield 700
    words = text().keyframes.get("text")
    check("text takes keyframes", words == [[0, "Hello", "hold"], [90, "Goodbye", "hold"]], words)
    g = row("text", "text").graph
    check("text has a keyframe lane", g is not None and g.kind == "lane")
    app.set_playhead(t0 + 10)
    row("text", "size").watch.set_active(True)
    yield 300
    app.set_playhead(t0 + 16)
    yield 100
    row("text", "size").numbers[0].emit("changed", 160.0, True)
    yield 500
    check("a property MLT cannot animate is baked frame by frame", len(app.graph.filters[text().id]) > 6,
          len(app.graph.filters[text().id]))
    yield 300
    shot = lambda t: (app.set_playhead(t), app.program.refresh())
    shot(t0 + 12)
    yield 400

    # ---- presets ------------------------------------------------------------
    app.select([title_id], expand=False)
    yield 200
    check("a preset is saved from the selected clip", app.save_preset(f"{TMP}/presets/Headline.tlpreset"))
    saved = presets.load(f"{TMP}/presets/Headline.tlpreset")
    check("the preset holds the effects and their keyframes",
          [e["type"] for e in saved["effects"]] == ["text"] and set(saved["effects"][0]["keyframes"]) ==
          {"fill", "halign", "text", "size"}, saved["effects"])
    app.set_playhead(t0 + 200)
    app.cmd_new_title("Second")
    yield 600
    second_id = app.selection[0]
    win.effects_panel.refresh()
    listed = [getattr(r.get_child(), "payload", None) for r in win.effects_panel.list]
    check("the Effects panel lists saved and built-in presets",
          ("preset", f"{TMP}/presets/Headline.tlpreset") in listed
          and sum(1 for x in listed if x and x[0] == "preset") >= 6, listed[:10])
    tl._dnd_drop(Mods(), f"tideline:preset:{TMP}/presets/Headline.tlpreset", tl.x_of(t0 + 210),
                 row_y(app.seq.find_clip(second_id)[0]))
    yield 500
    second = app.seq.find_clip(second_id)[1]
    styled = second.effect("text")
    check("dropping a preset on a clip applies it from the clip's start",
          styled.keyframes.get("fill") == [[second.in_point + k[0], k[1], k[2]] for k in saved["effects"][0]["keyframes"]["fill"]]
          and styled.keyframes["text"][1][1] == "Goodbye", styled.keyframes)
    app.select([pip_id], expand=False)
    builtin = next(path for path, pr, b in presets.library() if b and pr["name"] == "Slide In From Left")
    check("a built-in preset applies to the selection", app.apply_preset(builtin) == 1)
    yield 400
    slide = pip().effect("motion").keyframes.get("position")
    check("a preset made at 1080p is fitted to this frame size",
          slide and slide[0][1] == [-320.0, 180.0] and slide[-1][1] == [320.0, 180.0], slide)
    shutil.copy(builtin, f"{TMP}/Shared.tlpreset")
    check("importing a preset file adds it to the library", app.import_presets([f"{TMP}/Shared.tlpreset"]) == 1
          and os.path.exists(f"{TMP}/presets/Shared.tlpreset"))
    with open(f"{TMP}/broken.tlpreset", "w") as fh:
        fh.write("{not json")
    check("a damaged preset file is refused", app.import_presets([f"{TMP}/broken.tlpreset"]) == 0)

    # ---- custom fonts -------------------------------------------------------
    font_file = subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True).stdout
    if font_file.lower().endswith(fonts.FONT_EXT):
        added = app.add_fonts([font_file])
        yield 400
        check("a font file is added to the font folder", bool(added) and os.path.exists(
            os.path.join(fonts.store_dir(), os.path.basename(font_file))), added)
        app.select([title_id], expand=False)
        yield 300
        r = row("text", "font")
        app.set_param(r.clip, r.effect, r.pdef, added[0], True)
        yield 400
        check("the added font is used by the title", app.graph.filters[text().id][0][1][0].get("family") == added[0])
    check("something that is not a font is refused", app.add_fonts([f"{TMP}/a.mp4"]) == [])

    # ---- project settings: aspect ratio -------------------------------------
    from tideline.ui.dialogs import ProjectSettingsDialog
    app.select([pip_id], expand=False)
    before = (app.seq.width, app.seq.height)
    dlg = ProjectSettingsDialog(app)
    dlg.frame.aspect.set_selected(2)                           # 9:16 Vertical
    check("choosing an aspect ratio sets the frame size", dlg.frame.get() == (360, 640), dlg.frame.get())
    dlg.frame.resolution.set_selected(3)                       # 1080p
    check("choosing a resolution keeps the shape", dlg.frame.get() == (1080, 1920), dlg.frame.get())
    dlg.frame.width.set_value(1000)
    check("a typed size shows as a custom shape", dlg.frame.aspect.get_selected() == 0)
    dlg.frame.aspect.set_selected(2)
    dlg.frame.resolution.set_selected(2)                       # 720p
    dlg.accept()
    yield 700
    slide = pip().effect("motion").keyframes.get("position")
    check("project settings reshape the sequence", (app.seq.width, app.seq.height) == (720, 1280)
          and app.project.settings["height"] == 1280 and app.graph.profile.height() == 1280,
          (app.seq.width, app.seq.height))
    check("clips keep their place in the new frame", slide[-1][1] == [360.0, 640.0], slide)
    fx, fy, fw, fh = pm.view.frame_rect()
    check("the Program monitor shows the new shape", abs(fw / fh - 720 / 1280) < 0.01, (fw, fh))
    app.do("undo")
    yield 500
    check("undo restores the frame size and the project setting",
          (app.seq.width, app.seq.height) == before and app.project.settings["height"] == 1080)

    # ---- mixer, frame export, relink --------------------------------------
    track = a(0)
    win.mixer.sync()
    win.mixer.strips[track.id][3].emit("changed", -6.0, True)
    yield 300
    check("the mixer sets track volume", a(0).volume == -6.0 and
          abs(app.graph.tracks[a(0).id][1].get_double("level") + 6.0) < 1e-6)
    png = f"{TMP}/frame.png"
    app.export_frame_to(png, 100)
    info = GdkPixbuf.Pixbuf.get_file_info(png)
    check("export frame writes a full-size image", os.path.exists(png) and (info[1], info[2]) == (640, 360), info[1:])
    app.relink(items["c.mp4"], f"{TMP}/c_moved.mp4")
    yield 500
    check("relinking points the item at the new file", items["c.mp4"].path.endswith("c_moved.mp4")
          and app.graph is not None, items["c.mp4"].path)

    # ---- source monitor ----------------------------------------------------
    app.open_source(items["music.wav"])
    yield 500
    check("opening a clip gives the Source monitor the audio device", Player._active is app.source)
    app.source.seek(60)
    key(Gdk.KEY_i)
    app.source.seek(149)
    key(Gdk.KEY_o)
    check("I and O mark the source clip", app.source_marks() == (60, 150), app.source_marks())
    app.focus = "timeline"
    app.set_playhead(0)
    check("scrubbing the timeline hands playback back to the Program monitor", Player._active is app.program)
    app.focus = "source"
    key(Gdk.KEY_period)
    yield 300
    c0 = a(0).clips[0]
    check("overwrite places the marked range at the playhead",
          (c0.start, c0.duration, c0.in_point, seq.playhead) == (0, 90, 60, 90))

    # ---- time remapping --------------------------------------------------------
    from tideline.engine.effects import EFFECTS
    vid = next(c for c in v(0).clips if c.link_id and any(o.link_id == c.link_id for o in a(0).clips))
    vid_id = vid.id
    live = lambda: app.seq.find_clip(vid_id)[1]
    partner = lambda: next(c for _, c in app.seq.all_clips() if c.link_id == live().link_id and c.id != vid_id)
    app.select([vid_id])
    yield 100
    app.do("time_remap")
    yield 400
    check("Time Remapping goes on the clip and its linked audio",
          live().effect("time_remap") is not None and partner().effect("time_remap") is not None)
    win.show_panel("effect_controls")
    win.effect_controls.sync()
    yield 200
    check("Effect Controls shows the speed curve",
          any(r.effect.type == "time_remap" for r in win.effect_controls.rows))
    speed = EFFECTS["time_remap"].param("speed")
    app.set_playhead(live().start + 10)
    app.toggle_animation(live(), live().effect("time_remap"), speed, True)
    app.set_playhead(live().start + 40)
    app.set_param(live(), live().effect("time_remap"), speed, 25.0)
    yield 500
    keys = live().effect("time_remap").keyframes["speed"]
    shift = partner().in_point - live().in_point
    check("a speed ramp is keyframed and mirrored on the linked audio",
          [k[1] for k in keys] == [100.0, 25.0]
          and [k[0] - shift for k in partner().effect("time_remap").keyframes["speed"]] == [k[0] for k in keys],
          keys)
    check("the remapped clip plays through MLT's time remap",
          any("remap" in k for k in app.builder.cache.items) and app.graph is not None)
    app.do("time_remap")
    yield 300
    check("Time Remapping switches off again for both halves",
          live().effect("time_remap") is None and partner().effect("time_remap") is None)

    # ---- pop-out preview -------------------------------------------------------
    app.focus = "timeline"
    app.do("popout_monitor")
    yield 500
    pop = app.popout
    check("the pop-out preview opens with the program picture",
          pop is not None and pop.get_visible() and pop.view.texture is not None)
    check("the pop-out preview lists the displays", pop.display_menu.get_model().get_n_items() >= 1)
    before = pop.view.texture
    app.set_playhead(120)
    yield 400
    check("the pop-out preview follows the playhead", pop.view.texture is not before)
    pop.go_fullscreen()
    yield 800
    check("the pop-out preview goes full screen without its controls",
          pop.is_fullscreen() and not pop.bar.get_visible(), pop.is_fullscreen())
    pop._key(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0))
    yield 600
    check("Esc returns the pop-out preview to a window", not pop.is_fullscreen() and pop.bar.get_visible())
    pop._key(None, Gdk.KEY_space, 0, Gdk.ModifierType(0))
    yield 300
    playing = app.program.playing
    app.program.pause()
    check("keys in the pop-out preview drive playback", playing)
    pop.feed_menu.set_selected(1)
    yield 100
    check("the pop-out preview can show the Source monitor", pop.feed == "source")
    pop.close()
    yield 300
    check("closing the pop-out preview stops its updates",
          app.popout is None and pop._frame not in app._listeners["frame"])

    # ---- captions --------------------------------------------------------------
    from tideline import captions as capmod
    tracks_before = len(app.seq.video_tracks)
    app.add_captions([capmod.Cue(0.5, 1.5, "Hello world"), capmod.Cue(2.0, 3.0, "A second caption")], 0)
    yield 500
    ctracks = app.caption_tracks()
    cclips = ctracks[0].clips if ctracks else []
    check("captions land on a new caption track as titles",
          len(app.seq.video_tracks) == tracks_before + 1 and [(c.start, c.end) for c in cclips] == [(15, 45), (60, 90)],
          [(c.start, c.end) for c in cclips])
    check("each caption renders as a title",
          all(c.effect("text") is not None and app.graph.filters.get(c.effect("text").id) for c in cclips))
    first_text = cclips[0].effect("text")
    app.set_param(cclips[0], first_text, EFFECTS["text"].param("text"), "Hello, world!")
    yield 200
    srt = f"{TMP}/captions.srt"
    capmod.save(app.caption_cues(), srt)
    back = capmod.load(srt)
    check("edited captions export to SRT", [(c.start, c.end, c.text) for c in back] ==
          [(0.5, 1.5, "Hello, world!"), (2.0, 3.0, "A second caption")], back)
    app.add_captions([capmod.Cue(0.4, 1.0, "Replaced")], 0, 60, replace=True)
    yield 300
    check("new captions can replace a range", [(c.start, c.end) for c in app.caption_tracks()[0].clips] ==
          [(12, 30), (60, 90)] and len(app.caption_tracks()) == 1)
    app.do("auto_captions")
    yield 300
    from tideline.ui.captions_dialog import CaptionsDialog
    dlg = next((w for w in app.get_windows() if isinstance(w, CaptionsDialog)), None)
    check("Auto Captions opens its dialog", dlg is not None and dlg.note.get_text() != "")
    if dlg is not None:
        dlg.close()
    yield 200
    app.do("undo")
    app.do("undo")
    app.do("undo")
    yield 400
    check("undo removes the caption track", not app.caption_tracks() and len(app.seq.video_tracks) == tracks_before)

    # ---- proxies ------------------------------------------------------------
    imported = []
    app.import_paths([f"{TMP}/hd.mp4"], then=imported.extend)
    for _ in range(50):
        if imported:
            break
        yield 100
    hd = imported[0]
    app.prefs["proxy_size"] = 360
    app.create_proxies([hd])
    check("a proxy job is queued", app.proxy_queue.busy(hd.id) and app.proxy_state(hd) in ("Queued", "Creating 0%"),
          app.proxy_state(hd))
    for _ in range(150):
        if not app.proxy_queue.busy():
            break
        yield 100
    yield 300
    proxy = proxies.find(app.project, hd)
    check("the proxy is made and recorded", proxy is not None and os.path.exists(proxy), app.project.proxies)
    check("the proxy is 360 lines high", proxy and mediamod.probe(proxy).height == 360)
    check("the preview reads the proxy", app.builder.source_path(hd) == (proxy, True))
    check("the Project panel shows the proxy", app.proxy_state(hd) == "Proxy")
    app.do("toggle_proxies")
    yield 300
    check("Use Proxies switches the preview to the original",
          not app.prefs["use_proxies"] and app.builder.source_path(hd) == (hd.path, False))
    check("the monitor's proxy button follows", not win.program_monitor.proxy_button.get_active())
    win.program_monitor.proxy_button.set_active(True)
    yield 300
    check("the monitor's proxy button switches proxies back on", app.prefs["use_proxies"] and app.builder.proxies)
    app.create_proxies([hd])
    check("an item with a proxy is not queued again", not app.proxy_queue.busy())
    pp = win.project_panel
    idx = next(i for i in range(pp.store.get_n_items()) if pp.store.get_item(i).item.id == hd.id)
    pp.selection.unselect_all()
    pp.selection.select_item(idx, True)
    app.focus = "project"
    app.do("delete_proxies")
    yield 300
    check("Delete Proxies removes the file and the record", not os.path.exists(proxy) and hd.id not in app.project.proxies)
    app.attach_proxy(hd, f"{TMP}/hd.mp4")
    check("an existing file can be attached as a proxy", proxies.find(app.project, hd) == f"{TMP}/hd.mp4")
    app.project.remove_media(hd.id)
    app.emit("media")
    yield 200

    # ---- extensions ---------------------------------------------------------
    ext_dir = os.path.join(TMP, "extensions", "smoke")
    os.makedirs(ext_dir)
    with open(os.path.join(ext_dir, "extension.json"), "w") as fh:
        json.dump({"id": "smoke.test", "name": "Smoke Test", "main": "main.py",
                   "effects": [{"key": "dim", "label": "Smoke Dim", "category": "Smoke",
                                "services": ["brightness"],
                                "params": [{"name": "level", "default": 50, "mlt": "level", "scale": 0.01}]}]}, fh)
    with open(os.path.join(ext_dir, "main.py"), "w") as fh:
        fh.write("ran = []\n"
                 "def register(tl):\n"
                 "    tl.command('ping', 'Ping', lambda app: ran.append(app), accel='<Ctrl><Alt>F8')\n"
                 "    tl.on('project_saved', lambda path: ran.append(path))\n")
    app.extensions.load([os.path.dirname(ext_dir)])
    ext = app.extensions.get("smoke.test")
    check("an extension loads in the running app", ext is not None and ext.loaded, ext and ext.error)
    app.emit("presets")
    yield 200
    check("its effect is in the effects registry", "smoke.test/dim" in app_effects())
    check("its effect category is listed", "Smoke" in [c for c, _ in win.effects_panel._tree()[1][1]])
    app.do("ext-smoke-test-ping")
    check("its command runs from the action", ext.module.ran and ext.module.ran[0] is app)
    keyval = Gdk.keyval_from_name("F8")
    check("its shortcut is bound", app.key_command(keyval, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
          == "ext-smoke-test-ping")
    app.do("extensions")
    yield 300
    dlg = next((w for w in app.get_windows() if w.get_title() == "Extensions"), None)
    check("Manage Extensions opens", dlg is not None)
    if dlg is not None:
        dlg.close()
    app.extensions.unload_all()
    check("unloading removes the effect and command", "smoke.test/dim" not in app_effects()
          and app.lookup_action("ext-smoke-test-ping") is None)
    app.emit("presets")
    yield 200

    # ---- colour: curves and scopes -----------------------------------------
    from tideline import curves as cv
    from tideline.engine import export as exportmod
    from tideline.ui.dialogs import ExportDialog
    from tideline.ui.shortcuts import ShortcutMapper
    vclip = next(c for t in app.seq.video_tracks for c in t.clips if c.media_id in app.project.media)
    app.select([vclip.id])
    app.apply_effect("curves", [vclip.id])
    yield 400
    app.window.show_panel("effect_controls")
    yield 200
    row = next((r for r in win.effect_controls.rows if r.pdef.type == "curves"), None)
    ed = row.editor if row is not None else None
    check("RGB Curves opens a curve editor in Effect Controls", ed is not None and ed.points == cv.IDENTITY)
    curves_effect = lambda: next(e for t in app.seq.video_tracks for c in t.clips if c.id == vclip.id
                                 for e in c.effects if e.type == "curves")
    if ed is not None:
        n_hist = len(app.history.states)
        bx, by, size = ed._box()
        g = Mods()
        ed._begin(g, bx + size * 0.5, by + size * 0.5)
        ed._update(g, 0, -size * 0.25)
        ed._end(g, 0, -size * 0.25)
        yield 300
        master = curves_effect().params.get("curves", {}).get("master", [])
        check("dragging on a curve adds and moves a point", len(master) == 3 and abs(master[1][0] - 0.5) < 0.02
              and 0.7 < master[1][1] < 0.8, master)
        check("a curve drag is one undo step", len(app.history.states) == n_hist + 1, len(app.history.states) - n_hist)
        ed.buttons["red"].set_active(True)
        ed._click(Mods(button=3), 1, *ed._to_screen(*master[1]))     # nothing there on the red curve
        yield 200
        check("channels are edited separately", ed.channel == "red"
              and len(curves_effect().params["curves"]["master"]) == 3)
        ed.buttons["master"].set_active(True)
        ed._click(Mods(button=3), 1, *ed._to_screen(*master[1]))
        yield 300
        check("right-click removes a point", cv.is_identity(curves_effect().params["curves"]["master"]))
        app.do("undo")
        yield 300
        check("undo brings the point back", len(curves_effect().params["curves"]["master"]) == 3)
    app.do("workspace_color")
    yield 600
    sp = win.scopes_panel
    check("the Colour workspace shows Effect Controls and the Scopes",
          win.current_tab("scopes") == "scopes" and win.current_tab("effect_controls") == "effect_controls")
    app.set_playhead(20)
    yield 500
    seen = {}
    for i, (mode, _text) in enumerate(__import__("tideline.scopes", fromlist=["MODES"]).MODES):
        sp.mode.set_selected(i)
        yield 250
        seen[mode] = set(sp.images)
    check("each scope measures the program picture", seen.get("waveform") == {"waveform"}
          and seen.get("vectorscope") == {"vectorscope"} and seen.get("histogram") == {"histogram"}
          and seen.get("parade") == {"parade"}, seen)
    check("All Scopes shows four", seen.get("all") == {"waveform", "parade", "vectorscope", "histogram"}, seen)
    sp.mode.set_selected(0)
    app.do("workspace_editing")
    yield 300

    # ---- keyboard shortcut mapper ------------------------------------------
    app.do("shortcuts")
    yield 500
    mapper = next((w for w in app.get_windows() if isinstance(w, ShortcutMapper)), None)
    check("Keyboard Shortcuts opens the mapper", mapper is not None)
    if mapper is not None:
        check("F7 is free to begin with", app.key_command(Gdk.KEY_F7, 0) is None)
        mapper.capture("add_marker")
        mapper._key(None, Gdk.KEY_F7, 0, Gdk.ModifierType(0))
        yield 200
        check("a captured key becomes the shortcut", app.key_command(Gdk.KEY_F7, 0) == "add_marker"
              and app.key_command(Gdk.KEY_m, 0) is None)
        markers = len(app.seq.markers)
        key(Gdk.KEY_F7)
        check("the new key runs the command in the editor", len(app.seq.markers) == markers + 1)
        mapper.capture("tool_razor")
        mapper._key(None, Gdk.KEY_v, 0, Gdk.ModifierType(0))       # V belongs to the Selection tool
        yield 200
        check("a taken key asks before reassigning", mapper.banner.get_visible()
              and app.key_command(Gdk.KEY_v, 0) == "tool_select")
        mapper._confirm(True)
        yield 200
        check("reassigning moves the key", app.key_command(Gdk.KEY_v, 0) == "tool_razor"
              and app.keymap.accel("tool_select") == "")
        check("tooltips follow the shortcuts", win.tools.buttons["razor"].get_tooltip_text().endswith("(V)")
              and win.tools.buttons["select"].get_tooltip_text() == "Selection Tool")
        model = win.menubar.get_menu_model()

        def menu_accel(menu, action):
            for i in range(menu.get_n_items()):
                if menu.get_item_attribute_value(i, "action", None) and \
                        menu.get_item_attribute_value(i, "action", None).get_string() == action:
                    v = menu.get_item_attribute_value(i, "accel", None)
                    return v.get_string() if v else ""
                for link in ("section", "submenu"):
                    sub = menu.get_item_link(i, link)
                    if sub is not None:
                        found = menu_accel(sub, action)
                        if found is not None:
                            return found
            return None

        check("menus show the new shortcut", menu_accel(model, "app.add_marker") == "F7",
              menu_accel(model, "app.add_marker"))
        mapper.filter_key("v", "V")
        shown = [n for n, (r, *_rest) in mapper.rows.items() if r.get_visible()]
        check("clicking a key lists what it does with any modifier",
              sorted(shown) == ["paste", "paste_insert", "tool_razor"], shown)
        mapper.filter_key(None, None)
        keys_file = f"{TMP}/mine.tlkeys"
        app.keymap.dump(keys_file)
        mapper._reset_all()
        mapper._confirm(True)
        yield 200
        check("Reset All restores the defaults", app.keymap.overrides == {}
              and app.key_command(Gdk.KEY_m, 0) == "add_marker" and app.key_command(Gdk.KEY_v, 0) == "tool_select")
        app.keymap.load(keys_file)
        app.set_shortcuts()
        check("a saved shortcut file loads back", app.key_command(Gdk.KEY_F7, 0) == "add_marker")
        app.keymap.reset_all()
        app.set_shortcuts()
        mapper.close()
        yield 200

    # ---- export: hardware encoders -------------------------------------------
    app.do("export")
    yield 300
    dlg = next((w for w in app.get_windows() if isinstance(w, ExportDialog)), None)
    for _ in range(100):
        if dlg is None or not dlg.hw_note.get_text().startswith("Looking"):
            break
        yield 100
    yield 300
    if dlg is not None:
        listed = [f for f in dlg.formats if f.hw]
        check("Export lists only hardware formats that work here",
              all(exportmod.hardware_devices(f) for f in listed) and not dlg.hw_note.get_text().startswith("Looking"),
              [f.key for f in listed])
        if listed:
            dlg.format.set_selected(dlg.formats.index(listed[0]))
            yield 100
            check("a hardware format names its GPU", "encoded on" in dlg.summary.get_text(), dlg.summary.get_text())
        dlg.close()
        yield 200

    # ---- grading: colour wheels, hue curves, secondary ------------------------
    from tideline import captions as capmod
    from tideline import dock as dockmod
    from tideline import grading
    from tideline.ui.caption_style import PRESETS, CaptionStyleDialog
    from tideline.ui.grading_widgets import ColourWheel, HueCurveEditor, QualifierView
    from tideline.ui.multicam import MulticamDialog
    from tideline.ui.widgets import PanelDrag
    grading.CACHE_DIR = f"{TMP}/luts"
    orig_seq = app.seq.id
    vclip = next(c for t in app.seq.video_tracks for c in t.clips if c.media_id in app.project.media
                 and app.project.media[c.media_id].kind == "video")
    vid = vclip.id
    live = lambda: app.seq.find_clip(vid)[1]
    fx = lambda kind: next((e for e in live().effects if e.type == kind), None)
    app.set_playhead(live().start + 5)
    app.select([vid], expand=False)
    for kind in ("wheels", "hue_curves", "secondary"):
        app.apply_effect(kind, [vid])
    win.show_panel("effect_controls")
    yield 600
    wheels = [r.editor for r in win.effect_controls.rows if isinstance(r.editor, ColourWheel)]
    check("Colour Wheels shows lift, gamma, gain and offset wheels (and the secondary's tint)",
          len(wheels) == 5, len(wheels))
    if wheels:
        n_hist = len(app.history.states)
        g = Mods()
        wheels[2]._begin(g, 50, 50)
        wheels[2]._update(g, 30, -10)
        wheels[2]._end(g, 30, -10)
        yield 400
        gain = fx("wheels").params.get("gain")
        check("dragging a wheel pushes its colour", gain is not None and gain[0] > 0.1 and gain[1] > 0.0, gain)
        check("a wheel drag is one undo step", len(app.history.states) == n_hist + 1)
        entry = app.graph.filters.get(fx("wheels").id) if app.graph else None
        lut = entry[0][1][0].get("av.file") if entry else ""
        check("the grade reaches the graph as a LUT", bool(lut) and lut.endswith(".cube") and os.path.exists(lut), lut)
    hue = next((r.editor for r in win.effect_controls.rows if isinstance(r.editor, HueCurveEditor)), None)
    check("Hue / Saturation Curves opens its curve editor", hue is not None)
    if hue is not None:
        hue.buttons["hue_sat"].set_active(True)
        bx, by, bw, bh = hue._box()
        g = Mods()
        hue._begin(g, bx + bw * 0.33, by + bh * 0.5)
        hue._update(g, 0, -bh * 0.3)
        hue._end(g, 0, -bh * 0.3)
        yield 400
        pts = fx("hue_curves").params.get("curves", {}).get("hue_sat", [])
        check("clicking a flat hue curve adds a point with two anchors", len(pts) == 3 and max(p[1] for p in pts) > 0.7,
              pts)
    sec = fx("secondary")
    view = next((p for p in win.effect_controls.previews if isinstance(p, QualifierView)), None)
    check("Secondary Correction shows its qualifier", view is not None)
    app.pick_qualifier(live(), sec)
    check("Pick Colour waits for a click in the Program monitor", app.picker is not None)
    win.program_monitor._xf_begin(None, *[c / 2 for c in (win.program_monitor.view.get_width(),
                                                         win.program_monitor.view.get_height())])
    yield 600
    sec = fx("secondary")
    check("a click picks the qualifier's colour", app.picker is None and sec.params.get("hue") is not None
          and sec.params.get("luma_high", 100) <= 100, sec.params)
    app.set_param(live(), sec, app_effects()["secondary"].param("view"), "mask")
    yield 900
    frame = app.frames.get("program")
    if frame and frame[0]:
        img = __import__("numpy").frombuffer(frame[0], dtype="uint8").reshape(frame[2], frame[1], 3).astype(int)
        check("Show Mask turns the picture into the selection's matte",
              abs(img[..., 0] - img[..., 1]).max() <= 2 and abs(img[..., 1] - img[..., 2]).max() <= 2)
    for kind in ("secondary", "hue_curves", "wheels"):
        app.remove_effect(live(), fx(kind))
    yield 300

    # ---- captions: style, animation and per-clip captions ---------------------
    cues = [capmod.Cue(0.2, 1.4, "Hello there everyone"), capmod.Cue(1.6, 3.0, "Second caption")]
    app.add_captions(cues, 0)
    yield 500
    ctrack = app.caption_tracks()[-1]
    check("captions land on a caption track", len(ctrack.clips) == 2)
    app.do("caption_style")
    yield 400
    dlg = next((w for w in app.get_windows() if isinstance(w, CaptionStyleDialog)), None)
    check("Caption Style opens", dlg is not None)
    if dlg is not None:
        dlg._preset(dict(PRESETS)["Bold Pop"])
        dlg.shadow.set_active(True)
        dlg.scope_all.set_active(True)
        n_hist = len(app.history.states)
        dlg.accept()
        yield 400
        texts = [c.effect("text") for c in app.caption_tracks()[-1].clips]
        check("a style applies to every caption", all(t.params.get("anim_in") == "pop"
                                                     and t.params.get("stroke_width") == 5 for t in texts))
        check("…with a drop shadow, in one undo step", all(c.effect("drop_shadow") for c in app.caption_tracks()[-1].clips)
              and len(app.history.states) == n_hist + 1)
        check("…and becomes the style for new captions", app.seq.caption_style.get("anim_in") == "pop")
    first_cap = app.caption_tracks()[-1].clips[0].id
    app.select([first_cap], expand=False)
    app.do("caption_style")
    yield 400
    dlg = next((w for w in app.get_windows() if isinstance(w, CaptionStyleDialog)), None)
    if dlg is not None:
        check("with captions selected the style applies to them", dlg.scope_selected.get_active())
        dlg.fill.set_rgba(__import__("tideline.ui.widgets", fromlist=["parse_rgba"]).parse_rgba("#ffd43bff"))
        dlg.anim_in.set_selected([k for k, _ in dlg.anim_in.items].index("typewriter"))
        dlg.accept()
        yield 600
        caps = app.caption_tracks()[-1].clips
        check("only the selected caption changes", caps[0].effect("text").params["fill"] == "#ffd43bff"
              and caps[1].effect("text").params["fill"] != "#ffd43bff")
        tid = caps[0].effect("text").id
        check("a typewriter caption types itself in, a filter per word", app.graph is not None
              and len(app.graph.filters.get(tid, [])) >= 3, len(app.graph.filters.get(tid, [])))
    source, a0, b0 = app.caption_source([vid])
    kept = [c for _, c in source.all_clips()]
    check("per-clip captions hear only that clip and its sound",
          {c.id for c in kept} == {vid} | {c.id for _, c in app.editor().partners(live())}
          and (a0, b0) == (live().start, live().end), (len(kept), a0, b0))
    from tideline.ui.captions_dialog import CaptionsDialog
    app.select([vid])
    cd = CaptionsDialog(app)
    check("Auto Captions offers the selected clip(s) only", cd._mode() == "clips" and cd._range() == (a0, b0))
    cd.close()
    yield 200

    # ---- multi-camera ---------------------------------------------------------
    done = []
    app.import_paths([f"{TMP}/camA.mp4", f"{TMP}/camB.mp4"], then=done.append)
    while not done:
        yield 100
    cams = [m for m in app.project.media.values() if m.name in ("camA.mp4", "camB.mp4")]
    cams.sort(key=lambda m: m.name)
    mc_dlg = MulticamDialog(app, cams)
    mc_dlg.present()
    mc_dlg.accept()
    for _ in range(100):
        if mc_dlg.job is None:
            break
        yield 100
    yield 600
    inner = next((s_ for s_ in app.project.sequences.values() if s_.multicam), None)
    check("Create Multi-Camera Sequence makes a camera per clip", inner is not None and len(inner.video_tracks) == 2)
    if inner is not None:
        starts = [t.clips[0].start for t in inner.video_tracks]
        check("the cameras are lined up by their sound", starts == [0, 45], starts)
        check("…and a sequence to cut it in opens", app.seq is not inner and app.seq.end() == inner.end()
              and all(c.angle == 0 for _, c in app.seq.all_clips()))
        mcp = win.multicam_panel
        check("the Multi-Camera panel shows", win.current_tab("multicam") == "multicam")
        app.set_playhead(60)
        for _ in range(50):
            if len(mcp.images) == 2:
                break
            yield 100
        check("…every camera at the playhead", len(mcp.images) == 2 and mcp.target is not None, len(mcp.images))
        app.clear_selection()
        app.do("camera_2")
        yield 600
        vclips = sorted(app.seq.video_tracks[0].clips, key=lambda c: c.start)
        check("pressing 2 cuts to camera 2 at the playhead", [(c.start, c.angle) for c in vclips] == [(0, 0), (60, 1)]
              and all(c.angle == 1 for c in app.seq.audio_tracks[0].clips if c.start == 60))
        frame = app.frames.get("program")
        if frame and frame[0]:
            img = __import__("numpy").frombuffer(frame[0], dtype="uint8").reshape(frame[2], frame[1], 3)
            mid = img[frame[2] // 2, frame[1] // 2]
            check("…and the Program monitor shows camera 2", mid[2] > 150 and mid[0] < 80, mid)
        app.set_playhead(100)
        app.program.play()
        yield 400
        app.do("camera_1")
        yield 300
        check("switches while playing wait for playback to stop", len(app.camera_cuts) == 1)
        app.program.pause()
        yield 600
        check("…then become cuts", len(app.seq.video_tracks[0].clips) == 3 and not app.camera_cuts,
              [(c.start, c.angle) for c in app.seq.video_tracks[0].clips])
        last = max(app.seq.video_tracks[0].clips, key=lambda c: c.start)
        app.select([last.id])
        app.set_playhead(last.start + 10)
        app.do("camera_2")
        yield 400
        last = max(app.seq.video_tracks[0].clips, key=lambda c: c.start)
        check("a selected multi-camera clip changes camera without a cut",
              len(app.seq.video_tracks[0].clips) == 3 and last.angle == 1)
        app.set_multicam_audio(inner, "all")
        yield 400
        check("the multi-camera sound can mix every camera", inner.multicam_audio == "all" and app.graph is not None)
        app.activate_action("set_camera", GLib.Variant.new_int32(0))
        yield 300
        check("the clip menu's Camera items set the camera", max(app.seq.video_tracks[0].clips,
                                                                 key=lambda c: c.start).angle == 0)

    # ---- dockable panels ------------------------------------------------------
    app.set_active_sequence(orig_seq)
    yield 300
    win.dock_panel("scopes", "program", "right")
    yield 400
    group = win.panel_group.get("scopes")
    check("a panel docks beside another, splitting its group",
          group is not None and group.keys == ["scopes"] and group is not win.panel_group["effects"]
          and dockmod.leaf_of(win.state, "program")["tabs"] == ["program"])
    tl_group = win.panel_group["timeline"]
    tl_group._drop(None, PanelDrag("mixer"), 5, tl_group.get_height() / 2)
    yield 400
    check("dropping a tab on a group's edge docks it there", win.panel_group["mixer"].keys == ["mixer"]
          and win.panel_group["timeline"].keys == ["timeline"])
    tl_group = win.panel_group["timeline"]
    tl_group._drop(None, PanelDrag("history"), tl_group.get_width() / 2, 4)
    yield 400
    check("dropping a tab on a group's tabs joins the group", win.panel_group["history"] is win.panel_group["timeline"])
    win.float_panel("history")
    yield 500
    check("a panel floats in a window of its own", len(win.floats) == 1
          and win.panel_group["history"].get_root() is win.floats[0])
    win.floats[0].close()
    yield 500
    check("closing a floating window docks its panels back", not win.floats and "history" in win.panel_group
          and win.panel_group["history"].get_root() is win)
    win.close_panel("info")
    yield 300
    check("a panel can be closed", "info" not in win.panel_group and "info" not in dockmod.all_panels(win.state))
    app.do("panel_info")
    yield 300
    check("its Window menu entry brings it back", "info" in win.panel_group)
    custom = dockmod.panels(win.state["tree"])
    app.do("workspace_audio")
    yield 300
    app.do("workspace_editing")
    yield 400
    check("each workspace keeps its own layout", dockmod.panels(win.state["tree"]) == custom)
    win.hovered = win.panel_group["program"]
    win.toggle_maximize()
    yield 300
    check("the group under the pointer maximises", list(win.panel_group) == ["program"])
    win.toggle_maximize()
    yield 300
    check("…and restores", "timeline" in win.panel_group and "scopes" in win.panel_group)
    yield 900
    check("layouts are remembered between sessions", isinstance(app.prefs["dock_layouts"].get("editing"), dict)
          and "scopes" in dockmod.all_panels(app.prefs["dock_layouts"]["editing"]))
    app.do("reset_workspace")
    yield 400
    check("Reset Workspace Layout restores the starting layout", win.state == dockmod.default("editing"),
          (win.state, dockmod.default("editing")))

    # ---- nesting, project panel, persistence --------------------------------
    first = app.project.media_for_sequence(app.seq.id)
    inner_end = app.seq.end()
    app.create_sequence("Outer", 640, 360, 30, 1, 48000, 2, 2)
    yield 400
    app.drop_media([first], 0)
    yield 600
    check("a sequence nests inside another", seq.name == "Outer" and app.graph.length == inner_end,
          (app.graph.length, inner_end))
    app.set_active_sequence(first.sequence_id)
    yield 400
    pp = win.project_panel
    app.do("new_bin")
    yield 200
    bin_ = next(m for m in app.project.media.values() if m.is_bin)
    idx = next(i for i in range(pp.store.get_n_items()) if pp.store.get_item(i).item.name == "b.mp4")
    pp.selection.select_item(idx, True)
    pp._move_to(bin_.id)
    yield 200
    check("items move into bins", items["b.mp4"].parent == bin_.id)
    path = f"{TMP}/project.tideline"
    app.project.path = path
    app.cmd_save()
    yield 200
    snapshot = app.project.to_dict()
    app.load_project(path)
    yield 800
    check("a saved project reloads identically", app.project.to_dict() == snapshot)
    check("the graph is rebuilt after loading", app.graph is not None and app.graph.length == app.seq.end())
    n = len(app.history.states)
    app.do("undo")
    check("history starts fresh after opening a project", n == 1 and app.project.to_dict() == snapshot)
