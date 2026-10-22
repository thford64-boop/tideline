# DeepVoice

A Tideline extension that isolates a person's voice from any clip — or removes it and keeps
the background — using the **Demucs** AI model running entirely on your own computer.
No account, no upload, works offline once the model is downloaded.

## Install

DeepVoice comes with Tideline from 0.0.6. To use a newer copy, put it in
`~/.config/tideline/extensions/` (or **Extensions › Manage Extensions › Install from Zip…**) and
restart Tideline; it replaces the built-in one.

1. **Extensions › DeepVoice › Set Up AI (download model)…** — one time. It builds a private Python
   environment in `~/.local/share/tideline-deepvoice/venv`, installs PyTorch + Demucs, and downloads
   the model. Progress shows in the status bar.
   - No NVIDIA GPU: installs the CPU build of PyTorch (~200 MB).
   - NVIDIA GPU detected: installs the CUDA build (~2.5 GB) and separates many times faster.
   - Needs a system `python3` with `venv` (Debian/Ubuntu/Mint: `sudo apt install python3-venv`;
     Arch has it already).

## Use

Select one or more **audio clips**, or **video clips with linked audio**, then:

| Command | Shortcut | What it does |
| --- | --- | --- |
| Isolate Voice | Ctrl+Alt+V | Switches the clip's audio to just the voice |
| Remove Voice (Keep Background) | Ctrl+Alt+Shift+V | Switches to music/ambience without the voice |
| Restore Original Audio | — | Puts the original audio back |
| Cancel DeepVoice Jobs | — | Stops what's running and clears the queue |
| DeepVoice Settings… | — | Model, CPU/GPU, quality, where files are saved |

Separation runs in the background; keep editing. When it's done the clip switches over in one
undo step — its in/out points, speed, time remapping, effects and links all stay as they were.
The new file also appears in the Project panel as "*name* — Voice" / "*name* — Background".

Both stems are made in one pass and cached, so switching between Voice, Background and the
original afterwards is instant. Re-running on an already-processed file costs nothing.

## Settings

- **AI model** — *Fast (htdemucs)* is the default. *Best quality (htdemucs_ft)* is cleaner and about
  4× slower. Changing the model downloads it on first use (or run Set Up AI again while online).
- **Run on** — Automatic uses the GPU when PyTorch can see one.
- **Quality passes** — 1 is normal; 2–5 averages several passes for a slightly cleaner result.
- **Files** — on: results go in a `DeepVoice/` folder next to the saved project, so they travel with
  it. Off (or an unsaved project): `~/.cache/tideline/deepvoice/`.
- **Python** — use your own interpreter that already has `torch`, `demucs` and `soundfile`.
- **Model folder** — for machines with no internet: a folder holding Demucs model files
  (`.th`, plus the `.yaml` for bag models such as `htdemucs_ft`).

## Speed (rough)

Fast model on a modern 8-core CPU: about real time to 2× real time. On an NVIDIA GPU: a few
seconds per minute of audio. Long files are processed a minute at a time, so memory stays low.

## How it works

`main.py` adds the commands and swaps clips' media. `deepvoice_core.py` queues jobs on a
background thread, extracts the audio with `ffmpeg`, and starts `dv_separate.py` in the separate
AI environment, so PyTorch never loads inside Tideline. The runner separates in 60 s blocks with
2 s of context on each side and crossfades the joins, then writes float WAVs.

## Troubleshooting

- *"the AI isn't installed yet"* — run **Set Up AI**.
- *"Python's venv module is missing"* — `sudo apt install python3-venv`, then Set Up AI again.
- *GPU ran out of memory* — set **Run on** to CPU.
- Full logs: start Tideline from a terminal.
