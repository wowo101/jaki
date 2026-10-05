"""serving_state – what the box's serving tier is doing, read the same way by every script that asks.

    from serving_state import router_addr, call, image_generating, env_list

Used by `engine/infra/model-serving/hatch-image-front` and `hatch-pressure-watch`. The guard
reads the same marker from bash; its `image_generating` is the same test.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.request

GPU_BINARIES = pathlib.Path(os.environ.get("HATCH_GPU_BINARIES")
                            or pathlib.Path(__file__).resolve().parent / "gpu-binaries.env")
MARKER = pathlib.Path(os.environ.get("HATCH_IMAGE_MARKER") or
                      pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
                      / "hatch" / "image-generating")


def env_list(key: str) -> list[str]:
    """A space-separated key from gpu-binaries.env; [] when the file or the key is missing."""
    try:
        m = re.search(rf'^{key}="([^"]*)"', GPU_BINARIES.read_text(), re.M)
    except OSError:
        return []
    return m.group(1).split() if m else []


def router_addr() -> str:
    """The router's host:port – the environment llama-swap passes down, then the machine's file."""
    addr = os.environ.get("HATCH_SERVE_ADDR", "")
    if not addr or "$" in addr:
        try:
            for line in (pathlib.Path.home() / ".config" / "hatch" / "env").read_text().splitlines():
                if line.startswith("HATCH_SERVE_ADDR="):
                    addr = line.split("=", 1)[1].strip()
        except OSError:
            pass
    return "" if "$" in addr else addr


def call(method: str, url: str, timeout: float, body: dict | None = None):
    """(status, body bytes); status None when nothing answered."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                            # noqa: BLE001 - the caller logs it
        return None, str(e).encode()


def image_generating() -> bool:
    """True while the front's marker names a live, unreaped writer."""
    try:
        pid = int(json.loads(MARKER.read_text())["pid"])
        os.kill(pid, 0)
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False
    return state != "Z"


def mem_available_gib() -> float:
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1048576
    raise RuntimeError("no MemAvailable in /proc/meminfo")
