"""Machine config — the committed, non-secret per-machine declaration.

One `machines/<hostname>.toml` per host, selected by normalised hostname.
Declares the machine's role, optional platform overrides, substrate
endpoints, which artefacts it wants, and which of their systemd units it
runs. Opaque to substrate identity: endpoints are just endpoints.

`[units].enabled` is per-machine because enablement is: the same app lands on
the laptop and the box, and which timer fires differs between them
(`note-collect` here, `note-transcribe` there). It belongs beside the artefact
list rather than in a `provision.toml`, which is machine-agnostic.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_ROLES = {"surface", "engine", "full"}


@dataclass
class MachineConfig:
    role: str
    description: str
    platform: dict       # raw [platform] overrides (opener, git_bin, path_prepend)
    endpoints: dict      # raw [endpoints] (vault, paperless_url, serve_url, pi_host)
    artefacts: dict      # {"verbs": [...], "plugins": [...]}
    # Defaulted: a machine toml with no [units] runs nothing unattended, which is
    # the right answer for a fresh one and keeps every existing caller valid.
    units: dict = field(default_factory=dict)   # {"enabled": [...]}

    def enabled_units(self) -> set:
        """Unit basenames `install` enables and `doctor` requires to be enabled."""
        return set(self.units.get("enabled", []))


def normalise_hostname(raw: str) -> str:
    name = raw.strip().lower()
    return name[:-6] if name.endswith(".local") else name


def machine_config_path(repo_root: Path, hostname: str) -> Path:
    return repo_root / "machines" / f"{normalise_hostname(hostname)}.toml"


def load_machine_config(path: Path) -> MachineConfig:
    if not path.exists():
        raise FileNotFoundError(f"no machine config at {path}; run `hatch init`")
    d = tomllib.loads(path.read_text())
    role = d.get("role")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
    return MachineConfig(
        role=role,
        description=d.get("description", ""),
        platform=d.get("platform", {}),
        endpoints=d.get("endpoints", {}),
        artefacts=d.get("artefacts", {}),
        units=d.get("units", {}),
    )
