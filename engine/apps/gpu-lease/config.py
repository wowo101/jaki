"""Paths, the standing tier, the knobs, and the few helpers every module needs.

Importing this module reads `engine/lib/gpu-binaries.env` and exits 1 when it cannot,
or when neither standing-tier key names anything – the two states in which the
arbiter cannot tell an idle box from one holding 90 GiB.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

LEASE_UNAVAILABLE = 75


def log(msg: str) -> None:
    print(f"gpu-lease: {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    log(msg)
    sys.exit(code)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def sh(*argv: str) -> subprocess.CompletedProcess:
    """Run a command, never raise: a missing binary reads as a failed call."""
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return subprocess.CompletedProcess(argv, 1, "", "")


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return sh("systemctl", "--user", *args)


def is_active(unit: str) -> bool:
    return systemctl("is-active", "--quiet", unit).returncode == 0


def is_enabled(unit: str) -> bool:
    return systemctl("is-enabled", "--quiet", unit).returncode == 0


# ── durations ────────────────────────────────────────────────────────────────

_DURATION = re.compile(r"^[0-9]+[smhd]?$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def valid_duration(d: str) -> bool:
    return bool(_DURATION.match(d))


def to_seconds(d: str) -> int:
    """Bare seconds, or 90s / 45m / 8h / 1d."""
    if not valid_duration(d):
        die(f"not a duration: {d} (use 90s, 45m, 8h)")
    return int(d.rstrip("smhd")) * _UNIT_SECONDS.get(d[-1], 1)


def human_dur(s: int) -> str:
    s = max(int(s), 0)
    h, m = divmod(s, 3600)
    m, sec = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{s}s"


# ── state on tmpfs ───────────────────────────────────────────────────────────
# HATCH_GPU_RUNDIR exists so the test harness can hold its own lock instead of
# fighting the live one; nothing else should set it.

RUNDIR = Path(os.environ.get("HATCH_GPU_RUNDIR")
              or f"{os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'}/hatch")
LOCK = RUNDIR / "gpu.lock"
SIDECAR = RUNDIR / "gpu.lease"
WAITD = RUNDIR / "gpu.wait.d"     # who is waiting for the lease
USERSD = RUNDIR / "gpu.users.d"   # who is using the resident without holding the lease
# A restore that did not bring the residents back. The lease's own log line dies with its
# transient unit, so the failure needs a file that outlives it: `status` reads this, the
# taskboard reads `status --json`, and the next successful restore removes it. Without it a
# lease can leave the box with no resident, exit 0, and say so only in a journal nobody greps.
RESTORE_FAILED = RUNDIR / "gpu.restore-failed"
POLL_WAIT_STALE = 600             # a polling waiter counts as live if it retried this recently
RUNDIR.mkdir(parents=True, exist_ok=True)

# The repo root, from this file's fixed depth (engine/apps/gpu-lease); a copy elsewhere
# needs HATCH_REPO.
REPO = Path(os.environ.get("HATCH_REPO") or Path(__file__).resolve().parents[3])

# ── the shared holder definition ─────────────────────────────────────────────
# Who holds the GPU, which holders are the chat surface's own, and which are standing
# residents: all defined once, in engine/lib/gpu-binaries.env, and read by this app,
# hatch-serving-guard and the taskboard. A name missing from it is invisible to all
# three – no refusal, no listing, no sweep. Add names there, never here.

GPU_BINARIES = Path(os.environ.get("HATCH_GPU_BINARIES") or REPO / "engine/lib/gpu-binaries.env")


def _read_env_file(path: Path) -> dict[str, str]:
    """Plain KEY="value" lines, no expansion, no logic – the file's own contract."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^([A-Z_][A-Z0-9_]*)="(.*)"$', line.strip())
        if m:
            values[m.group(1)] = m.group(2)
    return values


try:
    _file = _read_env_file(GPU_BINARIES)
except OSError:
    die(f"cannot read {GPU_BINARIES} — it defines what holds the GPU")

GPU_PROC = os.environ.get("HATCH_GPU_PROC") or _file.get("HATCH_GPU_SERVERS", "")
CHAT_CONTAINERS = _file.get("HATCH_CHAT_CONTAINERS", "").split()
# Router models hatch-image-front unloads for the length of an image, and the marker it holds
# meanwhile. A paused model counts as loaded while the marker's writer lives: a `--resident
# stop` lease taken mid-image must record it among what it gives back.
IMAGE_PAUSES = _file.get("HATCH_IMAGE_PAUSES", "").split()
IMAGE_MARKER = Path(os.environ.get("HATCH_IMAGE_MARKER") or
                    Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
                    / "hatch" / "image-generating")


class Resident(namedtuple("Resident", "unit port gguf")):
    """A standing model with its own systemd unit, from `unit:port:gguf:container`.

    The gguf is a label, so a refusal can say "one of ours, unleased" instead of "a
    stranger"; the port is what identifies a resident running inside a container,
    where no unit name appears in the cgroup. Trailing fields may be empty.
    """
    __slots__ = ()

    @property
    def key(self) -> str:
        return f"{self.unit}:{self.port}"

    @property
    def health(self) -> str:
        return f"http://127.0.0.1:{self.port}/health"


def resident(entry: str) -> Resident:
    unit, port, gguf, *_ = entry.split(":") + ["", ""]
    return Resident(unit, port, gguf)


def _standing_tier() -> tuple[list[Resident], list[str]]:
    """The two shapes, with an EXPLICITLY EMPTY environment override beating the file.

    "This machine has no resident units" is a real thing to say – it is what the box
    says since 2026-09-08 – and a `:-` default made it indistinguishable from saying
    nothing, so the file won and the harness asserted against production ids.
    Precedence for the unit shape: HATCH_RESIDENTS, then HATCH_RESIDENT_UNIT/PORT/GGUF
    (a single-resident machine and the harness), then the file. The router shape takes
    the environment, then the file.
    """
    env = os.environ
    if "HATCH_RESIDENTS" in env:
        units = env["HATCH_RESIDENTS"].split()
    elif env.get("HATCH_RESIDENT_UNIT"):
        units = [f"{env['HATCH_RESIDENT_UNIT']}:{env.get('HATCH_RESIDENT_PORT') or 8081}"
                 f":{env.get('HATCH_RESIDENT_GGUF', '')}"]
    else:
        units = _file.get("HATCH_RESIDENTS", "").split()
    router = (env["HATCH_ROUTER_RESIDENTS"] if "HATCH_ROUTER_RESIDENTS" in env
              else _file.get("HATCH_ROUTER_RESIDENTS", "")).split()
    return [resident(u) for u in units], router


RESIDENTS, ROUTER_RESIDENTS = _standing_tier()
if not RESIDENTS and not ROUTER_RESIDENTS:
    die("no standing models defined – set HATCH_RESIDENTS or HATCH_ROUTER_RESIDENTS, "
        f"or check {GPU_BINARIES}")

# The router: since 2026-09-08 its children are the whole serving tier – the resident,
# the delegate and the image server, all behind one address.
ROUTER_UNIT = os.environ.get("HATCH_ROUTER_UNIT") or "llama-swap.service"
# What a router-held holder is called wherever one is printed. Sanctioned use: it never
# refuses a lease and is never swept, because `--resident stop` reclaims all of it
# through the router's own unload API, and the non-standing entries self-release on ttl.
SANCTIONED_KIND = "the router (standing tier, or unloads on ttl)"
# Overridable for the harness only – a test must never POST unload to a live router.
ROUTER_ADDR = os.environ.get("HATCH_ROUTER_ADDR", "")
# A cold load runs to minutes; the timeout exists so a wedged load reports rather than
# hangs. HATCH_HEALTH_TIMEOUT is for the harness.
HEALTH_TIMEOUT = int(os.environ.get("HATCH_HEALTH_TIMEOUT") or 300)
GTT_FILE = Path("/sys/class/drm/card1/device/mem_info_gtt_used")

# Units this app never stops, however it attributes a process to them.
# `user@N.service` is the whole user session: sweeping it would log the user out.
NEVER_STOP = re.compile(r"^(user@[0-9]+\.service|init\.scope|llama-swap\.service|"
                        r"open-webui\.service|dbus\.service)$")
