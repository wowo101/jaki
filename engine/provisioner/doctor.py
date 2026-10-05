"""`hatch doctor` — read-only machine health report.

Two halves. The **machine** half checks what the provisioner itself needs:
Python, a parsing machine config, git, node, the PATH dir, its own shim,
DEVONthink, and whether the emitted `resolved.env` still agrees with the
machine config.

The **artefact** half walks this machine's `[artefacts]` registry, loads each
`provision.toml`, and reports per artefact — the gate binaries that stop a
deployment, the deploy targets that should exist and resolve, the `needs` keys
that must be non-empty in the emitted `resolved.env`, and any PATH or systemd
symlink into this repo that no declared artefact claims. Targets are computed
by `layout`, the same functions `install` deploys through.

Reports drift; never repairs it. `hatch install` is idempotent and is the
repair (spec §9).
"""
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import platform
from .config import machine_config_path, load_machine_config
from .emit import read_resolved_env, resolved_env_path
from .layout import (BIN_DIR, SYSTEMD_USER_DIR, app_bin_target, artefact_dir,
                     artefact_key, claim_path_names, file_dirs, file_target,
                     plugin_link, slot_accepts, unit_is_enablable, unit_target,
                     vault_targets,
                     verb_target)
from .manifest import load_manifest, missing_requires
from .resolve import resolve


def repo_root() -> Path:
    # engine/provisioner/doctor.py → parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    informational: bool = False   # absence is a capability gap, not a failure
    section: str = ""             # blank = the machine half


# What each deploy strategy actually reads. A manifest field outside its kind's
# set parses, deploys nothing and raises nothing, which is what this check
# exists to surface.
#
# `units` is in BOTH the verb and app sets: a unit is a property of the
# artefact, not of its tier, and `deploy_verb` links them, so a verb that runs
# on a timer no longer has to be filed as an app to get them. It was app-only
# until 2026-08-23, and `hatch-sync` lived in engine/apps/ purely because of
# that - its provision.toml said as much.
_CONSUMED = {
    "verb": {"exec_name", "requires", "needs", "probe", "units"},
    "app": {"requires", "needs", "bin", "units", "files", "probe"},
    "plugin": {"plugin_id", "build", "outputs", "data"},
}
_DECLARABLE = set().union(*_CONSUMED.values())
_TOML_KEY = {"exec_name": "exec", "plugin_id": "id"}


def _inert_fields(m) -> list:
    """Manifest fields this artefact declares that its own kind never reads."""
    return [_TOML_KEY.get(f, f) for f in sorted(_DECLARABLE - _CONSUMED[m.kind])
            if getattr(m, f)]


def _link_problem(target: Path, source: Path, label: str):
    """What is wrong with one deployed symlink, or None.

    Compares where the link *resolves*, not the bytes `readlink` returns: a
    relative link is as correct as an absolute one, and the plugin-into-vault
    links predate the provisioner and were made by hand. `exists()` follows the
    link, so a dangling one is the case a bare presence test misses — and the
    case that stays invisible until the verb is invoked.
    """
    if not target.is_symlink():
        return (f"{label}: {target} is a real file, not hatch's symlink"
                if target.exists() else f"{label}: not deployed ({target} missing)")
    if not target.exists():
        return f"{label}: dangling symlink → {target.readlink()}"
    if target.resolve() != source.resolve():
        return f"{label}: points at {target.readlink()}, expected {source}"
    return None


def unit_is_enabled(name: str) -> bool:
    """Ask systemd whether the unit would actually run.

    `is-enabled` is the consumer's own tool. The symlink this artefact deploys
    is a different fact: for weeks every highlight-sync unit was `linked` (file
    in place, doctor green) with no `timers.target.wants/` entry, so no sweep
    ran. Checking the link cannot distinguish those two states; this can.
    """
    r = subprocess.run(["systemctl", "--user", "is-enabled", name],
                       capture_output=True, text=True)
    return r.stdout.strip() == "enabled"


_TIME_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1, "sec": 1, "seconds": 1,
               "m": 60, "min": 60, "minutes": 60, "h": 3600, "hr": 3600,
               "hours": 3600, "d": 86400, "days": 86400, "w": 604800}


def parse_systemd_time(value: str) -> float:
    """systemd time span → seconds. `15min`, `1h30m`, `90` (bare = seconds)."""
    total, number, unit = 0.0, "", ""

    def flush():
        nonlocal total, number, unit
        if number:
            total += float(number) * _TIME_UNITS.get(unit or "s", 1)
        number, unit = "", ""

    for ch in value.strip():
        if ch.isdigit() or ch == ".":
            if unit:
                flush()
            number += ch
        elif ch.isalpha():
            unit += ch
        else:
            flush()
    flush()
    return total


def _timer_cadence(unit_source: Path) -> float:
    """`OnUnitActiveSec` in seconds, or 0 when the timer does not repeat."""
    try:
        lines = unit_source.read_text().splitlines()
    except OSError:
        return 0.0
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "OnUnitActiveSec":
            return parse_systemd_time(value)
    return 0.0


def awake_seconds() -> float:
    """Seconds this machine has been awake since boot, excluding suspend.

    `CLOCK_MONOTONIC` does not advance across a suspend on Linux, which is the
    same clock `OnUnitActiveSec` runs on — so this is the ceiling on how much
    timer time can have elapsed since boot.
    """
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return float("inf")     # unknown: fall back to the wall-clock age alone


def heartbeat_age(tool: str, now: float = None) -> float:
    """Seconds of *timer time* since the sweep last recorded finishing, or -1.

    Reads the sweep's own stamp, not systemd's opinion of the timer: a unit
    disabled behind systemd's back still reports its last trigger, and the
    journal rotates. `-1` is *absent*, distinct from a large age — a missing
    input must not read as a number.

    The stamp is wall-clock and the cadence is monotonic, so comparing them
    directly faults every sweep on the machine after a lid-close longer than
    the threshold. A timer cannot have missed more time than the machine has
    been awake, so the age is capped at that.
    """
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = base / tool / "heartbeat"
    try:
        stamp = datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return -1.0
    wall_age = (now if now is not None else time.time()) - stamp.timestamp()
    return min(wall_age, awake_seconds())


def _describe(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# A sweep is stale once it has missed this many consecutive fires. Two would
# alarm on one skipped run (a laptop asleep over a cadence); three is a sweep
# that has genuinely stopped.
STALE_AFTER_MISSED_FIRES = 3


def _staleness_problems(m, art: Path, enabled_units: set) -> tuple:
    """Enabled repeating timers whose sweep has not recorded finishing lately.

    The companion to the enablement check: enabled says systemd would run it,
    this says it actually did. A timer whose service fails every fire stays
    `enabled` forever.
    """
    problems, notes = [], []
    for rel in m.units:
        name = Path(rel).name
        if not name.endswith(".timer") or name not in enabled_units:
            continue
        cadence = _timer_cadence(art / rel)
        if not cadence:
            continue                      # a boot-only timer has no expected rate
        tool = name[:-len(".timer")]
        age = heartbeat_age(tool)
        if age < 0:
            notes.append(f"{tool}: no completed run recorded yet")
        elif age > cadence * STALE_AFTER_MISSED_FIRES:
            problems.append(
                f"{tool}: last completed run {_describe(age)} ago, "
                f"cadence {_describe(cadence)} — the timer is enabled but the "
                f"sweep is not finishing")
    return problems, notes


def _enablement_problems(m, art: Path, enabled_units: set) -> tuple:
    """Units this machine declares it runs, that are not actually enabled.

    Returns (problems, linked-only). A unit outside `enabled_units` is
    deliberately off here — the same app lands on the laptop and the box and a
    different timer fires on each — so it is reported, not faulted.
    """
    if not m.units or shutil.which("systemctl") is None:
        return [], []
    problems, off = [], []
    for rel in m.units:
        name, source = Path(rel).name, art / rel
        enablable = unit_is_enablable(source)
        if name in enabled_units:
            if not enablable:
                # `install` skips it, so the "not enabled" fault below would be
                # permanent and unfixable. Name the real problem: the toml.
                problems.append(f"{name}: declared in [units].enabled but carries no "
                                f"[Install] — systemd cannot enable it; it is started "
                                f"by its timer")
            elif not unit_is_enabled(name):
                problems.append(f"{name}: linked but NOT enabled — nothing runs it "
                                f"(this machine's toml lists it under [units].enabled)")
        elif enablable:
            # Ask systemd rather than assume: `install` has no disable path, so
            # dropping a unit from the toml leaves it running while the report
            # would otherwise claim it is off.
            if unit_is_enabled(name):
                problems.append(f"{name}: enabled and running but NOT declared in "
                                f"[units].enabled — `install` will not manage it, and "
                                f"nothing records that this machine runs it")
            else:
                off.append(name)
    return problems, off


def _deployed_problems(m, art: Path, bin_dir: Path, systemd_dir: Path, vaults: list):
    """Everything this artefact should have on disk, checked where install put it.

    Returns the problems and the PATH names whose link came back clean — only
    those are worth asking PATH about.
    """
    probs, sound = [], []

    def note(p, name=None):
        if p:
            probs.append(p)
        elif name:
            sound.append(name)

    if m.kind == "verb":
        note(_link_problem(verb_target(bin_dir, m), art / m.exec_name, m.exec_name),
             m.exec_name)
    elif m.kind == "app":
        for entry in m.bin:
            note(_link_problem(app_bin_target(bin_dir, entry), art / entry.source, entry.name),
                 entry.name)
        for entry in m.files:
            note(_link_problem(file_target(entry), art / entry.source, str(file_target(entry))))
    # A unit is a property of the artefact, not of its tier: `deploy_verb` links
    # units too, so checking them only under `app` left a verb's units unverified.
    # deploy skips the unit block without systemctl, so the check does too.
    if m.kind in ("verb", "app") and m.units and shutil.which("systemctl") is not None:
        for rel in m.units:
            note(_link_problem(unit_target(systemd_dir, rel), art / rel, Path(rel).name))

    if m.kind == "plugin":
        for f in m.outputs:
            if not (art / f).is_file():
                probs.append(f"not built: {f} missing from {art}")
        if not vaults:
            # install refuses this outright; doctor saying "fine" would make the
            # one machine where nothing can deploy the one machine that reads green.
            probs.append("no vault endpoint configured — nothing to link into")
        for vault in vaults:
            note(_link_problem(plugin_link(vault, m), art, str(vault)))
    return probs, sound


def _probe_problems(m, art: Path) -> list:
    """Runtime prerequisites that are not satisfied on this machine.

    The gap this closes: a verb resting on a system package the provisioner
    cannot install deploys perfectly and reports a clean tick, because a
    resolved symlink is a proxy for "it works" and the two come apart the moment
    the package is missing. `hatch browse` sat green while dying on import for
    want of `webkitgtk-6.0`, and the only way to find out was to run it.

    Probes run only once the gate has passed and the links are sound, so a verb
    that is not deployed here on purpose stays quiet. Failure is reported with
    the manifest's hint, because a probe that says only FAILED costs a second
    debugging session.
    """
    probs = []
    for pr in m.probe:
        try:
            r = subprocess.run(pr.run, cwd=art, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            probs.append(f"probe timed out after 30s: {' '.join(pr.run)}")
            continue
        except OSError as e:
            probs.append(f"probe could not run ({e.__class__.__name__}: {e}): {' '.join(pr.run)}")
            continue
        if r.returncode != 0:
            # The probe's own stderr names the missing piece; the hint says what
            # to do about it. Both, because neither alone is actionable.
            why = (r.stderr or r.stdout or "").strip().splitlines()
            detail = why[-1][:160] if why else f"exit {r.returncode}"
            probs.append(f"runtime prerequisite missing: {detail}"
                         + (f" — {pr.hint}" if pr.hint else ""))
    return probs


def _on_path(bin_dir: Path) -> bool:
    """Whether bin_dir is on PATH as the kernel will read it.

    No `expanduser`: `execve` does not expand `~`, so a PATH entry written as a
    literal tilde is a broken entry, not a match.
    """
    return any(Path(p) == bin_dir
               for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _path_problems(names: list, bin_dir: Path) -> list:
    """What PATH says about names whose symlink is already known good.

    The symlink existing is a proxy for the command running; what PATH resolves
    is the artefact. Two entries symlinked to the same source are not a shadow,
    which is the pre-provisioner `/usr/local/bin/keep` case.
    """
    if not _on_path(bin_dir):
        return []          # already reported once, as the PATH check
    out = []
    for name in names:
        found = shutil.which(name)
        if found is None:
            out.append(f"{name}: deployed but not executable — PATH finds nothing")
        elif Path(found).resolve() != (bin_dir / name).resolve():
            out.append(f"{name}: shadowed on PATH by {found}")
    return out


def _needs_problems(m, emitted: dict, emits: set) -> list:
    """`needs` keys that will not reach the artefact at runtime.

    Three states with three different repairs, so they are not collapsed into
    one word: a key `resolve()` never emits is a manifest fault, a key absent
    from the file wants a re-install, and an empty value wants the toml edited.
    """
    out = []
    for k in m.needs:
        if k not in emits:
            out.append(f"{k}: not a key hatch emits — check the manifest")
        elif k not in emitted:
            out.append(f"{k}: missing from resolved.env — run `hatch install`")
        elif not emitted[k]:
            out.append(f"{k} is empty in resolved.env")
    return out


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _ok_detail(m, bin_dir: Path, vaults: list, units_off: list = None,
               enabled_count: int = 0, notes: list = None) -> str:
    """The green line. `units_off` are linked-only units and `notes` are sweeps
    with no completed run yet — both named, never counted, because "6 units" is
    what read green while none of the six ran."""
    parts = []
    if units_off:
        parts.append(f"{', '.join(units_off)} linked only")
    if notes:
        parts.extend(notes)
    off = f" ({'; '.join(parts)})" if parts else ""
    if m.kind == "verb":
        return str(verb_target(bin_dir, m)) + off
    if m.kind == "app":
        units = f", {_plural(enabled_count, 'unit')} enabled" if m.units else ""
        files = f", {_plural(len(m.files), 'file')}" if m.files else ""
        return f"{_plural(len(m.bin), 'shim')} on PATH{units}{files}{off}"
    return f"{m.plugin_id} in {_plural(len(vaults), 'vault')}"


def artefact_check(root: Path, name: str, slot: str, emitted, emits: set, bin_dir: Path,
                   systemd_dir: Path, vaults: list, claimed: dict, deployed: set,
                   dest_names: dict = None, enabled_units: set = None,
                   shipped_units: set = None) -> Check:
    """One line per declared artefact: gate, targets, PATH, needs, inert fields.

    `emitted` is the parsed resolved.env — the file the artefacts read — or None
    when it has not been written; `emits` is the key set `resolve()` produces.
    `claimed` accumulates PATH and unit names for collision detection, and
    `deployed` the subset this machine should actually carry, so the drift scan
    afterwards can tell an orphan from a deployment. `dest_names` collects each
    `[[files]]` destination directory and the basenames claimed in it, so the
    drift scan can reach directories no fixed pair would name.
    """
    art = artefact_dir(root, name, slot)
    try:
        m = load_manifest(art)
    except (FileNotFoundError, ValueError) as e:
        return Check(name, False, str(e), section="artefacts")
    if not slot_accepts(slot, m.kind):
        return Check(name, False,
                     f"listed under [artefacts].{artefact_key(slot)} but its "
                     f"manifest says kind={m.kind!r}", section="artefacts")

    path_names = [m.exec_name] if m.kind == "verb" else [e.name for e in m.bin]
    unit_names = [Path(u).name for u in m.units]
    if shipped_units is not None:
        shipped_units.update(unit_names)
    # A file dest is claimed by its full path: two artefacts may legitimately
    # deploy a `config.yaml` each, as long as they are different directories.
    file_dests = [str(file_target(e)) for e in m.files]
    # Claimed whether or not the gate passes, matching what `install` refuses.
    collisions = (claim_path_names(claimed, name, path_names)
                  + claim_path_names(claimed, name, unit_names)
                  + claim_path_names(claimed, name, file_dests))

    inert = _inert_fields(m)
    gate = missing_requires(m)
    if gate:
        # Not deployed on purpose. Its absent targets are not faulted — but the
        # names stay out of `deployed`, so a link that IS present here surfaces
        # under drift instead of falling between the two halves.
        detail = f"not deployed — requires {', '.join(gate)}"
        extra = collisions + ([f"inert manifest fields: {', '.join(inert)}"] if inert else [])
        if extra:
            return Check(name, False, "; ".join([detail] + extra), section="artefacts")
        return Check(name, True, detail, informational=True, section="artefacts")

    deployed.update(path_names + unit_names)
    if dest_names is not None:
        for entry in m.files:
            target = file_target(entry)
            dest_names.setdefault(target.parent, set()).add(target.name)
    probs, sound = _deployed_problems(m, art, bin_dir, systemd_dir, vaults)
    enable_probs, units_off = _enablement_problems(m, art, enabled_units or set())
    stale_probs, stale_notes = _staleness_problems(m, art, enabled_units or set())
    probs += enable_probs + stale_probs
    probs += _path_problems(sound, bin_dir)
    probs += _probe_problems(m, art)
    if emitted is not None:      # the resolved.env check owns the absent case
        probs += _needs_problems(m, emitted, emits)
    probs += collisions
    if inert:
        probs.append(f"inert manifest fields: {', '.join(inert)}")
    if probs:
        return Check(name, False, "; ".join(probs), section="artefacts")
    running = len([u for u in m.units if Path(u).name in (enabled_units or set())])
    return Check(name, True,
                 _ok_detail(m, bin_dir, vaults, units_off, running, stale_notes),
                 section="artefacts")


def drift_checks(root: Path, directory: Path, claimed: set) -> list:
    """Symlinks into this repo that no declared artefact claims.

    Two causes, and the report does not guess between them: an artefact dropped
    from a machine toml leaves its symlink behind (`hatch` has no uninstall), and
    a component outside the registry may have been hand-deployed on purpose.
    Nothing in this repo is a standing instance today: the serving guard used to be one and
    became a declared artefact, so the check is a net waiting for the next hand-deployed
    shim. It caught one on 2026-09-09 – the pre-rename `hatch-chat-guard` symlink, left
    dangling by a directory move that `hatch install` had no reason to clean up.
    Either way it is the one drift direction `install` structurally cannot report,
    because it visits only what is declared.
    """
    if not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if entry.name in claimed or not entry.is_symlink():
            continue
        # normpath, not is_relative_to on the raw join: `../../Repositories/hatch/…`
        # is lexically outside every root, and relative links are the local norm
        # — every Stow-managed entry in ~/.local/bin is one.
        target = Path(os.path.normpath(directory / entry.readlink()))
        if not target.is_relative_to(root):
            continue                      # not ours to have an opinion about
        if entry.exists():
            out.append(Check(entry.name, True, f"{entry} → {target}: no declared artefact "
                             "claims it — a leftover, or hand-deployed and unmanaged",
                             informational=True, section="drift"))
        else:
            out.append(Check(entry.name, False,
                             f"{entry} → {target}: dangling, and no declared artefact claims it",
                             section="drift"))
    return out


def resolved_env_check(root: Path, r):
    """The emitted file, and whether it still matches what the machine declares.

    Returns the Check and the parsed env (None when never written). A machine
    toml edited without a re-install leaves the two disagreeing, and every
    artefact goes on reading the stale file.
    """
    path = resolved_env_path(root)
    emitted = read_resolved_env(path)
    if emitted is None:
        return Check("resolved.env", False, f"{path} missing — run `hatch install`"), None
    expected = dict(r.env, HATCH_REPO=str(root))
    differ = sorted(k for k in set(expected) | set(emitted)
                    if emitted.get(k) != expected.get(k))
    if differ:
        return Check("resolved.env", False,
                     f"stale against the machine config: {', '.join(differ)} "
                     "— run `hatch install`"), emitted
    return Check("resolved.env", True, str(path)), emitted


def units_env_link_check(root: Path, link: Path = None) -> Check:
    """`~/.config/hatch/env`, which both serving units name as their EnvironmentFile.

    Checked because nothing else looks here: the drift scan searches the PATH directory, the
    systemd user directory and every `[[files]]` destination, and this link is install output
    rather than an artefact, so it has no `[[files]]` entry to be found by. Delete it and both
    units fail to start with `Failed to load environment files` while every other line is a tick.

    Resolved, not just stat'ed: the target is an absolute path into the checkout, so moving or
    renaming the repo leaves a link that exists and points at nothing.
    """
    from .layout import UNITS_ENV_LINK
    link = link or UNITS_ENV_LINK
    want = resolved_env_path(root)
    if not link.is_symlink() and not link.exists():
        return Check("units env", False,
                     f"{link} missing – the serving units name it as their EnvironmentFile "
                     "and will not start. Run `hatch install`")
    if not link.exists():
        return Check("units env", False,
                     f"{link} is a dangling link to {os.readlink(link)} – the checkout moved. "
                     "Run `hatch install`")
    if link.resolve() != want.resolve():
        return Check("units env", False,
                     f"{link} points at {link.resolve()}, not {want} – run `hatch install`")
    return Check("units env", True, f"{link} -> {want}")


def run_doctor(root: Path, hostname: str, bin_dir: Path = None,
               systemd_dir: Path = None) -> list:
    bin_dir = BIN_DIR if bin_dir is None else bin_dir
    systemd_dir = SYSTEMD_USER_DIR if systemd_dir is None else systemd_dir

    checks = [Check("python>=3.11", sys.version_info >= (3, 11),
                    f"{sys.version_info.major}.{sys.version_info.minor}")]
    cfg_path = machine_config_path(root, hostname)
    if not cfg_path.exists():
        checks.append(Check("config", False, f"missing {cfg_path.name}; run `hatch init`"))
        return checks
    cfg = load_machine_config(cfg_path)
    checks.append(Check("config", True, f"role={cfg.role}"))
    r = resolve(cfg, platform.detect())
    checks.append(Check("git", Path(r.env["GIT_BIN"]).exists() or shutil.which("git") is not None,
                        r.env["GIT_BIN"]))
    # Plugins are the only thing node builds. A serving box declares none, and failing its
    # doctor on a missing node would fail a correct install of the jaki guide.
    checks.append(Check("node", shutil.which("node") is not None, "needed for plugin builds",
                        informational=not cfg.artefacts.get(artefact_key("plugin"), [])))
    checks.append(Check("PATH", _on_path(bin_dir), f"{bin_dir} is where verbs and app shims land"))
    # The shim is in no machine's [artefacts] and is excluded from drift, so
    # without this it is the one deployed thing nothing checks — the bootstrap
    # blind spot `deploy_hatch_shim` exists to close.
    shim = _link_problem(bin_dir / "hatch", root / "hatch", "hatch")
    checks.append(Check("hatch shim", shim is None, shim or str(bin_dir / "hatch")))
    checks.append(Check("devonthink", r.env["HATCH_HAS_DEVONTHINK"] == "1",
                        "DT-gated verbs (recall -d / keep -p)", informational=True))
    env_check, emitted = resolved_env_check(root, r)
    checks.append(env_check)
    # Only where a serving unit actually names it. A laptop that deploys no serving tier has
    # no use for the link and should not be told it is missing one.
    if "model-serving" in cfg.artefacts.get(artefact_key("infra"), []):
        checks.append(units_env_link_check(root))

    vaults = vault_targets(cfg, r)
    emits = set(r.env) | {"HATCH_REPO"}
    claimed, deployed, dest_names = {"hatch": "hatch"}, {"hatch"}, {}
    shipped_units = set()
    for slot in ("verb", "app", "infra", "plugin"):
        for name in cfg.artefacts.get(artefact_key(slot), []):
            checks.append(artefact_check(root, name, slot, emitted, emits, bin_dir,
                                         systemd_dir, vaults, claimed, deployed,
                                         dest_names, cfg.enabled_units(), shipped_units))
    # A name in [units].enabled that no declared artefact ships enables nothing
    # and faults nothing — the silent-declaration class this whole check exists
    # to close, reintroduced one typo lower down.
    unknown = sorted(cfg.enabled_units() - shipped_units)
    if unknown:
        checks.append(Check("[units].enabled", False,
                            f"names no artefact on this machine ships: {', '.join(unknown)}",
                            section="artefacts"))
    # A `[[files]]` dest landing in one of the two standard dirs is claimed there
    # by name, not rescanned; anywhere else gets its own pass, because a strategy
    # that writes to arbitrary paths makes the scanned set a declared one.
    for d in (bin_dir, systemd_dir):
        deployed |= dest_names.pop(d, set())
    checks += drift_checks(root, bin_dir, deployed)
    checks += drift_checks(root, systemd_dir, deployed)
    for d in sorted(dest_names):
        checks += drift_checks(root, d, dest_names[d])
    return checks
