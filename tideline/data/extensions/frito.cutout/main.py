"""Cutout for Tideline: remove the background of a picture or video with an
AI model (Meta's SAM 2) that runs on your own computer.

Select a video or picture clip and choose Extensions › Cutout › Remove
Background (Ctrl+Alt+B). Click what you want to keep; the AI outlines it and
everything around it becomes transparent. The clip is switched to the
cut-out in one undo step, keeping its in and out points, speed and effects,
so whatever is on the tracks below shows through.
"""

import atexit
import json
import math
import os

from cutout_core import (CACHE_DIR, DEFAULTS, DEVICES, GPU_BUILDS, PICTURE_MODELS, VIDEO_MODELS,
                         VIDEO_SIZES, AIEnv, CutoutError, Session, Setup, VideoJobs, VideoTask, file_key,
                         suggested_build, video_choice)
# Imported now: the extension folder is only on sys.path while Tideline loads it.
from cutout_ui import CutoutWindow

HANDLE_SECONDS = 1.0         # extra video processed around a clip, so trimming it a little still works
CUTOUT = {}                  # the running parts, for tests and scripts


def register(tl):
    settings = tl.settings
    for k, v in DEFAULTS.items():
        settings.setdefault(k, v)
    tl.save_settings()

    def post(fn, *args):
        """Run fn(*args) on Tideline's main thread."""
        from gi.repository import GLib

        def call():
            fn(*args)
            return False
        GLib.idle_add(call)

    def status(text):
        if text:
            tl.status(text)

    session = Session(settings, post, lambda text: status(f"Cutout: {text}" if text else ""))
    state = {"window": None}

    # -- media helpers -------------------------------------------------------------
    def info_of(media):
        return (media.params or {}).get("cutout") if media is not None else None

    def original_of(project, media):
        """(the media a Cutout file was made from — or the media itself, its
        Cutout info or None)."""
        info = info_of(media)
        if info and info.get("source") in project.media:
            return project.media[info["source"]], info
        return media, None

    def offset_of(media):
        info = info_of(media)
        return int(info.get("offset") or 0) if info else 0

    def out_dir(app):
        path = getattr(app.project, "path", None)
        if settings.get("save_next_to_project") and path:
            return os.path.join(os.path.dirname(os.path.abspath(path)), "Cutout")
        return CACHE_DIR

    def frame_exact(clip, seq, media):
        """True when the clip's frames are the source's frames one for one,
        so a cut-out of just the part in use can line up exactly."""
        from tideline import timeremap
        return (abs(clip.speed - 1.0) < 1e-9 and not clip.reverse and timeremap.remap_of(clip) is None
                and media.fps > 0 and abs(media.fps - seq.fps) < 0.01)

    # -- results ---------------------------------------------------------------------
    def swap(task_payload, path):
        app = tl.app
        if app is None:
            return
        project = app.project
        p = task_payload
        src = project.media.get(p["source_id"])
        if src is None or src.path != p["source_path"]:
            return tl.status("Cutout finished, but that media is no longer in the project.", warn=True)
        from tideline.engine import media as mediamod

        item = project.media_for_path(path)
        if item is None:
            try:
                item = mediamod.probe(path)
            except mediamod.ProbeError as exc:
                return tl.status(f"Cutout: could not read the result: {exc}", warn=True)
            item.name = project.unique_name(f"{src.name} — Cutout")
            item.parent = src.parent
            project.add_media(item)        # before the edit, so its undo step holds the item
        item.params = dict(item.params or {}, cutout={"source": src.id, "offset": p["offset"],
                                                       "prompts": p["prompts"]})

        seq = app.seq
        if seq is None or seq.id != p["seq_id"]:
            app.emit("media")
            return tl.status(f"Cutout: “{item.name}” is in the Project panel "
                             "(the sequence it was made for isn't open).")

        def run(editor):
            changed = 0
            for cid in p["clip_ids"]:
                _, clip = editor.seq.find_clip(cid)
                if clip is None or clip.media_id == item.id:
                    continue
                current = project.media.get(clip.media_id)
                if original_of(project, current)[0] is not src:
                    continue                      # switched to something else meanwhile
                clip.in_point += offset_of(current) - p["offset"]
                clip.media_id = item.id
                clip.name = item.name
                changed += 1
            return changed

        changed = tl.edit("Remove Background", run)
        app.emit("media")
        if changed:
            tl.status(f"Cutout: background removed on {changed} clip(s) — “{item.name}”. "
                      "Undo, or Extensions › Cutout › Restore Original, brings it back.")
        else:
            tl.status(f"Cutout: “{item.name}” is ready in the Project panel.")

    def on_video_error(task, message):
        tl.status(f"Cutout: {os.path.basename(task.path)}: {message}", warn=True)

    jobs = VideoJobs(settings, post, status, lambda task, path: swap(task.payload, path), on_video_error)
    setup = Setup(settings, post, status, lambda ok, msg: (tl.status(msg, warn=not ok), session.stop()))
    atexit.register(jobs.cancel_all)
    atexit.register(setup.cancel)
    atexit.register(session.stop)

    # -- the command -------------------------------------------------------------------
    def targets(app):
        """(source media, [clips], previous cutout info) for the selection."""
        project, seq = app.project, app.seq
        picked = [(t, c) for t, c in app.selected_clips() if t.kind == "video" and not t.locked]
        if not picked:
            return None, [], None
        groups = {}
        for _, clip in picked:
            media = project.media.get(clip.media_id)
            src, info = original_of(project, media)
            if src is None or src.kind not in ("video", "image") or not src.path or src.offline \
                    or not os.path.isfile(src.path):
                continue
            g = groups.setdefault(src.id, [src, [], info])
            g[1].append(clip)
            g[2] = g[2] or info
        if not groups:
            return False, [], None
        # Work on the clip the playhead is over first, else the first selected.
        ph = seq.playhead if seq is not None else 0
        best = next((g for g in groups.values() for c in g[1] if c.start <= ph < c.end), None)
        src, clips, info = best or next(iter(groups.values()))
        return src, clips, info

    def remove_background(app):
        if setup.running:
            return tl.status("Cutout is still setting up the AI. Try again when it's done.", warn=True)
        if app.seq is None:
            return tl.status("Cutout: open a sequence first.", warn=True)
        src, clips, info = targets(app)
        if src is None:
            return tl.status("Cutout: select a video or picture clip on a video track.", warn=True)
        if src is False:
            return tl.status("Cutout: the selected clip isn't a video or picture file "
                             "(titles, colours and nested sequences can't be cut out).", warn=True)
        if not AIEnv(settings).exists():
            return tl.status("Cutout: the AI isn't installed yet. "
                             "Choose Extensions › Cutout › Set Up AI first (one time).", warn=True)
        if state["window"] is not None:
            state["window"].present()
            return
        seq, project = app.seq, app.project
        source = {"path": src.path, "kind": src.kind, "name": src.name}
        if src.kind == "video":
            fps = src.fps or seq.fps
            total = max(1, int(src.duration * fps))
            ranges = []
            for clip in clips:
                media = project.media.get(clip.media_id)
                if frame_exact(clip, seq, src):
                    off = offset_of(media)
                    ranges.append((clip.in_point + off, clip.in_point + off + clip.duration - 1))
                else:
                    ranges.append((0, total - 1))
            first = max(0, min(r[0] for r in ranges))
            last = min(total - 1, max(r[1] for r in ranges))
            clip = clips[0]
            ph = seq.playhead
            frame = first
            if clip.start <= ph < clip.end and frame_exact(clip, seq, src):
                frame = clip.in_point + offset_of(project.media.get(clip.media_id)) + (ph - clip.start)
            elif clip.start <= ph < clip.end:
                frame = int(round(clip.source_frame(ph) * clip.speed / seq.fps * fps))
            source.update(fps=fps, first=first, last=last, frame=min(last, max(first, frame)), total=total)
        prompts = (info or {}).get("prompts")

        def apply(prompts):
            settings["edges"] = dict(prompts.get("edges") or {})
            tl.save_settings()
            run_cutout(app, src, [c.id for c in clips], source, prompts)

        def closed():
            state["window"] = None

        try:
            session.start()
        except CutoutError as exc:
            return tl.status(f"Cutout: {exc}", warn=True)
        win = CutoutWindow(app, session, source, prompts, settings.get("edges"), apply, closed)
        state["window"] = win
        win.present()

    def run_cutout(app, src, clip_ids, source, prompts):
        folder = out_dir(app)
        os.makedirs(folder, exist_ok=True)
        base = os.path.splitext(os.path.basename(src.path))[0]
        payload = {"source_id": src.id, "source_path": src.path, "clip_ids": clip_ids,
                   "seq_id": app.seq.id, "prompts": prompts, "offset": 0}
        if src.kind == "image":
            model = settings.get("picture_model") or DEFAULTS["picture_model"]
            out = os.path.join(folder, f"{base}.cutout.{file_key(src.path, json.dumps(prompts, sort_keys=True), model)}.png")
            if os.path.isfile(out):
                return swap(payload, out)
            tl.status("Cutout: removing the background…")

            def saved(reply):
                if reply.get("ok"):
                    swap(payload, out)
                else:
                    tl.status(f"Cutout: {reply.get('error')}", warn=True)

            session.request("load", {"path": src.path}, lambda r: None)
            session.request("save", {"objects": prompts["objects"], "frame": 0, "edges": prompts.get("edges"),
                                     "output": out}, saved)
            return

        # Video: only the part the clips use (plus a second each side) when
        # the clips show the source frame for frame; otherwise all of it.
        seq = app.seq
        fps = source["fps"]
        total = source["total"]
        exact = True
        for cid in clip_ids:
            _, clip = seq.find_clip(cid)
            if clip is not None and not frame_exact(clip, seq, src):
                exact = False
        handle = int(math.ceil(HANDLE_SECONDS * fps))
        if exact:
            start = max(0, source["first"] - handle)
            end = min(total - 1, source["last"] + handle)
        else:
            start, end = 0, total - 1
        payload["offset"] = start
        model, size = video_choice(settings)
        key = file_key(src.path, json.dumps(prompts, sort_keys=True), model, size, start, end,
                       settings.get("picture_model"))
        out = os.path.join(folder, f"{base}.cutout.{key}.mkv")
        if os.path.isfile(out):
            return swap(payload, out)
        session.stop()                    # free its memory for the video job
        jobs.submit(VideoTask(src.path, out, prompts, start, end, payload))
        n = end - start + 1
        tl.status(f"Cutout: removing the background of {n} frames ({n / fps:.1f} s) in the background — "
                  "keep editing; the clip switches over when it's done.")

    # -- other commands --------------------------------------------------------------------
    def restore(app):
        project = app.project
        pairs = []
        for t, clip in app.selected_clips():
            media = project.media.get(clip.media_id)
            src, info = original_of(project, media)
            if info:
                pairs.append((clip.id, src, int(info.get("offset") or 0)))
        if not pairs:
            return tl.status("Cutout: no selected clip uses a Cutout file.", warn=True)

        def run(editor):
            n = 0
            for cid, src, off in pairs:
                _, clip = editor.seq.find_clip(cid)
                if clip is not None:
                    clip.media_id, clip.name = src.id, src.name
                    clip.in_point += off
                    n += 1
            return n

        n = tl.edit("Restore Original", run)
        tl.status(f"Cutout: restored the original on {n or 0} clip(s).")

    def cancel(app):
        n = jobs.cancel_all()
        if setup.running:
            setup.cancel()
            n += 1
        tl.status(f"Cutout: cancelled {n} job(s)." if n else "Cutout: nothing is running.")

    def start_setup(app, build=None):
        if jobs.busy:
            return tl.status("Cutout: wait for the current video (or cancel it) first.", warn=True)
        session.stop()
        build = build or settings.get("gpu_build") or suggested_build()
        settings["gpu_build"] = build
        tl.save_settings()
        if setup.start(build):
            size = {"cpu": "about 1 GB", "rocm": "about 6 GB", "cuda": "about 4 GB"}.get(build, "")
            tl.status(f"Cutout: setting up the AI in the background — downloading PyTorch and the models "
                      f"({size}). Progress shows here.")

    def settings_dialog(app):
        try:
            _open_settings(tl, app, settings, start_setup, session)
        except Exception as exc:          # the dialog uses Tideline UI internals
            path = os.path.join(os.path.expanduser("~/.config/tideline/extension-data"), tl.id, "settings.json")
            tl.status(f"Cutout: settings window failed ({exc}); edit {path} instead.", warn=True)

    tl.command("remove_background", "Remove Background…", remove_background, accel="<Ctrl><Alt>b")
    tl.command("restore_original", "Restore Original", restore)
    tl.command("cancel", "Cancel Cutout Jobs", cancel)
    tl.command("setup", "Set Up AI (download models)…", start_setup)
    tl.command("settings", "Cutout Settings…", settings_dialog)

    CUTOUT.update(session=session, jobs=jobs, setup=setup, swap=swap, run=run_cutout, state=state)


# -- settings window ---------------------------------------------------------------
def _open_settings(tl, app, settings, start_setup, session):
    import threading

    from gi.repository import GLib, Gtk
    from tideline.ui.dialogs import _Dialog, _spin
    from tideline.ui.widgets import label

    class SettingsDialog(_Dialog):
        def __init__(self):
            super().__init__(app, "Cutout Settings", ok_label="Save")
            self.picture = self._drop(PICTURE_MODELS, settings.get("picture_model"))
            self.picture.set_tooltip_text("Large is the most accurate and still takes only a second or two a picture.")
            self.add_row("Pictures", self.picture)
            self.vmodel = self._drop(VIDEO_MODELS, settings.get("video_model"))
            self.add_row("Video model", self.vmodel)
            self.vsize = self._drop(VIDEO_SIZES, settings.get("video_size"))
            self.vsize.set_tooltip_text("The size the AI looks at video in. Smaller is much faster; the edges "
                                        "are refined at full size either way.")
            self.add_row("Video detail", self.vsize)
            self.device = self._drop(DEVICES, settings.get("device"))
            self.add_row("Run on", self.device)
            current = settings.get("gpu_build") or suggested_build()
            self.build = self._drop(GPU_BUILDS, current)
            self.build.set_tooltip_text("Which PyTorch Set Up AI installs. A GPU build is many times faster for "
                                        "video but a much bigger download. Run Set Up AI again after changing it.")
            self.add_row("Install for", self.build)
            self.keep = _spin(int(settings.get("keep_ai_loaded_minutes") or 10), 1, 120)
            self.keep.set_tooltip_text("Minutes the AI stays loaded after the window closes, so it opens "
                                       "instantly next time. It uses 1–3 GB of memory while loaded.")
            self.add_row("Keep AI loaded (min)", self.keep)
            self.beside = Gtk.CheckButton(label="Save cut-outs in a Cutout folder next to the project")
            self.beside.set_active(bool(settings.get("save_next_to_project")))
            self.add_row("Files", self.beside)
            self.python = Gtk.Entry(text=settings.get("python") or "", width_chars=34,
                                    placeholder_text="Cutout's own (recommended)")
            self.add_row("Python", self.python)
            self.state = label("Checking the AI…", css="dim")
            self.state.set_wrap(True)
            self.state.set_max_width_chars(60)
            self.extra.append(self.state)
            setup_btn = Gtk.Button(label="Set Up / Repair AI")
            setup_btn.set_halign(Gtk.Align.START)
            setup_btn.connect("clicked", lambda *_: (self.accept(), start_setup(app, settings["gpu_build"])))
            self.extra.append(setup_btn)
            threading.Thread(target=self._check, daemon=True).start()

        @staticmethod
        def _drop(options, current):
            dd = Gtk.DropDown.new_from_strings([lab for _, lab in options])
            keys = [k for k, _ in options]
            dd.set_selected(keys.index(current) if current in keys else 0)
            dd._keys = keys
            return dd

        def _check(self):
            ok, msg = AIEnv(settings).check()
            GLib.idle_add(lambda: (self.state.set_text(("Ready — " if ok else "") + msg), False)[1])

        def accept(self):
            settings["picture_model"] = self.picture._keys[self.picture.get_selected()]
            settings["video_model"] = self.vmodel._keys[self.vmodel.get_selected()]
            settings["video_size"] = self.vsize._keys[self.vsize.get_selected()]
            settings["device"] = self.device._keys[self.device.get_selected()]
            settings["gpu_build"] = self.build._keys[self.build.get_selected()]
            settings["keep_ai_loaded_minutes"] = int(self.keep.get_value())
            settings["save_next_to_project"] = self.beside.get_active()
            settings["python"] = self.python.get_text().strip()
            tl.save_settings()
            session.stop()                # picks up a new model or device next time
            if not AIEnv(settings).models_ready():
                tl.status("Cutout settings saved. That model isn't downloaded yet: run Set Up AI.", warn=True)
            else:
                tl.status("Cutout settings saved.")
            self.close()

    SettingsDialog().present()
