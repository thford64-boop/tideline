#!/usr/bin/env python3
"""Cutout AI runner.

Runs in Cutout's own Python environment (never inside Tideline), so PyTorch
and SAM 2 never touch the editor. Three ways to run it:

    cutout_ai.py serve  --model large
        Interactive: reads one JSON request per line on stdin and answers
        with one JSON line on stdout. Used by the click dialog.

    cutout_ai.py video  --input clip.mp4 --prompts p.json --output out.mkv
        Tracks the clicked object(s) through a video with SAM 2 and writes a
        lossless video with an alpha channel. Prints STAGE / PROGRESS lines.

    cutout_ai.py image  --input pic.jpg --prompts p.json --output out.png
        The same for one picture, without the dialog (tests, scripts).

    cutout_ai.py download --model large
        Fetches a model checkpoint.

Prompts are JSON with coordinates as fractions of the picture (0..1):

    {"objects": [{"frames": {"0": {"points": [[x, y, 1], [x, y, 0]],
                                   "box": [x0, y0, x1, y1]}}}],
     "edges": {"refine": true, "despeckle": true, "decontaminate": true,
               "grow": 0, "feather": 0}}

A point's third value is 1 to keep what it is on and 0 to remove it. The
frame key is a frame number of the source (always "0" for pictures).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
MODELS_DIR = os.path.join(DATA_HOME, "tideline-cutout", "models")
MODEL_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_{}.pt"
MODELS = {"small": "s", "base_plus": "b+", "large": "l"}     # name -> config suffix

# On consumer Radeons, ROCm's fast attention kernels are behind an
# "experimental" switch; without them attention runs ~15x slower.
os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

# The protocol goes to the real stdout; anything a library prints goes to stderr.
_OUT = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
sys.stdout = sys.stderr


def say(*parts):
    print(*parts, file=_OUT, flush=True)


def fail(msg):
    print(msg, file=sys.stderr, flush=True)
    sys.exit(2)


# -- model --------------------------------------------------------------------
def checkpoint(name, models_dir):
    return os.path.join(models_dir, f"sam2.1_hiera_{name}.pt")


def download(name, models_dir):
    path = checkpoint(name, models_dir)
    if os.path.isfile(path):
        return path
    os.makedirs(models_dir, exist_ok=True)
    part = path + ".part"
    say(f"STAGE Downloading the {name.replace('_', ' ')} model")
    with urllib.request.urlopen(MODEL_URL.format(name), timeout=60) as resp, open(part, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and time.time() - last > 0.5:
                say(f"PROGRESS {done / total:.4f}")
                last = time.time()
    if total and os.path.getsize(part) != total:
        os.remove(part)
        raise OSError("the download was cut short; try again")
    os.replace(part, path)
    return path


def physical_cores():
    try:
        with open("/proc/cpuinfo") as f:
            text = f.read()
        cores = set()
        phys = core = None
        for line in text.splitlines():
            if line.startswith("physical id"):
                phys = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
            elif not line.strip() and core is not None:
                cores.add((phys, core))
                phys = core = None
        if core is not None:
            cores.add((phys, core))
        return max(1, len(cores))
    except OSError:
        return max(1, (os.cpu_count() or 2) // 2)


def cpu_has_bf16():
    try:
        with open("/proc/cpuinfo") as f:
            flags = f.read()
        return "avx512_bf16" in flags or "amx_bf16" in flags
    except OSError:
        return False


class Engine:
    """Torch, the device and the number format, chosen once."""

    def __init__(self, device="auto", precision="auto", threads=0):
        import torch
        self.torch = torch
        torch.set_grad_enabled(False)
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and not torch.cuda.is_available():
            fail("No GPU is usable by this PyTorch. Set Run on to CPU in Cutout Settings.")
        self.device = device
        if device == "cpu":
            # One thread per physical core is fastest (hyper-threads slow it down).
            torch.set_num_threads(threads if threads > 0 else physical_cores())
        if precision == "auto":
            if device == "cpu":
                precision = "bf16" if cpu_has_bf16() else "fp32"
            else:
                precision = "bf16" if torch.cuda.is_bf16_supported() else "fp16"
        self.precision = precision
        name = torch.cuda.get_device_name(0) if device == "cuda" else "CPU"
        say(f"DEVICE {name} ({precision})")

    def autocast(self):
        torch = self.torch
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16}.get(self.precision)
        return torch.autocast(self.device, dtype=dtype, enabled=dtype is not None)

    def build(self, name, models_dir, video=False, size=1024):
        if name not in MODELS:
            fail(f"unknown model {name}")
        path = checkpoint(name, models_dir)
        if not os.path.isfile(path):
            fail(f"The {name} model isn't downloaded. Run Extensions › Cutout › Set Up AI.")
        cfg = f"configs/sam2.1/sam2.1_hiera_{MODELS[name]}.yaml"
        # SAM 2 was trained at 1024 px; a smaller working size (a multiple of
        # 64) is much faster for video and the edges are refined at full size.
        size = max(256, int(size) // 64 * 64)
        extra = [f"++model.image_size={size}"] if size != 1024 else []
        if video:
            from sam2.build_sam import build_sam2_video_predictor
            return build_sam2_video_predictor(cfg, path, device=self.device, hydra_overrides_extra=extra)
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        return SAM2ImagePredictor(build_sam2(cfg, path, device=self.device, hydra_overrides_extra=extra))


# -- media ------------------------------------------------------------------------
def ffmpeg_bin(name="ffmpeg"):
    found = shutil.which(name)
    if not found:
        fail(f"{name} not found; install FFmpeg")
    return found


def probe(path):
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", "-select_streams", "v:0", path],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        fail(f"could not read {os.path.basename(path)}")
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        fail(f"{os.path.basename(path)} has no picture")
    s = streams[0]
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    rotation = 0
    for sd in s.get("side_data_list", []):
        rotation = int(sd.get("rotation", rotation) or 0)
    if abs(rotation) in (90, 270):
        w, h = h, w

    def frac(v):
        try:
            a, b = (v or "0/1").split("/")
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    fps = frac(s.get("avg_frame_rate")) or frac(s.get("r_frame_rate")) or 30.0
    duration = float(info.get("format", {}).get("duration") or s.get("duration") or 0)
    frames = int(s.get("nb_frames") or 0) or int(round(duration * fps))
    return {"w": w, "h": h, "fps": fps, "fps_text": s.get("avg_frame_rate") or f"{fps:.6f}",
            "duration": duration, "frames": max(1, frames),
            "colorspace": (s.get("color_space") or "").lower()}


def matrix(info):
    """The YUV matrix MLT (and so Tideline) uses for this file, so the cut-out
    has exactly the colours the original shows in the editor."""
    tag = info["colorspace"]
    if tag in ("smpte170m", "bt470bg", "smpte240m"):
        return "bt601"
    if tag in ("bt2020nc", "bt2020c"):
        return "bt2020"
    if tag in ("", "unknown", "unspecified"):
        return "bt601" if info["w"] * info["h"] <= 750000 else "bt709"
    return "bt709"


class FrameReader:
    """Frames of a video as RGB arrays, in order, at a constant frame rate.

    ``size`` is (w, h) to scale to, or None for the picture's own size. Every
    reader of one file uses the same decoding, so frame N is the same moment
    whichever size it is read at."""

    def __init__(self, path, info, start=0, size=None):
        import numpy as np
        self.np = np
        self.w, self.h = size or (info["w"], info["h"])
        flt = f"fps={info['fps_text']},scale={self.w}:{self.h}:flags=bicubic" \
              f":in_color_matrix={matrix(info)}:in_range=auto,format=rgb24"
        cmd = [ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error"]
        if start > 0:
            cmd += ["-ss", f"{start / info['fps']:.6f}"]
        cmd += ["-i", path, "-an", "-sn", "-vf", flt, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 22)
        self.index = start
        self.last = None

    def read(self):
        """The next frame, or the last one again once the file has ended
        (frame counts from containers are sometimes one or two too high)."""
        n = self.w * self.h * 3
        buf = self.proc.stdout.read(n)
        if buf is not None and len(buf) == n:
            self.last = self.np.frombuffer(buf, self.np.uint8).reshape(self.h, self.w, 3)
        elif self.last is None:
            fail("could not decode any frames")
        self.index += 1
        return self.last

    def close(self):
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        self.proc.kill()
        self.proc.wait()


def read_picture(path):
    """(RGB array, alpha array or None) of a picture, upright."""
    import numpy as np
    from PIL import Image, ImageOps
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        fail(f"could not open {os.path.basename(path)}: {exc}")
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16-bit greyscale: PIL would clip it to 8 bits instead of scaling it
        arr = np.asarray(img).astype(np.float32)
        top = 65535.0 if arr.max() > 255 else 255.0
        img = Image.fromarray((arr * (255.0 / top) + 0.5).clip(0, 255).astype(np.uint8), "L")
    alpha = None
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        alpha = np.asarray(img)[..., 3].astype(np.float32) / 255.0
        if alpha.min() > 0.999:
            alpha = None
    rgb = np.ascontiguousarray(np.asarray(img.convert("RGB")))
    return rgb, alpha


def video_frame(path, info, frame):
    reader = FrameReader(path, info, start=frame)
    try:
        return reader.read().copy()
    finally:
        reader.close()


# -- prompts ------------------------------------------------------------------------
def frame_prompts(obj, frame):
    return (obj.get("frames") or {}).get(str(frame))


def to_pixels(fp, w, h):
    """(points (N,2), labels (N,), box (4,) or None) in pixels."""
    import numpy as np
    pts = [p for p in (fp.get("points") or []) if len(p) >= 3]
    coords = np.array([[p[0] * w, p[1] * h] for p in pts], np.float32).reshape(-1, 2)
    labels = np.array([1 if p[2] else 0 for p in pts], np.int32)
    box = fp.get("box")
    if box:
        x0, y0, x1, y1 = box
        box = np.array([min(x0, x1) * w, min(y0, y1) * h, max(x0, x1) * w, max(y0, y1) * h], np.float32)
    return coords, labels, box


def has_prompts(fp):
    return bool(fp) and (bool(fp.get("box")) or any(p[2] for p in fp.get("points") or []))


def _single_choice(scores, areas):
    """Which of SAM's three answers to a lone click to use: the largest one
    it is reasonably sure of. Its best-scored answer is often a part (a
    face, a shirt) when the click was meant for the whole person; measured
    on game renders, photos of people and objects this picks the whole
    subject far more often."""
    import numpy as np
    best = float(np.max(scores))
    ok = [i for i in range(len(scores)) if scores[i] >= max(0.5, best - 0.45)]
    return max(ok, key=lambda i: areas[i]) if ok else int(np.argmax(scores))


def _sam(engine, pred, coords, labels, box, size=None):
    """(logits, picked, choices) for one prompt. For a lone click SAM gives
    three answers (choices = 3), ordered here from smallest to largest;
    ``size`` picks one, else ``_single_choice`` does."""
    import numpy as np
    single_click = box is None and len(labels) == 1
    with engine.autocast():
        masks, scores, _ = pred.predict(
            point_coords=coords if len(labels) else None,
            point_labels=labels if len(labels) else None,
            box=box, multimask_output=single_click, return_logits=True)
    if not single_click:
        return np.asarray(masks[0], np.float32), 0, 1
    areas = [float((m > 0).mean()) for m in masks]
    order = sorted(range(len(masks)), key=lambda i: areas[i])
    if size is None:
        pick = order.index(_single_choice(scores, areas))
    else:
        pick = min(len(order) - 1, max(0, int(size)))
    return np.asarray(masks[order[pick]], np.float32), pick, len(order)


def picture_logits(engine, pred, objects, frame, w, h, picks=None):
    """Full-size mask logits (float32, h×w) for everything kept on ``frame``:
    the highest score of all objects. None when nothing is kept. ``picks``,
    a dict, gets {object index: [picked size, choices]} for lone clicks.

    SAM reads all of an object's clicks together, and sometimes the joint
    answer leaves out a part that was clicked (it was unsure about it). What
    is clicked is what the user asked for, so a keep-click outside the
    result adds that part on its own, and a remove-click inside it takes
    that part away."""
    import numpy as np
    out = None
    for i, obj in enumerate(objects):
        logits = object_logits(engine, pred, obj, frame, w, h, picks, i)
        if logits is not None:
            out = logits if out is None else np.maximum(out, logits)
    return out


def object_logits(engine, pred, obj, frame, w, h, picks=None, index=0):
    import numpy as np
    fp = frame_prompts(obj, frame)
    if not has_prompts(fp):
        return None
    coords, labels, box = to_pixels(fp, w, h)
    logits, pick, choices = _sam(engine, pred, coords, labels, box, fp.get("size"))
    if picks is not None and choices > 1:
        picks[index] = [pick, choices]
    if len(labels) > 1 or box is not None:
        for (x, y), lab in zip(coords, labels):
            xi, yi = min(w - 1, max(0, int(x))), min(h - 1, max(0, int(y)))
            inside = logits[yi, xi] > 0
            if lab == 1 and not inside:
                part = _sam(engine, pred, np.array([[x, y]], np.float32), np.array([1], np.int32), None)[0]
                logits = np.maximum(logits, part)
            elif lab == 0 and inside:
                part = _sam(engine, pred, np.array([[x, y]], np.float32), np.array([1], np.int32), None)[0]
                logits = np.where(part > 0, np.minimum(logits, -part), logits)
    return logits


# -- edges -------------------------------------------------------------------------
class Edges:
    """Turns SAM's mask scores into a clean alpha matte.

    1. remove specks: stray islands and pin-holes SAM leaves on busy texture
    2. refine: a guided filter lets the edge follow the picture's own edges,
       inside a narrow band around SAM's outline only (so it can't leak)
    3. grow/shrink and feather, in pixels of the full picture
    4. decontaminate: edge pixels take the colour of the subject next to
       them instead of a halo of the old background
    """

    def __init__(self, opts, torch, device="cpu"):
        self.o = {"refine": True, "despeckle": True, "decontaminate": True, "grow": 0, "feather": 0}
        self.o.update(opts or {})
        self.torch = torch
        self.device = device
        import torch.nn.functional as F
        self.F = F

    # binary clean-up, at any size
    def despeckle(self, fg, keep_xy=()):
        import numpy as np
        from scipy import ndimage
        h, w = fg.shape
        lab, n = ndimage.label(fg)
        if n > 1:
            areas = ndimage.sum_labels(np.ones_like(lab), lab, index=np.arange(1, n + 1))
            keep = areas >= max(areas.max() * 0.04, h * w * 0.0004)
            for x, y in keep_xy:                     # never drop what was clicked
                xi, yi = min(w - 1, max(0, int(x * w))), min(h - 1, max(0, int(y * h)))
                if lab[yi, xi]:
                    keep[lab[yi, xi] - 1] = True
            fg = keep[lab - 1] & (lab > 0)
        holes, n = ndimage.label(~fg)
        if n > 0:
            areas = ndimage.sum_labels(np.ones_like(holes), holes, index=np.arange(1, n + 1))
            border = np.unique(np.concatenate([holes[0], holes[-1], holes[:, 0], holes[:, -1]]))
            small = areas < h * w * 0.0008
            small[border[border > 0] - 1] = False
            fg = fg | (small[holes - 1] & (holes > 0))
        return fg

    def _box(self, x, r):
        F = self.F
        k = 2 * r + 1
        x = F.avg_pool2d(F.pad(x, (r, r, 0, 0), mode="replicate"), (1, k), stride=1)
        return F.avg_pool2d(F.pad(x, (0, 0, r, r), mode="replicate"), (k, 1), stride=1)

    def _guided(self, guide, p, r, eps):
        """Fast guided filter (He & Sun): runs at a quarter size for big
        radii, then upsamples its linear coefficients."""
        F = self.F
        h, w = p.shape[-2:]
        s = 4 if r >= 8 else (2 if r >= 4 else 1)
        if s > 1:
            gs = F.interpolate(guide, scale_factor=1 / s, mode="bilinear", align_corners=False)
            ps = F.interpolate(p, scale_factor=1 / s, mode="bilinear", align_corners=False)
            rs = max(1, r // s)
        else:
            gs, ps, rs = guide, p, r
        mi, mp = self._box(gs, rs), self._box(ps, rs)
        cov = self._box(gs * ps, rs) - mi * mp
        var = self._box(gs * gs, rs) - mi * mi
        a = cov / (var + eps)
        b = mp - a * mi
        ma, mb = self._box(a, rs), self._box(b, rs)
        if s > 1:
            ma = F.interpolate(ma, size=(h, w), mode="bilinear", align_corners=False)
            mb = F.interpolate(mb, size=(h, w), mode="bilinear", align_corners=False)
        return ma * guide + mb

    def _morph(self, x, r, grow):
        """Square dilation (grow) or erosion, as two 1-D passes."""
        F = self.F
        k = 2 * r + 1
        if not grow:
            return -self._morph(-x, r, True)
        x = F.max_pool2d(x, (1, k), stride=1, padding=(0, r))
        return F.max_pool2d(x, (k, 1), stride=1, padding=(r, 0))

    def matte(self, logits, rgb, keep_xy=()):
        """logits: float32 array (any size; resized to the picture),
        rgb: uint8 h×w×3 → (alpha float32 h×w in 0..1, rgb uint8)."""
        import numpy as np
        torch, F, o = self.torch, self.F, self.o
        h, w = rgb.shape[:2]
        lg = torch.from_numpy(np.ascontiguousarray(logits, np.float32))[None, None]
        if o["despeckle"]:
            fg = self.despeckle(lg[0, 0].numpy() > 0, keep_xy)
            # push changed pixels well over the line so the soft edge follows
            lg = torch.where(torch.from_numpy(fg)[None, None], lg.clamp(min=4.0), lg.clamp(max=-4.0))
        lg = lg.to(self.device)
        if lg.shape[-2:] != (h, w):
            lg = F.interpolate(lg, size=(h, w), mode="bilinear", align_corners=False)
        alpha = torch.sigmoid(lg)
        img = torch.from_numpy(np.ascontiguousarray(rgb)).to(self.device).permute(2, 0, 1)[None].float() / 255.0
        scale = max(h, w)
        hard = (alpha > 0.5).float()
        if o["refine"] and hard.any():
            r = max(2, int(round(scale / 270)))          # 4 px at 1080p, 8 at 4K
            gray = (img * torch.tensor([0.299, 0.587, 0.114], device=img.device).view(1, 3, 1, 1)).sum(1, keepdim=True)
            soft = self._guided(gray, alpha, r, 1e-3).clamp(0, 1)
            band = self._morph(hard, r, True) - self._morph(hard, r, False)
            alpha = torch.where(band > 0, soft, hard)
        grow = int(o.get("grow") or 0)
        if grow:
            alpha = self._morph(alpha, abs(grow), grow > 0)
        feather = float(o.get("feather") or 0)
        if feather > 0:
            r = max(1, int(round(feather)))
            alpha = self._box(self._box(alpha, (r + 1) // 2), (r + 1) // 2)
        alpha = alpha.clamp(0, 1)
        out_rgb = rgb
        if o["decontaminate"] and hard.any():
            solid = (alpha > 0.98).float()
            r = max(3, int(round(scale / 120)))
            num = self._box(self._box(img * solid, r), r)
            den = self._box(self._box(solid, r), r)
            fgc = num / den.clamp(min=1e-4)
            edge = ((alpha < 0.98) & (alpha > 0.0) & (den > 1e-3)).float()
            mix = edge * (1 - alpha)
            out = img * (1 - mix) + fgc * mix
            out_rgb = (out[0].permute(1, 2, 0).clamp(0, 1) * 255 + 0.5).byte().cpu().numpy()
        return alpha[0, 0].cpu().numpy(), out_rgb


def keep_points(objects, frame):
    pts = []
    for obj in objects:
        fp = frame_prompts(obj, frame) or {}
        pts += [(p[0], p[1]) for p in fp.get("points") or [] if p[2]]
        if fp.get("box"):
            x0, y0, x1, y1 = fp["box"]
            pts.append(((x0 + x1) / 2, (y0 + y1) / 2))
    return pts


def save_rgba(path, rgb, alpha, prev_alpha=None):
    import numpy as np
    from PIL import Image
    a = alpha if prev_alpha is None else alpha * prev_alpha
    rgba = np.dstack([rgb, (a * 255 + 0.5).astype(np.uint8)])
    tmp = path + ".part.png"
    Image.fromarray(rgba, "RGBA").save(tmp, compress_level=4)
    os.replace(tmp, path)


# -- interactive server ------------------------------------------------------------
class Server:
    """Answers the click dialog. One picture is loaded at a time; SAM's
    expensive part (the image encoder) runs once per picture, every click
    after that takes a few hundredths of a second."""

    PREVIEW_MAX = 1600

    def __init__(self, args):
        self.args = args
        self.engine = Engine(args.device, args.precision, args.threads)
        say("STAGE Loading the model")
        self.pred = self.engine.build(args.model, args.models, size=args.size)
        self.key = None
        self.rgb = self.alpha_in = None
        self.preview_rgb = None
        self.tmp = tempfile.mkdtemp(prefix="cutout-")

    def run(self):
        say(json.dumps({"ready": True, "model": self.args.model}))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            rid = req.get("id")
            if req.get("op") == "quit":
                break
            try:
                res = getattr(self, "op_" + req.get("op", ""))(req)
                res.update(id=rid, ok=True)
            except SystemExit as exc:
                res = {"id": rid, "ok": False, "error": str(exc.code)}
            except Exception as exc:
                res = {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            say(json.dumps(res))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _preview_size(self):
        h, w = self.rgb.shape[:2]
        s = min(1.0, self.PREVIEW_MAX / max(w, h))
        return max(1, int(round(w * s))), max(1, int(round(h * s)))

    def op_load(self, req):
        """{path, frame?} → {w, h, preview}: the picture (or a video's frame
        number ``frame``), and a PNG of it for the dialog to show."""
        from PIL import Image
        path, frame = req["path"], req.get("frame")
        key = (path, frame, os.path.getmtime(path))
        if key != self.key:
            t = time.time()
            if frame is None:
                self.rgb, self.alpha_in = read_picture(path)
            else:
                self.rgb, self.alpha_in = video_frame(path, probe(path), int(frame)), None
            with self.engine.autocast():
                self.pred.set_image(self.rgb)
            self.key = key
            pw, ph = self._preview_size()
            self.preview_rgb = self.rgb if (pw, ph) == self.rgb.shape[1::-1] else \
                __import__("numpy").asarray(Image.fromarray(self.rgb).resize((pw, ph), Image.BILINEAR))
            self.preview = os.path.join(self.tmp, f"frame-{time.time_ns()}.png")
            Image.fromarray(self.preview_rgb).save(self.preview, compress_level=1)
            say(f"STAGE Analysed in {time.time() - t:.1f}s")
        h, w = self.rgb.shape[:2]
        return {"w": w, "h": h, "preview": self.preview}

    def op_predict(self, req):
        """{objects, frame, edges} → {mask, area}: a PNG the dialog's size
        whose alpha is 255 where the picture will be REMOVED."""
        import numpy as np
        from PIL import Image
        if self.rgb is None:
            raise RuntimeError("no picture loaded")
        frame = int(req.get("frame") or 0)
        h, w = self.rgb.shape[:2]
        objects = req.get("objects") or []
        picks = {}
        logits = picture_logits(self.engine, self.pred, objects, frame, w, h, picks)
        pw, ph = self.preview_rgb.shape[1], self.preview_rgb.shape[0]
        if logits is None:
            alpha = np.zeros((ph, pw), np.float32)
        else:
            small = self.engine.torch.nn.functional.interpolate(
                self.engine.torch.from_numpy(logits)[None, None], size=(ph, pw), mode="bilinear",
                align_corners=False)[0, 0].numpy()
            edges = dict(req.get("edges") or {}, decontaminate=False)
            # grow and feather are in full-size pixels; scale them to the preview
            s = pw / w
            edges["grow"] = int(round((edges.get("grow") or 0) * s))
            edges["feather"] = (edges.get("feather") or 0) * s
            alpha, _ = Edges(edges, self.engine.torch).matte(small, self.preview_rgb, keep_points(objects, frame))
        removed = np.zeros((ph, pw, 4), np.uint8)
        removed[..., 3] = ((1 - alpha) * 255 + 0.5).astype(np.uint8)
        out = os.path.join(self.tmp, f"mask-{time.time_ns()}.png")
        Image.fromarray(removed, "RGBA").save(out, compress_level=1)
        return {"mask": out, "area": float(alpha.mean()), "picks": {str(k): v for k, v in picks.items()}}

    def op_save(self, req):
        """{objects, frame, edges, output} → {output}: the full-size cut-out."""
        if self.rgb is None:
            raise RuntimeError("no picture loaded")
        frame = int(req.get("frame") or 0)
        h, w = self.rgb.shape[:2]
        objects = req.get("objects") or []
        logits = picture_logits(self.engine, self.pred, objects, frame, w, h)
        if logits is None:
            raise RuntimeError("nothing is selected to keep")
        alpha, rgb = Edges(req.get("edges"), self.engine.torch, self.engine.device).matte(logits, self.rgb, keep_points(objects, frame))
        save_rgba(req["output"], rgb, alpha, self.alpha_in)
        return {"output": req["output"], "area": float(alpha.mean())}


# -- one picture without the dialog ------------------------------------------------------
def run_image(args):
    prompts = json.load(open(args.prompts))
    engine = Engine(args.device, args.precision, args.threads)
    say("STAGE Loading the model")
    pred = engine.build(args.model, args.models, size=args.size)
    rgb, alpha_in = read_picture(args.input)
    say("STAGE Finding the subject")
    with engine.autocast():
        pred.set_image(rgb)
    h, w = rgb.shape[:2]
    objects = prompts.get("objects") or []
    logits = picture_logits(engine, pred, objects, 0, w, h)
    if logits is None:
        fail("nothing is selected to keep")
    alpha, out = Edges(prompts.get("edges"), engine.torch, engine.device).matte(logits, rgb, keep_points(objects, 0))
    save_rgba(args.output, out, alpha, alpha_in)
    say("DONE")


# -- video ---------------------------------------------------------------------------
class LazyFrames:
    """Video frames as SAM 2 wants them (square, normalised tensors), read
    on demand instead of all at once: SAM's own loader keeps every frame in
    memory, about 12 MB each, which a phone video fills RAM with in seconds.

    SAM reads forward from the first prompted frame, then backward from it.
    Forward frames are streamed; the few backward ones are decoded once
    into a small file first."""

    def __init__(self, path, info, size, first, start, end, workdir, torch):
        import numpy as np
        self.np, self.torch = np, torch
        self.path, self.info, self.size = path, info, size
        self.first, self.start, self.end = first, start, end
        self.workdir = workdir
        self.reader = None
        self.back = None
        self.cache = {}
        self.mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

    def __len__(self):
        return self.end + 1             # SAM indexes by absolute frame number

    def _tensor(self, rgb):
        t = self.torch.from_numpy(rgb.copy()).permute(2, 0, 1).float() / 255.0
        return (t - self.mean) / self.std

    def _backward(self, i):
        if self.back is None:
            n = self.first - self.start
            self.back = self.np.lib.format.open_memmap(
                os.path.join(self.workdir, "back.npy"), mode="w+", dtype=self.np.uint8,
                shape=(max(1, n), self.size, self.size, 3))
            r = FrameReader(self.path, self.info, self.start, (self.size, self.size))
            try:
                for k in range(n):
                    self.back[k] = r.read()
            finally:
                r.close()
        return self.back[i - self.start]

    def __getitem__(self, i):
        if i in self.cache:
            return self.cache[i]
        if i < self.first:
            rgb = self._backward(i)
        else:
            if self.reader is None or self.reader.index > i:
                if self.reader is not None:
                    self.reader.close()
                self.reader = FrameReader(self.path, self.info, i, (self.size, self.size))
            while self.reader.index < i:
                self.reader.read()
            rgb = self.reader.read()
        t = self._tensor(rgb)
        self.cache = {k: v for k, v in self.cache.items() if abs(k - i) <= 2}
        self.cache[i] = t
        return t

    def close(self):
        if self.reader is not None:
            self.reader.close()


def run_video(args):
    import numpy as np
    prompts = json.load(open(args.prompts))
    objects = [o for o in prompts.get("objects") or []
               if any(has_prompts(fp) for fp in (o.get("frames") or {}).values())]
    if not objects:
        fail("nothing is selected to keep")
    info = probe(args.input)
    w, h, fps = info["w"], info["h"], info["fps"]
    start = max(0, args.start)
    end = min(info["frames"] - 1, args.end if args.end >= 0 else info["frames"] - 1)
    cond = sorted({int(k) for o in objects for k, fp in (o.get("frames") or {}).items() if has_prompts(fp)
                   and start <= int(k) <= end})
    if not cond:
        fail("the clicked frames are outside the part of the video being processed")
    first = cond[0]
    total = end - start + 1
    say(f"FRAMES {total}")

    engine = Engine(args.device, args.precision, args.threads)
    torch = engine.torch
    say("STAGE Loading the model")
    pred = engine.build(args.model, args.models, video=True, size=args.size)
    work = tempfile.mkdtemp(prefix="cutout-video-", dir=args.tmp or None)
    frames = LazyFrames(args.input, info, pred.image_size, first, start, end, work, torch)

    # Masks are kept at a modest size between SAM's two passes; the final
    # edge comes from refining against the full-size frame.
    ms = min(1.0, 640 / max(w, h))
    mw, mh = max(8, int(round(w * ms))), max(8, int(round(h * ms)))
    import sam2.sam2_video_predictor as svp
    real_loader = svp.load_video_frames
    svp.load_video_frames = lambda **kw: (frames, mh, mw)
    try:
        state = pred.init_state(video_path="lazy", offload_video_to_cpu=True,
                                offload_state_to_cpu=engine.device == "cpu")
    finally:
        svp.load_video_frames = real_loader

    # Start the tracker from exactly the masks the window showed: work them
    # out on each clicked frame with the window's own model and logic, and
    # give them to SAM's tracker as mask prompts.
    say("STAGE Reading your clicks")
    still = engine.build(args.picture_model or args.model, args.models)
    with engine.autocast():
        for f in cond:
            rgb = video_frame(args.input, info, f)
            still.set_image(rgb)
            for oid, obj in enumerate(objects, 1):
                logits = object_logits(engine, still, obj, f, w, h)
                if logits is None:
                    continue
                pred.add_new_mask(state, frame_idx=f, obj_id=oid,
                                  mask=torch.from_numpy(logits > 0))
    del still
    import gc
    gc.collect()
    if engine.device == "cuda":
        torch.cuda.empty_cache()

    masks = np.lib.format.open_memmap(os.path.join(work, "masks.npy"), mode="w+", dtype=np.float16,
                                      shape=(first - start, mh, mw)) if first > start else None
    done = [0]
    t0 = time.time()

    def trim(frame_idx, reverse):
        """Forget tracking memory SAM no longer looks at (it uses the
        last 7 frames and the prompted ones), so RAM stays flat."""
        for out in state["output_dict_per_obj"].values():
            old = out["non_cond_frame_outputs"]
            for f in [f for f in old if (f > frame_idx + 16 if reverse else f < frame_idx - 16)]:
                del old[f]

    def progress():
        done[0] += 1
        if done[0] % 2 == 0 or done[0] == total:
            rate = done[0] / max(1e-6, time.time() - t0)
            say(f"PROGRESS {done[0] / total:.4f} {rate:.2f}")

    def union(video_res_masks):
        return video_res_masks.float().amax(0)[0].cpu().numpy()

    edges = Edges(prompts.get("edges"), torch, engine.device)
    tmp_out = args.output + ".part.mkv"
    enc = subprocess.Popen(
        [ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", info["fps_text"], "-i", "-",
         "-c:v", "ffv1", "-level", "3", "-slices", "16", "-slicecrc", "0", "-g", "1",
         "-pix_fmt", args.pix_fmt, "-threads", str(max(2, physical_cores())), tmp_out],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    full = FrameReader(args.input, info, start)

    spent = {"decode": 0.0, "edges": 0.0, "encode": 0.0}

    def write(frame_idx, logits):
        t = time.time()
        rgb = full.read()
        spent["decode"] += time.time() - t
        t = time.time()
        alpha, out = edges.matte(logits, rgb, keep_points(objects, frame_idx))
        rgba = np.dstack([out, (alpha * 255 + 0.5).astype(np.uint8)])
        spent["edges"] += time.time() - t
        t = time.time()
        try:
            enc.stdin.write(rgba.tobytes())
            spent["encode"] += time.time() - t
        except BrokenPipeError:
            fail("the video encoder stopped: " + enc.stderr.read().decode(errors="replace")[-300:])

    try:
        say("STAGE Tracking")
        with engine.autocast():
            if first > start:
                for f, _, vm in pred.propagate_in_video(state, start_frame_idx=first, reverse=True):
                    if f < first:
                        masks[f - start] = union(vm)
                        progress()
                    trim(f, True)
            # Write the part before the first prompt, then stream the rest:
            # each frame is written as soon as SAM has tracked it.
            for f in range(start, first):
                write(f, masks[f - start].astype(np.float32))
            for f, _, vm in pred.propagate_in_video(state, start_frame_idx=first, reverse=False):
                if f > end:
                    break
                write(f, union(vm))
                progress()
                trim(f, False)
        enc.stdin.close()
        if enc.wait() != 0:
            fail("the video encoder failed: " + enc.stderr.read().decode(errors="replace")[-300:])
        os.replace(tmp_out, args.output)
    finally:
        full.close()
        frames.close()
        if enc.poll() is None:
            enc.kill()
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
    took = time.time() - t0
    other = " ".join(f"{k} {v:.0f}s" for k, v in spent.items())
    say(f"STAGE Done in {took:.0f}s (tracking {took - sum(spent.values()):.0f}s, {other})")
    say("DONE")


# -- entry ---------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("serve", "video", "image", "download", "check"))
    ap.add_argument("--model", default="large", choices=sorted(MODELS))
    ap.add_argument("--models", default=MODELS_DIR, help="folder holding the model checkpoints")
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    ap.add_argument("--precision", default="auto", choices=("auto", "fp32", "bf16", "fp16"))
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--size", type=int, default=1024, help="SAM's working size in pixels (multiple of 64)")
    ap.add_argument("--picture-model", default="", choices=[""] + sorted(MODELS),
                    help="video: the model the clicks were previewed with")
    ap.add_argument("--input")
    ap.add_argument("--prompts")
    ap.add_argument("--output")
    ap.add_argument("--start", type=int, default=0, help="first source frame to process")
    ap.add_argument("--end", type=int, default=-1, help="last source frame to process")
    ap.add_argument("--pix-fmt", default="bgra", help="FFV1 pixel format of the result")
    ap.add_argument("--tmp", default="", help="folder for temporary files")
    args = ap.parse_args()
    args.models = os.path.expanduser(args.models)

    if args.mode == "download":
        download(args.model, args.models)
        say("DONE")
    elif args.mode == "check":
        import numpy
        import scipy
        import torch
        import sam2  # noqa: F401
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""
        say(json.dumps({"torch": torch.__version__, "gpu": gpu, "numpy": numpy.__version__,
                        "scipy": scipy.__version__, "bf16": cpu_has_bf16(),
                        "models": sorted(m for m in MODELS if os.path.isfile(checkpoint(m, args.models)))}))
    elif args.mode == "serve":
        Server(args).run()
    elif not (args.input and args.prompts and args.output):
        fail("--input, --prompts and --output are required")
    elif args.mode == "image":
        run_image(args)
    else:
        run_video(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
