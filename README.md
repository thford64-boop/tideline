# Tideline

A non-linear video editor for Linux, written for Arch. It follows the workflow
professional editors share — source and program monitors, a project bin, a
multitrack timeline with trim tools, effect controls with keyframes — on top of
[MLT](https://www.mltframework.org/), FFmpeg and GTK 4.

![Tideline editing workspace](docs/screenshot.png)

## Run it

Everything it needs is in the Arch repositories:

```sh
sudo pacman -S --needed python python-gobject python-cairo python-numpy gtk4 libadwaita \
    mlt ffmpeg frei0r-plugins sdl2 qt6-base qt6-svg qt6-5compat rubberband
./run.sh                      # from this directory, no install needed
./run.sh project.tideline     # open a project
```

To install it as a package (menu entry, icon, `tideline` command, `.tideline` file type):

```sh
makepkg -si
```

On other distributions, install the same dependencies from your package
manager and run `./install.sh` (into `~/.local`; `--uninstall` removes it).

## What it does

**Project panel** — import files or folders (dialog or drag and drop), list and
icon views with thumbnails, bins, search, labels, relink offline media. New
items: sequences, titles, colour mattes, black video, bars and tone.

**Source monitor** — preview a clip, mark In/Out, then Insert (`,`) or
Overwrite (`.`) into the timeline, or drag the picture onto a track. Three-point
edits use the sequence In/Out when set.

**Timeline** — any number of video and audio tracks; linked audio/video;
snapping; thumbnails and waveforms; markers; track target, lock, hide, mute and
solo; sequences nested inside sequences. Tools:

| Key | Tool | Key | Tool |
|-----|------|-----|------|
| V | Selection (move, trim; Ctrl = ripple, Alt = duplicate, Ctrl-drop = insert) | Y | Slip |
| A | Track Select Forward | U | Slide |
| B | Ripple Edit | P | Pen (opacity / volume keyframes on the clip) |
| N | Rolling Edit | H | Hand |
| R | Rate Stretch | Z | Zoom |
| C | Razor (Shift = all tracks) | T | Type (click in the Program monitor) |

**Editing commands** — lift, extract, ripple delete (clips or gaps), add edit,
ripple trim to playhead (`Q` / `W`), nudge, copy/paste/paste insert, duplicate,
speed/duration with reverse and optional ripple, enable/disable, link/unlink,
match frame. Unlimited undo through a History panel.

**Program monitor** — playback with J/K/L shuttle, loop and In-to-Out
playback, playback resolution, safe margins, frame export. Drag a selected clip
in the picture to move it; drag a corner to scale it.

**Project settings** — **File ▸ Project Settings** (Ctrl+Shift+P) sets the
aspect ratio (16:9, 9:16, 1:1, 4:5, 4:3, 21:9… or any custom size), resolution,
frame rate and sample rate. It reshapes the open sequence (or every sequence)
and is what new sequences start with. Clips keep their place in the frame:
positions and their keyframes move in proportion. Sequence Settings has the
same aspect ratio and resolution pickers for one sequence.

**Effects** — every clip has Motion (position, scale, rotation), Opacity with
blend modes, Volume and Balance. 67 more effects (51 video, 16 audio: colour
correction, LUTs, blurs, keying, crop, distortion, stylise, EQ, dynamics,
pitch…) come from the Effects panel, each editable in Effect Controls.

**Keyframes on everything** — every parameter of every effect has a stopwatch:
numbers and positions, colours, switches, choices (blend mode, alignment,
wave…), and a title's text, font, size and style. Numbers, positions and
colours blend along a curve; switches, choices and text step at their
keyframes. The one exception is Motion's Uniform Scale, which only links the
two scale rows.

**Animate** — seven effects that move the picture without keyframes: Shake
(seeded camera shake with position, rotation and zoom, optionally scaled to
hide its edges), Oscillate (sine, triangle, square, sawtooth or bounce; also
circular), Spin, Pulse (to a tempo), Pan & Zoom, Strobe and Flicker. Their
Amount can itself be keyframed, and Effect Controls plots the resulting motion
over the clip.

**Keyframe graphs** — an animated parameter folds out a graph (the curve
button beside its keyframe controls). Drag keyframes in time and value (Shift
locks an axis), double-click to add one, drag the handles of the selected
keyframe to draw a custom Bezier curve, and right-click for presets: linear,
hold, ease, sine, expo, overshoot, elastic and bounce. Dragging empty space
scrubs the playhead. A colour is drawn as its red, green, blue and alpha
curves over a strip of the colour itself; a switch or choice as a staircase
between its options; text, fonts and files as a lane of labelled keyframes.

**Presets** — **Clip ▸ Save Effects as Preset** writes the selected clip's
effects, keyframes and curves to a `.tlpreset` file; **File ▸ Import Preset**
(or dropping the file on the window) adds one to the library. Presets are
listed at the top of the Effects panel, with a few built-in ones: double-click
to apply to the selection or drag onto a clip. Keyframes start at the clip's
first frame and are fitted to the sequence's frame rate and frame size. The
library is `~/.config/tideline/presets`.

**Fonts** — the **+** beside a title's Font (or **File ▸ Add Fonts**) installs
`.ttf`/`.otf` files into `~/.local/share/tideline/fonts`, for Tideline only.
They appear in the font chooser, render in titles and export without a restart.

**Transitions** — cross dissolve, dips, wipes, iris, clock, push and slide for
video; constant power, constant gain and exponential crossfades for audio.
Between two clips or as a fade at a clip's free end.

**Audio** — track mixer with faders, balance, mute and solo; output meters.

**Export** — H.264, H.265, AV1, VP9, ProRes 422 HQ, DNxHR HQ, GIF, PNG sequence,
WAV, FLAC, MP3, Opus; whole sequence or In to Out; optional rescale. Rendering
runs in a separate `melt` process, so the editor stays usable.

**Start-up** — the first launch walks through a short setup: pick a layout
(Editing, Effects or Audio, each shown as a miniature), a playback quality and
your first project (name, folder and format; it is saved in its own folder).
Later launches open the project manager: recent projects (filterable), **New
Project**, **Open Project** and an unsaved empty project. It is also under
**File ▸ Project Manager** (Ctrl+Alt+O) and can be turned off in Preferences.
Opening a file from the command line or file manager skips it, as does
`TIDELINE_NO_LAUNCHER=1`.

**Playback quality** — **Edit ▸ Preferences** (Ctrl+,) has Performance,
Balanced and Quality presets, or set each part: preview resolution (Full, 1/2,
1/4, 1/8), preview scaling (fast, smooth, sharp), render threads, dropping late
frames, how often the timeline and panels follow the playhead while playing
(the picture always plays at the full frame rate), and whether Effect Controls
updates during playback. Each monitor's own resolution menu still overrides the
default. Exports always render at full quality. Preferences and the recent list
live in `~/.config/tideline/settings.json`.

The editor opens full screen; **F11** (or the button in the header) switches
back to a window, and `TIDELINE_WINDOWED=1` starts it windowed. The full
shortcut list is under **Edit ▸ Keyboard Shortcuts** (Ctrl+Alt+K).

## Not there yet

Being upfront about the gaps compared with a commercial editor:

- no multicam, captions, proxy workflow or hardware (VAAPI) encoding presets;
- no curves/scopes panel; colour tools are parameter sliders;
- titles are single text blocks edited in Effect Controls, not on the canvas;
- transitions and the track mixer have no keyframes;
- a keyframed title size, stroke or padding is rendered with one text filter
  per changed frame (MLT reads those once), so a long ramp makes a heavy graph;
- Echo, Tremolo, Limiter attack/release and Stereo Widener delay restart when
  reconfigured, so their keyframes step instead of gliding;
- custom fonts are installed on this machine, not stored in the project file;
- speed is constant per clip (no time remapping);
- a nested sequence is rendered at the size and rate of the sequence that
  contains it;
- no interchange import/export (EDL, FCP XML, OTIO) and no reading of other
  editors' project files;
- panels switch between three fixed workspaces; they cannot be re-docked freely.

## Project files

Projects are plain JSON (`.tideline`) that reference media by absolute path
plus a path relative to the project file, so a folder containing both can be
moved. An `.autosave` copy is written beside a saved project every two minutes
while there are unsaved changes.

## Tests

```sh
python -m unittest tests.test_core tests.test_engine    # 88 tests, no display needed
TIDELINE_DEBUG_SCRIPT=tests/ui_smoke.py python -m tideline   # 104 end-to-end UI checks
```

`test_engine` builds real MLT graphs from generated media and checks rendered
pixels and audio, including every transition and every export preset. The UI
script opens the real window and drives the same handlers the mouse and
keyboard use.

## How it is put together

```
tideline/
  model.py        project, sequences, tracks, clips, effects (plain data, JSON)
  edits.py        every timeline operation, independent of MLT and GTK
  keyframes.py    keyframe evaluation, easing curves, compilation to MLT animations
  presets.py      .tlpreset files: capture, validation, fitting to another clip
  settings.py     preferences, playback quality presets and recent projects
  fonts.py        the private font folder and its registration with Pango, Qt and melt
  procedural.py   seeded noise and waves behind the Animate effects
  engine/
    builder.py    model -> MLT graph (rebuilt after each edit, producers cached)
    player.py     real-time playback into GTK through MLT's SDL audio consumer
    effects.py    effect and transition registry
    media.py      probing, thumbnails, waveform peaks (FFmpeg, cached)
    export.py     export presets and the melt render job
  ui/             GTK 4 panels; the timeline is one custom-drawn canvas
  app.py          controller: commands, selection, undo, players
  commands.py     command table that menus and shortcuts are generated from
```

The data model is the single source of truth: an edit changes the model, the
MLT graph is rebuilt from it (opening no files twice), and parameter tweaks
update the live filters without a rebuild.

## Originality

Tideline is an independent work. It contains no code, artwork, icons, text or
file-format support from any commercial editor; the name, palette and icons are
its own, and nothing in it uses another vendor's product or feature names. What
it shares with other editors is the common, long-established way of working —
three-point editing, ripple/roll/slip/slide trims, J/K/L shuttle — which many
editors have used for decades.

That is a description of how it was built, not legal advice. In particular:
the name has only had a casual search for conflicts, not a trademark clearance,
and software patents cannot be ruled out by inspection. Encoding H.264, H.265
or AAC is done by FFmpeg; if you distribute builds or sell exports commercially,
codec licensing is your responsibility (the AV1, VP9, Opus and FLAC presets
avoid that question). Get proper advice before distributing it under this name.

## Licence

GPL-3.0-or-later. Tideline loads MLT (LGPL) and MLT/frei0r plugins, several of
which are GPL.
