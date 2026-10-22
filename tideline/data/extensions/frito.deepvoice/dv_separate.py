#!/usr/bin/env python3
"""DeepVoice separation runner.

Runs in DeepVoice's own Python environment (not inside Tideline), so the
heavy AI libraries never touch the editor. It reads a WAV, splits it into
the voice and everything else with a locally stored Demucs model, and
writes two WAV files.

Talks to the extension with plain lines on stdout:
    STAGE <text>        what it is doing
    DEVICE <name>       cpu or cuda
    PROGRESS <0..1>     fraction done
    DONE                both files written
Errors go to stderr and end with a non-zero exit code.

    python dv_separate.py --model htdemucs --input in.wav \
        --vocals voice.wav --background rest.wav
    python dv_separate.py --model htdemucs --download-only
"""

import argparse
import os
import sys


def say(*parts):
    print(*parts, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--repo", default="", help="folder holding downloaded model files (offline use)")
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    ap.add_argument("--shifts", type=int, default=1, help="more = slightly cleaner, proportionally slower")
    ap.add_argument("--block", type=float, default=60.0, help="seconds processed at a time (bounds memory)")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--input")
    ap.add_argument("--vocals")
    ap.add_argument("--background")
    ap.add_argument("--download-only", action="store_true")
    args = ap.parse_args()

    say("STAGE Loading AI libraries")
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    if args.threads > 0:
        torch.set_num_threads(args.threads)

    say("STAGE Loading model " + args.model)
    repo = None
    if args.repo:
        from pathlib import Path
        repo = Path(os.path.expanduser(args.repo))
    model = get_model(args.model, repo=repo)
    model.eval()
    if "vocals" not in model.sources:
        sys.exit(f"model {args.model} has no vocals stem (it has: {', '.join(model.sources)})")
    if args.download_only:
        say("DONE")
        return

    if not (args.input and args.vocals and args.background):
        sys.exit("--input, --vocals and --background are required")

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    say("DEVICE " + device)

    info = sf.info(args.input)
    sr = info.samplerate
    total = info.frames
    if total <= 0:
        sys.exit("the audio is empty")
    msr = model.samplerate
    chans = model.audio_channels
    voice_idx = model.sources.index("vocals")

    # Work through the file a block at a time, with context on both sides so
    # the model hears what comes before and after, and a short crossfade
    # between blocks so there is never a seam.
    block = max(10, int(args.block * sr))
    pad = int(2.0 * sr)
    fade = int(0.5 * sr)

    def separate(chunk):
        """chunk: (frames, ch) float32 at sr → (voice, rest) as (frames, ch)."""
        x = torch.from_numpy(np.ascontiguousarray(chunk.T))
        in_ch = x.shape[0]
        if in_ch == 1:
            x = x.repeat(chans, 1)
        elif in_ch > chans:
            x = x[:chans]
        if sr != msr:
            import julius
            x = julius.resample_frac(x, sr, msr)
        ref = x.mean(0)
        mean, std = ref.mean(), ref.std()
        if not torch.isfinite(std) or std < 1e-8:
            std = torch.tensor(1.0)
        x = (x - mean) / std
        with torch.no_grad():
            out = apply_model(model, x[None], device=device, shifts=args.shifts, split=True,
                              overlap=0.25, progress=False)[0]
        out = out * std + mean
        voice = out[voice_idx]
        rest = out.sum(0) - voice
        if sr != msr:
            import julius
            voice = julius.resample_frac(voice, msr, sr)
            rest = julius.resample_frac(rest, msr, sr)
        n = chunk.shape[0]
        voice, rest = voice[:, :n], rest[:, :n]
        if voice.shape[1] < n:                          # resampling rounding
            extra = n - voice.shape[1]
            voice = torch.nn.functional.pad(voice, (0, extra))
            rest = torch.nn.functional.pad(rest, (0, extra))
        if in_ch == 1:
            voice, rest = voice.mean(0, keepdim=True), rest.mean(0, keepdim=True)
        return voice.T.cpu().numpy(), rest.T.cpu().numpy()

    out_ch = 1 if info.channels == 1 else chans
    tmp_v, tmp_b = args.vocals + ".part", args.background + ".part"
    say("STAGE Separating voice")
    say("PROGRESS 0")
    with sf.SoundFile(args.input) as src, \
            sf.SoundFile(tmp_v, "w", sr, out_ch, subtype="FLOAT", format="WAV") as fv, \
            sf.SoundFile(tmp_b, "w", sr, out_ch, subtype="FLOAT", format="WAV") as fb:
        tail = None
        start = 0
        while start < total:
            end = min(total, start + block)
            keep_to = min(total, end + fade)            # also make the crossfade region
            lo, hi = max(0, start - pad), min(total, keep_to + pad)
            src.seek(lo)
            chunk = src.read(hi - lo, dtype="float32", always_2d=True)
            voice, rest = separate(chunk)
            a, b = start - lo, keep_to - lo
            voice, rest = voice[a:b], rest[a:b]
            if tail is not None:
                n = min(len(tail[0]), len(voice))
                ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
                voice[:n] = tail[0][:n] * (1 - ramp) + voice[:n] * ramp
                rest[:n] = tail[1][:n] * (1 - ramp) + rest[:n] * ramp
            body = end - start
            fv.write(voice[:body])
            fb.write(rest[:body])
            tail = (voice[body:], rest[body:])
            start = end
            say(f"PROGRESS {start / total:.4f}")
    os.replace(tmp_v, args.vocals)
    os.replace(tmp_b, args.background)
    say("DONE")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
