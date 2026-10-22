"""DeepVoice for Tideline: isolate a voice from any clip with an AI model
that runs on your own computer.

Select audio clips (or video clips with linked audio) and choose
Extensions › DeepVoice › Isolate Voice. The audio is split by Demucs into
the voice and everything else; the clip is switched to the voice file in
one undo step, keeping its in and out points, speed, remapping and effects.
"""

import atexit
import os

from deepvoice_core import (CACHE_DIR, DEFAULTS, DEVICES, MODELS, STEMS, AIEnv, Separator, Setup,
                            Task)

ACTION_LABEL = {"voice": "Isolate Voice", "background": "Remove Voice"}


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

    # -- finding what to work on ----------------------------------------------
    def audio_targets(app):
        """Audio clips to change: selected audio clips, plus the audio linked
        to selected video clips."""
        seq = app.seq
        if seq is None:
            return []
        found = {}
        for track, clip in app.selected_clips():
            if track.kind == "audio":
                found[clip.id] = (track, clip)
            elif clip.link_id:
                for t, c in seq.all_clips():
                    if t.kind == "audio" and c.link_id == clip.link_id:
                        found[c.id] = (t, c)
        return [tc for tc in found.values() if not tc[0].locked]

    def original_of(project, media):
        """The media a DeepVoice file was made from (or the media itself)."""
        info = (media.params or {}).get("deepvoice") if media is not None else None
        if info and info.get("source") in project.media:
            return project.media[info["source"]]
        return media

    def out_dir(app):
        path = getattr(app.project, "path", None)
        if settings.get("save_next_to_project") and path:
            return os.path.join(os.path.dirname(os.path.abspath(path)), "DeepVoice")
        return CACHE_DIR

    # -- results ----------------------------------------------------------------
    def on_status(text):
        tl.status(text)

    def on_error(task, message):
        name = os.path.basename(task.path)
        tl.status(f"DeepVoice: {name}: {message}", warn=True)

    def on_done(task, result):
        app = tl.app
        if app is None:
            return
        project = app.project
        p = task.payload
        src = project.media.get(p["source_id"])
        if src is None or src.path != task.path:
            return tl.status("DeepVoice finished, but that media is no longer in the project.", warn=True)
        from tideline.engine import media as mediamod

        path = result[task.stem]
        item = project.media_for_path(path)
        new = item is None
        if new:
            try:
                item = mediamod.probe(path)
            except mediamod.ProbeError as exc:
                return tl.status(f"DeepVoice: could not read the result: {exc}", warn=True)
            item.name = project.unique_name(f"{src.name} — {STEMS[task.stem]}")
            item.parent = src.parent
            item.params = {"deepvoice": {"source": src.id, "stem": task.stem,
                                         "model": settings.get("model")}}
            project.add_media(item)        # before the edit, so its undo step holds the item

        seq = app.seq
        if seq is None or seq.id != p["seq_id"]:
            app.emit("media")
            return tl.status(f"DeepVoice: “{item.name}” is in the Project panel "
                             "(the sequence it was made for isn't open).")

        def swap(editor):
            changed = 0
            for cid in p["clip_ids"]:
                _, clip = editor.seq.find_clip(cid)
                if clip is None or clip.media_id == item.id:
                    continue
                clip.media_id = item.id
                clip.name = item.name
                changed += 1
            return changed

        changed = tl.edit(ACTION_LABEL[task.stem], swap)
        app.emit("media")
        if changed:
            tl.status(f"DeepVoice: {ACTION_LABEL[task.stem].lower()} done on {changed} clip(s) "
                      f"— “{item.name}”. Undo restores the original.")
        else:
            tl.status(f"DeepVoice: “{item.name}” is ready in the Project panel.")

    sep = Separator(settings, post, on_status, on_done, on_error)
    setup = Setup(settings, post, on_status,
                  lambda ok, msg: tl.status(msg, warn=not ok))
    atexit.register(sep.cancel_all)
    atexit.register(setup.cancel)

    # -- commands ------------------------------------------------------------------
    def separate(app, stem):
        if setup.running:
            return tl.status("DeepVoice is still setting up the AI. Try again when it's done.", warn=True)
        targets = audio_targets(app)
        if not targets:
            return tl.status("DeepVoice: select an audio clip, or a video clip with linked audio.", warn=True)
        if not AIEnv(settings).exists():
            return tl.status("DeepVoice: the AI isn't installed yet. "
                             "Choose Extensions › DeepVoice › Set Up AI first (one time).", warn=True)
        project, folder = app.project, out_dir(app)
        groups, skipped = {}, 0
        for _, clip in targets:
            src = original_of(project, project.media.get(clip.media_id))
            if src is None or not src.path or not src.has_audio or src.offline \
                    or not os.path.isfile(src.path):
                skipped += 1
                continue
            groups.setdefault(src.id, (src, []))[1].append(clip.id)
        if not groups:
            return tl.status("DeepVoice: the selected clips have no audio file to work on "
                             "(nested sequences and generated items can't be separated).", warn=True)
        queued = 0
        for src, clip_ids in groups.values():
            task = Task(src.path, folder, stem,
                        {"source_id": src.id, "clip_ids": clip_ids, "seq_id": app.seq.id})
            hit = sep.cached(src.path, folder)
            if hit:
                on_done(task, hit)                    # already separated: instant
            else:
                sep.submit(task)
                queued += 1
        if queued:
            note = f", skipped {skipped}" if skipped else ""
            tl.status(f"DeepVoice: separating {queued} file(s) in the background{note}…")

    def restore(app):
        project = app.project
        pairs = []
        for _, clip in audio_targets(app):
            media = project.media.get(clip.media_id)
            info = (media.params or {}).get("deepvoice") if media else None
            if info and info.get("source") in project.media:
                pairs.append((clip.id, project.media[info["source"]]))
        if not pairs:
            return tl.status("DeepVoice: no selected clip uses a DeepVoice file.", warn=True)

        def run(editor):
            n = 0
            for cid, src in pairs:
                _, clip = editor.seq.find_clip(cid)
                if clip is not None:
                    clip.media_id, clip.name = src.id, src.name
                    n += 1
            return n

        n = tl.edit("Restore Original Audio", run)
        tl.status(f"DeepVoice: restored the original audio on {n or 0} clip(s).")

    def cancel(app):
        n = sep.cancel_all()
        if setup.running:
            setup.cancel()
            n += 1
        tl.status(f"DeepVoice: cancelled {n} job(s)." if n else "DeepVoice: nothing is running.")

    def start_setup(app):
        if sep.busy:
            return tl.status("DeepVoice: wait for the current separation (or cancel it) first.", warn=True)
        if setup.start():
            tl.status("DeepVoice: setting up the AI in the background. The first time downloads "
                      "PyTorch and the model (a few hundred MB on CPU).")

    def settings_dialog(app):
        try:
            _open_settings(tl, app, settings, start_setup)
        except Exception as exc:          # the dialog uses Tideline UI internals
            path = os.path.join(os.path.expanduser("~/.config/tideline/extension-data"), tl.id, "settings.json")
            tl.status(f"DeepVoice: settings window failed ({exc}); edit {path} instead.", warn=True)

    tl.command("isolate_voice", "Isolate Voice", lambda app: separate(app, "voice"), accel="<Ctrl><Alt>v")
    tl.command("remove_voice", "Remove Voice (Keep Background)", lambda app: separate(app, "background"),
               accel="<Ctrl><Alt><Shift>v")
    tl.command("restore_original", "Restore Original Audio", restore)
    tl.command("cancel", "Cancel DeepVoice Jobs", cancel)
    tl.command("setup", "Set Up AI (download model)…", start_setup)
    tl.command("settings", "DeepVoice Settings…", settings_dialog)


# -- settings window ---------------------------------------------------------------
def _open_settings(tl, app, settings, start_setup):
    import threading

    from gi.repository import GLib, Gtk
    from tideline.ui.dialogs import _Dialog, _spin
    from tideline.ui.widgets import label

    class SettingsDialog(_Dialog):
        def __init__(self):
            super().__init__(app, "DeepVoice Settings", ok_label="Save")
            self.model = self._drop(MODELS, settings.get("model"))
            self.add_row("AI model", self.model)
            self.device = self._drop(DEVICES, settings.get("device"))
            self.add_row("Run on", self.device)
            self.shifts = _spin(int(settings.get("shifts") or 1), 1, 5)
            self.shifts.set_tooltip_text("Passes per file. 2–5 is a little cleaner and that many times slower.")
            self.add_row("Quality passes", self.shifts)
            self.beside = Gtk.CheckButton(label="Save separated audio in a DeepVoice folder next to the project")
            self.beside.set_active(bool(settings.get("save_next_to_project")))
            self.add_row("Files", self.beside)
            self.python = Gtk.Entry(text=settings.get("python") or "", width_chars=34,
                                    placeholder_text="DeepVoice's own (recommended)")
            self.add_row("Python", self.python)
            self.repo = Gtk.Entry(text=settings.get("model_repo") or "", width_chars=34,
                                  placeholder_text="Downloaded automatically")
            self.repo.set_tooltip_text("A folder of Demucs model files (.th and .yaml) for machines with no internet.")
            self.add_row("Model folder", self.repo)
            self.state = label("Checking the AI…", css="dim")
            self.state.set_wrap(True)
            self.extra.append(self.state)
            setup_btn = Gtk.Button(label="Set Up / Repair AI")
            setup_btn.set_halign(Gtk.Align.START)
            setup_btn.connect("clicked", lambda *_: (self.accept(), start_setup(app)))
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
            settings["model"] = self.model._keys[self.model.get_selected()]
            settings["device"] = self.device._keys[self.device.get_selected()]
            settings["shifts"] = int(self.shifts.get_value())
            settings["save_next_to_project"] = self.beside.get_active()
            settings["python"] = self.python.get_text().strip()
            settings["model_repo"] = self.repo.get_text().strip()
            tl.save_settings()
            tl.status("DeepVoice settings saved.")
            self.close()

    SettingsDialog().present()
