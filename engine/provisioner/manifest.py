"""Per-artefact provisioning manifest — each verb/plugin declares its own deploy.

`hatch` supplies the generic deploy strategies (verb → symlink-on-PATH; plugin
→ build-and-link-into-vault); this manifest parametrises them. One file, one
owner: the recipe lives beside the artefact, not in the provisioner.
"""
import tomllib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .layout import app_bin_name


@dataclass(frozen=True)
class BinEntry:
    """One PATH shim: the repo-relative file, and the name it lands under.

    `bin = ["scripts/taskboard"]` derives the name from the basename; the table
    form `{source = "qmd-guard.sh", name = "qmd"}` is for the cases where the
    deployed name is not the repo's. A derived name is the common case and stays
    a bare string, so the table form appears only where it carries information.
    """
    source: str
    name: str


@dataclass(frozen=True)
class ProbeEntry:
    """A runtime prerequisite `doctor` can actually test.

    `requires` gates *deployment* on a binary being present. A probe answers the
    different question of whether the deployed thing can RUN, which for anything
    resting on a system package the provisioner cannot install is the only honest
    check: `browse` is a deployed symlink that resolves cleanly and a verb that
    dies on import until `webkitgtk-6.0` is installed by hand.

    `run` is argv, executed without a shell like `build`. `hint` is the
    remediation line, normally the one the artefact prints for itself.
    """
    run: tuple
    hint: str = ""


@dataclass(frozen=True)
class FileEntry:
    """One file symlinked to a path no strategy can derive.

    `dest` is absolute and `~`-expanded at parse time. There is deliberately no
    copy mode: a symlink carries its own provenance — it names the repo file it
    came from, so `doctor` tells current from stale by reading the link — while a
    copy carries nothing and is verifiable only against a record of what was
    written, which is state that goes stale on its own. Every consumer this
    strategy exists for reads a symlink transparently. If one ever cannot, a copy
    wants content hashing on top, and this entry already names both ends for it.
    """
    source: str
    dest: Path


@dataclass
class ArtefactManifest:
    name: str
    kind: str                                   # "verb" | "plugin" | "app"
    exec_name: str = None                       # verb: the file to put on PATH
    requires: list = field(default_factory=list)   # verb/app: binaries that gate deployment
    needs: list = field(default_factory=list)      # verb/app: resolved-env keys that must be non-empty (doctor)
    plugin_id: str = None                       # plugin: vault folder name
    build: list = field(default_factory=list)   # plugin: list of argv lists, run without a shell
    outputs: list = field(default_factory=list) # plugin: build outputs that must exist
    data: dict = field(default_factory=dict)    # plugin: data.json template ({git_bin} etc.)
    bin: list = field(default_factory=list)     # app: BinEntry, symlinked onto PATH
    units: list = field(default_factory=list)   # app: systemd user units to link (Linux-gated)
    files: list = field(default_factory=list)   # app: FileEntry, symlinked to an arbitrary path
    probe: list = field(default_factory=list)   # verb/app: ProbeEntry, runtime prerequisites (doctor)


def _parse_bin(raw, path: Path) -> list:
    out = []
    for e in raw:
        if isinstance(e, str):
            out.append(BinEntry(e, app_bin_name(e)))
            continue
        if not isinstance(e, dict):
            raise ValueError(f"{path}: a `bin` entry must be a string or a table, got {e!r}")
        unknown = set(e) - {"source", "name"}
        if unknown:
            raise ValueError(f"{path}: unknown key(s) in a `bin` entry: {', '.join(sorted(unknown))}")
        if not e.get("source") or not e.get("name"):
            raise ValueError(f"{path}: a `bin` table entry needs both `source` and `name`, got {e!r}")
        out.append(BinEntry(e["source"], e["name"]))
    return out


def _parse_files(raw, path: Path) -> list:
    out = []
    for e in raw:
        if not isinstance(e, dict):
            raise ValueError(f"{path}: a `files` entry must be a table with `source` and `dest`, got {e!r}")
        unknown = set(e) - {"source", "dest"}
        if unknown:
            raise ValueError(f"{path}: unknown key(s) in a `files` entry: {', '.join(sorted(unknown))}")
        if not e.get("source") or not e.get("dest"):
            raise ValueError(f"{path}: a `files` entry needs both `source` and `dest`, got {e!r}")
        dest = Path(e["dest"]).expanduser()
        if not dest.is_absolute():
            raise ValueError(f"{path}: `dest` must be absolute or ~-rooted, got {e['dest']!r}")
        out.append(FileEntry(e["source"], dest))
    return out


def _parse_probe(raw, path: Path) -> list:
    out = []
    for e in raw:
        unknown = set(e) - {"run", "hint"}
        if unknown:
            raise ValueError(f"{path}: unknown key(s) in a `probe` entry: {', '.join(sorted(unknown))}")
        run = e.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(a, str) for a in run):
            raise ValueError(f"{path}: a `probe` entry needs `run` as a non-empty list of strings")
        out.append(ProbeEntry(run=tuple(run), hint=e.get("hint", "")))
    return out


def load_manifest(artefact_dir: Path) -> ArtefactManifest:
    path = artefact_dir / "provision.toml"
    if not path.exists():
        raise FileNotFoundError(f"no provision.toml in {artefact_dir}")
    d = tomllib.loads(path.read_text())
    kind = d.get("kind")
    if kind not in {"verb", "plugin", "app"}:
        raise ValueError(f"kind must be verb|plugin|app, got {kind!r}")
    # Both are dereferenced into a path by their deploy strategy, so an omission
    # is a TypeError deep in install or doctor rather than a named manifest fault.
    if kind == "verb" and not d.get("exec"):
        raise ValueError(f"{path}: a verb manifest must declare `exec`")
    if kind == "plugin" and not d.get("id"):
        raise ValueError(f"{path}: a plugin manifest must declare `id`")
    return ArtefactManifest(
        name=artefact_dir.name,
        kind=kind,
        exec_name=d.get("exec"),
        requires=d.get("requires", []),
        needs=d.get("needs", []),
        plugin_id=d.get("id"),
        build=d.get("build", []),
        outputs=d.get("outputs", []),
        data=d.get("data", {}),
        bin=_parse_bin(d.get("bin", []), path),
        units=d.get("units", []),
        files=_parse_files(d.get("files", []), path),
        probe=_parse_probe(d.get("probe", []), path),
    )


def missing_requires(m) -> list:
    """The manifest's gate binaries that this machine does not have.

    One definition, because `deploy_verb`, `deploy_app` and `doctor` each need
    the same answer and three copies of a gate drift the way three copies of a
    path do.
    """
    return [r for r in m.requires if shutil.which(r) is None]
