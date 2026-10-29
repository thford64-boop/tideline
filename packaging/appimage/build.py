#!/usr/bin/env python3
"""Build a Tideline AppImage from the libraries installed on this machine.

    python packaging/appimage/build.py            # -> dist/Tideline-<version>-x86_64.AppImage
    python packaging/appimage/build.py --appdir   # stop after making the AppDir (no packing)

It copies Python, PyGObject, pycairo, numpy, the MLT bindings, GTK 4,
libadwaita, MLT with its modules, FFmpeg, frei0r, Qt (for titles) and every
library they load into an AppDir, then packs it with appimagetool (found on
PATH, named by $APPIMAGETOOL, or downloaded once into build/appimage/).

The image runs on distributions whose C library is at least as new as the
one it was built on: build on the oldest system you want to support (an
older distribution in a container works) for the widest reach. Graphics
drivers, the display server, sound server and fonts always come from the
system the AppImage runs on.
"""

import argparse
import glob
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(os.path.dirname(HERE))
APP_ID = "org.tideline.Tideline"
# A tagged release of appimagetool, used when none is installed.
APPIMAGETOOL_URL = "https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage"

# Never bundled: the C library, and what must match the running system's
# drivers, display server and sound server (the usual AppImage exclude list).
# Loaders that read the host's driver lists (OpenCL, Vulkan) and the PulseAudio
# and JACK client libraries are bundled: FFmpeg links them, and a system
# without them must still be able to start it.
EXCLUDE = re.compile(r"^(" + "|".join([
    r"ld-linux.*", r"libc\.so.*", r"libm\.so.*", r"libmvec\.so.*", r"libdl\.so.*", r"libpthread\.so.*",
    r"librt\.so.*", r"libresolv\.so.*", r"libutil\.so.*", r"libanl\.so.*", r"libnss_.*", r"libBrokenLocale.*",
    r"libthread_db.*", r"libgcc_s\.so.*",
    r"libGL\.so.*", r"libGLX.*", r"libGLdispatch.*", r"libOpenGL.*", r"libEGL.*", r"libGLES.*", r"libglapi.*",
    r"libvulkan_.*", r"libVkLayer.*", r"libdrm.*", r"libgbm.*", r"libcuda.*", r"libnvidia.*",
    r"libX11\.so.*", r"libX11-xcb.*", r"libxcb\.so.*", r"libxcb-dri.*", r"libxshmfence.*",
    r"libasound\.so.*", r"libpipewire.*",
]) + r")$")

# Opened with dlopen, so ldd does not list them: SDL2 on current distributions
# is sdl2-compat, which runs on SDL3.
DLOPENED = ["libSDL3.so.0"]

# MLT modules that need hardware, GPU frameworks or programs Tideline does not use.
MLT_SKIP = {"libmltmovit.so", "libmltglaxnimate.so", "libmltglaxnimate-qt6.so", "libmltopencv.so",
            "libmltdecklink.so", "libmltndi.so", "libmltjackrack.so", "libmltsdl.so", "libmltrtaudio.so"}
# frei0r plugins that pull in OpenCV (face detection), which no Tideline effect uses.
FREI0R_SKIP = {"facebl0r.so", "facedetect.so"}
STDLIB_SKIP = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "venv", "__pycache__",
               "site-packages", "dist-packages", "config-3", "pydoc_data"}
GI_NAMESPACES = [("Gtk", "4.0"), ("Gdk", "4.0"), ("Gsk", "4.0"), ("Adw", "1"), ("GdkPixbuf", "2.0"),
                 ("Pango", "1.0"), ("PangoCairo", "1.0"), ("PangoFc", "1.0"), ("Graphene", "1.0")]


def log(*args):
    print("::", *args, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def version():
    text = open(os.path.join(SRC, "tideline", "__init__.py"), encoding="utf-8").read()
    return re.search(r'VERSION = "(.+?)"', text).group(1)


def is_elf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=True)
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IWUSR)


def copy_tree(src, dst, skip=()):
    def ignore(d, names):
        return [n for n in names if n in skip or n == "__pycache__" or n.endswith(".pyc")]
    shutil.copytree(src, dst, ignore=ignore, symlinks=False, dirs_exist_ok=True)


def ldd(path):
    """{soname: resolved path or None} of what ``path`` links against."""
    out = subprocess.run(["ldd", path], capture_output=True, text=True).stdout
    deps = {}
    for line in out.splitlines():
        line = line.strip()
        if "=>" in line:
            name, _, rest = line.partition("=>")
            rest = rest.strip()
            deps[name.strip()] = None if rest.startswith("not found") else rest.split(" (")[0].strip() or None
    return deps


class Builder:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.work = os.path.join(SRC, "build", "appimage")
        self.appdir = os.path.join(self.work, "Tideline.AppDir")
        self.usr = os.path.join(self.appdir, "usr")
        self.lib = os.path.join(self.usr, "lib")
        self.py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        self.site = os.path.join(self.lib, f"python{self.py_ver}", "site-packages")
        self.roots = []          # ELF files whose libraries must be bundled
        self.system_libs = []    # libraries opened by name at run time, bundled as they are
        self.bundled = {}        # soname -> source path

    # -- pieces ----------------------------------------------------------------
    def python(self):
        log(f"Python {self.py_ver}")
        exe = os.path.realpath(sys.executable)
        copy(exe, os.path.join(self.usr, "bin", "python3"))
        self.roots.append(os.path.join(self.usr, "bin", "python3"))
        stdlib = sysconfig.get_paths()["stdlib"]
        dst = os.path.join(self.lib, f"python{self.py_ver}")
        for name in os.listdir(stdlib):
            if name in STDLIB_SKIP or name.startswith("config-"):
                continue
            src = os.path.join(stdlib, name)
            if os.path.isdir(src):
                copy_tree(src, os.path.join(dst, name), STDLIB_SKIP)
            else:
                copy(src, os.path.join(dst, name))
        for path in glob.glob(os.path.join(dst, "lib-dynload", "_tkinter*.so")):
            os.remove(path)
        self.roots += glob.glob(os.path.join(dst, "lib-dynload", "*.so"))
        # sysconfig reads the build configuration module at start-up.
        for path in glob.glob(os.path.join(stdlib, "_sysconfigdata_*.py")):
            copy(path, os.path.join(dst, os.path.basename(path)))

    def site_packages(self):
        log("PyGObject, pycairo, numpy, MLT bindings")
        import importlib.util
        for mod in ("gi", "cairo", "numpy"):
            spec = importlib.util.find_spec(mod)
            src = os.path.dirname(spec.origin)
            copy_tree(src, os.path.join(self.site, mod), {"tests", "testing"} if mod == "numpy" else ())
            self.roots += [p for p in glob.glob(os.path.join(self.site, mod, "**", "*.so"), recursive=True)]
            libs = os.path.join(os.path.dirname(src), mod + ".libs")
            if os.path.isdir(libs):
                copy_tree(libs, os.path.join(self.site, mod + ".libs"))
        for mod in ("mlt7", "_mlt7"):
            spec = importlib.util.find_spec(mod)
            copy(spec.origin, os.path.join(self.site, os.path.basename(spec.origin)))
            if spec.origin.endswith(".so"):
                self.roots.append(os.path.join(self.site, os.path.basename(spec.origin)))
        log("Tideline")
        copy_tree(os.path.join(SRC, "tideline"), os.path.join(self.site, "tideline"))

    def typelibs(self):
        """The typelibs Tideline loads, their dependencies, and the libraries
        behind them (which GObject introspection opens by name at run time,
        so ldd cannot see them)."""
        log("GObject introspection")
        code = ("import gi, json\n"
                + "".join(f"gi.require_version({ns!r}, {v!r})\n" for ns, v in GI_NAMESPACES)
                + "".join(f"from gi.repository import {ns}\n" for ns, _ in GI_NAMESPACES)
                + "Gtk.Box; Adw.Application; GdkPixbuf.Pixbuf; PangoCairo.create_layout; PangoFc.FontMap\n"
                + "Graphene.Rect; Gsk.ScalingFilter; Gdk.RGBA; Pango.FontDescription\n"
                + "repo = gi.Repository.get_default()\n"
                + "maps = sorted({l.split()[-1] for l in open('/proc/self/maps') if '.so' in l.split()[-1]})\n"
                + "print(json.dumps({'typelibs': [repo.get_typelib_path(ns) for ns in repo.get_loaded_namespaces()],"
                + " 'maps': maps}))\n")
        import json
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        found = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                          check=True, env=env).stdout)
        dst = os.path.join(self.lib, "girepository-1.0")
        for path in found["typelibs"]:
            if path and os.path.exists(path):
                copy(path, os.path.join(dst, os.path.basename(path)))
        pyroot = os.path.realpath(sysconfig.get_paths()["stdlib"])
        self.system_libs += [p for p in found["maps"]
                             if not os.path.realpath(p).startswith(pyroot) and "/site-packages/" not in p]
        log(f"  {len(os.listdir(dst))} typelibs")

    def mlt(self):
        log("MLT")
        repo = next(d for d in ("/usr/lib/mlt-7", "/usr/lib64/mlt-7", "/usr/lib/x86_64-linux-gnu/mlt-7")
                    if os.path.isdir(d))
        data = next(d for d in ("/usr/share/mlt-7",) if os.path.isdir(d))
        dst = os.path.join(self.lib, "mlt-7")
        os.makedirs(dst, exist_ok=True)
        for path in glob.glob(os.path.join(repo, "*.so")):
            if os.path.basename(path) not in MLT_SKIP:
                copy(path, os.path.join(dst, os.path.basename(path)))
                self.roots.append(os.path.join(dst, os.path.basename(path)))
        skip = {"movit", "glaxnimate", "opencv", "decklink", "ndi", "jackrack", "sdl", "rtaudio"}
        os.makedirs(os.path.join(self.usr, "share", "mlt-7"), exist_ok=True)
        for name in os.listdir(data):
            if name in skip:
                continue
            src = os.path.join(data, name)
            target = os.path.join(self.usr, "share", "mlt-7", name)
            copy_tree(src, target) if os.path.isdir(src) else copy(src, target)
        frei0r = next((d for d in ("/usr/lib/frei0r-1", "/usr/lib64/frei0r-1",
                                   "/usr/lib/x86_64-linux-gnu/frei0r-1") if os.path.isdir(d)), None)
        if frei0r:
            log("frei0r")
            copy_tree(frei0r, os.path.join(self.lib, "frei0r-1"), FREI0R_SKIP)
            self.roots += glob.glob(os.path.join(self.lib, "frei0r-1", "*.so"))

    def tools(self):
        log("melt, ffmpeg, ffprobe")
        libexec = os.path.join(self.usr, "libexec", "tideline")
        tools = os.path.join(self.usr, "tools")
        os.makedirs(tools, exist_ok=True)
        for name in ("melt", "ffmpeg", "ffprobe", "gdk-pixbuf-query-loaders"):
            src = shutil.which(name) or next((p for p in (f"/usr/lib/gdk-pixbuf-2.0/{name}",
                                                          f"/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/{name}")
                                              if os.path.exists(p)), None)
            if src is None:
                sys.exit(f"{name} was not found")
            copy(src, os.path.join(libexec, name))
            self.roots.append(os.path.join(libexec, name))
            if name == "gdk-pixbuf-query-loaders":
                continue
            wrapper = os.path.join(tools, name)
            with open(wrapper, "w") as fh:
                fh.write("#!/bin/sh\n"
                         "# The bundled tool with the AppImage's libraries (Tideline hands programs it\n"
                         "# starts the system's library path, see tideline/appimage.py).\n"
                         'U="$(dirname "$(dirname "$(readlink -f "$0")")")"\n'
                         'LD_LIBRARY_PATH="$U/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" '
                         f'exec "$U/libexec/tideline/{name}" "$@"\n')
            os.chmod(wrapper, 0o755)

    def gtk_runtime(self):
        log("GdkPixbuf loaders, GIO modules, GSettings schemas")
        loaders = next((d for d in ("/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders",
                                    "/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders",
                                    "/usr/lib64/gdk-pixbuf-2.0/2.10.0/loaders") if os.path.isdir(d)), None)
        if loaders:
            copy_tree(loaders, os.path.join(self.lib, "gdk-pixbuf-2.0", "2.10.0", "loaders"))
            self.roots += glob.glob(os.path.join(self.lib, "gdk-pixbuf-2.0", "2.10.0", "loaders", "*.so"))
        for d in ("/usr/lib/gio/modules", "/usr/lib/x86_64-linux-gnu/gio/modules", "/usr/lib64/gio/modules"):
            p = os.path.join(d, "libdconfsettings.so")
            if os.path.exists(p):
                copy(p, os.path.join(self.lib, "gio", "modules", "libdconfsettings.so"))
                self.roots.append(os.path.join(self.lib, "gio", "modules", "libdconfsettings.so"))
                break
        schemas = os.path.join(self.usr, "share", "glib-2.0", "schemas")
        os.makedirs(schemas, exist_ok=True)
        for path in glob.glob("/usr/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml"):
            copy(path, os.path.join(schemas, os.path.basename(path)))
        run(["glib-compile-schemas", schemas])

    def qt(self):
        """The Qt plugins MLT's title and blending services need off screen."""
        log("Qt plugins")
        base = next((d for d in ("/usr/lib/qt6/plugins", "/usr/lib/x86_64-linux-gnu/qt6/plugins",
                                 "/usr/lib64/qt6/plugins") if os.path.isdir(d)), None)
        if base is None:
            return
        dst = os.path.join(self.lib, "qt6", "plugins")
        for sub, names in (("platforms", ["libqoffscreen.so", "libqminimal.so"]),
                           ("imageformats", None), ("iconengines", ["libqsvgicon.so"])):
            for path in glob.glob(os.path.join(base, sub, "*.so")):
                # Qt's own image formats only, not other projects' plugins installed beside them.
                if os.path.basename(path) in (names or []) or (names is None and os.path.basename(path).startswith("libq")):
                    copy(path, os.path.join(dst, sub, os.path.basename(path)))
                    self.roots.append(os.path.join(dst, sub, os.path.basename(path)))
        for d, prefix in ((os.path.join(self.usr, "bin"), ".."), (os.path.join(self.usr, "libexec", "tideline"), "../..")):
            with open(os.path.join(d, "qt.conf"), "w") as fh:
                fh.write(f"[Paths]\nPrefix = {prefix}\nPlugins = lib/qt6/plugins\n")

    def desktop(self):
        log("Desktop entry and icons")
        copy(os.path.join(HERE, "AppRun"), os.path.join(self.appdir, "AppRun"))
        os.chmod(os.path.join(self.appdir, "AppRun"), 0o755)
        desktop = open(os.path.join(SRC, "packaging", f"{APP_ID}.desktop"), encoding="utf-8").read()
        for target in (os.path.join(self.appdir, f"{APP_ID}.desktop"),
                       os.path.join(self.usr, "share", "applications", f"{APP_ID}.desktop")):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(desktop)
        icons = os.path.join(SRC, "tideline", "data", "icons", "hicolor")
        copy_tree(icons, os.path.join(self.usr, "share", "icons", "hicolor"))
        copy(os.path.join(icons, "256x256", "apps", f"{APP_ID}.png"), os.path.join(self.appdir, f"{APP_ID}.png"))
        if os.path.lexists(os.path.join(self.appdir, ".DirIcon")):
            os.remove(os.path.join(self.appdir, ".DirIcon"))
        os.symlink(f"{APP_ID}.png", os.path.join(self.appdir, ".DirIcon"))
        copy(os.path.join(SRC, "packaging", f"{APP_ID}.xml"),
             os.path.join(self.usr, "share", "mime", "packages", f"{APP_ID}.xml"))
        for name in ("LICENSE", "README.md"):
            copy(os.path.join(SRC, name), os.path.join(self.usr, "share", "doc", "tideline", name))

    # -- libraries -------------------------------------------------------------
    def libraries(self):
        """Copy every library the bundled programs and modules link against."""
        log("Shared libraries")
        sonames = {}
        for line in subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout.splitlines():
            parts = line.strip().split(" => ")
            if len(parts) == 2 and "x86-64" in parts[0]:
                sonames.setdefault(os.path.realpath(parts[1]), parts[0].split(" ")[0])
        pending = list(self.roots)
        by_soname = {v: k for k, v in sonames.items()}
        for path in self.system_libs + [by_soname[s] for s in DLOPENED if s in by_soname]:
            soname = sonames.get(os.path.realpath(path))
            if soname is None or EXCLUDE.match(soname) or soname in self.bundled:
                continue
            self.bundled[soname] = path
            copy(path, os.path.join(self.lib, soname))
            pending.append(path)
        seen = set()
        missing = set()
        while pending:
            path = pending.pop()
            if path in seen or not is_elf(path):
                continue
            seen.add(path)
            for soname, resolved in ldd(path).items():
                if EXCLUDE.match(soname) or soname in self.bundled:
                    continue
                if resolved is None:
                    # Satisfied by a library the AppDir already has (an MLT module
                    # linking libmlt, say) or genuinely missing.
                    if not os.path.exists(os.path.join(self.lib, soname)):
                        missing.add((soname, os.path.basename(path)))
                    continue
                if resolved.startswith(self.appdir):
                    continue
                self.bundled[soname] = resolved
                dst = os.path.join(self.lib, soname)
                if not os.path.exists(dst):
                    copy(resolved, dst)
                pending.append(resolved)
        missing = {m for m in missing if not os.path.exists(os.path.join(self.lib, m[0]))}
        # A plugin whose library this machine lacks would only fail to load: leave it out.
        for soname, user in sorted(missing):
            for root in self.roots:
                if os.path.basename(root) == user and root.startswith(self.appdir) and os.path.exists(root):
                    os.remove(root)
                    log(f"  left out {user}: it needs {soname}, which is not installed here")
        log(f"  {len(self.bundled)} libraries")

    def strip_caches(self):
        for root, dirs, files in os.walk(self.appdir):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d))
                    dirs.remove(d)

    def compile(self):
        """Byte-compile Python: the image is read-only, so nothing could cache later."""
        log("Compiling Python modules")
        pylib = os.path.join(self.lib, f"python{self.py_ver}")
        run([os.path.join(self.usr, "bin", "python3"), "-I", "-m", "compileall", "-q", "-j", "0", "-d",
             f"/usr/lib/python{self.py_ver}", pylib],
            env=dict(os.environ, LD_LIBRARY_PATH=self.lib), stdout=subprocess.DEVNULL)

    def check(self):
        """Start the bundled Python with only the AppDir's libraries and
        import everything Tideline imports at start-up."""
        log("Checking the AppDir")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "GI_", "MLT_", "LD_"))}
        code = ("import os, sys\n"
                "assert sys.prefix.startswith(os.environ['APPDIR']), sys.prefix\n"
                "import gi\n"
                "gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')\n"
                "from gi.repository import Gtk, Adw, GdkPixbuf, Pango\n"
                "import numpy, cairo, mlt7\n"
                "from tideline.engine import mltlib\n"
                "mltlib.init()\n"
                "from tideline.engine import effects\n"
                "missing = [k for k, d in effects.EFFECTS.items() if not d.available()]\n"
                "print('effects unavailable:', missing or 'none')\n"
                "import tideline.app\n"
                "print('ok: Python', sys.version.split()[0])\n")
        script = os.path.join(self.work, "check.sh")
        with open(script, "w") as fh:
            fh.write(open(os.path.join(HERE, "AppRun")).read().replace(
                'exec "$U/bin/python3" -I -m tideline "$@"', 'exec "$U/bin/python3" -I -c "$CHECK_CODE"'))
        r = subprocess.run(["sh", script], env=dict(env, APPDIR=self.appdir, CHECK_CODE=code),
                           capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            print(r.stderr[-3000:])
            sys.exit("The AppDir does not start; see above.")
        r = subprocess.run([os.path.join(self.usr, "tools", "melt"), "-version"], capture_output=True, text=True,
                           env=dict(env, APPDIR=self.appdir))
        if r.returncode != 0:
            sys.exit("The bundled melt does not start:\n" + r.stderr[-2000:])

    # -- packing ---------------------------------------------------------------
    def appimagetool(self):
        tool = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
        if tool:
            return tool
        tool = os.path.join(self.work, "appimagetool-x86_64.AppImage")
        if not os.path.exists(tool):
            log(f"Downloading appimagetool from {APPIMAGETOOL_URL}")
            urllib.request.urlretrieve(APPIMAGETOOL_URL, tool + ".part")
            os.replace(tool + ".part", tool)
            os.chmod(tool, 0o755)
        return tool

    def pack(self):
        os.makedirs(self.out_dir, exist_ok=True)
        target = os.path.join(self.out_dir, f"Tideline-{version()}-x86_64.AppImage")
        log(f"Packing {target}")
        env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
        run([self.appimagetool(), "--no-appstream", "--comp", "zstd", self.appdir, target], env=env)
        return target

    def build(self, pack=True):
        if os.path.exists(self.appdir):
            shutil.rmtree(self.appdir)
        os.makedirs(self.lib)
        self.python()
        self.site_packages()
        self.typelibs()
        self.mlt()
        self.tools()
        self.gtk_runtime()
        self.qt()
        self.desktop()
        self.libraries()
        self.strip_caches()
        self.check()
        self.compile()
        return self.pack() if pack else self.appdir


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--appdir", action="store_true", help="only make the AppDir, do not pack it")
    ap.add_argument("--out", default=os.path.join(SRC, "dist"), help="where the AppImage goes (default: dist/)")
    args = ap.parse_args()
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        sys.exit("AppImages are built on x86-64 Linux")
    result = Builder(args.out).build(pack=not args.appdir)
    log(f"Done: {result}")


if __name__ == "__main__":
    main()
