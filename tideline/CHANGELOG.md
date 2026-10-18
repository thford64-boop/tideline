# Changelog

## 0.0.5 (2026-10-18)

- **Multi-camera editing**: Clip ▸ Multi-Camera ▸ Create Multi-Camera Sequence
  lines up several cameras by their sound (onsets cross-correlated; a recorder
  without a picture syncs too), their In points or their first frames, and
  opens a sequence to cut it in. The Multi-Camera panel (Shift+9) shows every
  camera at the playhead; press 1–9 (keypad too) or click a camera to cut to
  it. While playing, the switches are made when playback stops, in one undo
  step; when stopped, a number cuts at the playhead or changes a selected
  clip's camera. Right-click ▸ Camera on clips; the sound follows one
  recording, the camera, or mixes them all.
- **Captions per clip**: Auto Captions can caption all of the sequence, its In
  to Out range, or only the selected clip(s), listening to just their sound.
  New captions go on a caption track that is free there before a new one.
- **Caption style**: Sequence ▸ Captions ▸ Caption Style sets font, size, bold,
  italic, underline, text, outline and box colours, outline width, padding,
  drop shadow, position and margin, with presets and a preview, for the
  selected captions or all of them (and new ones).
- **Title animations**: the Text effect has Animate In / Animate Out — Fade,
  Pop, Zoom, Slide Up/Down/Left/Right, Drop, and Typewriter or Word by Word
  typing that keeps lines in place — with their own duration. Outlines can be
  up to 20 px wide.
- **Colour Wheels**: Lift, Gamma, Gain and Offset wheels with masters, plus
  Contrast, Pivot and Saturation, oriented like the vectorscope.
- **Hue / Saturation Curves**: Hue vs Hue, Hue vs Sat, Hue vs Luma, Luma vs
  Sat and Sat vs Sat, over the picture's own distribution; hue curves wrap.
- **Secondary Correction**: a hue, saturation and luma qualifier with soft
  edges and invert, Pick Colour from the Program monitor, Show Mask and
  Highlight, and Hue Shift, Saturation, Lightness and Tint inside the
  selection. The three grades are baked into cached 3D LUTs that FFmpeg
  applies, so preview and export match.
- **Dockable panels**: drag a tab onto another group, onto a group's edge to
  split it, or out of the window to float it; right-click a tab to float or
  close it, and the Window menu reopens closed panels. Each workspace keeps
  its own layout between sessions; Window ▸ Reset Workspace Layout restores it.
- Fixed: right after an edit, the monitor could show MLT's grey test card for
  a frame with titles, crops, shadows, RGB Curves or LUTs (the first picture
  through those filters in a new graph fails); that frame is now rendered
  again before it is shown.

## 0.0.4 (2026-10-18)

- **Scopes**: a Scopes panel (Window ▸ Scopes, Shift+8) with a waveform (luma,
  RGB overlay or YCbCr), RGB parade, vectorscope (75 % bar targets, skin tone
  line, each point in its own colour) and histogram with crushed and clipped
  percentages, or all four together. It follows the Program or Source monitor
  as it plays, at up to 15 updates a second, using Rec. 709 luma.
- **RGB Curves** (Effects ▸ Color): master, red, green and blue curves edited
  in Effect Controls over the Program picture's histogram. Points are added,
  dragged, nudged with the arrow keys and removed; the curve drawn is exactly
  the one FFmpeg renders (PCHIP, no overshoot), and the change shows live.
- **Colour workspace** (Alt+Shift+4, and in the first-run setup): Effect
  Controls beside the Program monitor with the Scopes under it.
- **Hardware encoding presets**: H.264, H.265 and AV1 with VA-API (AMD, Intel),
  NVENC (NVIDIA) and Quick Sync (Intel). The Export dialog tries each encoder
  on each graphics card and offers only the ones that work, naming the card,
  with a choice when more than one can do it.
- **Keyboard Shortcut Mapper** (Edit ▸ Keyboard Shortcuts, Ctrl+Alt+K): a
  keyboard that shows what every key does with any modifiers, and every
  command (extensions' too) with a shortcut you can change by pressing the
  new keys. Taken keys are reassigned only after asking; shortcuts can be
  removed, reset, and exported or imported as `.tlkeys` files. Menus and
  tooltips follow the new shortcuts at once.
- Fixed: the Shift+1 … Shift+8 panel shortcuts never fired, because Shift
  turns the digit into a symbol (US layout); they are matched as written now.
- **AppImage**: `packaging/appimage/build.py` builds a single-file
  `Tideline-<version>-x86_64.AppImage` with Python, GTK 4, libadwaita, MLT,
  FFmpeg, frei0r and Qt inside. Programs Tideline starts (a file manager,
  whisper.cpp) get the system's own environment, not the AppImage's.

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
