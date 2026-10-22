"""DeepVoice's work, kept apart from Tideline's UI so it can be tested alone.

* ``AIEnv``     finds or builds the private Python environment the AI runs in.
* ``Separator`` turns a media file into ``…voice.wav`` and ``…background.wav``,
  one job at a time on a background thread, caching the results.

Nothing here touches GTK. Callbacks are handed to ``post`` (GLib.idle_add in
Tideline) so they always run on the main thread.
"""

import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "dv_separate.py")

DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
CACHE_HOME = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
ENV_DIR = os.path.join(DATA_HOME, "tideline-deepvoice", "venv")
CACHE_DIR = os.path.join(CACHE_HOME, "tideline", "deepvoice")

MODELS = [
    ("htdemucs", "Fast (htdemucs)"),
    ("htdemucs_ft", "Best quality (htdemucs_ft, ~4× slower)"),
    ("mdx_extra", "MDX Extra (older, good on speech)"),
]
DEVICES = [("auto", "Automatic (GPU if available)"), ("cpu", "CPU"), ("cuda", "NVIDIA GPU (CUDA)")]
STEMS = {"voice": "Voice", "background": "Background"}

DEFAULTS = {
    "model": "htdemucs",
    "device": "auto",
    "shifts": 1,                 # 1 = normal, 2–5 = cleaner but slower
    "threads": 0,                # 0 = let PyTorch decide
    "python": "",                # empty = DeepVoice's own environment
    "model_repo": "",            # a folder of model files, for fully offline machines
    "save_next_to_project": True,
}

PIP_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class DeepVoiceError(Exception):
    pass


def clean_env():
    """The environment for a program DeepVoice starts: the host session's,
    without anything that would point another Python at Tideline's own."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("PYTHON") or k.startswith("TIDELINE_HOST_"):
            env.pop(k)
    appdir = env.get("APPDIR")
    if appdir:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(appdir))
    env["PYTHONUNBUFFERED"] = "1"
    return env


def file_key(path, *extra):
    st = os.stat(path)
    raw = "|".join([os.path.abspath(path), str(st.st_size), str(int(st.st_mtime))] + [str(e) for e in extra])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def has_nvidia():
    return bool(shutil.which("nvidia-smi")) or os.path.exists("/proc/driver/nvidia/version")


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

    @staticmethod
    def host_python():
        """A system Python to build the environment with (never the one
        inside Tideline's AppImage)."""
        appdir = os.environ.get("APPDIR") or "\0"
        for cand in ("/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3", path=clean_env()["PATH"])):
            if cand and os.path.isfile(cand) and not cand.startswith(appdir):
                return cand
        raise DeepVoiceError("No system python3 found. Install Python 3 with your package manager.")

    def check(self, timeout=120):
        """(ok, message): can the environment import the AI libraries?"""
        if not self.exists():
            return False, "The AI isn't installed yet. Run Extensions › DeepVoice › Set Up AI."
        code = ("import torch, demucs, soundfile, numpy; "
                "print(torch.__version__, 'cuda' if torch.cuda.is_available() else 'cpu')")
        try:
            out = subprocess.run([self.python, "-c", code], capture_output=True, text=True,
                                 timeout=timeout, env=clean_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Could not start the AI's Python: {exc}"
        if out.returncode != 0:
            last = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
            return False, f"The AI environment is broken ({last}). Run Set Up AI again."
        torch_v, dev = (out.stdout.split() + ["?", "?"])[:2]
        return True, f"PyTorch {torch_v} on {dev.upper()}"

    def setup_steps(self, gpu=None):
        """Commands that build the environment and download the model."""
        gpu = has_nvidia() if gpu is None else gpu
        steps = []
        if not (self.settings.get("python") or "").strip():
            steps.append(("Creating the AI environment", [self.host_python(), "-m", "venv", "--clear", ENV_DIR]))
        py = self.python
        pip = [py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
        steps.append(("Updating pip", pip + ["pip"]))
        torch_cmd = pip + ["torch", "torchaudio"]
        if not gpu:
            torch_cmd += ["--index-url", PIP_CPU_INDEX]    # ~200 MB instead of ~2.5 GB
        steps.append(("Installing PyTorch" + (" (GPU)" if gpu else " (CPU)"), torch_cmd))
        steps.append(("Installing Demucs", pip + ["demucs", "soundfile", "numpy", "huggingface_hub"]))
        steps.append(("Downloading the AI model", self.runner_cmd(download_only=True)))
        return steps

    def runner_cmd(self, download_only=False, **files):
        s = self.settings
        cmd = [self.python, "-u", RUNNER, "--model", s.get("model") or DEFAULTS["model"]]
        if (s.get("model_repo") or "").strip():
            cmd += ["--repo", s["model_repo"].strip()]
        if download_only:
            return cmd + ["--download-only"]
        cmd += ["--device", s.get("device") or "auto", "--shifts", str(max(1, int(s.get("shifts") or 1)))]
        if int(s.get("threads") or 0) > 0:
            cmd += ["--threads", str(int(s["threads"]))]
        for k in ("input", "vocals", "background"):
            cmd += ["--" + k, files[k]]
        return cmd


# -- running a child process ----------------------------------------------------
class Child:
    """A cancellable subprocess whose output is read line by line, splitting
    on carriage returns too (pip and tqdm draw progress with them)."""

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def run(self, cmd, on_line, cwd=None):
        if self.cancelled:
            raise DeepVoiceError("Cancelled")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=clean_env(), cwd=cwd, start_new_session=True)
        except OSError as exc:
            raise DeepVoiceError(f"could not start {os.path.basename(cmd[0])}: {exc}") from exc
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
        self.proc = None
        if self.cancelled:
            raise DeepVoiceError("Cancelled")
        if code != 0:
            raise DeepVoiceError(explain_failure(tail, code))

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
        return (f"missing Python module {mod[-1] if mod else ''}; run Extensions › DeepVoice › Set Up AI"
                ).replace("  ", " ")
    if "ensurepip is not" in text or "python3-venv" in text:
        return "Python's venv module is missing. Install it (Debian/Mint: sudo apt install python3-venv)."
    if "CUDA out of memory" in text or "OutOfMemoryError" in text:
        return "the GPU ran out of memory. Set the device to CPU in DeepVoice Settings."
    if "Torch not compiled with CUDA" in text or "no CUDA GPUs" in text.lower():
        return "this PyTorch has no GPU support. Set the device to CPU or Automatic."
    if "No matching distribution" in text or "Could not find a version" in text:
        return "pip couldn't download the packages. Check your internet connection and try Set Up AI again."
    if "is neither a single pre-trained model" in text:
        return "that model isn't in the model folder (check DeepVoice Settings › Model folder)."
    if "urlopen error" in text or "Temporary failure in name resolution" in text or "HTTPError" in text:
        return "the model isn't downloaded yet and there is no internet connection. Run Set Up AI while online."
    useful = [l for l in lines if not l.startswith(("STAGE", "PROGRESS", "DEVICE", "Traceback", "File "))]
    return (useful[-1] if useful else f"exited with code {code}")[:300]


# -- separation jobs --------------------------------------------------------------
class Task:
    def __init__(self, path, out_dir, stem, payload):
        self.path = path
        self.out_dir = out_dir
        self.stem = stem              # what the caller wants back: "voice" or "background"
        self.payload = payload        # anything; handed back to on_done


class Separator:
    """One worker thread, one job at a time, results cached by file content."""

    def __init__(self, settings, post, on_status, on_done, on_error):
        self.settings = settings
        self.post = post
        self.on_status = on_status    # (text)
        self.on_done = on_done        # (task, {"voice": path, "background": path})
        self.on_error = on_error      # (task, message)
        self.queue = queue.Queue()
        self.child = None
        self.current = None
        self._lock = threading.Lock()
        self._thread = None

    # paths -------------------------------------------------------------
    def outputs(self, path, out_dir):
        model = self.settings.get("model") or DEFAULTS["model"]
        key = file_key(path, model, int(self.settings.get("shifts") or 1))
        base = os.path.splitext(os.path.basename(path))[0]
        return {stem: os.path.join(out_dir, f"{base}.{stem}.{model}.{key}.wav") for stem in STEMS}

    def cached(self, path, out_dir):
        out = self.outputs(path, out_dir)
        return out if all(os.path.isfile(p) for p in out.values()) else None

    # queue ---------------------------------------------------------------
    @property
    def busy(self):
        return self.current is not None or not self.queue.empty()

    def pending(self):
        return self.queue.qsize() + (1 if self.current is not None else 0)

    def submit(self, task):
        with self._lock:
            self.queue.put(task)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True, name="deepvoice")
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
                result = self._separate(task)
            except DeepVoiceError as exc:
                self.post(self.on_error, task, str(exc))
            except Exception as exc:                       # never kill the worker
                self.post(self.on_error, task, f"{type(exc).__name__}: {exc}")
            else:
                self.post(self.on_done, task, result)
            finally:
                self.current = None
                self.child = None

    def _status(self, text):
        self.post(self.on_status, text)

    def _separate(self, task):
        name = os.path.basename(task.path)
        hit = self.cached(task.path, task.out_dir)
        if hit:
            return hit
        os.makedirs(task.out_dir, exist_ok=True)
        out = self.outputs(task.path, task.out_dir)
        env = AIEnv(self.settings)
        if not env.exists():
            raise DeepVoiceError("The AI isn't installed yet. Run Extensions › DeepVoice › Set Up AI.")
        self.child = Child()
        work = tempfile.mkdtemp(prefix="deepvoice-")
        try:
            wav = os.path.join(work, "input.wav")
            self._status(f"DeepVoice: reading audio from {name}…")
            self.child.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-i", task.path, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                            "-c:a", "pcm_f32le", wav], lambda line: None)
            state = {"device": "", "stage": "Starting the AI"}
            left = self.pending() - 1
            more = f" (+{left} queued)" if left > 0 else ""

            def line(text):
                if text.startswith("STAGE "):
                    state["stage"] = text[6:]
                    self._status(f"DeepVoice: {state['stage']}…{more}")
                elif text.startswith("DEVICE "):
                    state["device"] = text[7:].upper()
                elif text.startswith("PROGRESS "):
                    try:
                        pct = float(text.split()[1]) * 100
                    except (IndexError, ValueError):
                        return
                    dev = f" on {state['device']}" if state["device"] else ""
                    self._status(f"DeepVoice: isolating voice in {name}{dev} — {pct:.0f}%{more}")
                elif "%|" in text and "Downloading" not in state["stage"]:
                    m = re.search(r"(\d+)%\|", text)                     # first-run model download
                    if m:
                        self._status(f"DeepVoice: downloading model — {m.group(1)}%")

            self.child.run(env.runner_cmd(input=wav, vocals=out["voice"], background=out["background"]),
                           line, cwd=work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            for p in out.values():
                try:
                    os.remove(p + ".part")
                except OSError:
                    pass
        if not all(os.path.isfile(p) for p in out.values()):
            raise DeepVoiceError("the AI finished but wrote no output")
        return out


# -- setting up the environment ------------------------------------------------------
class Setup:
    def __init__(self, settings, post, on_status, on_done):
        self.settings = settings
        self.post = post
        self.on_status = on_status
        self.on_done = on_done          # (ok, message)
        self.child = Child()
        self.running = False

    def start(self):
        if self.running:
            return False
        self.running = True
        threading.Thread(target=self._work, daemon=True, name="deepvoice-setup").start()
        return True

    def cancel(self):
        self.child.cancel()

    def _work(self):
        env = AIEnv(self.settings)
        try:
            steps = env.setup_steps()
            for i, (label, cmd) in enumerate(steps, 1):
                prefix = f"DeepVoice setup {i}/{len(steps)}: {label}"
                self.post(self.on_status, prefix + "…")

                def line(text, prefix=prefix):
                    m = re.search(r"(\d+)%\|", text)
                    if m:
                        self.post(self.on_status, f"{prefix} — {m.group(1)}%")
                    elif text.startswith(("Collecting ", "Downloading ")):
                        self.post(self.on_status, f"{prefix} — {text[:90]}")

                self.child.run(cmd, line)
            ok, msg = env.check()
            self.post(self.on_done, ok, ("DeepVoice is ready: " + msg) if ok else msg)
        except DeepVoiceError as exc:
            self.post(self.on_done, False, f"DeepVoice setup failed: {exc}")
        except Exception as exc:
            self.post(self.on_done, False, f"DeepVoice setup failed: {type(exc).__name__}: {exc}")
        finally:
            self.running = False
            self.child = Child()


if __name__ == "__main__":
    # Headless use for testing: python deepvoice_core.py FILE [OUT_DIR]
    import json
    import time
    settings = dict(DEFAULTS)
    if len(sys.argv) > 3:
        settings["python"] = sys.argv[3]
    finished = threading.Event()
    sep = Separator(settings, lambda fn, *a: fn(*a), print,
                    lambda t, r: (print(json.dumps(r, indent=1)), finished.set()),
                    lambda t, m: (print("ERROR:", m), finished.set()))
    sep.submit(Task(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else CACHE_DIR, "voice", None))
    t0 = time.time()
    finished.wait()
    print(f"{time.time() - t0:.1f}s")
