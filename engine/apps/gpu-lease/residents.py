"""The standing tier's lifecycle: which residents are up, stop, restore, serve.

Two shapes. A unit-held resident is stopped and started through systemd and asserted
by the process on its port and its /health. A router-held one is reclaimed through
llama-swap's own unload API and brought back by a one-token request, because the
router has no load endpoint and a request is the only thing that proves the entry
serves rather than merely starting.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from config import (HEALTH_TIMEOUT, IMAGE_MARKER, IMAGE_PAUSES, RESIDENTS, ROUTER_ADDR, ROUTER_RESIDENTS, ROUTER_UNIT,
                    Resident, is_active, is_enabled, log, resident, systemctl)
from holders import avail_gib, gtt_gib, kill_pid, pid_on_port, wait_pid_gone


def http(method: str, url: str, timeout: float, body: dict | None = None) -> tuple[int | None, str]:
    """(status, body). Status None means the request got no answer at all."""
    # Imported here, not at the top: urllib.request pulls in http.client, ssl and the
    # email package, ~300 ms that `status --takes-box` – the chat load path – never needs.
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001 – a torn or malformed answer is no answer, never a traceback
        return None, ""


def healthy(r: Resident) -> tuple[bool, str]:
    """llama.cpp answers {"status":"loading model"} for most of a load – assert `ok`."""
    _, body = http("GET", r.health, 5)
    try:
        return json.loads(body).get("status") == "ok", body
    except (ValueError, AttributeError):
        return False, body


# ── the router ───────────────────────────────────────────────────────────────

def router_addr() -> str:
    """Where llama-swap listens, read from the machine and never hardcoded.

    THE MACHINE'S RESOLVED ENV FIRST. Since 2026-09-10 the unit's ExecStart says
    `--listen ${HATCH_SERVE_ADDR}` so a second machine can install it unedited, and
    systemd reports argv UNEXPANDED — so reading ExecStart yields that literal string,
    which is truthy and passes every `if not addr` guard downstream. What that cost,
    measured on the box: `--resident stop` POSTed its unload to a host called
    `${HATCH_SERVE_ADDR}`, got nothing, logged "its memory may still be committed" and
    handed an eval a box still holding 74 GiB.

    The ExecStart read stays as the fallback for a machine provisioned before that
    change, where the address really is in the line. A value that still contains `$` is
    not an address and is discarded, so an unresolvable one reads as empty and the
    callers' existing guards do their job.
    """
    if ROUTER_ADDR:
        return ROUTER_ADDR
    try:
        for line in (Path.home() / ".config" / "hatch" / "env").read_text().splitlines():
            if line.startswith("HATCH_SERVE_ADDR="):
                addr = line.split("=", 1)[1].strip()
                if addr and "$" not in addr:
                    return addr
    except OSError:
        pass
    words = systemctl("show", ROUTER_UNIT, "-p", "ExecStart", "--value").stdout.split()
    hits = [words[i + 1] for i, w in enumerate(words[:-1]) if w == "--listen"]
    addr = hits[-1] if hits else ""
    return "" if "$" in addr else addr


def router_loaded() -> list[str] | None:
    """Which router-held standing models are loaded, as `router:<id>`, asked of the
    router's own /running.

    Three states, not two. None means COULD NOT ASK – a router slow past the timeout
    or mid-restart – and callers must branch on it: `--resident stop` records the
    loaded set before unloading, so silence read as "nothing loaded" takes the box's
    models away and never returns them. A STOPPED router is knowledge and answers []:
    its children died with it, nothing is loaded, nothing is owed back. Reporting it as
    "could not ask" made a lease invent residents the box was deliberately running
    without, and each phantom then cost the restore a full HEALTH_TIMEOUT.
    """
    if not ROUTER_RESIDENTS or not is_active(ROUTER_UNIT):
        return []
    addr = router_addr()
    if not addr:
        return None
    status, body = http("GET", f"http://{addr}/running", 10)
    # An empty body is also "could not ask": llama-swap answers {"running":[]} when it
    # holds nothing, so nothing legitimate reads as the empty string.
    if status != 200 or not body:
        return None
    try:
        running = {m.get("model") for m in json.loads(body).get("running", [])}
    except (ValueError, AttributeError, TypeError):
        return None
    if image_generating():
        running |= set(IMAGE_PAUSES)
    return [f"router:{i}" for i in ROUTER_RESIDENTS if i in running]


def image_generating() -> bool:
    """hatch-image-front's marker, counted only while its writer is alive and not a zombie."""
    try:
        pid = int(json.loads(IMAGE_MARKER.read_text())["pid"])
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def router_load(swap_id: str) -> bool:
    """Ask the router to load one standing model and wait until it answers. One token,
    so the wait is the load and not the generation."""
    addr = router_addr()
    if not addr:
        log(f"!! {ROUTER_UNIT} is up but its --listen address could not be read – cannot load {swap_id}")
        return False
    log(f"loading {swap_id} through the router…")
    deadline = time.monotonic() + HEALTH_TIMEOUT
    code = None
    while time.monotonic() < deadline:
        code, _ = http("POST", f"http://{addr}/v1/chat/completions", 120,
                       {"model": swap_id, "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1})
        if code == 200:
            log(f"{swap_id} serving")
            return True
        time.sleep(5)
    log(f"!! {swap_id} did not answer within {HEALTH_TIMEOUT}s (last HTTP {code or 'no response'})")
    return False


def unload_router_models() -> None:
    """`--resident stop` means "I want the box", so the router is asked to let go of
    whatever it holds through its own API – never by killing its children, which
    would leave it believing a model is still up."""
    if not is_active(ROUTER_UNIT):
        return
    addr = router_addr()
    if not addr:
        log(f"!! {ROUTER_UNIT} is up but its --listen address could not be read — not asking it to unload")
        return
    before = gtt_gib()
    code, _ = http("POST", f"http://{addr}/api/models/unload", 60)
    if code != 200:
        # Loud, and not fatal: the router may be mid-swap or a version without the
        # endpoint. The caller still gets its residents stopped.
        log(f"!! asked the chat surface to unload and it answered {code or 'no response'} "
            "— its memory may still be committed")
        return
    time.sleep(2)
    log(f"asked the chat surface to unload: GTT {before} → {gtt_gib()} GiB")


# ── the tier ─────────────────────────────────────────────────────────────────

def active_residents() -> list[str]:
    """Which residents are SERVING: router-held ones by /running, unit-held ones by the
    unit or by a live process on the resident's port.

    The second arm matters because the documented failure mode leaves exactly that
    state: a podman-exec'd llama-server survives its unit's stop still holding
    ~110 GiB, so the unit reads inactive while the GPU is very much occupied.
    """
    loaded = router_loaded()
    if loaded is None:
        log(f"!! could not ask {ROUTER_UNIT} what it is holding – assuming all of")
        log(f"   {' '.join(ROUTER_RESIDENTS) or 'none'} so this lease owes them back")
        loaded = [f"router:{i}" for i in ROUTER_RESIDENTS]
    units = [r.key for r in RESIDENTS if is_active(r.unit) or pid_on_port(r.port)]
    return loaded + units


def stop_residents(stopped: list[str]) -> None:
    """Takes down the set the caller has already recorded, so a holder killed
    part-way through still owes back the set it was taking rather than a guess."""
    n = 0
    for entry in stopped:
        # A router-held model was already reclaimed by unload_router_models. It is in
        # the recorded set so that the release gives it BACK; nothing to stop here.
        if entry.startswith("router:"):
            n += 1
            continue
        r = resident(entry)
        log(f"stopping resident {r.unit} (:{r.port})…")
        systemctl("stop", r.unit)
        # Assert the process is gone, not that systemd said so.
        pid = pid_on_port(r.port)
        if pid and not wait_pid_gone(pid, 60):
            log(f"resident pid {pid} outlived its unit — killing it directly")
            kill_pid(pid)
        n += 1
    if n:
        log(f"stopped {n} resident(s); MemAvailable {avail_gib()} GiB, GTT {gtt_gib()} GiB")
    else:
        log("no resident was running")


def start_residents(entries: list[str]) -> bool:
    """Give back a recorded set: a router id through a one-token load, a unit through
    systemd and its /health. Returns False when any of them did not come back."""
    ok = True
    for entry in entries:
        if not entry or entry == "none":
            continue
        if entry.startswith("router:"):
            ok &= router_load(entry[len("router:"):])
            continue
        r = resident(entry)
        log(f"restoring {r.unit} (:{r.port})…")
        systemctl("start", r.unit)
        deadline = time.monotonic() + HEALTH_TIMEOUT
        up, body = False, ""
        while time.monotonic() < deadline:
            up, body = healthy(r)
            if up:
                log(f"{r.unit} healthy: {body}")
                break
            if not is_active(r.unit):
                log(f"!! {r.unit} died while loading")
                break
            time.sleep(5)
        if not up:
            log(f"!! {r.unit} did not become healthy within {HEALTH_TIMEOUT}s (last: {body or 'no answer'})")
            ok = False
    return ok


def serve_residents() -> bool:
    """Maintain the intended steady state rather than deciding it: a resident that is
    enabled (the human's gesture in this repo) or already active is brought up and left
    up; one that is neither is not the box's business today and is not started."""
    ok, want = True, 0
    # The router's standing models first: a router entry with ttl 0 stays down until
    # something asks for it, which would otherwise be the caller, paying the cold load.
    if ROUTER_RESIDENTS:
        if is_active(ROUTER_UNIT):
            # A failed ask sends every id through router_load – idempotent, and the
            # right direction for `serve`: its job is that they are up.
            loaded = router_loaded() or []
            for swap_id in ROUTER_RESIDENTS:
                want += 1
                if f"router:{swap_id}" in loaded:
                    log(f"resident {swap_id} already loaded in the router")
                else:
                    ok &= router_load(swap_id)
        else:
            log(f"!! {ROUTER_UNIT} is not running, so its standing models "
                f"({' '.join(ROUTER_RESIDENTS)}) cannot be served")
            ok = False
    for r in RESIDENTS:
        if is_active(r.unit):
            want += 1
            # Active is not healthy: a unit mid-load answers "loading model" for most
            # of a minute, and a consumer told "serving" would post into that.
            if healthy(r)[0]:
                log(f"resident {r.unit} already serving (:{r.port})")
            else:
                log(f"resident {r.unit} is active but not yet answering — waiting")
                ok &= start_residents([r.key])
        elif is_enabled(r.unit):
            want += 1
            ok &= start_residents([r.key])
        else:
            log(f"resident {r.unit} is neither enabled nor active — not started")
    if not want:
        log("!! --resident serve brought nothing up: no resident unit is enabled or active.")
        ok = False
    return ok
