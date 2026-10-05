"""The lease itself: lock, sidecar, demand registrations, sweep, inhibitor, take and
finish.

The lock is the only truth about whether a lease is held. The sidecar is a label: a
killed holder leaves it behind, which reads as STALE, never as held. Registrations are
advisory visibility, never scheduling – flock still decides who runs next.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
from pathlib import Path

import residents
from config import (LEASE_UNAVAILABLE, LOCK, NEVER_STOP, POLL_WAIT_STALE, RESTORE_FAILED,
                    SIDECAR, USERSD, WAITD, die, human_dur, log, sh, systemctl, to_seconds)
from holders import (avail_gib, describe, foreign_holders, gtt_gib, kill_pid, pid_alive,
                     unit_of_pid, wait_pid_gone)

# ── files ────────────────────────────────────────────────────────────────────

def read_kv(path: Path) -> dict[str, str]:
    """key=value lines; the FIRST occurrence of a key wins, so an injected duplicate
    cannot override. {} when the file is absent."""
    values: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            values.setdefault(k, v)
    except OSError:
        pass
    return values


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def oneline(s: str) -> str:
    """A newline in a value would inject a whole field – a --why carrying
    `\\nexpires=9999999999` would sit on the board for ever."""
    return "".join(c for c in s if ord(c) >= 32)


def files_in(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def registrations(d: Path) -> list[dict[str, str]]:
    return [read_kv(p) for p in files_in(d)]


# ── lock ─────────────────────────────────────────────────────────────────────
# Python opens files close-on-exec, so nothing the arbiter spawns inherits the lock:
# an orphan of the job cannot keep the lease. A `pass_fds` or `os.set_inheritable` on
# it is reintroducing exactly that bug.

def lock_is_held() -> bool:
    try:
        with open(LOCK, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
    except OSError:
        return False


def acquire_lock(wait_s: int | None):
    """The open lock file, held for as long as the returned object lives, or None
    once the wait is over. None waits for ever, 0 does not wait at all."""
    try:
        f = open(LOCK, "w")
    except OSError:
        die(f"cannot open {LOCK}")
    deadline = None if wait_s is None else time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.5)


# ── sidecar ──────────────────────────────────────────────────────────────────

def write_sidecar(who: str, why: str, mode: str, resident: str, ttl: str, baseline: str) -> None:
    write_private(SIDECAR, f"who={who}\nwhy={oneline(why)}\nmode={mode}\nresident={resident}\n"
                           f"ttl={ttl}\npid={os.getpid()}\nunit={unit_of_pid(os.getpid())}\n"
                           f"since={int(time.time())}\nbaseline={baseline}\n")


def sidecar_append(line: str) -> None:
    if SIDECAR.exists():
        with open(SIDECAR, "a") as f:
            f.write(line + "\n")


@contextlib.contextmanager
def restoring_window():
    """THE RESTORE GOES THROUGH THE GUARD, and the guard refuses while a `--resident
    stop` lease holds the box – which would be this lease refusing its own restore.
    Measured 2026-09-08: the unload was correct and both reloads then 500'd for the
    full timeout, leaving the box empty.

    The window is marked in the sidecar rather than papered over by releasing the lock
    early, which would leave the box unprotected for the whole reload. It is NOT short:
    up to HEALTH_TIMEOUT per id. `status --takes-box` answers `no` while the mark is
    set – a lease that is handing the box back does not have it – and the guard's
    memory check still rules on anything else arriving in the window. Cleared however
    the restore ended: a restore that failed is still over.
    """
    sidecar_append("restoring=1")
    try:
        yield
    finally:
        if SIDECAR.exists():
            SIDECAR.write_text("".join(f"{l}\n" for l in SIDECAR.read_text().splitlines()
                                       if l != "restoring=1"))


def record_restore(ok: bool, owed: list[str], who: str = "", why: str = "") -> bool:
    """Leave the failure somewhere that outlives the transient unit, or clear it.

    `start_residents` already returns False when a resident did not come back, and the
    caller used to drop it: the lease then unlinked its sidecar, logged `lease released`
    and exited with the job's own status, leaving a box with no resident and the only
    trace in a `--collect`ed unit's journal. Exit status stays the JOB's – that is the
    caller's contract – so the box's state gets its own channel instead.

    AN EMPTY OWED SET CLEARS NOTHING. `start_residents([])` returns True by having no work
    to do, and after a failed restore the box IS empty, so the next `--resident stop` lease
    records an empty stopped set and would delete the alarm without restoring anything.
    Only a restore that actually brought something back is evidence the box is populated.
    """
    ids = " ".join(oneline(i) for i in owed if i and i != "none")
    if ok:
        if ids:
            RESTORE_FAILED.unlink(missing_ok=True)
        return True
    log(f"!! THE BOX HAS NO RESIDENT: {ids or 'the recorded set'} did not come back. "
        f"Recorded in {RESTORE_FAILED}; `hatch gpu-lease restore` retries it.")
    # write_private and oneline for the same reasons the sidecar uses them: `status` polls
    # this file, and a `--why` carrying a newline would inject an empty `owed=` that
    # read_kv's first-key-wins then hands to the retry as "nothing recorded".
    write_private(RESTORE_FAILED,
                  f"who={oneline(who)}\nwhy={oneline(why)}\nowed={ids}\nwhen={int(time.time())}\n")
    return False


def human_age(since: str | None) -> str:
    if not since or not since.isdigit():
        return "unknown"
    return human_dur(int(time.time()) - int(since))


def holder_desc() -> str:
    """Who the sidecar says holds it – only ever used to phrase a message."""
    s = read_kv(SIDECAR)
    if s.get("who"):
        return f"{s['who']} ({s.get('why') or '?'}, {human_age(s.get('since'))} ago)"
    return "an untagged holder"


# ── demand registration ──────────────────────────────────────────────────────
# One file per consumer, tmpfs, so poweroff clears the demand picture with the lease.

def reg_write(d: Path, who: str, kind: str, why: str, extra: dict[str, str] | None = None) -> None:
    """Write-then-rename, because `status` is polled continuously and a truncated file
    reads as a registration with no `last=`, which prunes a live waiter. A refresh
    preserves first-seen."""
    d.mkdir(parents=True, exist_ok=True)
    since = read_kv(d / who).get("since", "")
    lines = {"who": who, "kind": kind, "why": oneline(why),
             "since": since if since.isdigit() else str(int(time.time())),
             "last": str(int(time.time())), **(extra or {})}
    tmp = d / f".{who}.{os.getpid()}"
    write_private(tmp, "".join(f"{k}={v}\n" for k, v in lines.items()))
    os.replace(tmp, d / who)


def wait_register(who: str, kind: str, why: str) -> None:
    reg_write(WAITD, who, kind, why, {"pid": str(os.getpid())})


def wait_clear(who: str) -> None:
    (WAITD / who).unlink(missing_ok=True)


def prune_registrations() -> None:
    """A dead registration ages out on its own: a blocking waiter is live while its
    pid is, a poller while its retries keep arriving, a user until its expiry."""
    now = int(time.time())
    for p in files_in(WAITD):
        r = read_kv(p)
        if r.get("kind") == "block" and r.get("pid", "").isdigit():
            if not pid_alive(int(r["pid"])):
                p.unlink(missing_ok=True)
        elif not (r.get("last", "").isdigit() and now - int(r["last"]) <= POLL_WAIT_STALE):
            p.unlink(missing_ok=True)
    for p in files_in(USERSD):
        e = read_kv(p).get("expires", "")
        if not (e.isdigit() and int(e) > now):
            p.unlink(missing_ok=True)


# ── the sweep ────────────────────────────────────────────────────────────────

def sweep(baseline: set[int]) -> None:
    """Only the delta: processes that appeared while the lease was held. A process
    that predates the lease is a refusal at acquire, never a kill at release, so a
    lease can never take out another agent's work."""
    swept = 0
    for h in foreign_holders():
        if h.pid in baseline:
            continue
        # A model that loaded DURING the lease is llama-swap's, not this job's orphan.
        if h.sanctioned:
            log(f"leaving the router's model alone: pid {h.pid}, port {h.port}, model {h.model}")
            continue
        stopped = False
        if not h.unit:
            log(f"sweeping orphan GPU holder: {h.kind} — pid {h.pid} (no unit), port {h.port}, model {h.model}")
        elif NEVER_STOP.match(h.unit):
            log(f"!! orphan pid {h.pid} attributes to {h.unit}, which is never stopped — killing the pid only")
        else:
            log(f"sweeping orphan GPU holder: {h.kind} — pid {h.pid}, unit {h.unit}, port {h.port}, model {h.model}")
            systemctl("stop", h.unit)
            stopped = True
        # Only wait on a stop actually issued; otherwise the signal goes now.
        if not (stopped and wait_pid_gone(h.pid, 60)):
            if stopped:
                log(f"pid {h.pid} outlived the unit stop — signalling it directly")
            kill_pid(h.pid)
        if pid_alive(h.pid):
            log(f"!! pid {h.pid} STILL ALIVE after SIGKILL — the GPU is not released")
        else:
            swept += 1
    if swept:
        log(f"swept {swept} orphan(s); MemAvailable {avail_gib()} GiB, GTT {gtt_gib()} GiB")


# ── inhibitor ────────────────────────────────────────────────────────────────
# One primitive for two jobs: it keeps hatch-idle-poweroff from powering the box off
# under the work, and it is what puts the lease on the taskboard. logind refuses it
# from an ssh session (no seat); taken from the user manager – any `systemd-run --user`
# unit – it succeeds, which is why `launch` and `acquire` are the entry points for
# anything long.

def inhibitor_present(who: str) -> bool:
    for line in sh("systemd-inhibit", "--list", "--no-pager").stdout.splitlines():
        f = line.split()
        if len(f) >= 7 and f[0] == who and f[-1] == "block" and ("idle" in f[5] or "shutdown" in f[5]):
            return True
    return False


def take_inhibitor(who: str, why: str) -> subprocess.Popen | None:
    try:
        p = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--mode=block",
                              f"--who={who}", f"--why={why}", "sleep", "infinity"],
                             stdin=subprocess.DEVNULL)
    except OSError:
        p = None
    for _ in range(10):
        if p is None or p.poll() is not None:
            break
        if inhibitor_present(who):
            log(f"idle inhibitor held ({who}: {why})")
            return p
        time.sleep(0.5)
    log("!! could NOT take the idle inhibitor — the box may power off under this job,")
    log("   and it will not appear on the taskboard. logind refuses it from an ssh")
    log("   session; run this through 'hatch gpu-lease launch' (or acquire) instead.")
    return None


# ── take / policy / finish ───────────────────────────────────────────────────

class Lease:
    """What a held lease consists of; it lives exactly as long as the lock does."""

    def __init__(self, lock, who: str, why: str, resident: str, baseline: set[int]):
        self.lock, self.who, self.why, self.resident, self.baseline = lock, who, why, resident, baseline
        self.inhibitor: subprocess.Popen | None = None
        self.stopped: list[str] = []   # what this lease took down, and owes back


def preflight(force: bool) -> None:
    """The router never blocks a lease: it self-releases on ttl, it is admission-checked
    in the other direction by hatch-serving-guard, and `--resident stop` reclaims its
    memory outright. It is still printed: memory it holds is memory the job does not get."""
    holders = foreign_holders()
    sanctioned = [h for h in holders if h.sanctioned]
    violators = [h for h in holders if not h.sanctioned]
    if sanctioned:
        log("the router has models loaded – sanctioned, not a blocker:")
        log(describe(sanctioned))
    if not violators:
        return
    if force:
        log("--force: proceeding although the GPU is already held by:")
        log(describe(violators))
        return
    log("REFUSING — something already owns the GPU without a lease:")
    log(describe(violators))
    die("wait for it to finish, or re-run with --force if you know it is yours.", LEASE_UNAVAILABLE)


def take_lease(who: str, why: str, mode: str, resident: str, wait: str, ttl: str, force: bool) -> Lease:
    wait_s = to_seconds(wait) if wait else None
    # Try once so the holder can be named before blocking. The lock is the queue: a
    # second job waits here rather than evicting the first.
    lock = acquire_lock(0)
    if lock is None:
        if wait_s == 0:
            # A refused poller registers before leaving, so a consumer retrying every
            # few minutes is visible to the holder; the refresh preserves first-seen.
            wait_register(who, "poll", why)
            die(f"REFUSING — the lease is held by {holder_desc()}, and --wait 0 means do not queue",
                LEASE_UNAVAILABLE)
        wait_register(who, "block", why)
        log(f"the lease is held by {holder_desc()} — queueing{f' for up to {wait}' if wait else ''}")
        lock = acquire_lock(wait_s)
        if lock is None:
            wait_clear(who)
            die(f"REFUSING — still held by {holder_desc()} after {wait}", LEASE_UNAVAILABLE)
    wait_clear(who)
    prune_registrations()

    preflight(force)
    lease = Lease(lock, who, why, resident, {h.pid for h in foreign_holders()})
    write_sidecar(who, why, mode, resident, ttl, " ".join(str(p) for p in sorted(lease.baseline)))
    lease.inhibitor = take_inhibitor(who, why)
    log(f"lease taken by {who} ({why}); resident policy: {resident}")
    return lease


def apply_resident_policy(lease: Lease) -> None:
    if lease.resident == "stop":
        # Record the set BEFORE taking it, so a holder killed mid-stop still owes back
        # the right units. `none` distinguishes "there was nothing to stop" from a
        # sidecar written before this field existed.
        lease.stopped = residents.active_residents()
        sidecar_append(f"stopped={' '.join(lease.stopped) or 'none'}")
        # The router first: reclaiming it is the difference between "the lease governs
        # this GPU" and "the lease governs the half of it systemd happens to own".
        residents.unload_router_models()
        residents.stop_residents(lease.stopped)
    elif lease.resident == "serve":
        # `serve` is the recovery gesture a human reaches for when the box is empty, so its
        # success has to clear the alarm too. Dropping this boolean left the headline up
        # after the residents were back.
        if residents.serve_residents():
            RESTORE_FAILED.unlink(missing_ok=True)
    # keep: deliberately untouched


def finish_lease(lease: Lease) -> None:
    sweep(lease.baseline)
    # Only `stop` owes a restore: it is the policy that took the residents away.
    if lease.resident == "stop":
        with restoring_window():
            record_restore(residents.start_residents(lease.stopped), lease.stopped,
                           lease.who, lease.why)
    if lease.inhibitor is not None:
        lease.inhibitor.terminate()
    SIDECAR.unlink(missing_ok=True)
    log("lease released")
