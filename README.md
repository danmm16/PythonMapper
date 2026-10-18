# PythonMapper

[![tests](https://github.com/<you>/PythonMapper/actions/workflows/tests.yml/badge.svg)](https://github.com/<you>/PythonMapper/actions/workflows/tests.yml)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Find every Python on your machine, see how it all connects, and manage it from one place.**

Over time most machines collect several Pythons: a python.org install, Anaconda or Miniconda, Homebrew, a dozen project `.venv`s, pipx tools, and Jupyter kernels that point at environments you deleted months ago. PythonMapper finds all of them and draws the map: which interpreter each environment came from, which Jupyter kernel runs where, and what is installed in each. It can then create and remove environments, install and uninstall packages, and install or uninstall Python itself.

<!-- Add a screenshot: run `pymapper`, save a screenshot as docs/screenshot.png, then uncomment the next line. -->
<!-- ![PythonMapper map view](docs/screenshot.png) -->

## Features

- **Interactive map.** Interpreters and conda bases → the environments created from them → the Jupyter kernels that run in them. Hover to trace connections and click any box for details.
- **Packages across environments.** Type `numpy` to see every environment that has it, with its version. One tab lists every package and flags the ones whose versions differ between environments.
- **Finds what's broken.** Virtualenvs whose base Python was uninstalled, and Jupyter kernels whose interpreter no longer exists, are outlined in red.
- **Environments.** Create conda environments or venvs, and remove them along with any Jupyter kernels that ran in them.
- **Packages.** Install, upgrade and uninstall with **mamba, then conda, then pip**, falling back to pip when a package isn't on conda channels.
- **Python itself.** Install a new Python through the Python install manager, uv, pyenv or Homebrew, and uninstall Pythons through the tool that installed them.
- **Nothing runs without you seeing it.** Every change is shown as the exact commands it will run before it starts, and anything destructive asks first.
- **No dependencies.** Standard library only. Scanning reads files on disk and never runs your interpreters, so it is fast and safe even when installs are broken. The UI is one HTML file with no CDN, fonts or tracking.

## Quick start

You need Python 3.8 or newer. Any Python will do, and PythonMapper finds the others.

```bash
pip install git+https://github.com/<you>/PythonMapper
```

```bash
pymapper
```

This scans your machine and opens the map in your browser. Press `Ctrl+C` in the terminal to stop it.

To run from a clone without installing, use `python -m pymapper` in place of `pymapper` in the commands below.

## Commands

### Explore

| Command | What it does |
| --- | --- |
| `pymapper` or `pymapper ui` | Scan and open the interactive map |
| `pymapper list` | Print everything as a tree (`--json` for the raw scan) |
| `pymapper find numpy` | Show which environments have a package, and which versions |
| `pymapper show NAME` | Show the details, commands and packages of one environment |
| `pymapper kernels` | List Jupyter kernels and the environment each one runs in |
| `pymapper tools` | Show which of mamba, conda, uv, pyenv, Homebrew and the Python install manager were found |
| `pymapper export FILE` | Save the scan as `.json`, or as a self-contained `.html` map you can share |

### Manage

| Command | What it does |
| --- | --- |
| `pymapper create NAME [PACKAGES...]` | Create an environment. Options: `--python 3.12`, `--type conda\|venv`, `--path DIR` |
| `pymapper remove TARGET` | Delete an environment (venv or conda env) or a Jupyter kernel |
| `pymapper install TARGET PACKAGES...` | Install packages. Options: `--upgrade`, `--tool mamba\|micromamba\|conda\|pip` |
| `pymapper uninstall TARGET PACKAGES...` | Uninstall packages with whichever tool installed each one |
| `pymapper python install VERSION` | Install a Python distribution. Option: `--via pymanager\|uv\|pyenv\|brew` |
| `pymapper python uninstall TARGET` | Uninstall a Python distribution, or a whole Anaconda/Miniconda install |

`TARGET` is a name, path or id as shown by `pymapper list`. Every command prints its plan first. `remove`, `uninstall` and `python install`/`uninstall` then ask `Proceed? [y/N]`; pass `-y` / `--yes` to skip the question.

```bash
pymapper create ds numpy pandas ipykernel --python 3.12   # conda env if mamba/conda exists, else a venv
pymapper install ds scikit-learn                          # mamba -> conda -> pip
pymapper uninstall ds pandas
pymapper remove ds
pymapper python install 3.13
```

All of this is also available in the web UI: the **New environment** and **Install Python** buttons, plus the install box, the ✕ next to each package, and the remove/uninstall button in each item's panel.

### Options for every command

| Option | Meaning |
| --- | --- |
| `--scan-dir DIR` | Also search this folder for environments. Can be repeated. |
| `--depth N` | How many folder levels to search below your home folder and `--scan-dir` (default 3) |
| `--no-home` | Don't search your home folder for project environments |
| `--port N`, `--no-browser` | `ui` only: choose the port, and don't open a browser |

## How management works

**Which tool runs.** For packages in a conda environment, PythonMapper uses mamba if it's available, then micromamba, then conda, preferring the copy that belongs to that environment's own Anaconda/Miniconda install. If conda channels don't have a package, it falls back to the environment's pip. venvs and plain interpreters always use pip, because conda can't manage them. Uninstalling removes each package with the tool that installed it.

**New environments** are conda environments when mamba or conda is available, and venvs otherwise; choose with `--type`. venvs are created from the newest matching Python found, in `~/.virtualenvs/NAME` unless you give `--path`. A venv created without pip gets it added (`ensurepip`) the first time you install into it.

**Installing Python** uses the first tool found:

| Platform | Order |
| --- | --- |
| Windows | Python install manager (`pymanager`), uv, pyenv-win |
| macOS / Linux | uv, pyenv, Homebrew |

If none is installed, PythonMapper tells you how to get one. [uv](https://docs.astral.sh/uv/) is the quickest to set up.

**Uninstalling Python** goes through whatever installed it:

| Installed by | Uninstalled with |
| --- | --- |
| Python install manager | `pymanager uninstall` |
| python.org installer (Windows) | its own registered silent uninstaller |
| uv / pyenv / Homebrew | `uv python uninstall` / `pyenv uninstall` / `brew uninstall` |
| Anaconda, Miniconda, Miniforge | their uninstaller on Windows; elsewhere `conda init --reverse`, then the folder is deleted |
| The operating system, Apple's Command Line Tools, the Microsoft Store, python.org on macOS | not supported, with an explanation, because removing these can break the system or needs admin tools |

It refuses to uninstall the Python that PythonMapper itself is running on. It also warns about environments that will stop working, and removes the Jupyter kernels that pointed at the uninstalled Python.

## Where it looks

| What | Locations |
| --- | --- |
| Interpreters | `PATH`, the Windows registry (PEP 514), python.org installers, the Python install manager, Microsoft Store, Homebrew, Apple Command Line Tools, `/usr/bin`, `/usr/local/bin`, pyenv / pyenv-win, uv, asdf, rye |
| Conda | Anaconda, Miniconda, Miniforge, Mambaforge and micromamba in their usual places, `~/.conda/environments.txt`, `envs_dirs` in `.condarc`, `$CONDA_PREFIX` |
| Virtual environments | `~/.virtualenvs`, `$WORKON_HOME`, pipenv, poetry, pipx, hatch, pdm, uv tools, pyenv-virtualenv, and any environment folder up to `--depth` levels below your home folder |
| User packages | `pip install --user` folders for each Python version |
| Jupyter kernels | The Jupyter data folders (as in `jupyter --paths`), `$JUPYTER_PATH`, and every environment's `share/jupyter/kernels` |

The **Searched locations** tab in the UI lists every folder that was checked and whether it exists.

## FAQ

**An environment is missing.** It is probably deeper than 3 folders below your home folder, or on another drive. Point PythonMapper at it with `pymapper --scan-dir D:\projects --depth 5`.

**Something shows as "Missing interpreter".** The environments below it were created from a Python that has since been uninstalled or moved. They won't run until that Python is reinstalled at the same path, or the environments are recreated.

**The version shows as `?`.** PythonMapper reads versions from files rather than running the interpreter. A few unusual builds don't leave those files behind.

**Can I undo a removal?** No. Deleted environments and uninstalled Pythons are gone, which is why every destructive action shows its plan and asks first. Before removing an environment, you can save its package list with `pymapper show NAME --json`.

**pip says the environment is "externally managed".** Linux distributions and Homebrew protect their own Pythons from pip. Create a venv or conda environment and install there.

## How it compares

Every tool already lists and manages its own environments: `conda`, `jupyter kernelspec`, `py`/`pymanager`, `pyenv`, `pipx`, `uv`. Microsoft's [Python Environment Tools](https://github.com/microsoft/python-environment-tools) finds environments across managers, and it is the engine behind VS Code's Python environment picker.

PythonMapper is for seeing and managing everything in one place outside an editor. It links environments to their base interpreters and to Jupyter kernels, flags broken links, compares packages across environments, and drives the right tool for each change, all with nothing to install but itself.

## Security

The web UI listens on `127.0.0.1` only. Each run creates a random token that every API request must include, and requests whose `Host` header isn't local are refused, so other websites can't reach it.

Changes can only target items found by the scan. Package names are checked so they can't smuggle in options, file paths or URLs, and folders are only deleted after checking that they really are a venv, a Jupyter kernel or a conda install, and never a top-level folder like your home folder. Only one change runs at a time.

## Contributing

Issues and pull requests are welcome, especially new places to look for Pythons and new tools to drive. The code is small:

| File | Purpose |
| --- | --- |
| `pymapper/discover.py` | Finds interpreters, environments, packages and kernels (read-only) |
| `pymapper/manage.py` | Plans and runs changes: environments, packages, Python installs |
| `pymapper/server.py` | Local web server, API and background jobs |
| `pymapper/ui.html` | The whole interactive UI (vanilla JS) |
| `pymapper/cli.py` | Command line |

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

They build fake installs and fake tools in a temporary folder, so they pass on any machine and never install anything. GitHub Actions runs them on Windows, macOS and Linux.

## License

[MIT](LICENSE) © 2026 Daniel Margolis

---

<p align="center">Made by <b>Daniel Margolis</b></p>
