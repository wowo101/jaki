"""Who owns the GPU, by artefact – and the process primitives everything above builds on.

Attribution mirrors hatch-serving-guard: what a competitor IS – its cgroup, its
command line – never what its unit is called. A hand-maintained unit-name list is the
failure this replaces: the two newest probe scripts each omitted the other's unit.
"""

from __future__ import annotations

import os
import signal
import time
from collections import namedtuple
from pathlib import Path

from config import (CHAT_CONTAINERS, GPU_PROC, GTT_FILE, RESIDENTS, ROUTER_UNIT, SANCTIONED_KIND,
                    is_active, sh)


def gtt_gib() -> str:
    try:
        return f"{int(GTT_FILE.read_text()) / 1073741824:.1f}"
    except (OSError, ValueError):
        return "?"


def avail_gib() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1048576
    except OSError:
        pass
    return 0


# ── processes ────────────────────────────────────────────────────────────────

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_pid_gone(pid: int, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    return True


def kill_pid(pid: int) -> None:
    """TERM, then KILL, each waited for."""
    for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 30)):
        try:
            os.kill(pid, sig)
        except OSError:   # gone, or another uid's: the cleanup must carry on either way
            return
        if wait_pid_gone(pid, grace):
            return


def gpu_pids() -> list[int]:
    """Every process whose comm matches the holder definition (an ERE alternation)."""
    return [int(p) for p in sh("pgrep", "-x", GPU_PROC).stdout.split()]


def cgroup_of(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None


def unit_of_pid(pid: int) -> str:
    """The innermost *.service or *.scope in the pid's cgroup path, or ''."""
    for line in (cgroup_of(pid) or "").splitlines():
        for part in reversed(line.split("/")):
            if part.endswith((".service", ".scope")):
                return part
    return ""


def cmdline(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def arg_after(pid: int, *flags: str) -> str:
    """The value of the LAST occurrence of a flag, `--flag value` or `--flag=value`.

    Flags are tried in order and the first one that carries a value wins. A flag with
    nothing after it is not a value.
    """
    args = cmdline(pid)
    for flag in flags:
        eq = [a[len(flag) + 1:] for a in args if a.startswith(flag + "=")]
        if eq:
            return eq[-1]
        hits = [i for i, a in enumerate(args) if a == flag]
        if hits and hits[-1] + 1 < len(args) and args[hits[-1] + 1] != flag:
            return args[hits[-1] + 1]
    return ""


def port_of_pid(pid: int) -> str:
    # sd-server spells it --listen-port; llama-server spells it --port.
    return arg_after(pid, "--port", "--listen-port")


def pid_on_port(port: str) -> int | None:
    """A live GPU process bound to a resident's port – the artefact, not the unit."""
    return next((pid for pid in gpu_pids() if port and port_of_pid(pid) == port), None)


def model_of_pid(pid: int) -> str:
    # --diffusion-model is sd-server's split-weights form, which carries no -m.
    p = arg_after(pid, "-m", "--model", "--diffusion-model")
    return Path(p).name if p else ""


# ── holders ──────────────────────────────────────────────────────────────────

def chat_container_ids() -> list[str]:
    # `podman inspect` with no arguments is an error, not an empty answer.
    if not CHAT_CONTAINERS:
        return []
    return [l.strip() for l in sh("podman", "inspect", "--format", "{{.Id}}", *CHAT_CONTAINERS).stdout.splitlines()
            if l.strip()]


def holder_kind(model: str) -> str:
    """One of ours – a resident model, wherever it happens to run – or a stranger."""
    if model and any(r.gguf and r.gguf == model for r in RESIDENTS):
        return "a resident model"
    return "a stranger"


class Holder(namedtuple("Holder", "pid unit port model kind")):
    __slots__ = ()

    @property
    def sanctioned(self) -> bool:
        return self.kind == SANCTIONED_KIND

    def describe(self) -> str:
        return (f"    {self.kind} — pid {self.pid}  unit {self.unit or '–'}  "
                f"port {self.port or '?'}  model {self.model or '?'}")


def foreign_holders() -> list[Holder]:
    """Every GPU-holding process that is not a standing resident – the router's own
    included, labelled rather than omitted.

    The router reaches the GPU in two shapes: a toolbox entry lands in its CONTAINER's
    cgroup, a host-binary entry in llama-swap's own. One tier, one treatment. An
    omitted holder is not "not a violation", it is invisible: absent from `status`,
    from the refusal that names what holds memory, and from the picture of why the
    box is full.
    """
    ours = chat_container_ids()
    holders = []
    for pid in gpu_pids():
        cg = cgroup_of(pid)
        if cg is None:
            continue
        sanctioned = any(cid in cg for cid in ours) or f"/{ROUTER_UNIT}" in cg
        unit = unit_of_pid(pid)
        port = port_of_pid(pid)
        # A resident reaches the GPU through `toolbox run`, so its server sits in the
        # container's cgroup rather than the unit's; its port identifies it there.
        if any(unit == r.unit or (port and port == r.port and is_active(r.unit)) for r in RESIDENTS):
            continue
        model = model_of_pid(pid)
        holders.append(Holder(pid, unit, port, model, SANCTIONED_KIND if sanctioned else holder_kind(model)))
    return holders


def describe(holders: list[Holder]) -> str:
    return "\n".join(h.describe() for h in holders)
