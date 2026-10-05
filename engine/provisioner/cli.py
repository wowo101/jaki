"""hatch CLI — subcommand dispatch.

`doctor` is wired here. `init` / `install` / `status` are registered by their
own tasks (Phase 0.6 / Phase 1) as they land.

Anything that isn't a provisioner subcommand falls through to git-style verb
dispatch: `hatch <verb> …` execs `engine/tools/<verb>/<verb>`. The verbs stay
standalone commands (deployed onto PATH by `hatch install`); this is routing
sugar, not a framework. See specs/integration-patterns.md.
"""
import argparse
import os
import socket

from .doctor import repo_root, run_doctor
from .emit import resolved_env_path
from .init import init_machine
from .install import install

_SECTION_TITLE = {
    "artefacts": "artefacts — what this machine declares",
    "drift": "drift — deployed, claimed by nothing declared",
}


def _cmd_doctor(_args) -> int:
    checks = run_doctor(repo_root(), socket.gethostname())
    ok, section = True, ""
    for c in checks:
        if c.section != section:
            section = c.section
            # A check whose section has no title must still be printed: an
            # unrenderable failing check is a silent one.
            title = _SECTION_TITLE.get(section)
            if title:
                print(f"\n{title}")
        # `·` is the skip mark `install` already uses: a capability this
        # machine does not have is not a fault, so it never fails the run.
        mark = "·" if c.informational else ("✓" if c.ok else "✗")
        print(f"{mark} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        ok = ok and (c.ok or c.informational)
    return 0 if ok else 1


def _cmd_init(_args) -> int:
    dest = init_machine(repo_root())
    print(f"created {dest.relative_to(repo_root())} — review role / endpoints, then run `hatch install`")
    return 0


_STATUS_MARK = {"deployed": "✓", "skipped": "·", "error": "✗"}


def _cmd_install(args) -> int:
    results = install(repo_root(), socket.gethostname(), adopt=args.adopt)
    for r in results:
        mark = _STATUS_MARK.get(r.status, "?")
        print(f"{mark} {r.name}: {r.status}" + (f" — {r.detail}" if r.detail else ""))
    return 0 if all(r.status != "error" for r in results) else 1


def _cmd_status(_args) -> int:
    resolved = resolved_env_path(repo_root())
    if not resolved.exists():
        print("not provisioned yet — run `hatch install`")
        return 1
    print(resolved.read_text(), end="")
    return 0


_SUBCOMMANDS = {"doctor", "init", "install", "status"}


def _verb_exec(name: str):
    """A dispatchable verb is engine/tools/<name>/<name>, executable.

    Only bare names qualify: a path separator or dot-segment would let the
    joined path escape engine/tools/ (pathlib restarts the join on an
    absolute component, so `hatch /bin/sh` would otherwise dispatch).
    """
    if "/" in name or name in {".", ".."}:
        return None
    exe = repo_root() / "engine" / "tools" / name / name
    return exe if exe.is_file() and os.access(exe, os.X_OK) else None


def main(argv) -> int:
    # Verb dispatch first: provisioner subcommands always win, everything
    # else that names an executable verb is exec'd in place (exit code and
    # stdio flow through untouched).
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        verb = _verb_exec(argv[0])
        if verb is not None:
            os.execv(str(verb), [str(verb), *argv[1:]])

    p = argparse.ArgumentParser(
        prog="hatch",
        description="the machine provisioner — plus git-style dispatch to the "
        "Layer-1 verbs (`hatch <verb> …` runs engine/tools/<verb>/<verb>)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="report machine health (read-only)").set_defaults(fn=_cmd_doctor)
    sub.add_parser("init", help="scaffold machines/<hostname>.toml from the template").set_defaults(fn=_cmd_init)
    p_install = sub.add_parser("install", help="deploy verbs + plugins for this machine's role")
    p_install.add_argument("--adopt", action="store_true",
                           help="replace regular files at deploy targets (pre-provisioner "
                                "installs); directories are still refused")
    p_install.set_defaults(fn=_cmd_install)
    sub.add_parser("status", help="show the resolved config for this machine").set_defaults(fn=_cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)
