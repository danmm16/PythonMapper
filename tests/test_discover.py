"""Scanner tests against fake installs built in a temporary folder."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from pymapper import discover
from pymapper.server import render_page

WIN = discover.WINDOWS


def write(path: Path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def site(prefix: Path, mm: str = "3.11") -> Path:
    return prefix / "Lib" / "site-packages" if WIN else prefix / "lib" / f"python{mm}" / "site-packages"


def add_dist(prefix: Path, name: str, version: str, mm: str = "3.11"):
    write(site(prefix, mm) / f"{name}-{version}.dist-info" / "METADATA",
          f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: The {name} package\n\nBody text\n")
    write(site(prefix, mm) / f"{name}-{version}.dist-info" / "INSTALLER", "pip\n")


def make_base(prefix: Path, version: str = "3.11.4") -> Path:
    exe = prefix / ("python.exe" if WIN else "bin/python3")
    write(exe)
    write(prefix / "include" / "patchlevel.h", f'#define PY_VERSION "{version}"\n')
    write(prefix / ("Lib/os.py" if WIN else "lib/python3.11/os.py"))
    add_dist(prefix, "pip", "23.2")
    return exe


def make_venv(prefix: Path, home: Path, version: str = "3.11.4"):
    write(prefix / "pyvenv.cfg", f"home = {home}\ninclude-system-site-packages = false\nversion = {version}\n")
    write(prefix / ("Scripts/python.exe" if WIN else "bin/python3"))


def make_conda(prefix: Path, root: bool = False):
    write(prefix / "conda-meta" / "python-3.10.13-h1234.json", json.dumps({"name": "python", "version": "3.10.13"}))
    write(prefix / "conda-meta" / "numpy-1.26.0-py310.json",
          json.dumps({"name": "numpy", "version": "1.26.0", "channel": "https://conda.anaconda.org/conda-forge/win-64"}))
    write(prefix / ("python.exe" if WIN else "bin/python3"))
    if root:
        (prefix / "condabin").mkdir(parents=True)
    add_dist(prefix, "numpy", "1.26.0", "3.10")
    add_dist(prefix, "rich", "13.7.0", "3.10")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self):
        data = discover.scan(extra_dirs=[self.root], depth=3, home=False, system=False)
        json.dumps(data)  # must stay serialisable
        return data

    def env(self, data, path):
        return next(e for e in data["environments"] if discover.norm(e["path"]) == discover.norm(path))

    def test_venv_is_linked_to_its_base_interpreter(self):
        base = self.root / "pythons" / "py311"
        exe = make_base(base)
        venv = self.root / "projects" / "app" / ".venv"
        make_venv(venv, exe.parent)
        add_dist(venv, "Requests", "2.31.0")
        data = self.scan()
        v = self.env(data, venv)
        self.assertEqual(v["kind"], "venv")
        self.assertEqual(v["name"], "app/.venv")
        self.assertEqual(v["version"], "3.11.4")
        self.assertEqual([p["name"] for p in v["packages"]], ["Requests"])
        self.assertEqual(v["packages"][0]["summary"], "The Requests package")
        parent = next(e for e in data["environments"] if e["id"] == v["parent"])
        self.assertEqual(parent["kind"], "base")
        self.assertEqual(parent["version"], "3.11.4")
        self.assertIn("pip", parent["features"])
        self.assertFalse(v["issues"])

    def test_launcher_without_stdlib_is_not_an_interpreter(self):
        launcher = self.root / "PyManager"
        write(launcher / ("python.exe" if WIN else "bin/python3"))
        scanner = discover.Scanner(system=False, home=False)
        self.assertIsNone(scanner.add_base(launcher / ("python.exe" if WIN else "bin/python3")))

    def test_venv_with_deleted_base_points_at_missing_node(self):
        venv = self.root / "old-env"
        make_venv(venv, self.root / "gone" / "bin")
        data = self.scan()
        v = self.env(data, venv)
        ghost = next(e for e in data["environments"] if e["id"] == v["parent"])
        self.assertEqual(ghost["kind"], "missing")
        self.assertTrue(ghost["issues"] and v["issues"])

    def test_conda_root_envs_and_packages(self):
        root = self.root / "miniconda3"
        make_conda(root, root=True)
        make_conda(root / "envs" / "ds")
        data = self.scan()
        base, ds = self.env(data, root), self.env(data, root / "envs" / "ds")
        self.assertEqual(base["kind"], "conda-root")
        self.assertEqual(ds["kind"], "conda")
        self.assertEqual(ds["parent"], base["id"])
        self.assertEqual(ds["version"], "3.10.13")
        by_name = {p["name"]: p for p in ds["packages"]}
        self.assertEqual(by_name["numpy"]["installer"], "conda")  # conda record wins over its dist-info
        self.assertEqual(by_name["numpy"]["channel"], "conda-forge")
        self.assertEqual(by_name["rich"]["installer"], "pip")

    def test_kernels_link_to_environments(self):
        env = self.root / "miniconda3" / "envs" / "ml"
        make_conda(env)
        write(env / "share" / "jupyter" / "kernels" / "python3" / "kernel.json",
              json.dumps({"argv": ["python", "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                          "display_name": "Python 3 (ipykernel)", "language": "python"}))
        write(env / "share" / "jupyter" / "kernels" / "stale" / "kernel.json",
              json.dumps({"argv": [str(self.root / "nope" / "python"), "-m", "ipykernel_launcher"],
                          "display_name": "Stale", "language": "python"}))
        data = self.scan()
        ml = self.env(data, env)
        kernels = {k["name"]: k for k in data["kernels"]}
        self.assertEqual(kernels["python3"]["env"], ml["id"])
        self.assertIn(kernels["python3"]["id"], ml["kernels"])
        self.assertTrue(kernels["stale"]["issues"])

    def test_commands_and_refresh(self):
        venv = self.root / "tool"
        make_venv(venv, self.root / "x")
        add_dist(venv, "ipykernel", "6.29.0")
        rec = self.env(self.scan(), venv)
        labels = [c["label"] for c in rec["commands"]]
        self.assertIn("Register as Jupyter kernel", labels)
        add_dist(venv, "pip", "24.0")
        discover.refresh_packages(rec)
        self.assertIn("pip", rec["features"])

    def test_snapshot_page_escapes_script_end(self):
        page = render_page({"environments": [{"name": "</script><b>"}]})
        self.assertNotIn("</script><b>", page)
        self.assertIn("window.BOOT =", page)


class HelperTests(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(discover.canonical("Zope.Interface_x"), "zope-interface-x")

    def test_channel(self):
        self.assertEqual(discover._channel("https://repo.anaconda.com/pkgs/main/osx-arm64"), "main")
        self.assertEqual(discover._channel("conda-forge"), "conda-forge")

    def test_exe_names(self):
        for name in ("python", "python3", "python3.12", "python.exe", "python3.13t"):
            self.assertTrue(discover.EXE_NAME_RE.match(name), name)
        for name in ("python3-config", "python3.12-config", "pythonw.exe"):
            self.assertFalse(discover.EXE_NAME_RE.match(name), name)


if __name__ == "__main__":
    unittest.main()
