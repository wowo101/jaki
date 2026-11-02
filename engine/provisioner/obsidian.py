"""Community Obsidian plugins that hatch depends on: installed when absent, checked by doctor.

A machine toml lists them under `[obsidian].community_plugins`, each with its GitHub repo and
the release `install` fetches. The list is for plugins a hatch component needs in order to
work. The Readwise plugin carries highlights into the vault, the Agent Client is Pi's chat
surface, and Obsidian Git commits and pushes the vault. A fresh Obsidian install does not
bring them back, and Obsidian does not say a plugin is missing.

`install` leaves a plugin that is already in the vault alone. Obsidian updates community
plugins itself, and the plugin's directory holds its settings (`data.json`), so the pin says
what a fresh install gets. It does not say what has to be running. A missing plugin is
fetched into the canonical vault and added to `community-plugins.json`, the list Obsidian
reads at startup.

`doctor` faults a declared plugin that is missing or not enabled. A version other than the
pin is reported and not faulted. Obsidian's restricted mode stops every community plugin
from loading, but it is not recorded in the vault's files, so nothing here can see it.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .deploy import DeployResult

RELEASE_URL = "https://github.com/{repo}/releases/download/{version}/{file}"
REQUIRED_ASSETS = ("manifest.json", "main.js")
OPTIONAL_ASSETS = ("styles.css",)
ENABLED_LIST = Path(".obsidian") / "community-plugins.json"


def plugin_dir(vault: Path, plugin_id: str) -> Path:
    return vault / ".obsidian" / "plugins" / plugin_id


def fetch(url: str):
    """The asset's bytes, or None on 404. Any other failure raises.

    The request carries no identifying header beyond a bare tool name.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "hatch"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def enabled_ids(vault: Path):
    """The ids in `community-plugins.json`, or None when the file is absent or unreadable."""
    try:
        ids = json.loads((vault / ENABLED_LIST).read_text())
    except (OSError, ValueError):
        return None
    return ids if isinstance(ids, list) else None


def _read_manifest(path: Path) -> dict:
    """A plugin manifest as a dict. Raises OSError when absent, ValueError when not an object."""
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} is not a JSON object")
    return manifest


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".hatch-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def obsidian_running() -> bool:
    """Whether an Obsidian process is up. It may run as `obsidian` or as an Electron binary
    whose argv names Obsidian's `app.asar`."""
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(Path(a.decode(errors="replace")).name == "obsidian" or a.endswith(b"obsidian/app.asar")
               for a in argv[:3]):
            return True
    return False


def enable_plugin(vault: Path, plugin_id: str) -> bool:
    """Append the id to the enabled list. Returns whether the file changed.

    An unreadable list is refused instead of replaced: it names every other plugin
    the vault runs, and rewriting it with one entry would disable the rest.
    """
    path = vault / ENABLED_LIST
    ids = enabled_ids(vault)
    if ids is None and path.exists():
        raise ValueError(f"{path} is not a JSON list – fix or remove it by hand")
    ids = ids or []
    if plugin_id in ids:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(ids + [plugin_id], indent=2).encode())
    return True


def _download(spec: dict, fetcher) -> dict:
    """Every asset of the pinned release, checked against the declaration before anything
    is written. Raises ValueError naming the first problem."""
    files = {}
    for name in REQUIRED_ASSETS + OPTIONAL_ASSETS:
        url = RELEASE_URL.format(repo=spec["repo"], version=spec["version"], file=name)
        data = fetcher(url)
        if data is None:
            if name in REQUIRED_ASSETS:
                raise ValueError(f"{url} returned 404")
            continue
        files[name] = data
    try:
        manifest = json.loads(files["manifest.json"])
    except ValueError as e:
        raise ValueError(f"the release's manifest.json does not parse: {e}")
    if not isinstance(manifest, dict):
        raise ValueError("the release's manifest.json is not a JSON object")
    for key in ("id", "version"):
        if manifest.get(key) != spec[key]:
            raise ValueError(f"the release's manifest says {key}={manifest.get(key)!r}, "
                             f"the machine toml declares {spec[key]!r}")
    return files


def _installed(target: Path) -> bool:
    """A plugin counts as installed only when both files Obsidian needs are there.

    `manifest.json` is written last, so an interrupted install leaves no manifest and
    the next run fetches again."""
    return all((target / f).is_file() for f in REQUIRED_ASSETS)


def install_community_plugin(spec: dict, vault: Path, fetcher=fetch) -> DeployResult:
    """Fetch the plugin if it is absent, then make sure Obsidian will load it."""
    name = f"obsidian:{spec['id']}"
    if not vault.is_dir():
        # Creating `<vault>/.obsidian` from nothing would block a later clone into the path.
        return DeployResult(name, "error", f"vault {vault} does not exist")
    target = plugin_dir(vault, spec["id"])
    notes = []
    if not _installed(target):
        try:
            files = _download(spec, fetcher)
        except (OSError, ValueError) as e:
            return DeployResult(name, "error", f"could not fetch {spec['repo']} "
                                               f"{spec['version']}: {e}")
        # Written into an existing directory without clearing it: a directory with no
        # manifest may still hold the plugin's data.json.
        try:
            target.mkdir(parents=True, exist_ok=True)
            for fname in sorted(files, key=lambda f: f == "manifest.json"):
                _write_atomic(target / fname, files[fname])
        except OSError as e:
            return DeployResult(name, "error", f"could not write {target}: {e}")
        notes.append(f"installed {spec['version']}")
    try:
        if enable_plugin(vault, spec["id"]):
            notes.append("enabled")
    except (OSError, ValueError) as e:
        return DeployResult(name, "error", str(e))
    if not notes:
        return DeployResult(name, "deployed", f"present in {vault}")
    notes.append(restart_note())
    return DeployResult(name, "deployed", "; ".join(notes))


def restart_note() -> str:
    """What to do after the enabled list changed under Obsidian."""
    if obsidian_running():
        # A running Obsidian keeps the enabled list in memory and rewrites the file when
        # any plugin is toggled, which would drop an id appended behind its back.
        return "Obsidian is running: restart it before toggling any plugin"
    return "Obsidian loads it on its next start"


def community_vault(cfg, resolved):
    """(vault, error) for the community plugins this machine declares; both None when it
    declares none. One answer for install and doctor."""
    if not cfg.obsidian.get("community_plugins"):
        return None, None
    if not resolved.env.get("VAULT"):
        return None, "community plugins declared but no vault endpoint"
    return Path(resolved.env["VAULT"]), None


def community_plugin_problem(spec: dict, vault: Path):
    """(problem, detail) for doctor: problem is None when the plugin is installed and enabled."""
    if not vault.is_dir():
        return f"vault {vault} does not exist", ""
    target = plugin_dir(vault, spec["id"])
    try:
        manifest = _read_manifest(target / "manifest.json")
    except OSError:
        return f"not installed in {vault} – run `hatch install`", ""
    except ValueError as e:
        return f"{target / 'manifest.json'} is unreadable ({e}) – remove the directory and run `hatch install`", ""
    if manifest.get("id") != spec["id"]:
        return f"{target} holds plugin {manifest.get('id')!r}", ""
    if not (target / "main.js").is_file():
        return f"{target / 'main.js'} missing – remove the directory and run `hatch install`", ""
    ids = enabled_ids(vault)
    if ids is None or spec["id"] not in ids:
        return (f"installed but not enabled in {vault / ENABLED_LIST} – run `hatch install`, "
                f"or turn it on in Obsidian's settings"), ""
    version = manifest.get("version", "?")
    pin = "" if version == spec["version"] else f" (the toml pins {spec['version']} for a fresh install)"
    return None, f"{version} enabled{pin}"
