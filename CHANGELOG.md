# Changelog

## 0.0.3 (2026-10-16)

- **Proxy editing**: play small, fast-decoding copies of large footage while
  you edit, and export from the originals. Create proxies from the Project
  panel (right-click ▸ Proxy) or File ▸ Proxies; FFmpeg makes them in the
  background with progress in the status bar and a Proxy column in the
  Project panel. Use Proxies (Ctrl+Alt+P, or the new button under each
  monitor) switches between proxies and originals at any time. Preferences
  choose the size (360p–1080p), MJPEG or H.264, where they are kept (beside
  the project or in the cache) and automatic proxies on import for footage
  above 1080p or 4K. Attach Proxy uses a file made elsewhere; a proxy stands
  in for an offline original; a changed original makes its proxy stale.
  Proxies are saved with the project but never in the undo history.
- **Extensions**: folders in `~/.config/tideline/extensions` with an
  `extension.json` manifest add effects (from MLT filters, with every
  parameter keyframable), wipe transitions drawn by a greyscale image, dips
  and frei0r transitions, export formats and preset folders, all without
  code. A Python `main` file with `register(tl)` can also add commands with
  shortcuts to the new Extensions menu, event hooks, effects with their own
  parameter logic, undoable timeline edits and saved settings. Extensions ▸
  Manage Extensions installs from a zip or folder, turns extensions on or
  off, removes them, and shows what each added or why it failed. A failing
  extension is rolled back completely. No extensions are bundled; the full
  guide with syntax and examples is docs/EXTENSIONS.md.
- Effect and transition categories in the Effects panel now include any new
  ones extensions bring.

## 0.0.2 (2026-10-16)

- **Time remapping** (Clip ▸ Time Remapping, Ctrl+Alt+R): a clip's speed
  becomes a keyframable curve in Effect Controls, with the usual keyframe graph
  and ease presets, so you can ramp from real time to slow motion, freeze a
  frame (0 %) or run backwards (below 0). Linked audio follows the same curve
  with its pitch kept. Optional frame blending smooths slow motion. Works on
  top of Speed/Duration and survives trims, undo, save and export.
- **Pop-out preview** (Window ▸ Pop-out Preview, Ctrl+Shift+F11, or the button
  under the Program monitor): the Program or Source picture in its own window,
  full screen on whichever display you pick, for a second monitor, projector or
  TV. Optional timecode and safe-margin overlays; playback keys work in it; the
  pointer hides itself; Esc goes back to a window.
- **Auto captions** (Sequence ▸ Captions ▸ Auto Captions, Ctrl+Alt+C):
  transcribes the sequence (or In to Out) on your own computer with whisper.cpp
  or OpenAI Whisper, in 20 languages or detected automatically. The captions
  are laid out for reading (line length, up to three lines, balanced breaks,
  sentence ends) and placed on a Captions track as titles you can edit and
  restyle. Import and export SRT and WebVTT (File ▸ Import / Export Captions).

## 0.0.1 — first release (2026-10-16)

The first public release of Tideline, a non-linear video editor for Linux on
MLT, FFmpeg and GTK 4. See README.md for the full feature list.

New in this release:

- **First-run setup**: choose your layout (Editing, Effects or Audio, shown as
  previews), a playback quality and your first project (name, folder, format).
- **Project manager** on later starts: recent projects with filter, New
  Project, Open Project and an unsaved empty project. Also under
  File ▸ Project Manager (Ctrl+Alt+O).
- **Preferences** (Edit ▸ Preferences, Ctrl+,): Performance / Balanced /
  Quality playback presets, preview resolution down to 1/8, preview scaling,
  render threads, frame dropping, interface update rate during playback and
  live Effect Controls values. Start-up layout, full screen and project manager.
- Smoother playback: panels that follow the playhead are redrawn at a capped
  rate while the picture plays at full frame rate.
- App icon in every size, used by the windows even when run from source.
- `install.sh` for distributions other than Arch.
