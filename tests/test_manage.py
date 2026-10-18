"""Planning and running management operations against fake installs."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from pymapper import discover, manage
from test_discover import WIN, add_dist, make_base, make_conda, make_venv, write

NO_TOOLS = {}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self._tmp.name))
        self.base_exe = make_base(self.root / "py311")
        self.venv = self.root / "proj" / ".venv"
        make_venv(self.venv, self.base_exe.parent)
        add_dist(self.venv, "pip", "24.0")
        add_dist(self.venv, "requests", "2.31.0")
        self.conda_root = self.root / "miniconda3"
        make_conda(self.conda_root, root=True)
        make_conda(self.conda_root / "envs" / "ds")
        self.mamba = self.conda_root / ("Library/bin/mamba.exe" if WIN else "bin/mamba")
        write(self.mamba)
        write(self.conda_root / ("Scripts/conda.exe" if WIN else "bin/conda"))
        self.data = discover.scan(extra_dirs=[self.root], home=False, system=False)
        # Only the fake tools, so a real mamba/conda on the test machine can't change the result.
        self.tools = {k: v for k, v in manage.find_tools(self.data).items()
                      if discover.norm(v).startswith(discover.norm(self.root))}

    def tearDown(self):
        self._tmp.cleanup()

    def rec(self, path):
        return next(e for e in self.data["environments"] if discover.norm(e["path"]) == discover.norm(path))

    def plan(self, op, **params):
        return manage.make_plan(self.data, op, params, self.tools)

    def test_mamba_from_the_conda_install_is_preferred(self):
        ds = self.rec(self.conda_root / "envs" / "ds")
        plan = self.plan("install", target=ds["id"], packages="scipy")
        self.assertEqual(plan.steps[0].cmd[0], str(self.mamba))
        self.assertIn("pip", plan.steps[0].fallback[-1].cmd)  # pip if conda channels lack it

    def test_forced_tools(self):
        ds = self.rec(self.conda_root / "envs" / "ds")
        plan = self.plan("install", target=ds["id"], packages="scipy", tool="pip")
        self.assertEqual(plan.steps[-1].cmd[1:4], ["-m", "pip", "install"])
        plan = self.plan("install", target=ds["id"], packages="scipy", tool="conda")
        self.assertTrue(plan.steps[0].cmd[0].lower().startswith(str(self.conda_root).lower()))
        self.assertIn("conda", Path(plan.steps[0].cmd[0]).name)
        with self.assertRaises(manage.ManageError):
            self.plan("install", target=self.rec(self.venv)["id"], packages="scipy", tool="mamba")

    def test_venvs_use_pip(self):
        plan = self.plan("install", target=self.rec(self.venv)["id"], packages="flask==3.0", upgrade=True)
        self.assertEqual(plan.steps[-1].cmd[1:5], ["-m", "pip", "install", "flask==3.0"])

    def test_uninstall_uses_the_installer_of_each_package(self):
        ds = self.rec(self.conda_root / "envs" / "ds")
        plan = self.plan("uninstall", target=ds["id"], packages="numpy rich")
        self.assertEqual(plan.steps[0].cmd[0], str(self.mamba))
        self.assertIn("numpy", plan.steps[0].cmd)
        self.assertIn("rich", plan.steps[1].cmd)
        self.assertTrue(plan.destructive)
        with self.assertRaises(manage.ManageError):
            self.plan("uninstall", target=ds["id"], packages="notthere")

    def test_rejects_options_paths_and_urls(self):
        target = self.rec(self.venv)["id"]
        for bad in ("--index-url=http://x", "../evil", "pkg@http://x/a.whl", "C:\\x.whl"):
            with self.assertRaises(manage.ManageError, msg=bad):
                self.plan("install", target=target, packages=bad)

    def test_create_conda_and_venv(self):
        plan = self.plan("create", name="exp", python="3.12", packages="numpy")
        self.assertEqual(plan.steps[0].cmd[:5], [str(self.mamba), "create", "-y", "-n", "exp"])
        self.assertIn("python=3.12", plan.steps[0].cmd)
        target = self.root / "envs" / "web"
        plan = self.plan("create", name="web", kind="venv", path=str(target), packages="flask")
        self.assertEqual(plan.steps[0].cmd, [str(self.base_exe), "-m", "venv", str(target)])
        self.assertIn("flask", plan.steps[1].cmd)
        with self.assertRaises(manage.ManageError):
            self.plan("create", name="bad name!")
        with self.assertRaises(manage.ManageError):
            self.plan("create", name="x", kind="venv", python="2.7")

    def test_remove_plans(self):
        plan = self.plan("remove", target=self.rec(self.venv)["id"])
        self.assertEqual(plan.steps[0].delete, str(self.venv))
        ds = self.rec(self.conda_root / "envs" / "ds")
        plan = self.plan("remove", target=ds["id"])
        self.assertEqual(plan.steps[0].cmd[1:5], ["env", "remove", "-y", "-p"])
        with self.assertRaises(manage.ManageError):
            self.plan("remove", target=self.rec(self.conda_root)["id"])

    def test_python_install_needs_a_tool(self):
        with self.assertRaises(manage.ManageError):
            manage.make_plan(self.data, "python-install", {"version": "3.13"}, {})
        plan = manage.make_plan(self.data, "python-install", {"version": "3.13"}, {"uv": "uv"})
        self.assertEqual(plan.steps[0].cmd, ["uv", "python", "install", "3.13"])
        self.assertTrue(plan.confirm)

    def test_python_uninstall_of_conda_install(self):
        plan = self.plan("python-uninstall", target=self.rec(self.conda_root)["id"])
        self.assertTrue(plan.destructive)
        self.assertTrue(plan.notes)  # warns that the envs inside go too


class RunTests(unittest.TestCase):
    def test_fallback_runs_when_conda_lacks_the_package(self):
        fail = [sys.executable, "-c", "print('PackagesNotFoundError: nope'); raise SystemExit(1)"]
        ok = [sys.executable, "-c", "print('installed with pip')"]
        out = []
        plan = manage.Plan("t", [manage.Step("conda", fail, fallback=[manage.Step("pip", ok)])])
        self.assertTrue(manage.run_plan(plan, out.append))
        self.assertIn("installed with pip", "".join(out))

    def test_failure_without_fallback_stops(self):
        fail = [sys.executable, "-c", "raise SystemExit(3)"]
        never = [sys.executable, "-c", "print('should not run')"]
        out = []
        plan = manage.Plan("t", [manage.Step("a", fail), manage.Step("b", never)])
        self.assertFalse(manage.run_plan(plan, out.append))
        self.assertNotIn("should not run", "".join(out))

    def test_delete_step_removes_a_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "a" / "env"
            make_venv(venv, Path(tmp))
            (venv / "readonly.txt").write_text("x")
            os.chmod(venv / "readonly.txt", 0o444)
            self.assertTrue(manage.run_plan(manage.Plan("t", [manage.Step("rm", delete=str(venv))]), lambda s: None))
            self.assertFalse(venv.exists())


if __name__ == "__main__":
    unittest.main()
