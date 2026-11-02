"""Machine config — the committed, non-secret per-machine declaration.

One `machines/<hostname>.toml` per host, selected by normalised hostname.
Declares the machine's role, optional platform overrides, substrate
endpoints, which artefacts it wants, and which of their systemd units it
runs. Opaque to substrate identity: endpoints are just endpoints.

`[units].enabled` is per-machine because enablement is: the same app lands on
the laptop and the box, and which timer fires differs between them
(`note-collect` here, `note-transcribe` there). It belongs beside the artefact
list rather than in a `provision.toml`, which is machine-agnostic.

`[obsidian].community_plugins` names the third-party Obsidian plugins this
machine's hatch components depend on, each with the GitHub repo and release a
fresh install fetches (`obsidian.py`).
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
    obsidian: dict = field(default_factory=dict)  # {"community_plugins": [{id, repo, version}]}

    def enabled_units(self) -> set:
        """Unit basenames `install` enables and `doctor` requires to be enabled."""
        return set(self.units.get("enabled", []))


_PLUGIN_KEYS = ("id", "repo", "version")


def _check_obsidian(d) -> dict:
    """Refuse a community-plugin entry install could not act on, at load time.

    A missing key would otherwise surface as a 404 from GitHub naming a URL with
    `None` in it, or as a plugin doctor can never find.
    """
    if not isinstance(d, dict):
        raise ValueError(f"[obsidian] must be a table, got {d!r}")
    entries = d.get("community_plugins", [])
    if not isinstance(entries, list):
        raise ValueError("[obsidian].community_plugins must be a list of tables")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"[obsidian].community_plugins entries are tables, got {entry!r}")
        bad = [k for k in _PLUGIN_KEYS if not isinstance(entry.get(k), str) or not entry[k]]
        if bad:
            raise ValueError(f"[obsidian].community_plugins entry {entry!r} needs a non-empty "
                             f"string for {', '.join(bad)}")
        if entry["id"] in seen:
            raise ValueError(f"[obsidian].community_plugins lists {entry['id']!r} twice")
        seen.add(entry["id"])
    return d


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
        obsidian=_check_obsidian(d.get("obsidian", {})),
    )
