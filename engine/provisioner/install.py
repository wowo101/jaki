"""`hatch install` — resolve, emit, deploy the artefacts this role calls for.

Idempotent from a provisioner-prior state. Writes engine/.hatch/resolved.env,
self-deploys the `hatch` shim onto PATH, then deploys each declared verb
(symlink-on-PATH), app (bin shims + systemd user units) and plugin (build +
symlink-into-vault). Units named in the machine's `[units].enabled` are enabled
as they are linked. Returns DeployResults for the CLI to report.

Regular files at deploy targets (e.g. pre-provisioner cp'd units) are refused
by default; `--adopt` replaces them. Real *directories* (hand-installed plugin
copies) are always refused — they may hold user data.
"""
import subprocess
from pathlib import Path

from . import platform
from .config import machine_config_path, load_machine_config
from .resolve import resolve
from .emit import link_env_for_units, resolved_env_path, write_resolved_env
from .manifest import load_manifest
from .deploy import (deploy_verb, deploy_app, build_plugin, link_plugin,
                     DeployResult, _blocked_by_real_file, _symlink_replacing,
                     _REFUSE_HINT)
from .layout import (BIN_DIR, artefact_dir, artefact_key, app_bin_names,
                     claim_path_names, slot_accepts, vault_targets)


def deploy_hatch_shim(root: Path, bin_dir: Path, adopt: bool = False) -> DeployResult:
    """The provisioner deploys itself: `hatch <verb>` must work system-wide,
    not just as ./hatch from the repo root. The shim self-resolves through
    symlinks (Path(__file__).resolve()), so a plain symlink suffices.
    """
    source = root / "hatch"
    target = bin_dir / "hatch"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if _blocked_by_real_file(target, adopt):
        return DeployResult("hatch", "error", f"{target} {_REFUSE_HINT}")
    _symlink_replacing(target, source)
    return DeployResult("hatch", "deployed", f"{target} -> {source}")


def _ready(root: Path, name: str, slot: str, claimed: dict, path_names):
    """Load and vet one declared artefact; return (manifest, dir) or (None, DeployResult).

    The deploy strategies differ; getting to them does not — resolve the dir from
    the slot it was listed under, load the manifest, check its kind is one that
    slot accepts, claim its PATH names. `doctor` walks the registry the same way.
    """
    art = artefact_dir(root, name, slot)
    try:
        m = load_manifest(art)
    except (FileNotFoundError, ValueError) as e:
        return None, DeployResult(name, "error", str(e))
    if not slot_accepts(slot, m.kind):
        return None, DeployResult(
            name, "error",
            f"listed under [artefacts].{artefact_key(slot)} but its manifest says kind={m.kind!r}")
    collisions = claim_path_names(claimed, name, path_names(m))
    if collisions:
        return None, DeployResult(name, "error", f"PATH collision: {'; '.join(collisions)}")
    return m, art


def install(root: Path, hostname: str, adopt: bool = False) -> list:
    cfg = load_machine_config(machine_config_path(root, hostname))
    r = resolve(cfg, platform.detect())
    r.env["HATCH_REPO"] = str(root)        # exposed to data.json templates via {repo}
    write_resolved_env(r, resolved_env_path(root))
    # The serving units read this path as their EnvironmentFile, and llama-swap resolves the
    # `${env.…}` macros in its config out of the same environment. Before it existed, three
    # files carried this machine's address and home directory as literals.
    try:
        env_link = link_env_for_units(resolved_env_path(root))
        env_link_result = DeployResult("hatch:units-env", "deployed",
                                       f"{env_link} -> {resolved_env_path(root)}")
    except OSError as exc:
        # Reported, not raised: this is one deploy target among many, and an install that
        # dies here leaves a machine half-provisioned with no line saying which half.
        env_link_result = DeployResult("hatch:units-env", "error", str(exc))

    enable = cfg.enabled_units()
    claimed = {}
    results = [env_link_result, deploy_hatch_shim(root, BIN_DIR, adopt)]
    claim_path_names(claimed, "hatch", ["hatch"])

    for verb in cfg.artefacts.get(artefact_key("verb"), []):
        m, art = _ready(root, verb, "verb", claimed, lambda m: [m.exec_name])
        if m is None:
            results.append(art)
            continue
        results.extend(deploy_verb(m, r, art, BIN_DIR, adopt=adopt, enable=enable))

    # infra shares the app strategy — shims on PATH, units linked — and differs
    # only in which tier its source lives in.
    for slot in ("app", "infra"):
        for name in cfg.artefacts.get(artefact_key(slot), []):
            m, art = _ready(root, name, slot, claimed, app_bin_names)
            if m is None:
                results.append(art)
                continue
            results.extend(deploy_app(m, r, art, BIN_DIR, adopt=adopt, enable=enable))

    vaults = vault_targets(cfg, r)
    for plugin in cfg.artefacts.get(artefact_key("plugin"), []):
        m, art = _ready(root, plugin, "plugin", claimed, lambda m: [])
        if m is None:
            results.append(art)
            continue
        if not vaults:
            results.append(DeployResult(plugin, "error", "no vault endpoint configured"))
            continue
        try:
            build_plugin(m, r, art)          # build + data.json once, then link into each vault
        except subprocess.CalledProcessError as e:
            results.append(DeployResult(plugin, "error", f"build failed: {e}"))
            continue
        for vault in vaults:
            results.append(link_plugin(m, art, vault))

    return results
