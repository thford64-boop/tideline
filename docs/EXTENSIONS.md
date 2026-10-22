# Writing Tideline extensions

An extension is a folder that adds things to Tideline: effects, transitions,
export formats, effect presets, menu commands and event hooks. Tideline ships
with no extensions; this guide is everything you need to write your own.

- [What an extension can add](#what-an-extension-can-add)
- [Where extensions live](#where-extensions-live)
- [Your first extension in two minutes](#your-first-extension-in-two-minutes)
- [The manifest: `extension.json`](#the-manifest-extensionjson)
- [Effects](#effects)
- [Transitions](#transitions)
- [Export formats](#export-formats)
- [Presets](#presets)
- [Python extensions](#python-extensions)
- [Events](#events)
- [Examples](#examples)
- [Sharing an extension](#sharing-an-extension)
- [Troubleshooting](#troubleshooting)

## What an extension can add

| Kind | In the manifest (JSON only) | In Python |
|------|:---:|:---:|
| Video and audio effects built from MLT filters | ✓ | ✓ |
| Effects whose parameters you compute yourself | | ✓ |
| Wipe transitions drawn by a greyscale image, dips, frei0r transitions | ✓ | ✓ |
| Export formats (any codec FFmpeg and melt can write) | ✓ | ✓ |
| Effect preset folders (`.tlpreset`) | ✓ | ✓ |
| Commands in the **Extensions** menu, with shortcuts | | ✓ |
| Event hooks (project saved, media imported, proxy ready…) | | ✓ |

Everything an extension adds behaves like the built-in version: effects get
Effect Controls rows, keyframes, graphs and presets; transitions are dragged
from the Effects panel; export formats appear in the Export dialog.

> **Trust:** a Python extension runs with the same rights as Tideline itself.
> It can read and write your files like any program you run. Only install
> extensions whose source you trust. JSON-only extensions run no code.

## Where extensions live

```
~/.config/tideline/extensions/
    vignette-pack/
        extension.json
        wipes/diagonal.png
        presets/Dreamy.tlpreset
    marker-tools/
        extension.json
        main.py
```

- Each extension is one folder holding an `extension.json` file. The folder
  name doesn't matter; the manifest's `id` does.
- **Extensions ▸ Manage Extensions** lists what is installed, what each one
  added, and why one failed to load. Its switches turn extensions on or off,
  and it can install an extension from a `.zip` or a folder or remove one.
  Changes take effect the next time Tideline starts.
- **Extensions ▸ Open Extensions Folder** opens the folder in your file manager.
- `TIDELINE_EXTENSIONS_PATH=/path/one:/path/two` loads extensions from extra
  folders too, which is handy while you develop one in a project folder.
- `TIDELINE_NO_EXTENSIONS=1` starts Tideline with no extensions at all.
- Tideline ships two extensions of its own, DeepVoice (`frito.deepvoice`) and
  Cutout (`frito.cutout`), in `tideline/data/extensions/`. They are complete
  examples of Python extensions that run work in the background and swap a
  clip's media. A folder in the user extensions folder with the same `id`
  is loaded instead of the built-in one; turning a built-in extension off in
  Manage Extensions works like any other.
- An extension's own saved settings (see `tl.settings`) are kept in
  `~/.config/tideline/extension-data/<id>/settings.json`.

## Your first extension in two minutes

Make a folder `~/.config/tideline/extensions/my-first/` and save this in it as
`extension.json`:

**`my-first/extension.json`**

```json
{
  "id": "me.my-first",
  "name": "My First Extension",
  "version": "1.0.0",
  "effects": [
    {
      "key": "sepia_tone",
      "label": "Sepia Tone",
      "category": "Color",
      "services": ["sepia"]
    }
  ]
}
```

Restart Tideline. **Sepia Tone** now appears under **Effects ▸ Video Effects ▸
Color**; drag it onto a clip. That's an extension.

## The manifest: `extension.json`

| Field | Required | Meaning |
|-------|:---:|---------|
| `id` | ✓ | Unique id: 2–64 lower-case letters, digits, `.`, `-`, `_`. A reverse-domain style id (`me.vignette-pack`) avoids clashes. Never change it after release: projects store effects as `<id>/<key>`. |
| `name` | | Name shown in the manager and as the heading of its Extensions menu section. Defaults to the id. |
| `version` | | Your version, shown in the manager. |
| `author` | | Shown in the manager. |
| `description` | | One or two sentences shown in the manager. |
| `tideline` | | Lowest Tideline version it works with, e.g. `"0.0.3"`. Older versions skip it with a clear message. |
| `api` | | Extension API version it needs (currently `1`, the default). |
| `main` | | A `.py` file in the folder whose `register(tl)` is called (see [Python extensions](#python-extensions)). |
| `effects` | | List of [effects](#effects). |
| `transitions` | | List of [transitions](#transitions). |
| `export_formats` | | List of [export formats](#export-formats). |
| `presets` | | A folder name (or a list of them) holding `.tlpreset` files. See [Presets](#presets). |

The manifest is loaded first, then `main`. If anything fails, everything the
extension added so far is taken back and the error is shown in the status bar
and in the manager, so a broken extension never leaves half its parts behind.

## Effects

An effect wraps one or more **MLT filters** (Tideline's engine). A parameter
row in Effect Controls feeds a property of the first filter.

```json
{
  "key": "soft_vignette",
  "label": "Soft Vignette",
  "category": "Stylize",
  "kind": "video",
  "services": ["frei0r.vignette"],
  "static": {},
  "params": [
    {"name": "aspect", "label": "Aspect", "type": "float", "default": 50,
     "min": 0, "max": 100, "unit": "%", "mlt": "aspect", "scale": 0.01}
  ]
}
```

| Field | Required | Meaning |
|-------|:---:|---------|
| `key` | ✓ | Lower-case letters, digits, `-`, `_` (up to 48). Stored in projects as `<extension id>/<key>`. |
| `label` | | Name in the Effects panel and Effect Controls. Defaults to the key. |
| `services` | ✓ | MLT filter names, e.g. `["sepia"]`, `["frei0r.glow"]`, `["avfilter.hue"]`. Several make a chain; parameters go to the first. |
| `kind` | | `"video"` (default) or `"audio"`. |
| `category` | | Folder in the Effects panel. Use a built-in one (`Animate`, `Color`, `Blur & Sharpen`, `Transform`, `Distort`, `Keying`, `Stylize`, `Generate`; audio: `Amplitude`, `Filter & EQ`, `Dynamics`, `Time & Pitch`, `Stereo`) or any new name, which is listed after the built-in ones. Default `Extensions`. |
| `static` | | Fixed properties set on the first filter, e.g. `{"av.t": "q"}`. |
| `params` | | Parameter rows, below. An effect with none is a plain on/off filter. |

### Parameters

| Field | Applies to | Meaning |
|-------|------------|---------|
| `name` | all | Required. The name stored in projects and presets. |
| `label` | all | Row label. Defaults to the name. |
| `type` | all | `float` (default), `int`, `bool`, `color`, `choice` or `text`. |
| `default` | all | Starting value. Colours are `"#rrggbbaa"`. |
| `mlt` | all | The MLT property it sets. Without it the row is shown but sets nothing (useful only with a Python `apply`). |
| `min`, `max` | float, int | Slider range. |
| `unit` | float, int | Shown after the number (`%`, `px`, `dB`, `Hz`…). |
| `step`, `digits` | float | Drag step and decimals shown. |
| `scale`, `offset` | float | The value sent to MLT is `value × scale + offset`, so a 0–100 % row can drive a 0–1 property (`"scale": 0.01`). |
| `choices` | choice | `[["value", "Label"], …]`, or just `["a", "b"]`. The value is what MLT receives. |
| `animate` | float, int | How keyframes reach MLT: `smooth` (default; MLT animates the property), `hold` (the value changes only at keyframes, for filters that restart when reconfigured), `bake` (one filter per changed frame, for properties MLT reads only once). |
| `keyframes` | all | `false` hides the stopwatch. Default `true`. |

Booleans are sent as `1`/`0`, colours in MLT's colour format, and choices and
text as strings. Every keyframable parameter gets the stopwatch, the keyframe
graph and the ease presets automatically.

### Finding filters and their properties

MLT lists everything it can do:

```sh
melt -query filters                 # every filter name
melt -query filter=frei0r.vignette  # its properties, types, ranges and defaults
```

- `frei0r.*` filters number their properties as well as naming them; either
  works as `mlt`.
- `avfilter.*` filters are FFmpeg filters: an FFmpeg option `x` is the MLT
  property `av.x` (FFmpeg's `hue` filter's `s` option is `"mlt": "av.s"`).
- If a filter isn't installed (a frei0r or LADSPA plugin is missing), the
  effect is hidden from the Effects panel instead of failing.

## Transitions

```json
{"key": "diagonal", "label": "Diagonal Wipe", "category": "Wipe", "wipe": "wipes/diagonal.png"}
```

| Field | Meaning |
|-------|---------|
| `key`, `label` | As for effects. Stored as `<extension id>/<key>`. |
| `wipe` | A greyscale image in your folder (PNG, PGM, JPEG…). The incoming clip appears where the image is **black first and white last**, so a left-to-right black-to-white gradient wipes from left to right. Any size; it is stretched to the frame. Softness and Reverse are set per transition in Effect Controls. |
| `reverse` | `true` runs the wipe white-to-black instead. |
| `dip` | Instead of `wipe`: dip through a colour, e.g. `"#c00000ff"`. |
| `frei0r` | Instead of `wipe`: a frei0r transition service, e.g. `"frei0r.sleid0r_wipe-barn-door-h"` (see `melt -query transitions`). |
| `kind` | `"audio"` makes a crossfade (constant gain). Default `"video"`. |
| `category` | Folder in the Effects panel (`Dissolve`, `Wipe`, `Slide`, or a new one). Default `Extensions`. |

With none of `wipe`, `dip` or `frei0r`, the transition is a cross dissolve.

## Export formats

```json
{
  "key": "mjpeg",
  "label": "Motion JPEG (MOV)",
  "ext": ".mov",
  "video": {"f": "mov", "vcodec": "mjpeg", "pix_fmt": "yuvj422p"},
  "audio": {"acodec": "pcm_s16le"},
  "quality": [2, 5, 10],
  "quality_key": "qscale",
  "note": "Large files that decode fast"
}
```

| Field | Meaning |
|-------|---------|
| `key`, `label` | As for effects; the label is the Export dialog entry. |
| `ext` | File extension, with or without the dot. |
| `video`, `audio` | Options for melt's `avformat` consumer, which are mostly FFmpeg's: `f` (container), `vcodec`, `acodec`, `pix_fmt`, `preset`, `ab` (audio bitrate), `vb`… All values are strings or numbers. |
| `quality` | Three values of `quality_key` for the dialog's High, Medium and Low quality. Leave out for a codec without one. |
| `quality_key` | The option the quality values set: `crf` (default), `qscale`, `global_quality`… |
| `audio_only` | `true` for a sound-only format (no `video` options needed). |
| `note` | Shown in the Export dialog. |

`ffmpeg -h encoder=<name>` lists an encoder's options.

## Presets

Put `.tlpreset` files (made with **Clip ▸ Save Effects as Preset**) in a
folder of your extension and name it in the manifest:

```json
"presets": "presets"
```

They are listed under **Effects ▸ Presets ▸ Built-in** and are read-only. A
preset may use the extension's own effects (stored as `<id>/<key>`).

## Python extensions

Name a Python file in `main`. Tideline imports it and calls its `register`
function with an API object, by convention called `tl`:

```python
def register(tl):
    tl.command("hello", "Say Hello", lambda app: tl.status("Hello from my extension"))
```

The file can import other `.py` files from the extension folder, and any
module installed for the Python Tideline runs on. Everything in the manifest
is loaded before `register` runs.

### `tl` reference

**Identity**

| | |
|---|---|
| `tl.id` | The extension's id. |
| `tl.path` | Its folder. |
| `tl.file("a", "b.png")` | A path inside the folder. |
| `tl.version` | Tideline's version string. |
| `tl.api_version` | `1`. |
| `tl.app` | The running application. It holds the project (`app.project`), the active sequence (`app.seq`) and the selection (`app.selected_clips()`). Its internals may change between versions, so prefer the methods below where one exists. |

**Parameters**, for `tl.effect`. The keyword arguments are the same as the
manifest fields.

```python
tl.number(name, label, default=0.0, min=None, max=None, mlt=None, unit="", step=1.0, digits=1,
          scale=1.0, offset=0.0, animate="smooth", keyframes=True)
tl.integer(name, label, default=0, min=None, max=None, mlt=None, unit="", animate="smooth", keyframes=True)
tl.toggle(name, label, default=False, mlt=None, keyframes=True)
tl.color(name, label, default="#ffffffff", mlt=None, keyframes=True)
tl.choice(name, label, choices, default=None, mlt=None, keyframes=True)
tl.text(name, label, default="", mlt=None, keyframes=True)
```

**Content**

```python
tl.effect(key, label, services, params=(), category="Extensions", kind="video", static=None, apply=None)
tl.transition(key, label, wipe=None, frei0r=None, dip=None, kind="video", category="Extensions", reverse=False)
tl.export_format(key, label, ext, video=None, audio=None, quality=None, quality_key="crf",
                 audio_only=False, note="")
tl.preset_folder(folder="presets")
```

`apply(filters, effect, ctx)` takes over setting the filters' properties. It
runs when the clip is built and on every parameter change:

- `filters` are the MLT filters in `services` order. Call `.set(name, value)` on them.
- `effect.params[name]` holds the current values, `effect.keyframes[name]`
  the keyframes (`[[frame, value, interpolation], …]`), and
  `effect.value(name, frame)` the value at a source frame.
- `tl.mlt_value(effect, param, fmt=None)` returns the constant, or an MLT
  animation string when the parameter has keyframes. `fmt(value)` converts
  each value to the text MLT gets.
- `ctx["width"]`, `ctx["height"]` and `ctx["fps"]` describe the sequence.
- Turning the effect off is already handled; you don't need to set `disable`.

**Commands and hooks**

```python
tl.command(name, label, callback, accel="")   # callback(app); accel like "<Ctrl><Alt>j"
tl.on(event, fn)                              # see Events
```

Commands are listed in the **Extensions** menu under the extension's name. A
shortcut that clashes with an existing one is ignored; the menu item still
works. Errors raised in a command or hook are shown in the status bar instead
of stopping Tideline.

**Helpers**

```python
tl.status("text", warn=False)    # status bar message
tl.edit("Undo Label", fn)        # fn(editor) changes the active sequence as one undo step;
                                 # return something truthy if it changed anything
tl.settings                      # a dict that persists between sessions...
tl.save_settings()               # ...once you save it
```

The `editor` given to `tl.edit` is Tideline's timeline editor for the active
sequence (`tideline/edits.py`). `editor.seq` is the sequence, with
`video_tracks`, `audio_tracks`, `markers`, `fps` and `end()`. Its operations
(`place`, `razor`, `trim`, `ripple_delete`, `add_transition`…) are the ones
the timeline tools use. If `fn` raises `tideline.edits.EditError`, the
sequence is restored and the message shown.

## Events

`tl.on(event, fn)` calls `fn` when something happens:

| Event | `fn` receives | When |
|-------|---------------|------|
| `project` | — | A project was opened, created or saved |
| `project_saved` | path | The project was saved to `path` |
| `media` | — | Project panel items changed |
| `media_imported` | list of new items | Files were imported (each has `name`, `path`, `kind`, `duration`, `width`, `height`…) |
| `sequence` | — | The active sequence changed (any edit, undo or redo) |
| `selection` | — | The selected clips changed |
| `playhead` | — | The playhead moved |
| `history` | — | An undo step was added or undo/redo was used |
| `effects` | — | An effect parameter changed |
| `proxy_ready` | item | A proxy finished for `item` |

`playhead` fires often during playback, so keep its handler cheap.

## Examples

### A JSON-only look pack

An effect with a parameter, a custom wipe, a dip and an export format, with no
code. Make a 256×256 black-to-white diagonal gradient PNG for the wipe in any
image editor (or with
`ffmpeg -f lavfi -i "gradients=s=256x256:c0=black:c1=white:x0=0:y0=0:x1=255:y1=255" -frames:v 1 diagonal.png`)
and save it as `wipes/diagonal.png`.

**`look-pack/extension.json`**

```json
{
  "id": "example.look-pack",
  "name": "Look Pack",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "A vignette, a glow, a diagonal wipe, a red dip and an MJPEG export.",
  "tideline": "0.0.3",
  "effects": [
    {
      "key": "soft_vignette",
      "label": "Soft Vignette",
      "category": "Stylize",
      "services": ["frei0r.vignette"],
      "params": [
        {"name": "aspect", "label": "Aspect", "default": 50, "min": 0, "max": 100, "unit": "%",
         "mlt": "aspect", "scale": 0.01},
        {"name": "clear", "label": "Clear Centre", "default": 20, "min": 0, "max": 100, "unit": "%",
         "mlt": "clearCenter", "scale": 0.01},
        {"name": "soft", "label": "Softness", "default": 60, "min": 0, "max": 100, "unit": "%",
         "mlt": "soft", "scale": 0.01}
      ]
    },
    {
      "key": "glow",
      "label": "Glow",
      "category": "Looks",
      "services": ["frei0r.glow"],
      "params": [
        {"name": "blur", "label": "Glow", "default": 50, "min": 0, "max": 100, "unit": "%",
         "mlt": "Blur", "scale": 0.01}
      ]
    },
    {
      "key": "warm_hue",
      "label": "Hue & Saturation",
      "category": "Color",
      "services": ["avfilter.hue"],
      "params": [
        {"name": "hue", "label": "Hue", "default": 0, "min": -180, "max": 180, "unit": "°", "mlt": "av.h"},
        {"name": "sat", "label": "Saturation", "default": 1, "min": 0, "max": 3, "step": 0.05,
         "digits": 2, "mlt": "av.s"}
      ]
    }
  ],
  "transitions": [
    {"key": "diagonal", "label": "Diagonal Wipe", "category": "Wipe", "wipe": "wipes/diagonal.png"},
    {"key": "dip_red", "label": "Dip to Red", "category": "Dissolve", "dip": "#c00000ff"}
  ],
  "export_formats": [
    {"key": "mjpeg", "label": "Motion JPEG (MOV)", "ext": ".mov",
     "video": {"f": "mov", "vcodec": "mjpeg", "pix_fmt": "yuvj422p"},
     "audio": {"acodec": "pcm_s16le"},
     "quality": [2, 5, 10], "quality_key": "qscale",
     "note": "Large files that decode fast"}
  ]
}
```

### An audio effect with a choice

**`audio-tools/extension.json`**

```json
{
  "id": "example.audio-tools",
  "name": "Audio Tools",
  "effects": [
    {
      "key": "phaser",
      "label": "Phaser",
      "kind": "audio",
      "category": "Time & Pitch",
      "services": ["avfilter.aphaser"],
      "params": [
        {"name": "speed", "label": "Speed", "default": 0.5, "min": 0.1, "max": 2, "step": 0.1,
         "digits": 2, "unit": "Hz", "mlt": "av.speed", "animate": "hold"},
        {"name": "shape", "label": "Shape", "type": "choice", "mlt": "av.type",
         "choices": [["triangular", "Triangle"], ["sinusoidal", "Sine"]]}
      ]
    }
  ]
}
```

### Commands, hooks and settings in Python

A command that drops a marker every few seconds, a shortcut for it, and a
hook that labels imported phone footage (portrait video).

**`marker-tools/extension.json`**

```json
{
  "id": "example.marker-tools",
  "name": "Marker Tools",
  "version": "1.0.0",
  "description": "Markers at a regular interval; labels portrait clips on import.",
  "main": "main.py"
}
```

**`marker-tools/main.py`**

```python
from tideline.model import Marker


def register(tl):
    seconds = tl.settings.setdefault("interval", 5)

    def add_markers(app):
        if app.seq is None:
            return tl.status("Open a sequence first.", warn=True)

        def run(editor):
            seq = editor.seq
            step = max(1, int(round(tl.settings["interval"] * seq.fps)))
            taken = {m.t for m in seq.markers}
            new = [Marker(t=t, name="Beat") for t in range(step, seq.end(), step) if t not in taken]
            seq.markers.extend(new)
            return len(new)

        added = tl.edit("Add Interval Markers", run)
        tl.status(f"Added {added or 0} markers every {tl.settings['interval']} s")

    def label_portrait(items):
        for item in items:
            if item.kind == "video" and item.height > item.width:
                item.label = 4          # Plum, see LABELS in tideline/model.py
        if items:
            tl.app.emit("media")

    tl.command("interval_markers", f"Add Marker Every {seconds} Seconds", add_markers, accel="<Ctrl><Alt>F5")
    tl.on("media_imported", label_portrait)
    tl.save_settings()
```

Change `interval` in `~/.config/tideline/extension-data/example.marker-tools/settings.json`
to use a different spacing.

### An effect with its own parameter logic

`apply` turns a 0–100 % **Darken** row into the brightness filter's 1.0–0.0
`level`, keyframes included.

**`darken/extension.json`**

```json
{"id": "example.darken", "name": "Darken", "main": "darken.py"}
```

**`darken/darken.py`**

```python
def register(tl):
    amount = tl.number("amount", "Darken", 50, 0, 100, unit="%")

    def apply(filters, effect, ctx):
        level = tl.mlt_value(effect, amount, lambda v: f"{1.0 - float(v) / 100.0:.4f}")
        filters[0].set("level", level)

    tl.effect("darken", "Darken", ["brightness"], [amount], category="Color", apply=apply)
```

## Sharing an extension

Zip the folder (the zip may hold the folder itself or just its contents) and
share the `.zip`. Others install it with **Extensions ▸ Manage Extensions ▸
Install from Zip…**, which unpacks it into
`~/.config/tideline/extensions/<id>/` and replaces an older copy with the same
id. Leave `__pycache__` folders out of the zip.

Projects record extension effects and transitions by `<id>/<key>`. Someone
who opens your project without the extension keeps those effects in the
project file, but they are neither shown nor rendered until the extension is
installed.

## Troubleshooting

- **It doesn't show up.** Check **Extensions ▸ Manage Extensions**: a folder
  without `extension.json` is ignored, and any other problem is listed under
  the extension's name. The first problem also appears in the status bar at
  start-up.
- **An effect is missing from the Effects panel.** One of its `services`
  isn't installed: `melt -query filters | grep <name>`.
- **A parameter does nothing.** Check its `mlt` property name with
  `melt -query filter=<service>`, and its `scale`: many properties expect 0–1.
- **Python errors.** Run Tideline from a terminal (`./run.sh`) to see the full
  traceback; the status bar shows only the last line.
- **Changes don't appear.** Extensions load at start-up; restart Tideline
  after editing one.
