"""Which machine has the GPU, and how a subcommand reaches it.

The lock lives in the GPU machine's tmpfs, so a lease question asked on any other
machine reads a different lock and answers about the wrong box. That does not fail –
it answers plausibly and wrongly: run from the laptop, `status` reported `free` while
the box's lease was held. So the GPU host is resolved, never assumed: `--host` wins,
then HATCH_GPU_HOST, then the HATCH_SERVE_URL host in the machine config `hatch install`
emits. `--local` forces this machine, for callers describing what they can see here.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess

from config import REPO, sh


def _configured_host() -> str:
    try:
        text = (REPO / "engine/.hatch/resolved.env").read_text()
    except OSError:
        return ""
    # HATCH_SERVE_URL first, OLLAMA_URL as the name it had before 2026-09-10. Both are
    # emitted with the same value, so a machine provisioned either side of the rename works.
    m = (re.search(r"^HATCH_SERVE_URL=[a-z]*://([^:/]*)", text, re.M)
         or re.search(r"^OLLAMA_URL=[a-z]*://([^:/]*)", text, re.M))
    return m.group(1) if m else ""


def local_addresses() -> set[str]:
    """Every address this machine answers on, so a config that names the machine by its
    own tailnet IP – which is what resolved.env carries on the box – still reads as
    local, and `status` does not ssh the box to itself."""
    addrs = set()
    for line in sh("ip", "-o", "addr").stdout.splitlines():
        f = line.split()
        if len(f) > 3:
            addrs.add(f[3].split("/")[0])
    return addrs


def is_this_machine(host: str) -> bool:
    if host in ("", "localhost", "127.0.0.1", os.uname().nodename):
        return True
    try:
        resolved = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    return bool(resolved & local_addresses())


def gpu_host(opts) -> str:
    """The remote host, or '' when the GPU is this machine."""
    if opts.local:
        return ""
    host = opts.host or os.environ.get("HATCH_GPU_HOST") or _configured_host()
    return "" if is_this_machine(host) else host


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def delegate(host: str, sub: str, args: list[str]) -> int:
    """Run this same subcommand on the GPU machine. The box's login shell is fish, so
    the payload goes through `bash -s`. Every forwarded argument is single-quoted."""
    payload = (f'exec "$HOME/.local/bin/hatch" gpu-lease {sub} --local'
               f"{''.join(' ' + _sq(a) for a in args)}\n")
    return subprocess.run(["ssh", host, "bash", "-s"], input=payload, text=True).returncode
