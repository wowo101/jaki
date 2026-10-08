"""Deploy strategies — how an artefact lands on this machine.

Three strategies, selected by manifest kind:
  - verb   → symlink the executable onto a PATH dir (capability-gated)
  - plugin → build, symlink into the vault's plugins dir, write data.json (Phase 2)
  - app    → workstation integration for an engine/apps/ resident: bin shims
             onto PATH + systemd user units linked (the app's *code* needs no
             deploy step — consumers reach it via `uv run --project`; see
             engine/provisioner/README.md)
"""
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .layout import (SYSTEMD_USER_DIR, app_bin_target, file_target, plugin_link,
                     unit_is_enablable, unit_target, verb_target)
from .manifest import missing_requires


@dataclass
class DeployResult:
    name: str
    status: str          # "deployed" | "skipped" | "error"
    detail: str = ""


def _symlink_replacing(target: Path, source: Path) -> None:
    """Point target at source, replacing an existing symlink or file."""
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


def _blocked_by_real_file(target: Path, adopt: bool) -> bool:
    """A regular file at the target blocks deployment unless --adopt replaces it.

    Symlinks are always ours to replace; a real file may be human-placed (or a
    pre-provisioner cp), so it is refused by default and adopted only on the
    explicit flag.
    """
    return not adopt and not target.is_symlink() and target.exists()


_REFUSE_HINT = "exists and is not a symlink — refusing to overwrite (rm it or `hatch install --adopt`)"


def _enable_unit(name: str) -> str:
    """Enable one unit, starting it only if it is a timer. Returns a detail string.

    `enable --now` on a timer is idempotent and costs nothing; on a service it
    starts a daemon, which is why services are enabled without it.
    """
    argv = ["systemctl", "--user", "enable"]
    if name.endswith(".timer"):
        argv.append("--now")
    argv.append(name)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        # Print what it saw: a bare "enable failed" costs a second session.
        return f"enable FAILED rc={r.returncode}: {(r.stderr or r.stdout).strip()}"
    return "enabled" + (" and started" if name.endswith(".timer") else " (starts at next boot)")


def deploy_units(m, artefact_dir: Path, systemd_dir: Path = None,
                 reload_units: bool = True, systemctl_available: bool = None,
                 adopt: bool = False, enable: set = None) -> list:
    """Symlink an artefact's systemd user units; one DeployResult per unit.

    Shared by `deploy_verb` and `deploy_app`, because a unit is a property of
    the artefact, not of its tier: a verb that runs on a timer declares one the
    same way an app does. Gated on systemctl being present, so the whole block
    skips on macOS (`systemctl_available` overrides the autodetect for tests).

    A unit named in the machine's `[units].enabled` is enabled here; the rest
    are linked only, and reported as a hint. Enablement is per-machine because
    the same app lands on several and a different timer fires on each. Timers
    are also started (`--now`), services are not: starting a daemon can seize a
    GPU or a port, and an install is not where that should happen. A service
    named in `[units].enabled` therefore comes up at the next boot.
    """
    if not m.units:
        return []
    if systemctl_available is None:
        systemctl_available = shutil.which("systemctl") is not None
    if not systemctl_available:
        return [DeployResult(f"{m.name}:units", "skipped", "requires systemctl")]

    results, linked, linked_units = [], False, []
    sd = systemd_dir or SYSTEMD_USER_DIR
    sd.mkdir(parents=True, exist_ok=True)
    for rel in m.units:
        source = artefact_dir / rel
        if not source.is_file():
            results.append(DeployResult(f"{m.name}:{source.name}", "error", f"{source} not found"))
            continue
        link = unit_target(sd, rel)
        if _blocked_by_real_file(link, adopt):
            results.append(DeployResult(f"{m.name}:{source.name}", "error", f"{link} {_REFUSE_HINT}"))
            continue
        _symlink_replacing(link, source)
        linked = True
        result = DeployResult(f"{m.name}:{source.name}", "deployed", str(link))
        # Pair name to its own result: a unit erroring mid-loop makes any
        # position arithmetic over `results` point at the wrong entry.
        linked_units.append((source.name, source, result))
        results.append(result)
    if linked and reload_units:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    # After the reload: systemd will not enable a unit it has not seen.
    wanted = enable or set()
    for name, source, result in linked_units:
        if not unit_is_enablable(source):
            # No [Install]: systemd starts it from its timer and `enable` on it
            # fails. Offering it as something to declare hands the human an
            # instruction that breaks their next install.
            continue
        if name not in wanted:
            result.detail += (f" (linked only — add {name!r} to [units].enabled "
                              f"in this machine's toml to run it)")
            continue
        detail = _enable_unit(name) if reload_units else "enable skipped (no reload)"
        result.detail += f" ({detail})"
        if "FAILED" in detail:
            result.status = "error"
    return results


def deploy_verb(m, resolved, artefact_dir: Path, bin_dir: Path, adopt: bool = False,
                systemd_dir: Path = None, reload_units: bool = True,
                systemctl_available: bool = None, enable: set = None) -> list:
    """Symlink the verb onto PATH, then link any units it declares.

    Returns a list, like `deploy_app`: a verb may carry units (one that runs on
    a timer needs them), and a manifest whose declaration nothing acts on is
    worse than one that is refused.
    """
    missing = missing_requires(m)
    if missing:
        return [DeployResult(m.name, "skipped", f"requires {', '.join(missing)}")]
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = verb_target(bin_dir, m)
    source = artefact_dir / m.exec_name
    if _blocked_by_real_file(target, adopt):
        return [DeployResult(m.name, "error", f"{target} {_REFUSE_HINT}")]
    _symlink_replacing(target, source)
    results = [DeployResult(m.name, "deployed", f"{target} -> {source}")]
    results.extend(deploy_units(m, artefact_dir, systemd_dir, reload_units,
                                systemctl_available, adopt, enable))
    return results


def deploy_app(m, resolved, artefact_dir: Path, bin_dir: Path,
               systemd_dir: Path = None, reload_units: bool = True,
               systemctl_available: bool = None, adopt: bool = False,
               enable: set = None) -> list:
    """Deploy an app's workstation integration; one DeployResult per item.

    Bin shims land under their basename minus a `.sh` suffix (PATH names don't
    carry extensions); an existing real file at the target is refused, never
    clobbered (mirrors link_plugin — only symlinks are replaced) unless
    `adopt` explicitly takes it over. Units are symlinked into the systemd user
    dir — gated on systemctl being present, so the whole block skips on macOS
    (`systemctl_available` overrides the autodetect for tests) — and enabled
    when the machine's `[units].enabled` names them; see `deploy_units`.
    """
    missing = missing_requires(m)
    if missing:
        return [DeployResult(m.name, "skipped", f"requires {', '.join(missing)}")]

    results = []
    bin_dir.mkdir(parents=True, exist_ok=True)
    seen_names = set()
    for entry in m.bin:
        source = artefact_dir / entry.source
        name = entry.name
        if name in seen_names:
            results.append(DeployResult(f"{m.name}:{name}", "error",
                                        f"duplicate PATH name {name!r} (from {entry.source})"))
            continue
        seen_names.add(name)
        if not source.is_file():
            results.append(DeployResult(f"{m.name}:{name}", "error", f"{source} not found"))
            continue
        target = app_bin_target(bin_dir, entry)
        if _blocked_by_real_file(target, adopt):
            results.append(DeployResult(f"{m.name}:{name}", "error", f"{target} {_REFUSE_HINT}"))
            continue
        _symlink_replacing(target, source)
        results.append(DeployResult(f"{m.name}:{name}", "deployed", f"{target} -> {source}"))

    # `[[files]]` entries are not systemd-gated, so they run even on a machine
    # whose units were skipped.
    results.extend(deploy_units(m, artefact_dir, systemd_dir, reload_units,
                                systemctl_available, adopt, enable))
    results.extend(deploy_files(m, artefact_dir, adopt=adopt))
    return results


def deploy_files(m, artefact_dir: Path, adopt: bool = False) -> list:
    """Symlink each `[[files]]` entry to its declared destination.

    The fourth strategy, for a file whose destination no other strategy can
    derive — a service that reads its config from a fixed path of its own
    choosing. Destination parents are created; a real file there is refused
    exactly as it is for shims and units, because it may be the hand-placed copy
    this strategy is replacing and clobbering it would destroy the evidence of
    what was running.
    """
    results = []
    seen = set()
    for entry in m.files:
        source = artefact_dir / entry.source
        target = file_target(entry)
        label = f"{m.name}:{target.name}"
        if target in seen:
            results.append(DeployResult(label, "error", f"duplicate dest {target}"))
            continue
        seen.add(target)
        if not source.is_file():
            results.append(DeployResult(label, "error", f"{source} not found"))
            continue
        if _blocked_by_real_file(target, adopt):
            results.append(DeployResult(label, "error", f"{target} {_REFUSE_HINT}"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _symlink_replacing(target, source)
        results.append(DeployResult(label, "deployed", f"{target} -> {source}"))
    return results


def render_data(data: dict, resolved) -> dict:
    """Substitute {git_bin}/{path_prepend}/{vault} tokens in a data.json template."""
    mapping = {
        "git_bin": resolved.env["GIT_BIN"],
        "path_prepend": resolved.env["PATH_PREPEND"],
        "vault": resolved.env["VAULT"],
        "repo": resolved.env.get("HATCH_REPO", ""),
    }
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            for token, val in mapping.items():
                v = v.replace("{" + token + "}", val)
        out[k] = v
    return out


def build_plugin(m, resolved, artefact_dir: Path) -> None:
    """Build the plugin (pnpm etc.) and write its data.json once.

    Called once per plugin regardless of how many vaults it links into: each vault
    entry is a symlink back to this dir, so they share the built files and the
    single machine-global data.json (the resolved config — git path, PATH prepend —
    is the same for every vault on the host). Raises CalledProcessError on build
    failure; the caller reports it and skips linking.
    """
    for cmd in m.build:                      # each cmd is an argv list — no shell, no injection surface
        subprocess.run(cmd, cwd=artefact_dir, check=True)
    if m.data:
        (artefact_dir / "data.json").write_text(json.dumps(render_data(m.data, resolved), indent=2))


def link_plugin(m, artefact_dir: Path, vault: Path) -> DeployResult:
    """Symlink an already-built plugin into one vault's .obsidian/plugins dir.

    Idempotent: replaces an existing symlink, but refuses to overwrite a real
    directory (a hand-installed copy) so nothing is silently clobbered.
    """
    link = plugin_link(vault, m)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        return DeployResult(m.name, "error", f"{link} exists and is not a symlink — refusing to overwrite")
    link.symlink_to(artefact_dir)
    return DeployResult(m.name, "deployed", str(link))
