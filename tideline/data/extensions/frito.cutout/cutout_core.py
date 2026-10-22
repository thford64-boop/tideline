"""Cutout's work, kept apart from Tideline's UI so it can be tested alone.

* ``AIEnv``     finds or builds the private Python environment the AI runs in.
* ``Session``   a running ``cutout_ai.py serve`` that answers the click
                dialog; kept warm between dialogs, stopped when idle.
* ``VideoJobs`` turns a video and its clicks into a video with an alpha
                channel, one job at a time on a background thread.
* ``Setup``     builds the environment and downloads the models.

Nothing here touches GTK. Callbacks are handed to ``post`` (GLib.idle_add in
Tideline) so they always run on the main thread.
"""

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "cutout_ai.py")

DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
CACHE_HOME = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
HOME_DIR = os.path.join(DATA_HOME, "tideline-cutout")
ENV_DIR = os.path.join(HOME_DIR, "venv")
MODELS_DIR = os.path.join(HOME_DIR, "models")
CACHE_DIR = os.path.join(CACHE_HOME, "tideline", "cutout")
# Temporary files of the AI and of pip. Not /tmp: it is often a RAM disk too
# small for PyTorch's multi-gigabyte GPU wheels or a long video's frames.
TMP_DIR = os.path.join(HOME_DIR, "tmp")

PICTURE_MODELS = [("large", "Best (large)"), ("base_plus", "Balanced (base+)"), ("small", "Fast (small)")]
VIDEO_MODELS = [("auto", "Automatic"), ("small", "Fast (small)"), ("base_plus", "Balanced (base+)"),
                ("large", "Large")]
VIDEO_SIZES = [(0, "Automatic"), (512, "Fast (512 px)"), (768, "Balanced (768 px)"), (1024, "Full (1024 px)")]
# Measured on a 1080×1920 game clip: on a GPU base+ at full size tracked
# best (large lost an arm the others kept) at ~6 frames/s; on a CPU small at
# 768 px kept nearly everything base+ did at a third of the time.
AUTO_VIDEO = {"gpu": ("base_plus", 1024), "cpu": ("small", 768)}
DEVICES = [("auto", "Automatic (GPU if installed)"), ("cpu", "CPU"), ("cuda", "GPU")]
GPU_BUILDS = [("cpu", "CPU only (~1 GB)"), ("rocm", "AMD GPU — ROCm (~6 GB)"), ("cuda", "NVIDIA GPU — CUDA (~4 GB)")]

DEFAULTS = {
    "picture_model": "large",
    "video_model": "auto",
    "video_size": 0,
    "device": "auto",
    "gpu_build": "",             # "" = pick on first Set Up from the hardware
    "python": "",                # empty = Cutout's own environment
    "save_next_to_project": True,
    "keep_ai_loaded_minutes": 10,
    "edges": {"refine": True, "despeckle": True, "decontaminate": True, "grow": 0, "feather": 0},
}

TORCH_INDEX = {"cpu": "https://download.pytorch.org/whl/cpu",
               "rocm": "https://download.pytorch.org/whl/rocm7.2",
               "cuda": None}         # PyPI's default torch is the CUDA build
SAM2_VERSION = "1.1.0"           # cutout_ai.py hooks into its video loader


class CutoutError(Exception):
    pass


def clean_env(extra=None):
    """The environment for a program Cutout starts: the host session's,
    without anything that would point another Python at Tideline's own."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("PYTHON") or k.startswith("TIDELINE_HOST_"):
            env.pop(k)
    appdir = env.get("APPDIR")
    if appdir:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(appdir))
        for k in ("LD_LIBRARY_PATH", "GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE"):
            env.pop(k, None)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        env["TMPDIR"] = TMP_DIR
    except OSError:
        pass
    env.update(extra or {})
    return env


def file_key(path, *extra):
    st = os.stat(path)
    raw = "|".join([os.path.abspath(path), str(st.st_size), str(int(st.st_mtime))] + [str(e) for e in extra])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def gpu_vendor():
    """"nvidia", "amd" (a discrete Radeon) or "" — to suggest a build."""
    if shutil.which("nvidia-smi") or os.path.exists("/proc/driver/nvidia/version"):
        return "nvidia"
    try:
        for card in os.listdir("/sys/class/drm"):
            dev = os.path.join("/sys/class/drm", card, "device")
            if not re.fullmatch(r"card\d+", card):
                continue
            with open(os.path.join(dev, "vendor")) as f:
                vendor = f.read().strip()
            # Integrated Radeons share system memory and ROCm rarely supports them.
            vram = 0
            try:
                with open(os.path.join(dev, "mem_info_vram_total")) as f:
                    vram = int(f.read().strip())
            except (OSError, ValueError):
                pass
            if vendor == "0x1002" and vram >= 3 << 30 and os.path.exists("/dev/kfd"):
                return "amd"
    except OSError:
        pass
    return ""


def suggested_build():
    return {"nvidia": "cuda", "amd": "rocm"}.get(gpu_vendor(), "cpu")


INSTALLED = os.path.join(HOME_DIR, "installed.json")


def installed_build():
    """The PyTorch build Set Up AI last installed ("cpu", "rocm", "cuda") or ""."""
    try:
        with open(INSTALLED) as f:
            return json.load(f).get("build", "")
    except (OSError, ValueError):
        return ""


def uses_gpu(settings):
    if (settings.get("device") or "auto") == "cpu":
        return False
    if (settings.get("python") or "").strip():
        return settings.get("gpu_build") in ("rocm", "cuda")      # someone else's environment
    return installed_build() in ("rocm", "cuda")


def video_choice(settings, build=None):
    """(model, working size) for video, resolving "Automatic". ``build``
    is a PyTorch build about to be installed, else the installed one."""
    gpu = build in ("rocm", "cuda") if build else uses_gpu(settings)
    auto_model, auto_size = AUTO_VIDEO["gpu" if gpu else "cpu"]
    model = settings.get("video_model") or "auto"
    size = int(settings.get("video_size") or 0)
    return (auto_model if model == "auto" else model), (auto_size if size <= 0 else size)


def needed_models(settings, build=None):
    return list(dict.fromkeys([settings.get("picture_model") or DEFAULTS["picture_model"],
                               video_choice(settings, build)[0]]))


# -- the AI environment -------------------------------------------------------
class AIEnv:
    def __init__(self, settings):
        self.settings = settings

    @property
    def python(self):
        custom = (self.settings.get("python") or "").strip()
        if custom:
            return os.path.expanduser(custom)
        return os.path.join(ENV_DIR, "bin", "python")

    def exists(self):
        return os.path.isfile(self.python) and os.access(self.python, os.X_OK)

    def models_ready(self):
        need = needed_models(self.settings)
        return all(os.path.isfile(os.path.join(MODELS_DIR, f"sam2.1_hiera_{m}.pt")) for m in need)

    @staticmethod
    def host_python():
        """A system Python to build the environment with (never the one
        inside Tideline's AppImage)."""
        appdir = os.environ.get("APPDIR") or "\0"
        for cand in ("/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3", path=clean_env()["PATH"])):
            if cand and os.path.isfile(cand) and not cand.startswith(appdir):
                return cand
        raise CutoutError("No system python3 found. Install Python 3 with your package manager.")

    def check(self, timeout=120):
        """(ok, message): can the environment import the AI libraries?"""
        if not self.exists():
            return False, "The AI isn't installed yet. Run Extensions › Cutout › Set Up AI."
        try:
            out = subprocess.run([self.python, RUNNER, "check", "--models", MODELS_DIR],
                                 capture_output=True, text=True, timeout=timeout, env=clean_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Could not start the AI's Python: {exc}"
        if out.returncode != 0:
            last = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
            return False, f"The AI environment is broken ({last}). Run Set Up AI again."
        try:
            info = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False, "The AI environment answered strangely. Run Set Up AI again."
        if not self.models_ready():
            return False, "The models aren't all downloaded. Run Set Up AI again."
        dev = info["gpu"] or "CPU" + (" with bfloat16" if info.get("bf16") else "")
        return True, f"PyTorch {info['torch']} on {dev}; models: {', '.join(info['models'])}"

    def setup_steps(self, build=None):
        """Commands that build the environment and download the models."""
        build = build or self.settings.get("gpu_build") or suggested_build()
        steps = []
        if not (self.settings.get("python") or "").strip():
            steps.append(("Creating the AI environment", [self.host_python(), "-m", "venv", "--clear", ENV_DIR], None))
        py = self.python
        pip = [py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--resume-retries", "20",
               "--progress-bar", "raw"]
        steps.append(("Updating pip", [py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "pip"],
                      None))
        torch_cmd = pip + ["torch", "torchvision"]
        if TORCH_INDEX.get(build):
            torch_cmd += ["--index-url", TORCH_INDEX[build]]
        label = {"cpu": "CPU", "rocm": "AMD GPU", "cuda": "NVIDIA GPU"}.get(build, build)
        steps.append((f"Installing PyTorch ({label})", torch_cmd, None))
        steps.append(("Installing helpers", pip + ["numpy", "scipy", "pillow", "tqdm", "hydra-core", "iopath",
                                                   "setuptools", "wheel"], None))
        # Built against the PyTorch above; letting pip build it in isolation
        # would fetch a second, multi-gigabyte PyTorch just to compile it.
        steps.append(("Installing SAM 2", pip + ["--no-build-isolation", "--no-deps", f"sam2=={SAM2_VERSION}"],
                      {"SAM2_BUILD_CUDA": "0"}))
        for m in needed_models(self.settings, build):
            steps.append((f"Downloading the {m.replace('_', ' ')} model",
                          [py, "-u", RUNNER, "download", "--model", m, "--models", MODELS_DIR], None))
        return steps

    def runner(self, mode, model, size=None):
        s = self.settings
        cmd = [self.python, "-u", RUNNER, mode, "--model", model, "--models", MODELS_DIR,
               "--device", s.get("device") or "auto"]
        if size:
            cmd += ["--size", str(int(size))]
        return cmd


# -- running a child process ----------------------------------------------------
class Child:
    """A cancellable subprocess whose output is read line by line, splitting
    on carriage returns too (pip and tqdm draw progress with them)."""

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def run(self, cmd, on_line, env=None, cwd=None):
        if self.cancelled:
            raise CutoutError("Cancelled")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=clean_env(env), cwd=cwd, start_new_session=True)
        except OSError as exc:
            raise CutoutError(f"could not start {os.path.basename(cmd[0])}: {exc}") from exc
        tail, buf = [], b""
        fd = self.proc.stdout.fileno()
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            buf += data
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    tail = (tail + [line])[-40:]
                    on_line(line)
        if buf.strip():
            line = buf.decode("utf-8", "replace").strip()
            tail.append(line)
            on_line(line)
        code = self.proc.wait()
        self.proc.stdout.close()
        self.proc = None
        if self.cancelled:
            raise CutoutError("Cancelled")
        if code != 0:
            raise CutoutError(explain_failure(tail, code))

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass


def explain_failure(lines, code):
    text = "\n".join(lines)
    if "No module named" in text:
        mod = re.findall(r"No module named '([^']+)'", text)
        return f"missing Python module {mod[-1] if mod else ''}; run Extensions › Cutout › Set Up AI"
    if "ensurepip is not" in text or "python3-venv" in text:
        return "Python's venv module is missing. Install it (Debian/Mint: sudo apt install python3-venv)."
    if "No space left on device" in text or "Errno 28" in text:
        return f"the disk is full. Free some space (a GPU setup needs about 15 GB in {HOME_DIR} while installing)."
    if "out of memory" in text.lower() or "OutOfMemoryError" in text:
        return "ran out of memory. Pick a faster video setting or Run on: CPU in Cutout Settings."
    if "HIP error" in text or "hipErrorNoBinaryForGpu" in text or "invalid device function" in text:
        return "this GPU isn't supported by the installed PyTorch. Set Run on to CPU in Cutout Settings."
    if "No matching distribution" in text or "Could not find a version" in text:
        return "pip couldn't download the packages. Check your internet connection and try Set Up AI again."
    if "urlopen error" in text or "Temporary failure in name resolution" in text or "HTTPError" in text \
            or "Connection broken" in text or "ConnectionResetError" in text:
        return "the download failed (no internet connection?). Run Set Up AI again while online."
    if code == -signal.SIGKILL or code == 137:
        return "the AI was stopped by the system, probably out of memory. Try a faster video setting."
    useful = [ln for ln in lines if not ln.startswith(("STAGE", "PROGRESS", "DEVICE", "FRAMES", "Traceback", "File "))
              and "warn" not in ln.lower()]
    return (useful[-1] if useful else f"exited with code {code}")[:300]


# -- the interactive session ----------------------------------------------------------
class Session:
    """A ``cutout_ai.py serve`` process answering one request at a time.

    ``request(op, args, done)`` queues a request; ``done(reply)`` gets the
    reply dict (``reply["ok"]`` tells if it worked). A newer ``predict``
    replaces one still waiting, so fast clicking never builds a backlog.
    The process stays loaded between dialogs and stops after being idle for
    ``keep_ai_loaded_minutes``, or when the model or device setting changes.
    """

    def __init__(self, settings, post, on_status):
        self.settings = settings
        self.post = post
        self.on_status = on_status
        self.proc = None
        self.key = None
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.pending = []                # [(id, op, args, done)]
        self.waiting = {}                # id -> done, sent and unanswered
        self.next_id = 1
        self.wake = threading.Event()
        self.last_used = time.time()
        self.error = None
        self.tail = []
        threading.Thread(target=self._idle_watch, daemon=True, name="cutout-idle").start()

    def _wanted_key(self):
        s = self.settings
        return (AIEnv(s).python, s.get("picture_model") or DEFAULTS["picture_model"], s.get("device") or "auto")

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Start the AI process if it isn't running (returns at once)."""
        with self.lock:
            key = self._wanted_key()
            if self.alive and self.key == key:
                return
            self._stop_locked()
            env = AIEnv(self.settings)
            if not env.exists():
                raise CutoutError("the AI isn't installed yet. Choose Extensions › Cutout › Set Up AI first.")
            self.ready.clear()
            self.error = None
            self.tail = []
            self.key = key
            try:
                self.proc = subprocess.Popen(env.runner("serve", key[1]), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env(),
                                             text=True, bufsize=1, start_new_session=True)
            except OSError as exc:
                raise CutoutError(f"could not start the AI: {exc}") from exc
            proc = self.proc
            threading.Thread(target=self._read, args=(proc,), daemon=True, name="cutout-read").start()
            threading.Thread(target=self._read_err, args=(proc,), daemon=True, name="cutout-err").start()
            threading.Thread(target=self._send_loop, args=(proc,), daemon=True, name="cutout-send").start()

    def stop(self):
        with self.lock:
            self._stop_locked()

    def _stop_locked(self):
        proc, self.proc = self.proc, None
        self.wake.set()
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        failed, self.pending, waiting, self.waiting = self.pending, [], self.waiting, {}
        for _, _, _, done in failed:
            self.post(done, {"ok": False, "error": "stopped"})
        for done in waiting.values():
            self.post(done, {"ok": False, "error": "stopped"})

    def request(self, op, args, done):
        self.start()
        self.last_used = time.time()
        with self.lock:
            if op == "predict":
                for item in [p for p in self.pending if p[1] == "predict"]:
                    self.pending.remove(item)
                    self.post(item[3], {"ok": False, "error": "superseded", "superseded": True})
            rid = self.next_id
            self.next_id += 1
            self.pending.append((rid, op, args, done))
        self.wake.set()
        return rid

    def _send_loop(self, proc):
        if not self.ready.wait(timeout=600) or proc is not self.proc:
            return
        while proc is self.proc and proc.poll() is None:
            self.wake.wait(timeout=1)
            with self.lock:
                self.wake.clear()
                if self.waiting or not self.pending:
                    continue                      # one at a time
                rid, op, args, done = self.pending.pop(0)
                self.waiting[rid] = done
            try:
                proc.stdin.write(json.dumps(dict(args, op=op, id=rid)) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                break

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("ready"):
                    self.ready.set()
                    self.post(self.on_status, "")
                    continue
                with self.lock:
                    done = self.waiting.pop(msg.get("id"), None)
                self.last_used = time.time()
                if done is not None:
                    self.post(done, msg)
                self.wake.set()
            elif line.startswith("STAGE "):
                self.post(self.on_status, line[6:])
        code = proc.wait()
        proc.stdout.close()
        if proc is self.proc:                   # died on its own
            self.error = explain_failure(self.tail, code)
            with self.lock:
                failed, self.pending, waiting, self.waiting = self.pending, [], self.waiting, {}
                self.proc = None
            for _, _, _, done in failed:
                self.post(done, {"ok": False, "error": self.error})
            for done in waiting.values():
                self.post(done, {"ok": False, "error": self.error})
            self.post(self.on_status, "The AI stopped: " + self.error)

    def _read_err(self, proc):
        for line in proc.stderr:
            line = line.strip()
            if line:
                self.tail = (self.tail + [line])[-40:]
        proc.stderr.close()

    def _idle_watch(self):
        while True:
            time.sleep(20)
            minutes = float(self.settings.get("keep_ai_loaded_minutes") or 0)
            if self.alive and not self.pending and not self.waiting \
                    and time.time() - self.last_used > max(0.5, minutes) * 60:
                self.stop()


# -- video jobs -----------------------------------------------------------------------
class VideoTask:
    def __init__(self, path, output, prompts, start, end, payload):
        self.path = path
        self.output = output
        self.prompts = prompts
        self.start = start            # source frames, inclusive
        self.end = end
        self.payload = payload        # anything; handed back to on_done


class VideoJobs:
    """One worker thread, one video at a time."""

    def __init__(self, settings, post, on_status, on_done, on_error):
        self.settings = settings
        self.post = post
        self.on_status = on_status    # (text)
        self.on_done = on_done        # (task, output path)
        self.on_error = on_error      # (task, message)
        self.queue = queue.Queue()
        self.child = None
        self.current = None
        self._lock = threading.Lock()
        self._thread = None

    @property
    def busy(self):
        return self.current is not None or not self.queue.empty()

    def pending(self):
        return self.queue.qsize() + (1 if self.current is not None else 0)

    def submit(self, task):
        with self._lock:
            self.queue.put(task)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True, name="cutout-video")
                self._thread.start()

    def cancel_all(self):
        n = 0
        while True:
            try:
                self.queue.get_nowait()
                n += 1
            except queue.Empty:
                break
        if self.child is not None:
            self.child.cancel()
            n += 1
        return n

    def _work(self):
        while True:
            with self._lock:
                try:
                    task = self.queue.get_nowait()
                except queue.Empty:
                    self._thread = None
                    return
            self.current = task
            try:
                self._run(task)
            except CutoutError as exc:
                self.post(self.on_error, task, str(exc))
            except Exception as exc:                       # never kill the worker
                self.post(self.on_error, task, f"{type(exc).__name__}: {exc}")
            else:
                self.post(self.on_done, task, task.output)
            finally:
                self.current = None
                self.child = None

    def _run(self, task):
        if os.path.isfile(task.output):
            return
        env = AIEnv(self.settings)
        if not env.exists():
            raise CutoutError("the AI isn't installed yet. Run Extensions › Cutout › Set Up AI.")
        os.makedirs(os.path.dirname(task.output), exist_ok=True)
        name = os.path.basename(task.path)
        s = self.settings
        fd, prompts = tempfile.mkstemp(prefix="cutout-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(task.prompts, f)
        cmd = env.runner("video", *video_choice(s))
        cmd += ["--picture-model", s.get("picture_model") or DEFAULTS["picture_model"],
                "--input", task.path, "--prompts", prompts, "--output", task.output,
                "--start", str(task.start), "--end", str(task.end)]
        left = self.pending() - 1
        more = f" (+{left} queued)" if left > 0 else ""
        state = {"stage": "Starting the AI", "device": "", "frames": max(1, task.end - task.start + 1)}
        self.post(self.on_status, f"Cutout: starting the AI for {name}…{more}")

        def line(text):
            if text.startswith("STAGE "):
                state["stage"] = text[6:]
                if not text.startswith("STAGE Done"):
                    self.post(self.on_status, f"Cutout: {state['stage'].lower()} — {name}…{more}")
            elif text.startswith("DEVICE "):
                state["device"] = text[7:].split(" (")[0]
            elif text.startswith("FRAMES "):
                try:
                    state["frames"] = int(text.split()[1])
                except (IndexError, ValueError):
                    pass
            elif text.startswith("PROGRESS "):
                parts = text.split()
                try:
                    frac = float(parts[1])
                    rate = float(parts[2]) if len(parts) > 2 else 0.0
                except (IndexError, ValueError):
                    return
                eta = ""
                if rate > 0 and frac < 1:
                    secs = int(state["frames"] * (1 - frac) / rate)
                    eta = f", about {secs // 60} min {secs % 60:02d} s left" if secs >= 60 else f", {secs} s left"
                self.post(self.on_status, f"Cutout: removing the background of {name} — "
                                          f"{frac * 100:.0f}% ({rate:.1f} frames/s{eta}){more}")

        self.child = Child()
        try:
            self.child.run(cmd, line)
        finally:
            try:
                os.remove(prompts)
            except OSError:
                pass
        if not os.path.isfile(task.output):
            raise CutoutError("the AI finished but wrote no video")


# -- setting up the environment ------------------------------------------------------
class Setup:
    def __init__(self, settings, post, on_status, on_done):
        self.settings = settings
        self.post = post
        self.on_status = on_status
        self.on_done = on_done          # (ok, message)
        self.child = Child()
        self.running = False

    def start(self, build=None):
        if self.running:
            return False
        self.running = True
        threading.Thread(target=self._work, args=(build,), daemon=True, name="cutout-setup").start()
        return True

    def cancel(self):
        self.child.cancel()

    def _work(self, build):
        env = AIEnv(self.settings)
        build = build or self.settings.get("gpu_build") or suggested_build()
        try:
            steps = env.setup_steps(build)
            for i, (label, cmd, extra) in enumerate(steps, 1):
                prefix = f"Cutout setup {i}/{len(steps)}: {label}"
                self.post(self.on_status, prefix + "…")

                def line(text, prefix=prefix):
                    m = re.search(r"(\d+)%\|", text)
                    raw = re.match(r"Progress (\d+) of (\d+)", text)      # pip --progress-bar raw
                    if raw:
                        done, total = int(raw.group(1)), int(raw.group(2))
                        if total > 20 << 20:                               # only the big downloads
                            self.post(self.on_status, f"{prefix} — {done * 100 // max(1, total)}% of "
                                                      f"{total / (1 << 30):.1f} GB")
                    elif m:
                        self.post(self.on_status, f"{prefix} — {m.group(1)}%")
                    elif text.startswith("PROGRESS "):
                        try:
                            self.post(self.on_status, f"{prefix} — {float(text.split()[1]) * 100:.0f}%")
                        except (IndexError, ValueError):
                            pass
                    elif text.startswith(("Collecting ", "Downloading ")):
                        self.post(self.on_status, f"{prefix} — {text[:90]}")

                self.child.run(cmd, line, env=extra)
                if label.startswith("Installing PyTorch"):
                    with open(INSTALLED, "w") as f:
                        json.dump({"build": build or self.settings.get("gpu_build") or suggested_build(),
                                   "when": time.strftime("%Y-%m-%d %H:%M")}, f)
            ok, msg = env.check()
            self.post(self.on_done, ok, ("Cutout is ready: " + msg) if ok else msg)
        except CutoutError as exc:
            self.post(self.on_done, False, f"Cutout setup failed: {exc}")
        except Exception as exc:
            self.post(self.on_done, False, f"Cutout setup failed: {type(exc).__name__}: {exc}")
        finally:
            self.running = False
            self.child = Child()
            shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    # Headless use for testing: python cutout_core.py VIDEO PROMPTS.json OUT.mkv [START END]
    settings = dict(DEFAULTS)
    finished = threading.Event()
    jobs = VideoJobs(settings, lambda fn, *a: fn(*a), print,
                     lambda t, r: (print("DONE", r), finished.set()),
                     lambda t, m: (print("ERROR:", m), finished.set()))
    start, end = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (0, -1)
    jobs.submit(VideoTask(sys.argv[1], sys.argv[3], json.load(open(sys.argv[2])), start, end, None))
    t0 = time.time()
    finished.wait()
    print(f"{time.time() - t0:.1f}s")
