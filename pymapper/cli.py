"""Command line: `pymapper [ui | list | show | find | kernels | export | tools |
create | remove | install | uninstall | python]`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __author__, __version__, discover, manage

TOP_KINDS = ("conda-root", "base", "missing")


def _scan(args) -> dict:
    print("Scanning...", file=sys.stderr)
    return discover.scan(extra_dirs=args.scan_dir, depth=args.depth, home=not args.no_home)


def _line(rec: dict, prefix: str = "") -> str:
    label = prefix + rec["name"]
    extra = [rec["version"] or "?"]
    if rec["kind"] not in ("base", "missing"):
        extra.insert(0, rec["kind"])
    if rec.get("packages"):
        extra.append(f"{len(rec['packages'])} pkgs")
    text = f"{label:<34} {' | '.join(extra):<28} {rec['path']}"
    for issue in rec.get("issues", []):
        text += f"\n{' ' * len(prefix)}  !! {issue}"
    return text


def cmd_list(data: dict, args):
    envs = data["environments"]
    ids = {e["id"] for e in envs}
    kids, orphans = {}, []
    for e in envs:
        if e["kind"] in TOP_KINDS:
            continue
        if e["parent"] in ids:
            kids.setdefault(e["parent"], []).append(e)
        else:
            orphans.append(e)
    print(f"\nPythonMapper {__version__}: {len(envs)} interpreters/environments, "
          f"{len(data['kernels'])} Jupyter kernels ({data['duration']}s)\n")
    print("INTERPRETERS AND THEIR ENVIRONMENTS")
    for top in (e for e in envs if e["kind"] in TOP_KINDS):
        print(_line(top))
        for child in kids.get(top["id"], []):
            print(_line(child, "  `- "))
            for grandchild in kids.get(child["id"], []):
                print(_line(grandchild, "      `- "))
    if orphans:
        print("\nOTHER ENVIRONMENTS")
        for e in orphans:
            print(_line(e))
    if data["kernels"]:
        print()
        cmd_kernels(data, args)


def cmd_kernels(data: dict, args):
    names = {e["id"]: e["name"] for e in data["environments"]}
    print("JUPYTER KERNELS")
    for k in data["kernels"]:
        where = names.get(k["env"]) or k["note"] or "unknown environment"
        print(f"{k['name']:<24} {k['display_name']:<32} -> {where}")
        for issue in k["issues"]:
            print(f"  !! {issue}")
    if not data["kernels"]:
        print("(none found)")


def _match(data: dict, target: str) -> list:
    t = target.lower()
    items = data["environments"] + data["kernels"]
    exact = [i for i in items if i["id"] == target or discover.norm(i["path"]) == discover.norm(target)]
    return exact or [i for i in items if i["name"].lower() == t or i.get("display_name", "").lower() == t]


def cmd_show(data: dict, args):
    found = _match(data, args.target)
    if len(found) != 1:
        print(f"{'No' if not found else len(found)} matches for {args.target!r}."
              + (" Use the path or id instead:" if found else " Run `pymapper list` to see names."))
        for i in found:
            print(f"  {i['id']}  {i['path']}")
        return 1
    rec = found[0]
    print(json.dumps(rec, indent=2) if args.json else _describe(rec))
    return 0


def _describe(rec: dict) -> str:
    lines = [f"{rec.get('display_name') or rec['name']}  [{rec['kind']}]", f"  path:    {rec['path']}"]
    for key in ("version", "python"):
        if rec.get(key):
            lines.append(f"  {key + ':':<8} {rec[key]}")
    lines += [f"  !! {i}" for i in rec.get("issues", [])]
    if rec.get("argv"):
        lines.append("  command: " + " ".join(rec["argv"]))
    for c in rec.get("commands", []):
        lines.append(f"  {c['label']}: {c['cmd']}")
    if rec.get("packages") is not None and rec["kind"] != "kernel":
        lines.append(f"\n  {len(rec['packages'])} packages:")
        lines += [f"    {p['name']:<36} {p['version']:<16} {p.get('channel') or p['installer']}" for p in rec["packages"]]
    return "\n".join(lines)


def cmd_find(data: dict, args):
    want = discover.canonical(args.package)
    hits, near = [], set()
    for e in data["environments"]:
        for p in e["packages"]:
            name = discover.canonical(p["name"])
            if name == want:
                hits.append((e, p))
            elif want in name:
                near.add(p["name"])
    if not hits:
        print(f"{args.package} is not installed in any environment found.")
        if near:
            print("Similar names: " + ", ".join(sorted(near, key=str.lower)[:15]))
        return 1
    print(f"{hits[0][1]['name']} is installed in {len(hits)} place(s):")
    for e, p in hits:
        print(f"  {p['version']:<14} {e['name']:<30} {e['path']}")
    return 0


def cmd_export(data: dict, args):
    path = Path(args.file)
    if path.suffix.lower() in (".html", ".htm"):
        from .server import render_page
        path.write_text(render_page(data), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {path.resolve()}")


def _target(data: dict, target: str) -> dict:
    found = _match(data, target)
    if len(found) != 1:
        hint = "\n".join(f"  {i['id']}  {i['path']}" for i in found)
        raise manage.ManageError(f"{'No' if not found else len(found)} matches for {target!r}. "
                                 + ("Use the path or id instead:\n" + hint if found else "Run `pymapper list` to see names."))
    return found[0]


def _operate(data: dict, args, op: str, params: dict) -> int:
    """Plan, show, confirm if destructive, then run."""
    plan = manage.make_plan(data, op, params)
    print(plan.describe())
    if plan.confirm and not args.yes:
        try:
            answer = input("Proceed? [y/N] ") if sys.stdin.isatty() else ""
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. (Pass --yes to skip this question.)")
            return 1
    return 0 if manage.run_plan(plan) else 1


def cmd_create(data, args):
    return _operate(data, args, "create", {"name": args.name, "kind": args.type, "python": args.python,
                                           "path": args.path, "packages": args.packages})


def cmd_remove(data, args):
    return _operate(data, args, "remove", {"target": _target(data, args.target)["id"]})


def cmd_install(data, args):
    return _operate(data, args, "upgrade" if args.upgrade else "install",
                    {"target": _target(data, args.target)["id"], "packages": args.packages, "tool": args.tool})


def cmd_uninstall(data, args):
    return _operate(data, args, "uninstall",
                    {"target": _target(data, args.target)["id"], "packages": args.packages, "tool": args.tool})


def cmd_python(data, args):
    if args.action == "install":
        return _operate(data, args, "python-install", {"version": args.what, "via": args.via})
    return _operate(data, args, "python-uninstall", {"target": _target(data, args.what)["id"]})


def cmd_tools(data, args):
    tools = manage.find_tools(data)
    print("Package and Python managers found:")
    for name in ("mamba", "micromamba", "conda", "pymanager", "uv", "pyenv", "brew"):
        if name in tools:
            print(f"  {name:<11} {tools[name]}")
    if not tools:
        print("  (none; packages will be managed with each environment's pip)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scan-dir", action="append", default=[], metavar="DIR",
                        help="extra folder to search for environments (repeatable)")
    common.add_argument("--depth", type=int, default=3, metavar="N",
                        help="how many folder levels to search below home and --scan-dir (default 3)")
    common.add_argument("--no-home", action="store_true", help="don't search your home folder for project envs")

    parser = argparse.ArgumentParser(
        prog="pymapper", epilog=f"PythonMapper {__version__}. Made by {__author__}.",
        description="Find, map and manage every Python interpreter, environment, conda env and Jupyter kernel.")
    parser.add_argument("--version", action="version", version=f"PythonMapper {__version__}, made by {__author__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    ui = sub.add_parser("ui", parents=[common], help="open the interactive map in your browser (default)")
    ui.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    ui.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    lst = sub.add_parser("list", parents=[common], help="print everything found as a tree")
    lst.add_argument("--json", action="store_true", help="print the full scan as JSON")
    sub.add_parser("kernels", parents=[common], help="list Jupyter kernels and where they point")
    show = sub.add_parser("show", parents=[common], help="details and packages of one environment")
    show.add_argument("target", help="name, path or id (see `pymapper list`)")
    show.add_argument("--json", action="store_true")
    find = sub.add_parser("find", parents=[common], help="which environments have a package")
    find.add_argument("package")
    export = sub.add_parser("export", parents=[common], help="save the scan as .json or a shareable .html map")
    export.add_argument("file")
    sub.add_parser("tools", parents=[common], help="show which of mamba/conda/uv/pyenv/... were found")

    confirm = argparse.ArgumentParser(add_help=False)
    confirm.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    create = sub.add_parser("create", parents=[common, confirm], help="create a conda env or venv")
    create.add_argument("name")
    create.add_argument("packages", nargs="*", help="packages to install into it")
    create.add_argument("--python", metavar="VERSION", help="Python version, e.g. 3.12")
    create.add_argument("--type", choices=("conda", "venv"),
                        help="default: conda if mamba/conda is available, else venv")
    create.add_argument("--path", help="where to create it (default: conda's envs folder or ~/.virtualenvs/NAME)")
    remove = sub.add_parser("remove", parents=[common, confirm], help="delete an environment or Jupyter kernel")
    remove.add_argument("target", help="name, path or id")
    tool_help = "default: mamba, then conda, then pip"
    install = sub.add_parser("install", parents=[common, confirm], help="install packages into an environment")
    install.add_argument("target", help="environment name, path or id")
    install.add_argument("packages", nargs="+")
    install.add_argument("--tool", choices=("mamba", "micromamba", "conda", "pip"), help=tool_help)
    install.add_argument("--upgrade", action="store_true", help="upgrade packages that are already installed")
    uninstall = sub.add_parser("uninstall", parents=[common, confirm], help="uninstall packages from an environment")
    uninstall.add_argument("target", help="environment name, path or id")
    uninstall.add_argument("packages", nargs="+")
    uninstall.add_argument("--tool", choices=("mamba", "micromamba", "conda", "pip"),
                           help="default: whichever tool installed each package")
    python = sub.add_parser("python", parents=[common, confirm], help="install or uninstall a Python distribution")
    python.add_argument("action", choices=("install", "uninstall"))
    python.add_argument("what", metavar="VERSION|TARGET", help="version to install, or the Python to uninstall")
    python.add_argument("--via", choices=manage.DISTRO_TOOLS, help="tool to install with (default: first found)")
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "ui")
    args = build_parser().parse_args(argv)

    if args.command == "ui":
        from .server import serve
        serve({"extra_dirs": args.scan_dir, "depth": args.depth, "home": not args.no_home},
              port=args.port, open_browser=not args.no_browser)
        return 0
    data = _scan(args)
    if args.command == "list" and args.json:
        print(json.dumps(data, indent=2))
        return 0
    handler = {"list": cmd_list, "kernels": cmd_kernels, "show": cmd_show, "find": cmd_find,
               "export": cmd_export, "tools": cmd_tools, "create": cmd_create, "remove": cmd_remove,
               "install": cmd_install, "uninstall": cmd_uninstall, "python": cmd_python}[args.command]
    try:
        return handler(data, args) or 0
    except manage.ManageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
