"""Where an artefact lives in the repo, and where its pieces land on a machine.

`install` writes these paths and `doctor` stats them. They come from one place
so the two cannot drift: a check that recomputes the path it is verifying
tests its own arithmetic, not the deploy.
"""
from pathlib import Path

BIN_DIR = Path.home() / ".local" / "bin"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

# The resolved env, at a path a systemd unit can name. `EnvironmentFile=` takes an absolute
# path and no variable but systemd's own `%h`, so a unit cannot point into a checkout whose
# location differs per machine; this fixed path is the indirection, and `install` symlinks it
# to the emitted file. Here rather than in `emit.py` so `doctor` stats the same constant
# `install` writes – the serving units hard-depend on it, and without a check they would fail
# to start while `doctor` reported everything present.
UNITS_ENV_LINK = Path.home() / ".config" / "hatch" / "env"


# The `[artefacts]` key an entry is listed under says WHERE it lives; the
# manifest's own `kind` says HOW it deploys. The two coincide for verbs, apps and
# plugins and part company for infra: `engine/infra/<name>` is a runtime-service
# directory whose deployable pieces are shims and units, which is the app
# strategy. Keeping them separate is what lets a service tier be provisioned
# without pretending to be machinery.
#
# Every slot has a tier directory, and a plugin's directory name IS its config
# name. Plugins lived in `engine/tools/` until 2026-08-23, which made them the
# one slot with no tier and forced a resolver that guessed between `<name>` and
# `<name>-plugin`; a name that has to be guessed is a name that can be wrong.
SLOTS = {
    "verb":   {"key": "verbs",   "tier": ("engine", "tools"),   "kinds": {"verb"}},
    "app":    {"key": "apps",    "tier": ("engine", "apps"),    "kinds": {"app"}},
    "infra":  {"key": "infra",   "tier": ("engine", "infra"),   "kinds": {"app"}},
    "plugin": {"key": "plugins", "tier": ("engine", "plugins"), "kinds": {"plugin"}},
}


def artefact_key(slot: str) -> str:
    """The `[artefacts]` table key entries for this slot are listed under."""
    return SLOTS[slot]["key"]


def slot_accepts(slot: str, kind: str) -> bool:
    """Whether a manifest of this kind may be listed under this slot."""
    return kind in SLOTS[slot]["kinds"]


def artefact_dir(root: Path, name: str, slot: str) -> Path:
    """The repo dir holding an artefact's source and its `provision.toml`."""
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {sorted(SLOTS)}, got {slot!r}")
    return root.joinpath(*SLOTS[slot]["tier"], name)


def verb_target(bin_dir: Path, m) -> Path:
    return bin_dir / m.exec_name


def app_bin_name(rel: str) -> str:
    """PATH name for an app bin entry: basename minus a `.sh` suffix."""
    return Path(rel).name.removesuffix(".sh")


def app_bin_names(m) -> list:
    return [e.name for e in m.bin]


def app_bin_target(bin_dir: Path, entry) -> Path:
    return bin_dir / entry.name


def file_target(entry) -> Path:
    """Where a `[[files]]` entry lands. Absolute and ~-expanded by the manifest.

    Trivial today, and here anyway: `doctor` must read the destination from the
    same function `install` wrote it through, or the check tests its own
    arithmetic rather than the deploy.
    """
    return entry.dest


def file_dirs(m) -> list:
    """The destination directories a manifest's `[[files]]` entries write into.

    The drift scan takes its search paths from these rather than from a fixed
    pair, because a `[[files]]` entry can put a symlink anywhere and a directory
    nobody scans is a directory where drift is invisible.
    """
    seen, out = set(), []
    for e in m.files:
        d = e.dest.parent
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def unit_target(systemd_dir: Path, rel: str) -> Path:
    return systemd_dir / Path(rel).name


def unit_is_enablable(source: Path) -> bool:
    """Whether the unit can be enabled at all — i.e. carries an `[Install]`.

    A timer-triggered oneshot (`papers-pushback.service`) has none: systemd
    starts it from the timer, and `systemctl enable` on it fails. Both `install`
    and `doctor` need this: one must not offer to enable such a unit, the other
    must not list it among units that are merely not running.
    """
    try:
        return any(line.strip() == "[Install]"
                   for line in source.read_text().splitlines())
    except OSError:
        return False


def plugin_link(vault: Path, m) -> Path:
    return vault / ".obsidian" / "plugins" / m.plugin_id


def vault_targets(cfg, resolved) -> list:
    """Vaults that plugins deploy into: the canonical VAULT plus any [endpoints].extra_vaults.

    Order-preserving (canonical first) and deduplicated, so listing the primary vault
    in extra_vaults doesn't link it twice. Plugins are the same on every vault (shared
    symlink source); which ones you *enable* stays a per-vault Obsidian gesture.
    """
    targets = []
    if resolved.env.get("VAULT"):
        targets.append(Path(resolved.env["VAULT"]))
    for extra in cfg.endpoints.get("extra_vaults", []):
        targets.append(Path(extra).expanduser())
    seen, unique = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def claim_path_names(claimed: dict, artefact: str, names: list) -> list:
    """Register an artefact's PATH names; return the collisions.

    PATH deployment is last-wins across strategies (a verb and an app shim with
    the same name silently shadow each other), so uniqueness is checked over the
    whole install, `hatch` itself included. `install` refuses the second
    claimant; `doctor` reports it, because otherwise the refusal shows up as a
    link pointing at the wrong source and re-running `install` cannot fix it.
    """
    collisions = [f"{n!r} already claimed by {claimed[n]}" for n in names if n in claimed]
    for n in names:
        claimed.setdefault(n, artefact)
    return collisions
