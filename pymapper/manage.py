"""Change things: create and remove environments, (un)install packages and Pythons.

Every operation is first turned into a Plan: a list of Steps (commands to run or
folders to delete). The CLI prints the plan and asks before anything destructive;
the web UI shows it in a confirm dialog and runs it as a background job.

Package tools are picked in this order: mamba, then conda, then pip. Conda tools
only manage conda environments; venvs and plain interpreters always use pip.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .discover import HOME, LOCALAPPDATA, WINDOWS, canonical, isdir, isfile, major_minor, norm, python_exe

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENV_PYTHON_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
DISTRO_RE = re.compile(r"^\d+(?:\.\d+){0,2}[A-Za-z0-9.\-]*$")
# Package specs only: no leading "-" (options), no paths or URLs. "channel::name" is allowed for conda.
SPEC_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*::)?[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~*]*$")
# Output that means "conda has no such package", so pip is worth a try.
NOT_FOUND = ("packagesnotfounderror", "nothing provides", "does not exist", "not available from current channels",
             "could not be found", "no match")
CONDA_TOOLS = ("mamba", "micromamba", "conda")
DISTRO_TOOLS = ("pymanager", "uv", "pyenv") if WINDOWS else ("uv", "pyenv", "brew")


class ManageError(Exception):
    """A request that can't be carried out; the message is shown to the user."""


class Step:
    def __init__(self, label, cmd=None, delete=None, fallback=(), optional=False):
        self.label = label
        self.cmd = cmd                  # list of args, or a command-line string (Windows uninstallers)
        self.delete = delete            # folder to delete instead of running a command
        self.fallback = list(fallback)  # steps to run instead if cmd fails with a "not found" error
        self.optional = optional        # keep going if this step fails

    def show(self) -> str:
        if self.delete:
            return f"delete folder {self.delete}"
        if isinstance(self.cmd, str):
            return self.cmd
        return subprocess.list2cmdline(self.cmd) if WINDOWS else shlex.join(self.cmd)

    def to_dict(self) -> dict:
        return {"label": self.label, "command": self.show(),
                "fallback": [s.to_dict() for s in self.fallback], "optional": self.optional}


class Plan:
    def __init__(self, title, steps, notes=(), destructive=False, confirm=None):
        self.title, self.steps, self.notes, self.destructive = title, steps, list(notes), destructive
        self.confirm = destructive if confirm is None else confirm  # ask before running in the CLI

    def to_dict(self) -> dict:
        return {"title": self.title, "steps": [s.to_dict() for s in self.steps],
                "notes": self.notes, "destructive": self.destructive, "confirm": self.confirm}

    def describe(self) -> str:
        lines = [self.title]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"  {i}. {s.label}\n     {s.show()}")
            for f in s.fallback:
                lines.append(f"     if the package isn't on conda channels: {f.show()}")
        lines += [f"  Note: {n}" for n in self.notes]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Finding the tools
# --------------------------------------------------------------------------

def _root_exes(root: Path) -> dict:
    if WINDOWS:
        return {"mamba": [root / "Library" / "bin" / "mamba.exe", root / "Scripts" / "mamba.exe",
                          root / "condabin" / "mamba.bat"],
                "conda": [root / "Scripts" / "conda.exe", root / "condabin" / "conda.bat"]}
    return {"mamba": [root / "bin" / "mamba", root / "condabin" / "mamba"],
            "conda": [root / "bin" / "conda", root / "condabin" / "conda"]}


def find_tools(data: dict) -> dict:
    """Paths of the package and Python managers available on this machine."""
    tools = {}

    def put(name, path):
        if path and name not in tools and isfile(path):
            tools[name] = str(path)

    ext = ".exe" if WINDOWS else ""
    put("mamba", shutil.which("mamba"))
    put("micromamba", shutil.which("micromamba"))
    put("conda", os.environ.get("CONDA_EXE") or shutil.which("conda"))
    for root in (e for e in data["environments"] if e["kind"] == "conda-root"):
        for name, candidates in _root_exes(Path(root["path"])).items():
            for c in candidates:
                put(name, c)
    put("uv", shutil.which("uv") or HOME / ".local" / "bin" / f"uv{ext}")
    put("uv", HOME / ".cargo" / "bin" / f"uv{ext}")
    put("pyenv", shutil.which("pyenv") or HOME / ".pyenv" / "bin" / "pyenv")
    if WINDOWS:
        put("pymanager", shutil.which("pymanager"))
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), str(LOCALAPPDATA / "Programs")):
            if base:
                put("pymanager", Path(base) / "PyManager" / "pymanager.exe")
        put("pymanager", LOCALAPPDATA / "Microsoft" / "WindowsApps" / "pymanager.exe")
    else:
        put("brew", shutil.which("brew") or "/opt/homebrew/bin/brew")
        put("brew", "/usr/local/bin/brew")
    return tools


def conda_tool(data: dict, tools: dict, rec: dict = None, prefer: str = None):
    """(name, exe) of the best conda-style tool for rec: its own install's mamba, then conda."""
    root = None
    if rec:
        root = rec if rec["kind"] == "conda-root" else _by_id(data, rec.get("parent"))
    for name in [prefer] if prefer else CONDA_TOOLS:
        if root and root["kind"] == "conda-root":
            for c in _root_exes(Path(root["path"])).get(name, []):
                if isfile(c):
                    return name, str(c)
        if tools.get(name):
            return name, tools[name]
    return None


def _by_id(data: dict, item_id) -> dict | None:
    for item in data["environments"] + data["kernels"]:
        if item["id"] == item_id:
            return item
    return None


def _specs(packages) -> list:
    specs = packages.split() if isinstance(packages, str) else [str(p) for p in packages or []]
    bad = [s for s in specs if not SPEC_RE.match(s)]
    if bad:
        raise ManageError(f"Not a valid package name: {bad[0]!r}. Use names like 'requests' or 'numpy==1.26'.")
    return specs


def _pip(rec: dict, *args) -> list:
    return [rec["python"], "-m", "pip", *args, "--disable-pip-version-check"]


def _ensure_pip(rec: dict) -> list:
    if "pip" in rec.get("features", []):
        return []
    return [Step("Add pip to the environment", [rec["python"], "-m", "ensurepip", "--upgrade"])]


def _venv_python(path: Path) -> str:
    return str(path / ("Scripts/python.exe" if WINDOWS else "bin/python"))


def _kernel_steps(data: dict, rec: dict) -> list:
    return [Step(f"Remove Jupyter kernel '{k['display_name']}' (it runs in this environment)", delete=k["path"])
            for k in data["kernels"] if k["env"] == rec["id"]]


def _dependents(data: dict, rec: dict) -> list:
    return [e for e in data["environments"] if e.get("parent") == rec["id"] and e["kind"] in ("venv", "conda")]


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------

def pick_base(data: dict, python: str = None) -> dict | None:
    """The newest interpreter that matches `python` (e.g. "3.12"), preferring plain installs."""
    bases = [e for e in data["environments"] if e["kind"] in ("base", "conda-root") and e.get("python")]
    if python:
        bases = [e for e in bases if e["version"] and (e["version"] == python or e["version"].startswith(python + "."))]
    bases.sort(key=lambda e: (e["kind"] != "base", [-int(n) for n in re.findall(r"\d+", e["version"] or "0")[:3]]))
    return bases[0] if bases else None


def plan_create(data, tools, name, kind=None, python=None, path=None, packages=()) -> Plan:
    if not name or not NAME_RE.match(name):
        raise ManageError("Environment names may use letters, numbers, '.', '_' and '-'.")
    if python and not ENV_PYTHON_RE.match(python):
        raise ManageError("The Python version should look like 3.12 or 3.12.4.")
    specs = _specs(packages)
    conda = conda_tool(data, tools)
    kind = kind or ("conda" if conda else "venv")
    if kind == "conda":
        if not conda:
            raise ManageError("No mamba or conda was found. Create a venv instead, or install Miniforge.")
        tool, exe = conda
        target = ["-p", str(Path(path).expanduser())] if path else ["-n", name]
        py_spec = f"python={python}" if python else "python"
        steps = [Step(f"Create conda environment '{name}' with {tool}", [exe, "create", "-y", *target, py_spec, *specs])]
        if specs:  # some packages may only exist on PyPI: create with Python only, then pip
            prefix = Path(path).expanduser() if path else None
            env_py = _venv_python(prefix) if prefix else None
            steps[0].fallback = [Step("Create the environment with Python only",
                                      [exe, "create", "-y", *target, py_spec])]
            if env_py:
                steps[0].fallback.append(Step("Install the packages with pip", [env_py, "-m", "pip", "install", *specs]))
            else:
                steps[0].fallback.append(Step("Install the packages with pip",
                                              [exe, "run", "-n", name, "python", "-m", "pip", "install", *specs]))
        return Plan(f"Create conda environment '{name}'", steps)
    if kind != "venv":
        raise ManageError("Environment type must be 'venv' or 'conda'.")
    base = pick_base(data, python)
    if not base:
        raise ManageError(f"No Python {python or ''} was found to create the venv from. "
                          f"Install one first, e.g. `pymapper python install {python or '3.13'}`.".replace("  ", " "))
    target = Path(path).expanduser() if path else HOME / ".virtualenvs" / name
    if target.exists():
        raise ManageError(f"{target} already exists.")
    steps = [Step(f"Create venv '{name}' from {base['name']}", [base["python"], "-m", "venv", str(target)])]
    if specs:
        steps.append(Step("Install packages with pip", [_venv_python(target), "-m", "pip", "install",
                                                        "--disable-pip-version-check", *specs]))
    return Plan(f"Create venv '{name}' at {target}", steps)


def _safe_to_delete(path: str) -> bool:
    p = Path(path)
    return len(p.parts) >= 3 and norm(p) not in (norm(HOME), norm(p.anchor), norm(Path.cwd()))


def plan_remove(data, tools, rec) -> Plan:
    kind = rec["kind"]
    if kind == "kernel":
        return Plan(f"Remove Jupyter kernel '{rec['display_name']}'",
                    [Step("Delete the kernel spec", delete=rec["path"])], destructive=True)
    if kind == "venv":
        if not isfile(Path(rec["path"]) / "pyvenv.cfg") or not _safe_to_delete(rec["path"]):
            raise ManageError("That folder doesn't look like a virtual environment, so it won't be deleted.")
        steps = [Step(f"Delete virtual environment '{rec['name']}'", delete=rec["path"])]
    elif kind == "conda":
        conda = conda_tool(data, tools, rec)
        if not conda:
            raise ManageError("No mamba or conda was found to remove this environment with.")
        tool, exe = conda
        args = ["remove", "--all"] if tool == "conda" else ["env", "remove"]
        steps = [Step(f"Remove conda environment '{rec['name']}' with {tool}", [exe, *args, "-y", "-p", rec["path"]])]
    elif kind in ("base", "conda-root"):
        raise ManageError("This is a Python installation, not an environment. Use 'Uninstall Python' instead.")
    else:
        raise ManageError("This item can't be removed.")
    return Plan(f"Remove '{rec['name']}'", steps + _kernel_steps(data, rec), destructive=True)


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

def plan_install(data, tools, rec, packages, tool=None, upgrade=False) -> Plan:
    specs = _specs(packages)
    if not specs:
        raise ManageError("Enter at least one package.")
    if rec["kind"] in ("kernel", "missing", "user") or not rec.get("python") and rec["kind"] not in ("conda", "conda-root"):
        raise ManageError("Packages can only be installed into an interpreter or environment.")
    is_conda = rec["kind"] in ("conda", "conda-root")
    if tool and tool != "pip" and not is_conda:
        raise ManageError(f"{tool} can only manage conda environments; use pip here.")
    verb = "Upgrade" if upgrade else "Install"
    pip_args = ["install", "--upgrade"] if upgrade else ["install"]
    conda = conda_tool(data, tools, rec, None if tool in (None, "pip") else tool) if is_conda and tool != "pip" else None
    if is_conda and tool and tool != "pip" and not conda:
        raise ManageError(f"{tool} was not found.")
    if conda:
        name, exe = conda
        step = Step(f"{verb} {' '.join(specs)} with {name}",
                    [exe, "update" if upgrade else "install", "-y", "-p", rec["path"], *specs])
        if rec.get("python") and not any("::" in s for s in specs):
            step.fallback = _ensure_pip(rec) + [Step(f"{verb} with pip instead", _pip(rec, *pip_args, *specs))]
        return Plan(f"{verb} packages in '{rec['name']}'", [step])
    if not rec.get("python"):
        raise ManageError("This environment has no Python interpreter for pip to use.")
    steps = _ensure_pip(rec) + [Step(f"{verb} {' '.join(specs)} with pip", _pip(rec, *pip_args, *specs))]
    return Plan(f"{verb} packages in '{rec['name']}'", steps)


def plan_uninstall(data, tools, rec, packages, tool=None) -> Plan:
    specs = _specs(packages)
    installed = {canonical(p["name"]): p for p in rec.get("packages", [])}
    missing = [s for s in specs if canonical(s) not in installed]
    if not specs or missing:
        raise ManageError(f"{missing[0]} is not installed in '{rec['name']}'." if missing else "Enter at least one package.")
    via_conda = [s for s in specs if installed[canonical(s)]["installer"] == "conda" and tool != "pip"]
    via_pip = [s for s in specs if s not in via_conda]
    steps = []
    if via_conda:
        conda = conda_tool(data, tools, rec, tool if tool in CONDA_TOOLS else None)
        if not conda:
            raise ManageError("These packages came from conda, but no mamba or conda was found to remove them.")
        name, exe = conda
        steps.append(Step(f"Remove {' '.join(via_conda)} with {name}", [exe, "remove", "-y", "-p", rec["path"], *via_conda]))
    if via_pip:
        if not rec.get("python"):
            raise ManageError("This environment has no Python interpreter for pip to use.")
        steps.append(Step(f"Uninstall {' '.join(via_pip)} with pip", _pip(rec, "uninstall", "-y", *via_pip)))
    return Plan(f"Uninstall packages from '{rec['name']}'", steps, destructive=True)


# --------------------------------------------------------------------------
# Python distributions
# --------------------------------------------------------------------------

def plan_python_install(data, tools, version, via=None) -> Plan:
    if not version or not DISTRO_RE.match(version):
        raise ManageError("The version should look like 3.13 or 3.12.4.")
    available = [t for t in DISTRO_TOOLS if tools.get(t)]
    if via and via not in DISTRO_TOOLS:
        raise ManageError(f"Can't install Python with {via}. Choose one of: {', '.join(DISTRO_TOOLS)}.")
    tool = via or (available[0] if available else None)
    if not tool or not tools.get(tool):
        hint = ("Install the Python install manager from python.org, or uv (https://docs.astral.sh/uv/)."
                if WINDOWS else "Install uv (https://docs.astral.sh/uv/) or pyenv, or use your system's package manager.")
        raise ManageError(f"{'No tool for installing Python was found' if not via else via + ' was not found'}. {hint}")
    exe = tools[tool]
    cmd = {"pymanager": [exe, "install", "-y", version], "uv": [exe, "python", "install", version],
           "pyenv": [exe, "install", "-s", version], "brew": [exe, "install", f"python@{major_minor(version) or version}"]}[tool]
    return Plan(f"Install Python {version} with {tool}", [Step(f"Download and install Python {version}", cmd)],
                confirm=True)


def _windows_uninstaller(rec: dict) -> str | None:
    """QuietUninstallString of the python.org installer that matches rec's version."""
    try:
        import winreg
    except ImportError:
        return None
    want = rec.get("version") or ""
    bits = "32-bit" if re.search(r"-32$", rec["path"]) else "64-bit"
    roots = [(winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)]
    for hive, view in roots:
        try:
            base = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.OpenKey(base, winreg.EnumKey(base, i))
            except OSError:
                break
            i += 1
            try:
                name = winreg.QueryValueEx(sub, "DisplayName")[0]
                quiet = winreg.QueryValueEx(sub, "QuietUninstallString")[0]
            except OSError:
                continue
            if name == f"Python {want} ({bits})" and quiet:
                return quiet
    return None


def plan_python_uninstall(data, tools, rec) -> Plan:
    if rec["kind"] not in ("base", "conda-root"):
        raise ManageError("Only Python installations can be uninstalled here. Use 'remove' for environments.")
    prefix = Path(rec["path"])
    if norm(sys.base_prefix) == norm(prefix) or norm(sys.prefix) == norm(prefix):
        raise ManageError("PythonMapper is running on this Python. Run it from a different Python to uninstall this one.")
    tags = set(rec.get("tags", []))
    notes = []
    users = _dependents(data, rec)
    if users:
        notes.append(f"{len(users)} environment(s) created from it will stop working: "
                     + ", ".join(e["name"] for e in users[:8]) + (" ..." if len(users) > 8 else ""))
    kernel_steps = []
    if rec["kind"] == "conda-root":
        envs = [e["name"] for e in users]
        if envs:
            notes[-1:] = [f"All {len(envs)} conda environment(s) inside it will be deleted too."]
        uninstallers = sorted(prefix.glob("Uninstall-*.exe")) if WINDOWS else []
        if uninstallers:
            steps = [Step(f"Run {uninstallers[0].name}", [str(uninstallers[0]), "/S"])]
            notes.append("The uninstaller finishes in the background; rescan after a minute.")
        else:
            conda = _root_exes(prefix)["conda"]
            conda = next((str(c) for c in conda if isfile(c)), None)
            steps = ([Step("Undo `conda init` in your shell profiles", [conda, "init", "--reverse", "--all"], optional=True)]
                     if conda else [])
            steps.append(Step(f"Delete {prefix}", delete=str(prefix)))
        return Plan(f"Uninstall {rec['name']}", steps, notes, destructive=True)

    under = lambda p: norm(prefix).startswith(norm(p) + os.sep)  # noqa: E731
    if WINDOWS and ("pymanager" in tags or under(LOCALAPPDATA / "Python")):
        if not tools.get("pymanager"):
            raise ManageError("This Python was installed by the Python install manager, which wasn't found.")
        try:
            install_id = json.loads((prefix / "__install__.json").read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError):
            install_id = None
        cmd = ([tools["pymanager"], "uninstall", "-y", "--by-id", install_id] if install_id else
               [tools["pymanager"], "uninstall", "-y", re.sub(r"^pythoncore-", "", prefix.name)])
        step = Step(f"Uninstall {rec['name']} with the Python install manager", cmd)
    elif "uv" in tags:
        step = Step(f"Uninstall {rec['name']} with uv", [_need(tools, "uv"), "python", "uninstall", prefix.name])
    elif "pyenv" in tags:
        step = Step(f"Uninstall {rec['name']} with pyenv", [_need(tools, "pyenv"), "uninstall", "-f", prefix.name])
    elif "homebrew" in tags:
        step = Step(f"Uninstall {rec['name']} with Homebrew",
                    [_need(tools, "brew"), "uninstall", f"python@{major_minor(rec['version'])}"])
    elif WINDOWS and _windows_uninstaller(rec):
        step = Step(f"Run the python.org uninstaller for {rec['name']}", _windows_uninstaller(rec))
    elif "microsoft store" in tags:
        raise ManageError("Uninstall Microsoft Store Pythons from Settings > Apps > Installed apps.")
    elif tags & {"system", "apple"}:
        raise ManageError("This Python belongs to the operating system and should not be uninstalled.")
    else:
        raise ManageError("PythonMapper doesn't know which tool installed this Python, so it can't uninstall it safely.")
    for k in data["kernels"]:
        if k["env"] == rec["id"]:
            kernel_steps.append(Step(f"Remove Jupyter kernel '{k['display_name']}'", delete=k["path"]))
    return Plan(f"Uninstall {rec['name']}", [step] + kernel_steps, notes, destructive=True)


def _need(tools: dict, name: str) -> str:
    if not tools.get(name):
        raise ManageError(f"This Python was installed with {name}, which wasn't found.")
    return tools[name]


# --------------------------------------------------------------------------
# Dispatch and execution
# --------------------------------------------------------------------------

OPERATIONS = ("create", "remove", "install", "upgrade", "uninstall", "python-install", "python-uninstall")


def make_plan(data: dict, op: str, params: dict, tools: dict = None) -> Plan:
    """Build a plan from loosely-typed request parameters (CLI or web UI)."""
    tools = find_tools(data) if tools is None else tools
    p = {k: (v.strip() if isinstance(v, str) else v) for k, v in params.items()}
    rec = None
    if op in ("remove", "install", "upgrade", "uninstall", "python-uninstall"):
        rec = _by_id(data, p.get("target"))
        if rec is None:
            raise ManageError("Unknown environment; try rescanning.")
    tool = p.get("tool") or None
    if tool and tool not in CONDA_TOOLS + ("pip",):
        raise ManageError("Tool must be mamba, micromamba, conda or pip.")
    if op == "create":
        return plan_create(data, tools, p.get("name"), p.get("kind") or None, p.get("python") or None,
                           p.get("path") or None, p.get("packages") or ())
    if op == "remove":
        return plan_remove(data, tools, rec)
    if op in ("install", "upgrade"):
        return plan_install(data, tools, rec, p.get("packages"), tool, upgrade=op == "upgrade")
    if op == "uninstall":
        return plan_uninstall(data, tools, rec, p.get("packages"), tool)
    if op == "python-install":
        return plan_python_install(data, tools, p.get("version"), p.get("via") or None)
    if op == "python-uninstall":
        return plan_python_uninstall(data, tools, rec)
    raise ManageError(f"Unknown operation {op!r}.")


def _rmtree(path: str):
    def retry(func, target, _exc):  # read-only files (common on Windows)
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _run_step(step: Step, emit) -> tuple:
    """Run one step, streaming output to emit(). Returns (ok, output)."""
    emit(f"\n==> {step.label}\n$ {step.show()}\n")
    if step.delete:
        if not isdir(step.delete):
            emit("(already gone)\n")
            return True, ""
        if not _safe_to_delete(step.delete):
            emit("Refusing to delete a top-level folder.\n")
            return False, ""
        try:
            _rmtree(step.delete)
        except OSError as exc:
            emit(f"Could not delete: {exc}\n")
            return False, ""
        emit("Deleted.\n")
        return True, ""
    try:
        proc = subprocess.Popen(
            step.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", CONDA_ALWAYS_YES="true"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        emit(f"Could not start the command: {exc}\n")
        return False, ""
    captured = []
    with proc:
        for line in proc.stdout:
            captured.append(line)
            emit(line)
    if proc.returncode:
        emit(f"(exit code {proc.returncode})\n")
    return proc.returncode == 0, "".join(captured)


def run_plan(plan: Plan, emit=None) -> bool:
    """Execute a plan step by step. Stops at the first failing required step."""
    emit = emit or (lambda text: print(text, end="", flush=True))
    for step in plan.steps:
        ok, output = _run_step(step, emit)
        if not ok and step.fallback and any(s in output.lower() for s in NOT_FOUND):
            emit("\nNot available from conda channels; falling back.\n")
            ok = all(_run_step(f, emit)[0] for f in step.fallback)
        if not ok and not step.optional:
            emit("\nStopped: this step failed.\n")
            return False
    emit("\nDone.\n")
    return True
