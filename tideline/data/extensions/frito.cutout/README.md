# Cutout

A Tideline extension that removes the background of a **picture or video clip**. You click what
you want to keep; **SAM 2** (Meta's Segment Anything 2), running entirely on your own computer,
outlines it and everything around it becomes transparent, so the tracks below show through.
No account, no upload, works offline once the models are downloaded.

## Install

Cutout comes with Tideline from 0.0.6. To use a newer copy, put it in
`~/.config/tideline/extensions/` (or **Extensions › Manage Extensions › Install from Zip…**) and
restart Tideline; it replaces the built-in one.

1. **Extensions › Cutout › Set Up AI (download models)…** — one time. It builds a private Python
   environment in `~/.local/share/tideline-cutout/venv`, installs PyTorch + SAM 2 and downloads two
   models. Progress shows in the status bar. What it installs depends on **Cutout Settings › Install
   for**, picked from your hardware the first time:
   - **CPU only** — about 1 GB to download.
   - **AMD GPU (ROCm)** — about 6 GB to download, 16 GB on disk; video is 5–10× faster.
   - **NVIDIA GPU (CUDA)** — about 4 GB to download.

   Plus 1.2 GB of models. Temporary files go in `~/.local/share/tideline-cutout/tmp`, not `/tmp`
   (often a RAM disk too small for the GPU download).

   Needs a system `python3` with `venv` (Arch has it; Debian/Mint: `sudo apt install python3-venv`).

## Use

Select a clip on a video track, then **Extensions › Cutout › Remove Background…** (Ctrl+Alt+B).
A window opens on the frame under the playhead:

| Do this | To |
| --- | --- |
| **Click** | keep the thing under the pointer |
| **Right-click** (or Alt+click) | remove the thing under the pointer |
| **Drag** a box | keep the main thing inside it — the most reliable way to get a whole person |
| **Scroll** (or Smaller / Bigger) | after a single click: take a smaller or bigger part (face → head → whole person) |
| **New Object** | keep another, separate thing as well |
| **Ctrl+Z** / Clear | undo the last click / start over |
| Highlight · Cut-out · Original | how the preview looks |

Whatever you click is kept: if the AI's first answer leaves out a part you clicked, that part is
added. A single click has several possible answers (a face, the head, the whole person); Cutout
picks the largest one the AI is fairly sure of, and scrolling steps through the others. Add clicks
or drag a box to grow it further. For video, the tracker starts from exactly what the window showed.

Press **Remove Background**. Pictures switch over in a second or two. Videos are tracked in the
background with a progress bar and time left in the status bar — keep editing; the clip switches
over when it's done, in one undo step, keeping its in/out points, speed, effects and links. The new
file appears in the Project panel as "*name* — Cutout".

**Video tips**
- Pick a frame where the subject is clearly visible. The AI follows it forwards and backwards
  through the clip.
- If it loses track somewhere, run Remove Background again on that clip (your clicks are
  remembered), move to the frame where it went wrong and click again there.
- Only the part of the video the clip uses (plus a second either side) is processed, when the clip
  plays at normal speed. Sped-up, reversed or time-remapped clips process the whole file.

**Restore Original** puts the original media back on the selected clips.

## Edge settings (in the window)

- **Refine edges** — lets the outline follow the picture's own edges.
- **Remove specks** — removes stray dots and fills pin-holes the AI leaves on busy textures.
- **Fix edge colour** — removes the old background's colour fringe from soft edges.
- **Grow / Feather** — pixels to grow (or shrink, below 0) and soften the outline.

## Settings (Extensions › Cutout › Cutout Settings…)

- **Pictures** — Large is the most accurate and takes 1–2 s a picture on a modern CPU.
- **Video model / Video detail** — Automatic picks base+ at full detail on a GPU and small at
  768 px on a CPU, which kept the most of a fast-moving game character for the time in tests.
- **Run on** — Automatic uses the GPU when a GPU build is installed.
- **Keep AI loaded** — minutes the AI stays in memory after the window closes (1–3 GB), so it
  opens instantly next time.
- **Files** — on: results go in a `Cutout/` folder next to the saved project, so they travel with
  it. Off (or an unsaved project): `~/.cache/tideline/cutout/`.

## Speed (measured on a Ryzen 5 9600X + Radeon RX 9060 XT)

| | 4 s of 1080×1920 video | a picture |
| --- | --- | --- |
| GPU (ROCm), Automatic | about 30 s | under 1 s once loaded |
| CPU, Automatic | about 1 min 45 s | 1–2 s (4K: about 4 s) |

The first use after starting Tideline loads the AI (a few seconds); it stays loaded while you work.

## Output

Pictures become PNGs with transparency. Videos become lossless **FFV1** `.mkv` files with an alpha
channel, the same size, frame rate and colours as the original (decoded with the same colour
matrix Tideline uses). They are large — roughly 0.5–2 MB a frame at 1080p — because they are
lossless; export the finished edit as usual.

Proxies don't keep transparency: if you make a proxy of a Cutout video, the preview shows it on
black (the export is still right).

## How it works

`main.py` adds the commands and swaps clips' media. `cutout_ui.py` is the click window.
`cutout_core.py` runs the AI in a separate process (`cutout_ai.py`) in its own Python environment,
so PyTorch never loads inside Tideline: a warm *server* answers the window (the image encoder runs
once per frame, then each click takes a few hundredths of a second), and video *jobs* run one at a
time on a background thread.

For video, `cutout_ai.py` feeds SAM 2's tracker frames straight from FFmpeg instead of loading the
whole video into memory (SAM's own loader needs ~12 MB a frame), forgets tracking memory it no
longer uses, and writes each frame as soon as it is tracked, so memory stays flat however long the
clip is. Masks are refined against the full-size frame with a guided filter.

## Tests

```sh
# the AI runner alone
~/.local/share/tideline-cutout/venv/bin/python cutout_ai.py image --input pic.jpg --prompts p.json --output out.png
```

## Troubleshooting

- *"the AI isn't installed yet"* — run **Set Up AI**.
- *"this GPU isn't supported"* — set **Run on** to CPU.
- *Out of memory* — pick a faster video setting, or **Run on: CPU**.
- Full logs: start Tideline from a terminal.

SAM 2 is © Meta Platforms, Apache License 2.0.
