"""Auto Captions: transcribe the sequence's speech into caption clips."""

import os
import shutil
import tempfile

from gi.repository import GLib, Gtk

from .. import captions as cap
from .. import timecode as tc
from ..engine.builder import Builder
from .dialogs import _Dialog, _spin
from .widgets import alert, label


class CaptionsDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "Auto Captions", "Generate Captions")
        self.seq = app.seq
        self.job = None
        self._tmp = None
        prefs = app.prefs

        self.engine_menu = self.add_row("Recogniser", Gtk.DropDown.new_from_strings([]))
        self.model = self.add_row("Model", Gtk.DropDown.new_from_strings([m[1] for m in cap.MODELS]))
        self.model.set_selected(next((i for i, m in enumerate(cap.MODELS) if m[0] == prefs["caption_model"]), 1))
        self.language = self.add_row("Language", Gtk.DropDown.new_from_strings([lang[1] for lang in cap.LANGUAGES]))
        self.language.set_selected(next((i for i, lang in enumerate(cap.LANGUAGES)
                                         if lang[0] == prefs["caption_language"]), 0))
        seq = self.seq
        has_range = seq.in_point is not None or seq.out_point is not None
        self.range = self.add_row("Range", Gtk.DropDown.new_from_strings(["Entire sequence",
                                                                          "Sequence In to Out"]))
        self.range.set_selected(1 if has_range else 0)
        self.range.set_sensitive(has_range)
        layout = Gtk.Box(spacing=6)
        self.chars = _spin(prefs["caption_chars"], 16, 80)
        self.chars.set_width_chars(4)
        self.lines = _spin(prefs["caption_lines"], 1, 3)
        self.lines.set_width_chars(3)
        layout.append(self.chars)
        layout.append(label("characters a line, up to", css="dim"))
        layout.append(self.lines)
        layout.append(label("lines", css="dim"))
        self.add_row("Caption size", layout)
        self.replace = Gtk.CheckButton(label="Replace the captions already in this range")
        self.replace.set_visible(bool(app.caption_tracks(seq)))
        self.grid.attach(self.replace, 1, self._row, 1, 1)
        self._row += 1

        self.note = label("", css="faint", wrap=True, max_width_chars=58)
        self.extra.append(self.note)
        self.recheck = Gtk.Button(label="Check Again", halign=Gtk.Align.START, visible=False)
        self.recheck.connect("clicked", lambda b: self._find_engines())
        self.extra.append(self.recheck)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.progress.add_css_class("caption-progress")
        self.extra.append(self.progress)

        self.model.connect("notify::selected", lambda *_: self._describe())
        self.engine_menu.connect("notify::selected", lambda *_: self._describe())
        self.range.connect("notify::selected", lambda *_: self._describe())
        self.connect("close-request", self._closing)
        self._find_engines()

    # -- state -------------------------------------------------------------
    def _find_engines(self):
        self.engines = cap.engines()
        names = [e.label for e in self.engines] or ["None installed"]
        self.engine_menu.set_model(Gtk.StringList.new(names))
        pref = self.app.prefs["caption_engine"]
        self.engine_menu.set_selected(next((i for i, e in enumerate(self.engines) if e.key == pref), 0))
        self.engine_menu.set_sensitive(len(self.engines) > 1)
        self._describe()

    def _engine(self):
        i = self.engine_menu.get_selected()
        return self.engines[i] if 0 <= i < len(self.engines) else None

    def _range(self):
        seq = self.seq
        if self.range.get_selected() == 1:
            return seq.in_point or 0, seq.out_point if seq.out_point is not None else seq.end()
        return 0, seq.end()

    def _describe(self):
        engine = self._engine()
        self.recheck.set_visible(engine is None)
        self.ok_btn.set_sensitive(engine is not None and (self.job is None))
        self.model.set_sensitive(engine is not None)
        if engine is None:
            self.note.set_text(cap.INSTALL_HINT)
            return
        name, _, size = cap.MODELS[self.model.get_selected()]
        a, b = self._range()
        seq = self.seq
        text = f"Captions {tc.frames_to_tc(b - a, seq.fps_num, seq.fps_den)} of the sequence's mixed sound, " \
               "on your computer. "
        if engine.key == "cpp" and not engine.model_ready(name):
            text += f"The '{name}' model (about {size} MB) is downloaded once, the first time it is used."
        elif engine.key == "openai":
            text += "OpenAI Whisper downloads a model the first time it is used."
        text += " The captions land on a Captions track as titles you can edit and restyle."
        self.note.set_text(text)

    # -- running -------------------------------------------------------------
    def accept(self):
        engine = self._engine()
        if engine is None or (self.job is not None):
            return
        seq = self.seq
        a, b = self._range()
        if b - a < 1:
            return alert(self, "Nothing to caption", "The range is empty.")
        prefs = self.app.prefs
        model = cap.MODELS[self.model.get_selected()][0]
        prefs.update(caption_engine=engine.key, caption_model=model,
                     caption_language=cap.LANGUAGES[self.language.get_selected()][0],
                     caption_chars=int(self.chars.get_value()), caption_lines=int(self.lines.get_value()))
        prefs.save()
        self.app.program.pause()
        self._tmp = tempfile.mkdtemp(prefix="tideline-captions-xml-")
        xml = os.path.join(self._tmp, "sequence.mlt")
        try:
            Builder(self.app.project).write_xml(seq, xml)
        except Exception as exc:                      # noqa: BLE001 - shown to the user
            self._cleanup()
            return alert(self, "Could not prepare the sequence", str(exc))
        req = cap.Request(xml_path=xml, range_in=a, range_out=b, fps=seq.fps, engine=engine, model=model,
                          language=prefs["caption_language"], max_chars=prefs["caption_chars"],
                          max_lines=prefs["caption_lines"])
        self.job = cap.TranscribeJob(req, on_progress=lambda stage, f: GLib.idle_add(self._progress, stage, f),
                                     on_done=lambda job: GLib.idle_add(self._done, job))
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text("Preparing audio…")
        self.ok_btn.set_sensitive(False)
        self.cancel_btn.set_label("Stop")
        for w in (self.engine_menu, self.model, self.language, self.range, self.chars, self.lines, self.replace):
            w.set_sensitive(False)
        self.job.start()

    def _progress(self, stage, fraction):
        if self.job is None:
            return False
        self.progress.set_fraction(fraction)
        self.progress.set_text(f"{stage}…  {int(fraction * 100)}%")
        self.app.status(f"Auto captions: {stage.lower()}… {int(fraction * 100)}%")
        return False

    def _done(self, job):
        if job is not self.job:
            return False
        self.job = None
        self._cleanup()
        if job.error:
            if job.cancelled:
                self.app.status("Auto captions stopped.")
                super().close()
                return False
            self.progress.set_text("Failed")
            self.cancel_btn.set_label("Close")
            for w in (self.engine_menu, self.model, self.language, self.range, self.chars, self.lines,
                      self.replace):
                w.set_sensitive(True)
            self._describe()
            self.app.status("Auto captions failed", warn=True)
            alert(self, "Auto captions failed", job.error)
            return False
        a, b = self._range()
        self.app.add_captions(job.cues, a, b, replace=self.replace.get_active(), label="Auto Captions")
        super().close()
        return False

    def _cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def close(self):
        if self.job is not None:
            self.job.cancel()
            return
        self._cleanup()
        super().close()

    def _closing(self, *_):
        if self.job is not None:
            self.job.cancel()
            return True
        self._cleanup()
        return False
