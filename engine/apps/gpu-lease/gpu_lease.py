#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""gpu-lease — exclusive possession of the box's single GPU, for the duration of a job.

The box has one GPU and 124 GB of unified memory, and the models that matter take
32–118 GB, so exactly one big model is resident at a time. Consumers used to get the
GPU by stopping whatever held it; that collided twice in ninety minutes on
2026-08-14. This is the protocol.

The lease is advisory: a job that never calls this is unprotected. What makes a
violation visible rather than silent is `status` naming an unleased holder, and the
block-mode idle inhibitor every lease holds, which the taskboard surfaces.

Exit codes: the wrapped command's own (run/launch) · 75 lease unavailable ·
1 usage or failure.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

import lease as L
import residents
from config import (LEASE_UNAVAILABLE, RESIDENTS, RESTORE_FAILED, ROUTER_RESIDENTS, ROUTER_UNIT,
                    SIDECAR, USERSD, WAITD, die, human_dur, is_active, log, out, systemctl,
                    to_seconds, valid_duration)
from holders import avail_gib, describe, foreign_holders, gtt_gib
from remote import delegate, gpu_host

# What `launch` and `acquire` write into a transient unit's ExecStart and ExecStopPost.
# The verb exports its own resolved path, so the units keep the shape they have today;
# run directly, the app names itself.
VERB = os.environ.get("HATCH_GPU_LEASE_VERB") or os.path.abspath(__file__)
WHO_RE = re.compile(r"^[A-Za-z0-9_-]+$")


VALUE_FLAGS = {"who": "a value", "why": "a value", "resident": "stop|keep|serve",
               "wait": "a duration", "ttl": "a duration", "host": "a value",
               "setenv": "KEY or KEY=VALUE"}
BOOL_FLAGS = ("local", "force", "json", "takes_box", "restoring", "quiet")


def flag(name: str) -> str:
    return "--" + name.replace("_", "-")


FLAG_NAMES = {flag(n): n for n in (*VALUE_FLAGS, *BOOL_FLAGS)}


class Opts:
    def __init__(self):
        self.who = self.why = self.resident = self.wait = self.ttl = self.host = ""
        self.local = self.force = self.json = self.takes_box = self.quiet = False
        self.restoring = False
        self.setenv: list[str] = []
        self.cmd: list[str] = []


def parse_flags(argv: list[str]) -> Opts:
    o = Opts()
    args = list(argv)
    while args:
        a = args[0]
        name = FLAG_NAMES.get(a)
        if a == "--":
            o.cmd = args[1:]
            break
        if a in ("-h", "--help"):
            usage()
            sys.exit(0)
        if name in BOOL_FLAGS:
            setattr(o, name, True)
            del args[:1]
            continue
        if name is None:
            die(f"unknown option: {a}")
        if len(args) < 2:
            die(f"{a} needs {VALUE_FLAGS[name]}")
        if name == "setenv":
            o.setenv.append(args[1])
        else:
            setattr(o, name, args[1])
        del args[:2]
    # Durations are validated HERE, not where they are used, so `--wait bogus` cannot
    # leave the value empty, which reads as "queue for ever".
    if o.wait and not valid_duration(o.wait):
        die(f"not a duration: {o.wait} (use 0, 90s, 45m, 8h)")
    if o.ttl and not valid_duration(o.ttl):
        die(f"not a duration: {o.ttl} (use 90s, 45m, 8h)")
    return o


def argv_of(o: Opts, *names: str) -> list[str]:
    """The named options, rebuilt as argv from their parsed values."""
    argv: list[str] = []
    for n in names:
        v = getattr(o, n)
        if v is True:
            argv.append(flag(n))
        elif isinstance(v, list):
            for x in v:
                argv += [flag(n), x]
        elif v:
            argv += [flag(n), v]
    return argv


def forward_flags(o: Opts) -> list[str]:
    """What a delegated subcommand carries to the GPU machine, minus --host/--local."""
    return argv_of(o, "who", "why", "resident", "wait", "ttl", "setenv", "force", "json", "takes_box", "restoring", "quiet")


def lease_flags(o: Opts) -> list[str]:
    """What a transient unit's inner `run`/`hold` gets, --local included: the outer
    command already settled which machine this is."""
    return argv_of(o, "who", "why", "resident", "wait", "force", "local")


def require_who(o: Opts, hint: str = "") -> None:
    if not o.who:
        die(f"--who is required{hint}")
    # This value reaches file paths under the registration dirs. A --who built from a
    # variable that holds a path would delete an unrelated file and report success.
    if not WHO_RE.match(o.who):
        die(f"--who must be a plain token [A-Za-z0-9_-]: {o.who}")


def require_lease_flags(o: Opts) -> None:
    require_who(o, " (the stream or consumer taking the GPU)")
    if not o.why:
        die("--why is required (what the GPU is for — it is the taskboard label)")
    if not o.resident:
        tier = " ".join([r.unit for r in RESIDENTS] + ROUTER_RESIDENTS)
        die(f"--resident stop|keep|serve is required: state what happens to the standing models ({tier})")
    if o.resident not in ("stop", "keep", "serve"):
        die(f"--resident must be stop, keep or serve, not: {o.resident}")


def require_gpu_machine(o: Opts, sub: str) -> None:
    host = gpu_host(o)
    if host:
        die(f"{sub} runs its command on THIS machine, but the GPU is on '{host}'.\n"
            "  Taking a lease here would protect the wrong machine's GPU while the command ran\n"
            f"  against nothing. Run it on {host} (ssh {host}), or set --local if this really is\n"
            "  the GPU machine and the config disagrees.")


def unit_name(who: str) -> str:
    return f"gpu-lease-{who}"


def systemd_run(unit: str, o: Opts, argv: list[str], *props: str) -> bool:
    """A transient user unit: TimeoutStopSec leaves room for a model load to finish
    rather than being SIGKILLed mid-restore, and ExecStopPost runs `restore` however
    the main process died. `--setenv` is the only way the caller's environment gets
    in: systemd-run inherits from the user manager, not from the shell."""
    cmd = ["systemd-run", "--user", f"--unit={unit}", "--collect",
           "-p", "TimeoutStopSec=900", "-p", f"ExecStopPost={VERB} restore --quiet"]
    for p in props:
        cmd += ["-p", p]
    for kv in o.setenv:
        cmd.append(f"--setenv={kv}")
    return subprocess.run(cmd + ["--"] + argv).returncode == 0


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_run(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_lease_flags(o)
    require_gpu_machine(o, "run")
    if not o.cmd:
        die("run needs a command after --")

    lease = L.take_lease(o.who, o.why, "run", o.resident, o.wait, "", o.force)
    got: list[int] = []
    child: subprocess.Popen | None = None

    # The signal is forwarded to the job and remembered; the wrapper keeps waiting so
    # the cleanup runs after the job is gone, and a second signal cannot cut the
    # cleanup short. One that arrives before the job starts means it never starts.
    def on_signal(sig, _frame):
        got.append(sig)
        if child is not None and child.poll() is None:
            child.send_signal(sig)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    rc = 1
    try:
        L.apply_resident_policy(lease)
        if not got:
            log(f"running: {' '.join(o.cmd)}")
            try:
                child = subprocess.Popen(o.cmd)
                rc = child.wait()
            except OSError as e:
                log(f"cannot run {o.cmd[0]}: {e.strerror}")
                rc = 127
            if rc < 0:   # killed by a signal: report it the way a shell does
                rc = 128 - rc
            log(f"command exited {rc}")
    finally:
        L.finish_lease(lease)
    if signal.SIGINT in got:
        return 130
    if signal.SIGTERM in got:
        return 143
    return rc


def cmd_hold(argv: list[str]) -> int:
    """Internal: the ExecStart of an `acquire` unit. Holds until stopped; a stop that
    lands during the resident policy lets the policy finish first, so a half-applied
    `serve` or a half-recorded `stop` is never what the release starts from."""
    o = parse_flags(argv)
    require_lease_flags(o)
    lease = L.take_lease(o.who, o.why, "hold", o.resident, o.wait, o.ttl, o.force)
    stop: list[int] = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    try:
        L.apply_resident_policy(lease)
        log("HELD")
        while not stop:
            signal.pause()
    finally:
        L.finish_lease(lease)
    return 0


def cmd_launch(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_lease_flags(o)
    require_gpu_machine(o, "launch")
    if not o.cmd:
        die("launch needs a command after --")
    # A bounded command gets a unique suffix, so two probes from one stream queue at
    # the lock rather than colliding on the unit name.
    unit = f"{unit_name(o.who)}-{os.getpid()}"
    if not systemd_run(unit, o, [VERB, "run", *lease_flags(o), "--", *o.cmd]):
        die("systemd-run failed")
    log(f"launched as {unit} — follow with: journalctl --user -u {unit} -f")
    return 0


def cmd_acquire(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_lease_flags(o)
    o.ttl = o.ttl or "45m"
    host = gpu_host(o)
    if host:
        return delegate(host, "acquire", forward_flags(o))
    # A session keeps `gpu-lease-<who>`, because `release --who` has to name it.
    unit = unit_name(o.who)
    if is_active(unit):
        die(f"{unit} is already active — release it first")
    if not systemd_run(unit, o, [VERB, "hold", *lease_flags(o), "--ttl", o.ttl],
                       f"RuntimeMaxSec={to_seconds(o.ttl)}"):
        die("systemd-run failed")
    # Wait for the lease to be genuinely held by us — the lock, plus our own tag in
    # the sidecar. Not for a log line: the unit may also have exited already.
    deadline = time.monotonic() + (to_seconds(o.wait) if o.wait else 300) + 60
    while time.monotonic() < deadline:
        if L.lock_is_held() and L.read_kv(SIDECAR).get("who") == o.who:
            out(f"HELD {o.who} ({o.why}) ttl {o.ttl}")
            return 0
        if not is_active(unit):
            log("the lease unit exited before taking the lease:")
            subprocess.run(["journalctl", "--user", "-u", unit, "--no-pager", "-n", "15"],
                           stdout=sys.stderr)
            return LEASE_UNAVAILABLE
        time.sleep(2)
    log(f"timed out waiting for {unit} to report the lease")
    return LEASE_UNAVAILABLE


def cmd_release(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_who(o)
    host = gpu_host(o)
    if host:
        return delegate(host, "release", ["--who", o.who])
    unit = unit_name(o.who)
    if not is_active(unit):
        log(f"{unit} is not active")
        return 0
    log(f"stopping {unit}…")
    systemctl("stop", unit)
    log("released")
    return 0


def _reg_json(d) -> list[dict]:
    rows = []
    for r in L.registrations(d):
        row = {"who": r.get("who", ""), "why": r.get("why", ""), "kind": r.get("kind", ""),
               "since": _num(r.get("since")), "last": _num(r.get("last"))}
        if r.get("expires"):
            row["expires"] = _num(r["expires"])
        rows.append(row)
    return rows


def _num(s: str | None) -> int:
    return int(s) if s and s.isdigit() else 0


def cmd_status(argv: list[str]) -> int:
    o = parse_flags(argv)
    host = gpu_host(o)
    if host:
        return delegate(host, "status", forward_flags(o))
    # The one question a consumer asks in a conditional: is a lease held whose holder
    # took the GPU away from the residents? Answered from the lock and the sidecar
    # alone – no pgrep, no podman, no systemctl – because the caller is on the load
    # path of every chat turn. A WORD on stdout as well as an exit status, so a caller
    # can tell "not taking the box" from "this never ran".
    if o.takes_box:
        if o.json:
            die("--takes-box answers in its exit status; it does not combine with --json")
        s = L.read_kv(SIDECAR)
        # STALE (a sidecar with no lock) is `no`: its holder is gone, so nothing is
        # about to reload a model. `restoring=1` is `no` for the opposite reason: the
        # holder is giving the box back and the reload is itself a guarded load.
        takes = L.lock_is_held() and s.get("resident") == "stop" and not s.get("restoring")
        out("yes" if takes else "no")
        return 0 if takes else 1

    # The narrower question, and the only one that says "let this load through": is a lease
    # giving the box back right now? The guard skips its own unit on this and nothing else,
    # because a `--resident keep` or `serve` lease is ALSO holding the box between its arms
    # and its driver unit must still close the chat surface.
    if o.restoring:
        if o.json:
            die("--restoring answers in its exit status; it does not combine with --json")
        s = L.read_kv(SIDECAR)
        yes = L.lock_is_held() and bool(s.get("restoring"))
        out("yes" if yes else "no")
        return 0 if yes else 1

    L.prune_registrations()
    s = L.read_kv(SIDECAR)
    held = L.lock_is_held()
    state = "held" if held else ("stale" if s else "free")
    who, why, mode = s.get("who", ""), s.get("why", ""), s.get("mode", "")
    age = L.human_age(s.get("since")) if s else ""
    holders = foreign_holders()
    chat = [h for h in holders if h.sanctioned]
    viol = [h for h in holders if not h.sanctioned]
    loaded = residents.router_loaded()
    machine = os.uname().nodename
    # A restore that did not bring the residents back outlives the lease that failed it,
    # because the lease's own log line dies with its transient unit.
    failed = L.read_kv(RESTORE_FAILED)

    if o.json:
        rj = [{"unit": r.unit, "port": r.port, "state": systemctl("is-active", r.unit).stdout.strip()}
              for r in RESIDENTS]
        # A router-held model has no unit and no fixed port: it reports the router's
        # unit and its own id, and `state` from the router's own /running.
        rj += [{"unit": ROUTER_UNIT, "swap_id": i,
                "state": "unknown" if loaded is None else ("loaded" if f"router:{i}" in loaded else "unloaded")}
               for i in ROUTER_RESIDENTS]
        out(json.dumps({"machine": machine, "state": state, "who": who, "why": why, "mode": mode,
                        "resident": s.get("resident", ""), "pid": s.get("pid", ""), "age": age,
                        "ttl": s.get("ttl", "") if held else "", "residents": rj,
                        "waiting": _reg_json(WAITD), "users": _reg_json(USERSD),
                        "unleased_gpu": bool(viol) and state != "held",
                        "restore_failed": failed},
                       separators=(",", ":")))
        return 0

    if state == "free":
        out("lease: free")
    elif state == "held":
        ttl = s.get("ttl", "")
        out(f"lease: HELD by {who or 'unknown'} ({why or '?'}) — mode {mode or '?'}, {age or '?'} ago"
            f"{f', ttl {ttl}' if ttl else ''}, pid {s.get('pid') or '?'}")
    else:
        out(f"lease: STALE — sidecar says {who or 'unknown'} ({why or '?'}) from {age or '?'} ago, but nothing holds the lock.")
        out("       run 'hatch gpu-lease restore' to finish its cleanup.")
    # AFTER the lease line, deliberately: hatch-serving-guard names the holder in a refusal
    # by reading `status | head -1`, and a headline above it silently emptied that diagnostic
    # at the one moment it is worth having.
    if failed:
        out(f"!! THE BOX HAS NO RESIDENT: {failed.get('owed') or 'the recorded set'} did not come "
            f"back after {failed.get('who') or 'a lease'} ({failed.get('why') or '?'}) released, "
            f"{L.human_age(failed.get('when'))} ago.")
        out("   run 'hatch gpu-lease restore' to retry the load.")
    out(f"machine:  {machine} — MemAvailable {avail_gib()} GiB, GTT {gtt_gib()} GiB")
    parts = [f"{r.unit} {systemctl('is-active', r.unit).stdout.strip()} (:{r.port})" for r in RESIDENTS]
    rstate = "unknown (the router did not answer)" if loaded is None else "unloaded"
    parts += [f"{i} {'loaded' if loaded and f'router:{i}' in loaded else rstate} (in {ROUTER_UNIT.removesuffix('.service')})"
              for i in ROUTER_RESIDENTS]
    out(f"residents: {' · '.join(parts)}")
    for r in L.registrations(WAITD):
        kind = "queued on the lock" if r.get("kind") == "block" else "polling"
        out(f"waiting:  {r.get('who', '')} ({r.get('why', '')}) — {kind}, first asked "
            f"{L.human_age(r.get('since'))} ago, last seen {L.human_age(r.get('last'))} ago")
    for r in L.registrations(USERSD):
        out(f"using the resident: {r.get('who', '')} ({r.get('why', '')}) — since "
            f"{L.human_age(r.get('since'))} ago, expires in {human_dur(_num(r.get('expires')) - int(time.time()))}")
    if state == "held":
        if holders:
            out("GPU holders under this lease:")
            out(describe(holders))
    else:
        # The router's holders are sanctioned use, not violations – headlining them as
        # one would teach every reader to ignore the single enforcement point.
        if chat:
            out("the router has models loaded (llama-swap; standing tier, or unloads on ttl):")
            out(describe(chat))
        if viol:
            out("!! GPU held with NO lease — this is the violation the lease exists to make visible:")
            out(describe(viol))
    return 0


def cmd_using(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_who(o, " (the consumer using the resident)")
    if not o.why:
        die("--why is required (what the resident is being used for)")
    o.ttl = o.ttl or "15m"
    host = gpu_host(o)
    if host:
        return delegate(host, "using", forward_flags(o))
    L.prune_registrations()
    L.reg_write(USERSD, o.who, "user", o.why, {"expires": str(int(time.time()) + to_seconds(o.ttl))})
    o.quiet or log(f"registered as a resident user: {o.who} ({o.why}), expires in {o.ttl}")
    return 0


def cmd_done(argv: list[str]) -> int:
    o = parse_flags(argv)
    require_who(o)
    host = gpu_host(o)
    if host:
        return delegate(host, "done", forward_flags(o))
    (USERSD / o.who).unlink(missing_ok=True)
    o.quiet or log(f"{o.who} is no longer using the resident")
    return 0


def cmd_restore(argv: list[str]) -> int:
    o = parse_flags(argv)
    host = gpu_host(o)
    if host:
        return delegate(host, "restore", forward_flags(o))
    L.prune_registrations()
    if L.lock_is_held():
        o.quiet or log("a lease is currently held — nothing to restore")
        return 0
    s = L.read_kv(SIDECAR)
    if not s:
        # No stale lease, but the box may still be empty: a lease that released cleanly
        # and failed its restore unlinks its sidecar and leaves only the marker. The
        # marker carries the set it owed, so this is the retry `status` tells you to run.
        # NOT under --quiet, which is how every transient unit's ExecStopPost invokes this.
        # That hook exists to finish a STALE SIDECAR's cleanup. Letting it also chase the
        # marker makes each unit's stop retry a failure some other lease recorded, under a
        # `--resident keep` policy that said not to touch residents, and blocks the unit's
        # stop for up to HEALTH_TIMEOUT per id inside a 900 s TimeoutStopSec. The marker is
        # retried when a human or a script asks for it by name.
        failed = {} if o.quiet else L.read_kv(RESTORE_FAILED)
        if failed:
            owed = [i for i in failed.get("owed", "").split() if i]
            log(f"retrying a restore that failed after {failed.get('who') or 'a lease'} "
                f"({failed.get('why') or '?'}) released: {' '.join(owed) or 'nothing recorded'}")
            L.record_restore(residents.start_residents(owed), owed,
                             failed.get("who", ""), failed.get("why", ""))
            return 0
        o.quiet or log("no stale lease; nothing to do")
        return 0
    log(f"finishing the cleanup of a stale lease: {s.get('who', '')} ({s.get('why', '')}), "
        f"{L.human_age(s.get('since'))} ago")
    L.sweep({int(p) for p in s.get("baseline", "").split() if p.isdigit()})
    if s.get("resident") == "stop":
        if "stopped" in s:
            # `none` means it stopped nothing; anything else is the set it owes back.
            owed = s["stopped"].split()
        else:
            # Only a sidecar from a version that did not record the set. Restoring the
            # primary is a guess, and the guess errs towards the box having a resident:
            # an unwanted model is visible and reversible, a silently dead port is not.
            log("!! this sidecar predates the stopped-set record — GUESSING the primary resident")
            owed = [RESIDENTS[0].key if RESIDENTS else f"router:{ROUTER_RESIDENTS[0]}"]
        with L.restoring_window():
            L.record_restore(residents.start_residents(owed), owed,
                             s.get("who", ""), s.get("why", ""))
    SIDECAR.unlink(missing_ok=True)
    log("stale lease cleared")
    return 0


def usage() -> None:
    out("""gpu-lease — exclusive possession of the box's single GPU, for the duration of a job.

  gpu-lease run     --who TAG --why TEXT --resident stop|keep|serve [--wait DUR] [--force] -- CMD…
  gpu-lease launch   (same flags) [--setenv KEY[=VALUE]]…   run, inside a transient systemd unit
  gpu-lease acquire --who TAG --why TEXT --resident stop|keep|serve [--ttl DUR] [--wait DUR] [--setenv …] [--host H]
  gpu-lease release --who TAG [--host H]
  gpu-lease status  [--json|--takes-box|--restoring] [--host H]
  gpu-lease using   --who TAG --why TEXT [--ttl DUR] [--host H]
  gpu-lease done    --who TAG [--host H]
  gpu-lease restore [--quiet]

  --who       a plain token naming the consumer (stream or job)
  --why       what the GPU is for; this is the label the taskboard shows
  --resident  stop  = take the GPU from EVERY standing resident, give back exactly
                      the ones that were running, at release
              serve = ensure the enabled residents are up and healthy, leave them up
              keep  = do not touch any resident
  --wait      how long to queue behind another lease. Default: forever.
              --wait 0 fails immediately and names the holder.
  --ttl       acquire only: backstop, enforced by RuntimeMaxSec. Default 45m.
  --setenv    launch/acquire: a variable the transient unit gets (repeatable). KEY alone
              copies it from this environment; nothing else crosses into the unit.
  --host      override which machine has the GPU (else HATCH_GPU_HOST, else the
              HATCH_SERVE_URL host in resolved.env). status/acquire/release/restore
              delegate there; run/launch refuse rather than lease the wrong GPU.
  --local     force this machine, for a caller describing what it sees here.
  --restoring status only: print `yes`/`no` and exit 0/1 for "a lease is handing the
              box back right now". The guard's licence to let a load past its scan.
  --takes-box status only: print `yes`/`no` and exit 0/1 for "a lease is held with
              --resident stop". Reads the lock and the sidecar only, so it is cheap
              enough for a caller on the chat-load path.
  --force     take the lease although something already holds the GPU without one.

`run` wraps a bounded command and cleans up in the wrapper, so cleanup survives
the command's crash. `acquire`/`release` bracket an open-ended session — a soak
serve, a migration run. `restore` finishes the cleanup of a lease whose holder
was killed outright, and is safe to run at any time.

Demand is visible, not just possession: a job refused with --wait 0 registers
as a polling waiter (retries preserve first-seen), a queued job registers while
it blocks, and a consumer of the resident that holds no lease can say so with
`using` (auto-expiring; default --ttl 15m) and `done`. `status` shows holder +
waiters + users. Registration never changes who runs next.

The lease is advisory: a job that never calls this is unprotected. `status`
flags a GPU held with no lease, which is what makes a violation visible.""")


COMMANDS = {"run": cmd_run, "launch": cmd_launch, "acquire": cmd_acquire, "release": cmd_release,
            "hold": cmd_hold, "status": cmd_status, "using": cmd_using, "done": cmd_done,
            "restore": cmd_restore}


def main(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    if sub in ("-h", "--help", "help", ""):
        usage()
        return 0
    if sub not in COMMANDS:
        die(f"unknown subcommand: {sub} (try --help)")
    return COMMANDS[sub](argv[1:])


if __name__ == "__main__":
    # A reader that closes early (`status | head`) ends this the way it ends any
    # shell tool: silently, with the pipe's own signal, not a traceback.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:   # while queued on the lock, before a lease installs its handlers
        sys.exit(130)
