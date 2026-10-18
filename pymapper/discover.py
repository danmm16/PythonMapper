"""Locate Python interpreters, environments, packages and Jupyter kernels.

The scan only reads the filesystem. No interpreter is ever launched, so it is
fast and safe even when installs are broken or built for another architecture.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from . import __author__, __version__

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"
HOME = Path.home()

VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:(?:a|b|rc)\d+)?")
EXE_NAME_RE = re.compile(r"^python(\d+(?:\.\d+)?)?t?(?:\.exe)?$", re.IGNORECASE)

# Folder names never worth descending into while hunting for environments.
SKIP_DIRS = {
    "node_modules", "__pycache__", "site-packages", "dist-packages", "appdata",
    "library", "applications", "pictures", "music", "videos", "movies",
    "$recycle.bin", "system volume information",
}

KIND_ORDER = {"conda-root": 0, "base": 1, "missing": 2, "conda": 3, "venv": 4, "user": 5}


def _envvar(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


LOCALAPPDATA = _envvar("LOCALAPPDATA") or HOME / "AppData" / "Local"
APPDATA = _envvar("APPDATA") or HOME / "AppData" / "Roaming"
PROGRAMDATA = _envvar("PROGRAMDATA") or Path("C:/ProgramData")
XDG_DATA = _envvar("XDG_DATA_HOME") or HOME / ".local" / "share"
XDG_CACHE = _envvar("XDG_CACHE_HOME") or HOME / ".cache"


# --------------------------------------------------------------------------
# Small filesystem helpers
# --------------------------------------------------------------------------

def norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def real(path) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:
        return norm(path)


def make_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def subdirs(path) -> list:
    try:
        with os.scandir(path) as it:
            return sorted(Path(e.path) for e in it if e.is_dir())
    except OSError:
        return []


# os.path.isfile/isdir never raise (unlike Path methods on access-denied paths).
def isfile(path) -> bool:
    return os.path.isfile(str(path))


def isdir(path) -> bool:
    return os.path.isdir(str(path))


def is_env(path: Path) -> bool:
    return isfile(path / "pyvenv.cfg") or isdir(path / "conda-meta")


def is_conda_root(path: Path) -> bool:
    return isdir(path / "conda-meta") and (isdir(path / "condabin") or isdir(path / "envs"))


def has_stdlib(prefix: Path) -> bool:
    """True for a real interpreter install, false for launchers and shims (py.exe, PyManager, pyenv)."""
    if isfile(prefix / "Lib" / "os.py"):
        return True
    return any(prefix.glob("lib/python[0-9]*/os.py")) or isfile(prefix / "lib" / "python" / "os.py")


def prefix_of(exe: Path) -> Path:
    """The installation prefix an interpreter executable belongs to."""
    parent = exe.parent
    return parent.parent if parent.name.lower() in ("bin", "scripts") else parent


def python_exe(prefix: Path) -> Path | None:
    if WINDOWS:
        names = ("python.exe", "Scripts/python.exe", "bin/python.exe", "bin/python3")
    else:
        names = ("bin/python3", "bin/python")
    for name in names:
        candidate = prefix / name
        if isfile(candidate):
            return candidate
    return None


def major_minor(version: str | None) -> str | None:
    if not version:
        return None
    m = re.match(r"(\d+)\.(\d+)", version)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _version_key(text: str):
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def read_cfg(prefix: Path) -> dict:
    """Parse a venv's pyvenv.cfg (simple `key = value` lines)."""
    out = {}
    try:
        text = (prefix / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def detect_version(prefix: Path, hint: str | None = None) -> str | None:
    """Best-effort Python version of a prefix, read from files on disk."""
    cfg = read_cfg(prefix)
    for key in ("version", "version_info"):
        m = VERSION_RE.search(cfg.get(key, ""))
        if m:
            return m.group(0)
    meta = prefix / "conda-meta"
    if isdir(meta):
        for f in meta.glob("python-[0-9]*.json"):
            m = VERSION_RE.match(f.name[len("python-"):])
            if m:
                return m.group(0)
    headers = [prefix / "include" / "patchlevel.h"] + sorted(prefix.glob("include/python*/patchlevel.h"))
    for header in headers:
        try:
            text = header.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'#define\s+PY_VERSION\s+"([^"]+)"', text)
        if m and (not hint or m.group(1) == hint or m.group(1).startswith(hint + ".")):
            return m.group(1).rstrip("+")
    if hint:
        return hint
    found = []
    for dll in prefix.glob("python3*.dll"):
        m = re.match(r"python(\d)(\d+)\.dll$", dll.name, re.IGNORECASE)
        if m:
            found.append(f"{m.group(1)}.{m.group(2)}")
    for lib in prefix.glob("lib/python[0-9]*"):
        m = re.match(r"python(\d+\.\d+)t?$", lib.name)
        if m:
            found.append(m.group(1))
    return max(found, key=_version_key) if found else None


def site_dirs(prefix: Path, version: str | None = None) -> list:
    mm = major_minor(version) or "[0-9]*"
    candidates = [prefix / "Lib" / "site-packages"] if WINDOWS else []
    for pattern in (f"lib/python{mm}/site-packages", f"lib/python{mm}t/site-packages",
                    f"lib64/python{mm}/site-packages", f"lib/python{mm}/dist-packages",
                    "lib/python3/dist-packages"):
        candidates.extend(sorted(prefix.glob(pattern)))
    seen, out = set(), []
    for c in candidates:
        if isdir(c) and real(c) not in seen:
            seen.add(real(c))
            out.append(str(c))
    return out


def origin(path) -> str:
    """Guess who installed an interpreter from where it lives."""
    s = str(path).replace("\\", "/").lower()
    for needle, tag in (
        ("/.pyenv/", "pyenv"), ("/uv/python/", "uv"), ("/.asdf/", "asdf"), ("/.rye/", "rye"),
        ("/cellar/", "homebrew"), ("/homebrew/", "homebrew"), ("/linuxbrew/", "homebrew"),
        ("commandlinetools", "apple"), ("xcode.app", "apple"),
        ("/library/frameworks/python.framework", "python.org"), ("/programs/python/", "python.org"),
        ("/windowsapps/", "microsoft store"), ("/nix/store/", "nix"), ("/local/python/pythoncore-", "pymanager"),
    ):
        if needle in s:
            return tag
    if s.startswith(("/usr/bin/", "/bin/", "/usr/lib/")):
        return "system"
    return ""


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _headers(path: str) -> dict:
    """Read Name/Version/Summary from a METADATA or PKG-INFO header block."""
    wanted, out = ("Name", "Version", "Summary"), {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    break
                key, sep, value = line.partition(":")
                if sep and key in wanted and key not in out:
                    out[key] = value.strip()
    except OSError:
        pass
    return out


def read_site_packages(site_dir: str) -> list:
    pkgs = []
    try:
        with os.scandir(site_dir) as it:
            entries = list(it)
    except OSError:
        return pkgs
    for e in entries:
        name = e.name
        if name.endswith(".dist-info"):
            meta = os.path.join(e.path, "METADATA")
        elif name.endswith(".egg-info"):
            meta = os.path.join(e.path, "PKG-INFO") if e.is_dir() else e.path
        else:
            continue
        info = _headers(meta)
        stem_name, _, stem_version = name.rsplit(".", 1)[0].partition("-")
        installer = ""
        if name.endswith(".dist-info"):
            try:
                with open(os.path.join(e.path, "INSTALLER"), encoding="utf-8", errors="replace") as f:
                    installer = f.readline().strip()
            except OSError:
                pass
        pkgs.append({
            "name": info.get("Name") or stem_name,
            "version": info.get("Version") or stem_version.split("-")[0],
            "installer": installer,
            "summary": info.get("Summary", ""),
        })
    return pkgs


def _channel(url: str) -> str:
    parts = [p for p in url.rstrip("/").split("/") if p]
    while parts and re.match(r"^(noarch|(win|linux|osx)-\w+)$", parts[-1]):
        parts.pop()
    return parts[-1] if parts else ""


def read_conda_meta(prefix: Path) -> list:
    pkgs = []
    for f in (prefix / "conda-meta").glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name"):
            pkgs.append({
                "name": data["name"],
                "version": str(data.get("version", "")),
                "installer": "conda",
                "summary": "",
                "channel": _channel(str(data.get("channel", ""))),
            })
    return pkgs


def collect_packages(prefix: Path, sites: list, conda: bool) -> list:
    merged = {}
    if conda:
        for p in read_conda_meta(prefix):
            merged[canonical(p["name"])] = p
    for site in sites:
        for p in read_site_packages(site):
            key = canonical(p["name"])
            known = merged.get(key)
            if known and known["installer"] == "conda":
                known["summary"] = known["summary"] or p["summary"]
            elif not known:
                merged[key] = p
    return sorted(merged.values(), key=lambda p: p["name"].lower())


def features(packages: list) -> list:
    names = {canonical(p["name"]) for p in packages}
    out = [f for f in ("pip", "ipykernel") if f in names]
    if names & {"jupyterlab", "notebook", "jupyter-server", "jupyter"}:
        out.append("jupyter")
    return out


def dir_size(path) -> int:
    """Apparent size of a folder tree, without following links or junctions."""
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & 0x400:  # Windows junction
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += st.st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------
# Shell commands shown in the UI
# --------------------------------------------------------------------------

def commands(rec: dict) -> list:
    kind, path, py = rec["kind"], rec["path"], rec.get("python")
    out = []

    def add(label, cmd):
        out.append({"label": label, "cmd": cmd})

    run = (lambda exe: f'& "{exe}"') if WINDOWS else (lambda exe: f'"{exe}"')
    if kind == "venv":
        if WINDOWS:
            add("Activate (PowerShell)", f'& "{path}\\Scripts\\Activate.ps1"')
            add("Activate (cmd.exe)", f'"{path}\\Scripts\\activate.bat"')
        else:
            add("Activate", f'source "{path}/bin/activate"')
    elif kind in ("conda", "conda-root"):
        add("Activate", f'conda activate "{path}"')
    if py and kind != "kernel":
        add("Start Python", run(py))
        add("Install a package", f"{run(py)} -m pip install PACKAGE")
        if "ipykernel" in rec.get("features", []):
            slug = re.sub(r"[^a-z0-9._-]+", "-", rec["name"].lower()).strip("-") or "python"
            add("Register as Jupyter kernel",
                f'{run(py)} -m ipykernel install --user --name {slug} --display-name "{rec["name"]}"')
        else:
            add("Add Jupyter support", f"{run(py)} -m pip install ipykernel")
    if kind == "conda":
        add("Remove environment", f'conda env remove -p "{path}"')
    elif kind == "venv":
        add("Delete environment", f'Remove-Item -Recurse -Force "{path}"' if WINDOWS else f'rm -rf "{path}"')
    elif kind == "kernel":
        add("Remove kernel", f"jupyter kernelspec remove {rec['name']}")
    return out


def refresh_packages(rec: dict) -> dict:
    conda = rec["kind"] in ("conda", "conda-root")
    rec["packages"] = collect_packages(Path(rec["path"]), rec["site_packages"], conda)
    rec["features"] = features(rec["packages"])
    rec["commands"] = commands(rec)
    return rec


# --------------------------------------------------------------------------
# Where to look
# --------------------------------------------------------------------------

def conda_root_candidates() -> list:
    names = ["anaconda3", "miniconda3", "miniforge3", "mambaforge", "micromamba", "anaconda", "miniconda"]
    if not WINDOWS:
        names += ["Anaconda3", "Miniconda3", "Miniforge3"]
    parents = [HOME, HOME / ".local", Path("/opt"), Path("/usr/local")]
    if WINDOWS:
        parents = [HOME, LOCALAPPDATA, PROGRAMDATA, Path("C:/"), LOCALAPPDATA / "Continuum"]
    roots = [p / n for p in parents for n in names]
    roots += [Path("/opt/conda"), Path("/opt/homebrew/Caskroom/miniconda/base"),
              Path("/usr/local/Caskroom/miniconda/base"), Path("/opt/homebrew/Caskroom/miniforge/base"),
              Path("/usr/local/Caskroom/miniforge/base")]
    for var in ("CONDA_ROOT", "MAMBA_ROOT_PREFIX", "CONDA_PREFIX"):
        if _envvar(var):
            roots.append(_envvar(var))
    if _envvar("CONDA_EXE"):  # <root>/bin/conda or <root>\Scripts\conda.exe
        roots.append(_envvar("CONDA_EXE").parent.parent)
    return roots


def condarc_envs_dirs() -> list:
    """`envs_dirs:` entries from the user's .condarc (tiny YAML subset)."""
    out = []
    for rc in (HOME / ".condarc", HOME / ".conda" / ".condarc", HOME / ".config" / "conda" / ".condarc"):
        try:
            lines = rc.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        inside = False
        for line in lines:
            if re.match(r"^envs_dirs\s*:", line):
                inside = True
            elif inside and line.strip().startswith("- "):
                out.append(Path(os.path.expandvars(line.strip()[2:].strip().strip("'\""))).expanduser())
            elif inside and line.strip() and not line.startswith((" ", "\t")):
                inside = False
    return out


def base_exe_candidates() -> list:
    """(executable, tag) pairs from PATH and the usual install locations."""
    out = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    if EXE_NAME_RE.match(e.name) and "windowsapps" not in e.path.lower():
                        out.append((Path(e.path), "PATH"))
        except OSError:
            pass
    globs = []
    if WINDOWS:
        pf = [_envvar("ProgramFiles") or Path("C:/Program Files"), _envvar("ProgramFiles(x86)")]
        globs += [(LOCALAPPDATA / "Programs" / "Python", "Python*/python.exe"), (Path("C:/"), "Python*/python.exe")]
        globs += [(p, "Python*/python.exe") for p in pf if p]
        pyenv = _envvar("PYENV_ROOT") or HOME / ".pyenv" / "pyenv-win"
        globs += [(pyenv / "versions", "*/python.exe")]
        out += [(exe, "") for exe in registry_exes()]
    else:
        for d in (Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/homebrew/bin"), Path("/bin")):
            globs.append((d, "python3*"))
        if MACOS:
            globs += [(Path("/Library/Frameworks/Python.framework/Versions"), "*/bin/python3"),
                      (Path("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions"),
                       "*/bin/python3"),
                      (Path("/opt/homebrew/opt"), "python@*/bin/python3"),
                      (Path("/usr/local/opt"), "python@*/bin/python3")]
        globs += [((_envvar("PYENV_ROOT") or HOME / ".pyenv") / "versions", "*/bin/python3"),
                  (HOME / ".asdf" / "installs" / "python", "*/bin/python3")]
    uv_py = _envvar("UV_PYTHON_INSTALL_DIR") or (APPDATA / "uv" / "python" if WINDOWS else XDG_DATA / "uv" / "python")
    globs += [(uv_py, "*/python.exe" if WINDOWS else "*/bin/python3"),
              (HOME / ".rye" / "py", "*/python.exe" if WINDOWS else "*/bin/python3")]
    for base, pattern in globs:
        for exe in sorted(base.glob(pattern)) if isdir(base) else []:
            if EXE_NAME_RE.match(exe.name):
                out.append((exe, origin(exe)))
    return out


def registry_exes() -> list:
    """Interpreters registered under PEP 514 (HKCU/HKLM\\Software\\Python)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    views = [(winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)]

    def keys(key):
        i = 0
        while True:
            try:
                yield winreg.EnumKey(key, i)
            except OSError:
                return
            i += 1

    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, r"Software\Python", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        for company in list(keys(root)):
            if company == "PyLauncher":
                continue
            try:
                ckey = winreg.OpenKey(root, company)
            except OSError:
                continue
            for tag in list(keys(ckey)):
                try:
                    ip = winreg.OpenKey(ckey, tag + r"\InstallPath")
                except OSError:
                    continue
                try:
                    exe = winreg.QueryValueEx(ip, "ExecutablePath")[0]
                except OSError:
                    try:
                        exe = os.path.join(winreg.QueryValueEx(ip, "")[0], "python.exe")
                    except OSError:
                        continue
                out.append(Path(exe))
    return out


def venv_locations() -> list:
    """(folder, tag, depth) for tools that keep environments in a central place."""
    workon = _envvar("WORKON_HOME")
    locs = [(HOME / ".virtualenvs", "virtualenvwrapper", 1), (HOME / "Envs", "virtualenvwrapper", 1),
            (XDG_DATA / "virtualenvs", "pipenv", 1), (HOME / ".local" / "share" / "virtualenvs", "pipenv", 1)]
    if workon:
        locs.append((workon, "", 1))
    pipx = _envvar("PIPX_HOME")
    if pipx:
        locs.append((pipx / "venvs", "pipx", 1))
    locs += [(HOME / ".local" / "pipx" / "venvs", "pipx", 1), (XDG_DATA / "pipx" / "venvs", "pipx", 1)]
    pyenv = _envvar("PYENV_ROOT") or HOME / ".pyenv"
    for version in subdirs(pyenv / "versions"):
        locs.append((version / "envs", "pyenv-virtualenv", 1))
    if WINDOWS:
        locs += [(LOCALAPPDATA / "pypoetry" / "Cache" / "virtualenvs", "poetry", 1),
                 (HOME / "pipx" / "venvs", "pipx", 1), (LOCALAPPDATA / "pipx" / "pipx" / "venvs", "pipx", 1),
                 (LOCALAPPDATA / "hatch" / "env", "hatch", 4), (LOCALAPPDATA / "pdm" / "pdm" / "venvs", "pdm", 1),
                 (APPDATA / "uv" / "tools", "uv tool", 1)]
    else:
        cache = HOME / "Library" / "Caches" if MACOS else XDG_CACHE
        support = HOME / "Library" / "Application Support" if MACOS else XDG_DATA
        locs += [(cache / "pypoetry" / "virtualenvs", "poetry", 1),
                 (support / "pipx" / "venvs", "pipx", 1), (support / "hatch" / "env", "hatch", 4),
                 (support / "pdm" / "venvs", "pdm", 1), (XDG_DATA / "uv" / "tools", "uv tool", 1)]
    return locs


def jupyter_dirs() -> list:
    dirs = [Path(p) for p in os.environ.get("JUPYTER_PATH", "").split(os.pathsep) if p]
    if _envvar("JUPYTER_DATA_DIR"):
        dirs.append(_envvar("JUPYTER_DATA_DIR"))
    if WINDOWS:
        dirs += [APPDATA / "jupyter", PROGRAMDATA / "jupyter"]
    elif MACOS:
        dirs += [HOME / "Library" / "Jupyter", Path("/opt/homebrew/share/jupyter"),
                 Path("/usr/local/share/jupyter"), Path("/usr/share/jupyter")]
    else:
        dirs += [XDG_DATA / "jupyter", Path("/usr/local/share/jupyter"), Path("/usr/share/jupyter")]
    return dirs


def user_site_candidates() -> list:
    """(site-packages folder, major.minor) for per-user `pip install --user` folders."""
    out = []
    if WINDOWS:
        for d in (APPDATA / "Python").glob("Python3*/site-packages"):
            m = re.match(r"Python(\d)(\d+)", d.parent.name)
            if m:
                out.append((d, f"{m.group(1)}.{m.group(2)}"))
    else:
        patterns = [(HOME / ".local" / "lib", "python3*/site-packages")]
        if MACOS:
            patterns.append((HOME / "Library" / "Python", "3*/lib/python/site-packages"))
        for base, pattern in patterns:
            for d in base.glob(pattern):
                m = re.search(r"(\d+\.\d+)", str(d.relative_to(base)))
                if m:
                    out.append((d, m.group(1)))
    return out


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------

class Scanner:
    def __init__(self, extra_dirs=(), depth=3, home=True, system=True):
        self.extra_dirs = [Path(d).expanduser() for d in extra_dirs]
        self.depth = depth
        self.home = home
        self.system = system
        self.envs = {}       # key -> record
        self.kernels = {}    # real spec dir -> record
        self.by_exe = {}     # normalized executable path -> record
        self.by_prefix = {}  # normalized prefix -> record
        self.locations = {}

    def look(self, label: str, path) -> Path | None:
        """Remember a searched location; return it if it exists."""
        path = Path(path)
        key = norm(path)
        if key not in self.locations:
            self.locations[key] = {"label": label, "path": str(path), "exists": isdir(path)}
        return path if self.locations[key]["exists"] else None

    # -- records ----------------------------------------------------------

    def _new(self, key, kind, name, path, python=None, version=None, sites=(), tags=(), parent=None):
        rec = {
            "id": make_id(key), "kind": kind, "name": name, "path": str(path),
            "python": str(python) if python else None, "version": version, "parent": parent,
            "tags": [t for t in dict.fromkeys(tags) if t], "site_packages": list(sites),
            "packages": [], "features": [], "kernels": [], "issues": [], "commands": [], "size": None,
            "_homes": set(),
        }
        if python:
            rec["_homes"] = {norm(Path(python).parent), real(Path(python).parent)}
            self.by_exe.setdefault(norm(python), rec)
        self.by_prefix.setdefault(norm(path), rec)
        self.envs[key] = rec
        return refresh_packages(rec) if sites or kind in ("conda", "conda-root") else rec

    def add_env(self, prefix, tags=(), parent=None) -> dict:
        prefix = Path(prefix)
        key = norm(prefix)
        if key in self.envs:
            rec = self.envs[key]
            rec["tags"] = list(dict.fromkeys(rec["tags"] + [t for t in tags if t]))
            return rec
        cfg = read_cfg(prefix)
        if isdir(prefix / "conda-meta"):
            kind = "conda-root" if is_conda_root(prefix) else "conda"
        else:
            kind = "venv"
        version = detect_version(prefix)
        tags = list(tags) + [k for k in ("uv", "virtualenv") if k in cfg]
        exe = python_exe(prefix)
        if kind == "conda-root":
            name = f"{prefix.name} (base)"
        elif prefix.name.lower() in (".venv", "venv", ".env", "env"):
            name = f"{prefix.parent.name}/{prefix.name}"
        else:
            name = cfg.get("prompt", "").strip("'\"() ") or prefix.name
        rec = self._new(key, kind, name, prefix, exe, version, site_dirs(prefix, version), tags, parent)
        rec["home"] = cfg.get("home")
        if exe is None:
            rec["issues"].append("No Python interpreter in this environment."
                                 if kind == "venv" else "Python is not installed in this conda environment.")
        if kind == "conda-root":
            for sub in subdirs(prefix / "envs"):
                if is_env(sub):
                    self.add_env(sub, parent=rec["id"])
        return rec

    def add_base(self, exe, tag: str = "") -> dict | None:
        exe = Path(exe)
        if not isfile(exe) or "shims" in (p.lower() for p in exe.parts):
            return None
        if MACOS and str(exe) == "/usr/bin/python3":
            return None  # Apple's stub; the Command Line Tools Python is found directly
        if is_env(prefix_of(exe)):  # an activated venv or conda env on PATH
            return self.add_env(prefix_of(exe))
        real_exe = Path(os.path.realpath(exe))
        prefix = prefix_of(real_exe)
        if is_env(prefix):
            return self.add_env(prefix)
        if not has_stdlib(prefix):
            return None
        m = EXE_NAME_RE.match(real_exe.name) or EXE_NAME_RE.match(exe.name)
        hint = m.group(1) if m and m.group(1) and "." in m.group(1) else None
        version = detect_version(prefix, hint)
        key = f"base|{norm(prefix)}|{major_minor(version)}"
        tags = [origin(real_exe), "" if tag == "PATH" else tag]
        if key in self.envs:
            rec = self.envs[key]
            rec["tags"] = list(dict.fromkeys(rec["tags"] + [t for t in tags if t]))
        else:
            rec = self._new(key, "base", f"Python {version or '?'}", prefix, real_exe, version,
                            site_dirs(prefix, version), tags)
            self.by_exe[real(real_exe)] = rec
        rec["_homes"] |= {norm(exe.parent), real(exe.parent)}
        self.by_exe.setdefault(norm(exe), rec)
        if tag == "PATH" and "on PATH" not in rec["tags"]:
            rec["tags"].append("on PATH")
        return rec

    def add_install_dir(self, path: Path, tag: str):
        """A folder holding one interpreter (pyenv/uv/asdf versions, etc.)."""
        if is_env(path):
            self.add_env(path, tags=[tag])
        else:
            exe = python_exe(path)
            if exe:
                self.add_base(exe, tag)

    def walk(self, root: Path, depth: int, tag: str = ""):
        """Depth-limited search for environment folders below root."""
        if is_env(root):
            self.add_env(root, tags=[tag])
            return
        stack = [(root, 0)]
        while stack:
            folder, level = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = [e for e in it if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for e in entries:
                path = Path(e.path)
                if norm(path) in self.envs:
                    continue
                if is_env(path):
                    self.add_env(path, tags=[tag])
                elif level + 1 < depth and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                    stack.append((path, level + 1))

    # -- phases -----------------------------------------------------------

    def find_conda(self):
        for root in conda_root_candidates():
            if self.look("Conda / Mamba", root) and is_env(root):
                self.add_env(root)
            elif isdir(root):
                self.walk(root / "envs", 1)  # e.g. a micromamba root without a base env
        txt = HOME / ".conda" / "environments.txt"
        self.look("Conda environment list", txt.parent)
        try:
            listed = txt.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            listed = []
        for line in listed:
            path = Path(line.strip())
            if line.strip() and is_env(path):
                parent = self.envs.get(norm(path.parent.parent)) if path.parent.name == "envs" else None
                if parent is None and is_conda_root(path.parent.parent):
                    parent = self.add_env(path.parent.parent)
                self.add_env(path, parent=parent["id"] if parent else None)
        for d in condarc_envs_dirs() + [HOME / ".conda" / "envs"]:
            if self.look("Conda envs_dirs", d):
                self.walk(d, 1)

    def find_bases(self):
        for exe, tag in base_exe_candidates():
            try:
                self.add_base(exe, tag)
            except OSError:
                pass  # e.g. access denied inside C:\Program Files\WindowsApps
        for install_dir, tag in ((_envvar("PYENV_ROOT") or HOME / ".pyenv", "pyenv"),
                                 (HOME / ".pyenv" / "pyenv-win", "pyenv")):
            for d in subdirs(install_dir / "versions"):
                self.add_install_dir(d, tag)
        if WINDOWS:
            self.look("Python.org installs", LOCALAPPDATA / "Programs" / "Python")
            if self.look("Python install manager", LOCALAPPDATA / "Python"):
                for d in subdirs(LOCALAPPDATA / "Python"):
                    self.add_install_dir(d, "pymanager")
            self.find_store()
        elif MACOS:
            self.look("Python.org installs", "/Library/Frameworks/Python.framework/Versions")
            self.look("Homebrew", "/opt/homebrew/opt")
        self.look("pyenv", _envvar("PYENV_ROOT") or HOME / ".pyenv")
        self.look("uv-managed Pythons", APPDATA / "uv" / "python" if WINDOWS else XDG_DATA / "uv" / "python")

    def find_store(self):
        apps = self.look("Microsoft Store Python", LOCALAPPDATA / "Microsoft" / "WindowsApps")
        if not apps:
            return
        for d in sorted(apps.glob("PythonSoftwareFoundation.Python.3.*")):
            m = re.search(r"Python\.(\d+)\.(\d+)", d.name)
            if not m:
                continue
            mm = f"{m.group(1)}.{m.group(2)}"
            packages = sorted((LOCALAPPDATA / "Packages").glob(f"PythonSoftwareFoundation.Python.{mm}_*"))
            home = packages[0] if packages else d
            sites = [str(s) for s in home.glob(f"LocalCache/local-packages/Python{m.group(1)}{m.group(2)}/site-packages")]
            key = f"store|{mm}"
            if key not in self.envs:
                self._new(key, "base", f"Python {mm}", home, d / "python.exe", mm, sites, ["microsoft store"])

    def find_venvs(self):
        for folder, tag, depth in venv_locations():
            if self.look(f"{tag or 'Virtualenvs'}", folder):
                self.walk(folder, depth, tag)

    def find_user_sites(self):
        for site, mm in user_site_candidates():
            self.look("User site-packages", site.parent)
            key = f"user|{norm(site)}"
            if key not in self.envs:
                self._new(key, "user", f"User packages {mm}", site, None, mm, [str(site)], ["pip --user"])

    def link(self):
        """Connect venvs and user sites to the interpreter they belong to."""
        def homes():
            table = {}
            for rec in list(self.envs.values()):
                if rec["kind"] in ("base", "conda", "conda-root"):
                    for h in rec["_homes"]:
                        table.setdefault(h, []).append(rec)
            return table

        table = homes()
        for rec in [r for r in self.envs.values() if r["kind"] == "venv" and r.get("home")]:
            if rec["parent"]:
                continue
            home = rec["home"]
            matches = table.get(norm(home)) or table.get(real(home))
            if not matches:  # an interpreter we missed: add it from the home folder
                for name in ("python.exe",) if WINDOWS else ("python3", "python"):
                    if self.add_base(Path(home) / name, origin(home)):
                        table = homes()
                        matches = table.get(norm(home)) or table.get(real(home))
                        break
            if matches:
                mm = major_minor(rec["version"])
                best = next((m for m in matches if major_minor(m["version"]) == mm), matches[0])
                rec["parent"] = best["id"]
                continue
            key = f"missing|{norm(home)}"
            ghost = self.envs.get(key) or self._new(key, "missing", "Missing interpreter", home,
                                                     version=rec["version"])
            if not ghost["issues"]:
                ghost["issues"].append("The interpreter these environments were created from was not found. "
                                       "They will not run until it is reinstalled or the environments are rebuilt.")
            rec["parent"] = ghost["id"]
            rec["issues"].append("Base interpreter not found: " + home)
        bases = [r for r in self.envs.values() if r["kind"] == "base"]
        for rec in self.envs.values():
            if rec["kind"] == "user":
                rec["parent"] = next((b["id"] for b in bases if major_minor(b["version"]) == rec["version"]), None)

    def env_for_exe(self, exe: str) -> dict | None:
        for candidate in (Path(exe), Path(os.path.realpath(exe))):
            rec = self.by_exe.get(norm(candidate)) or self.by_prefix.get(norm(prefix_of(candidate)))
            if rec:
                return rec
        return None

    def find_kernels(self):
        sources = []
        if self.system:
            sources += [(d / "kernels", None) for d in jupyter_dirs()]
        sources += [(Path(r["path"]) / "share" / "jupyter" / "kernels", r) for r in list(self.envs.values())
                    if r["kind"] not in ("user", "missing")]
        for kernels_dir, owner in sources:
            if owner is None and not self.look("Jupyter kernels", kernels_dir):
                continue
            for spec_dir in subdirs(kernels_dir):
                self.add_kernel(spec_dir, owner)

    def add_kernel(self, spec_dir: Path, owner: dict | None):
        key = real(spec_dir)
        if key in self.kernels:
            return
        try:
            spec = json.loads((spec_dir / "kernel.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        argv = [str(a) for a in spec.get("argv") or []]
        argv0 = argv[0] if argv else ""
        rec = {
            "id": make_id("kernel|" + key), "kind": "kernel", "name": spec_dir.name,
            "display_name": spec.get("display_name") or spec_dir.name, "language": spec.get("language", ""),
            "path": str(spec_dir), "argv": argv, "env": None, "note": "", "issues": [],
        }
        target = None
        if argv0 and os.path.isabs(argv0):
            if not os.path.exists(argv0):
                rec["issues"].append("Kernel interpreter not found: " + argv0)
            target = self.env_for_exe(argv0)
        elif owner and Path(argv0).stem.lower().startswith("python"):
            target = owner
        elif argv0:
            rec["note"] = f"Runs '{argv0}' from whichever environment is active."
        if target:
            rec["env"] = target["id"]
            target["kernels"].append(rec["id"])
        rec["commands"] = commands(rec)
        self.kernels[key] = rec

    def run(self) -> dict:
        start = time.time()
        if self.system:
            self.find_conda()
            self.find_bases()
            self.find_venvs()
            self.find_user_sites()
        for d in self.extra_dirs:
            if self.look("Extra folder", d):
                self.walk(d, max(self.depth, 1))
        if self.home and self.depth > 0:
            self.look("Home folder", HOME)
            self.walk(HOME, self.depth)
        cwd = Path.cwd()
        for name in (".venv", "venv"):
            if is_env(cwd / name):
                self.add_env(cwd / name)
        self.link()
        self.find_kernels()

        envs = sorted(self.envs.values(), key=lambda r: (
            KIND_ORDER.get(r["kind"], 9), [-n for n in _version_key(r["version"] or "0")], r["name"].lower()))
        for rec in envs:
            rec.pop("_homes", None)
            rec["commands"] = commands(rec)
        kernels = sorted(self.kernels.values(), key=lambda k: k["display_name"].lower())
        return {
            "tool": "PythonMapper", "tool_version": __version__, "tool_author": __author__,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(time.time() - start, 2),
            "host": platform.node(), "platform": platform.platform(),
            "environments": envs, "kernels": kernels,
            "locations": sorted(self.locations.values(), key=lambda loc: (not loc["exists"], loc["label"].lower())),
        }


def scan(extra_dirs=(), depth=3, home=True, system=True) -> dict:
    """Scan this machine and return a JSON-serialisable map."""
    return Scanner(extra_dirs, depth, home, system).run()
