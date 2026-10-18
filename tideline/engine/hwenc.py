"""Hardware video encoders: which of them work on this computer.

FFmpeg lists encoders it was built with, not ones the graphics card can
run, so each is tried: a few frames are encoded to nowhere, on every VA-API
render node for the VA-API encoders. A card that lacks an encoder can crash
melt outright, which is why exports only offer encoders that passed. The
results are kept for the session; probing takes a second or two and runs on
a worker thread.
"""

import glob
import os
import shutil
import subprocess
import threading

APIS = {"vaapi": "VA-API", "nvenc": "NVENC", "qsv": "Quick Sync"}
VENDORS = {0x1002: "AMD", 0x8086: "Intel", 0x10de: "NVIDIA"}
PCI_IDS = ["/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids"]
# Some cards refuse frames smaller than this (HEVC on recent AMD needs more than 320×240).
PROBE_SIZE = "1280x720"

_lock = threading.Lock()
_results = None           # encoder -> [devices it works on] ([""] when it needs none; [] if it does not work)
_running = None           # the probing thread
_waiting = []


def render_nodes():
    return sorted(glob.glob("/dev/dri/renderD*"))


def _pci_name(vendor, device):
    """The marketing name of a PCI device from pci.ids, or None."""
    for path in PCI_IDS:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                in_vendor = False
                for line in fh:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):
                        in_vendor = line[:4].lower() == f"{vendor:04x}"
                    elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == f"{device:04x}":
                        name = line[5:].strip()
                        # "Navi 44 [Radeon RX 9060 XT]" -> "Radeon RX 9060 XT"
                        if "[" in name and name.endswith("]"):
                            name = name[name.rindex("[") + 1:-1]
                        return name
        except OSError:
            continue
    return None


def device_name(node):
    """A readable name for a render node: "AMD Radeon RX 9060 XT"."""
    base = os.path.join("/sys/class/drm", os.path.basename(node), "device")
    try:
        with open(os.path.join(base, "vendor")) as fh:
            vendor = int(fh.read().strip(), 16)
        with open(os.path.join(base, "device")) as fh:
            device = int(fh.read().strip(), 16)
    except (OSError, ValueError):
        return os.path.basename(node)
    brand = VENDORS.get(vendor, f"GPU {vendor:04x}")
    name = _pci_name(vendor, device)
    if not name:
        return f"{brand} ({os.path.basename(node)})"
    return name if name.startswith(brand) else f"{brand} {name}"


def compiled_encoders():
    """Names of the video encoders this FFmpeg was built with."""
    if not shutil.which("ffmpeg"):
        return set()
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def probe_command(encoder, api, device=""):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if api == "vaapi":
        cmd += ["-vaapi_device", device]
    cmd += ["-f", "lavfi", "-i", f"testsrc2=size={PROBE_SIZE}:rate=30:duration=0.1"]
    cmd += ["-vf", "format=nv12,hwupload"] if api == "vaapi" else ["-pix_fmt", "nv12"]
    return cmd + ["-c:v", encoder, "-f", "null", "-"]


def _works(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe(wanted):
    """{encoder: [devices]} for ``wanted`` [(encoder, api)]; runs FFmpeg."""
    have = compiled_encoders()
    nodes = render_nodes()
    out = {}
    for encoder, api in wanted:
        out[encoder] = []
        if encoder not in have:
            continue
        if api == "vaapi":
            out[encoder] = [n for n in nodes if _works(probe_command(encoder, api, n))]
        elif _works(probe_command(encoder, api)):
            out[encoder] = [""]
    return out


def results():
    """The probe results so far, or None while they are not known."""
    return _results


def detect(wanted, callback=None):
    """Probe ``wanted`` once per session on a worker thread; ``callback(results)``
    runs on that thread (or at once when they are already known)."""
    global _running
    with _lock:
        if _results is not None:
            done = _results
        else:
            done = None
            if callback is not None:
                _waiting.append(callback)
            if _running is None:
                _running = threading.Thread(target=_run, args=(list(wanted),), daemon=True)
                _running.start()
    if done is not None and callback is not None:
        callback(done)


def _run(wanted):
    global _results, _running
    found = probe(wanted)
    with _lock:
        _results = found
        _running = None
        callbacks = list(_waiting)
        _waiting.clear()
    for cb in callbacks:
        cb(found)


def forget():
    """Drop cached results (tests, or after a driver change)."""
    global _results
    with _lock:
        _results = None
