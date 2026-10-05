"""Resolve config + platform into a flat env every artefact consumes.

This is the runtime abstraction boundary: all OS/machine divergence collapses
here into a flat KEY=VALUE map. The verbs and plugins read it; they never
branch on the operating system again. Secrets are NOT included — they stay in
`.env`, which the verbs source alongside the emitted resolved.env.
"""
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .platform import PlatformInfo
from .config import MachineConfig


@dataclass
class Resolved:
    role: str
    env: dict          # flat resolved (non-secret) config
    platform: PlatformInfo
    config: MachineConfig


def _first_existing(candidates):
    for c in candidates:
        if Path(c).exists():
            return c
    return None


# Open WebUI's port. A constant because nothing configures it: the unit sets it and the
# browser URL quotes it, and a second machine changing it would have to change both anyway.
WEBUI_PORT = 3000


def _serving(serve_url: str) -> dict:
    """The serving address, in the three shapes the units and the router config need.

    ALL THREE ARE DERIVED FROM ONE VALUE, `[endpoints].serve_url`, so a machine states its
    address once. They existed before as literals inside `llama-swap.service`,
    `open-webui.service` and `llama-swap.yaml`, which is why installing on a second machine
    meant hand-editing three files and why leaving one unedited produced a router that could
    not bind an address that was not local.

    Empty in, empty out: a machine with no serving endpoint is one that serves nothing, and the
    router config's own `${env.…}` lookup then fails loudly at load rather than binding
    something arbitrary."""
    if not serve_url:
        return {}
    u = urlparse(serve_url)
    if not u.hostname:
        # A VALUE THAT DOES NOT PARSE IS NOT A MISSING VALUE. `urlparse("box:9090/v1")`
        # returns no hostname, so one omitted scheme used to fall through to the same
        # empty result as an unset key – and an empty HATCH_SERVE_ADDR reaches systemd as
        # an EMPTY ARGUMENT, which llama-swap answers by defaulting to `:8080` and binding
        # 0.0.0.0. Open WebUI does worse with an empty HOST and an empty base URL: it binds
        # 0.0.0.0 with signup open and points its chat at api.openai.com.
        raise ValueError(
            f"[endpoints].serve_url is {serve_url!r}, which has no host. It needs a scheme: "
            f"http://<address>:<port>/v1")
    port = u.port or (443 if u.scheme == "https" else 80)
    return {
        # `--listen` for llama-swap: host and port, no scheme and no path.
        "HATCH_SERVE_ADDR": f"{u.hostname}:{port}",
        # What Open WebUI binds under --network=host. The same host, because Open WebUI and
        # the router are the same machine seen from the same tailnet.
        "HATCH_SERVE_HOST": u.hostname,
        # What the browser is told to call itself. MagicDNS name, so a bookmark survives the
        # tailnet address changing.
        "HATCH_WEBUI_URL": f"http://{socket.gethostname()}:{WEBUI_PORT}",
    }


def _registry_env() -> dict:
    """`HATCH_BIN_<NAME>` for every `[[binaries]]` row and `HATCH_IMG_<NAME>` (`ref@digest`) for
    every `[[images]]` row, so the router config names no version and no digest.

    The version used to appear in the registry row AND in the path inside `llama-swap.yaml`,
    which made a bump a two-edit ritual. llama-swap resolves `${env.…}` at config load, so the
    seam was already open.

    A registry that cannot be read yields nothing, and llama-swap then fails at config load
    naming the variable, which is the same loud failure an unset HATCH_HOME gives.
    """
    import tomllib
    reg = Path(__file__).resolve().parents[1] / "models.toml"
    try:
        with reg.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out = {}
    for b in data.get("binaries", []):
        try:
            dest = b["dest"].replace("~", str(Path.home()), 1)
            out["HATCH_BIN_" + b["name"].upper().replace("-", "_")] = f"{dest}/{b['verify']}"
        except KeyError:
            continue
    for i in data.get("images", []):
        try:
            out["HATCH_IMG_" + i["name"].upper().replace("-", "_")] = f"{i['ref']}@{i['digest']}"
        except KeyError:
            continue
    return out


def resolve(config: MachineConfig, plat: PlatformInfo) -> Resolved:
    p, ep, caps = config.platform, config.endpoints, plat.capabilities
    git_bin = p.get("git_bin") or _first_existing(plat.git_candidates) or shutil.which("git") or "git"
    path_prepend = p.get("path_prepend") or plat.path_dirs
    vault = ep.get("vault", "")
    serve_url = ep.get("serve_url") or ep.get("ollama_url", "")
    env = {
        "HATCH_ROLE": config.role,
        "OPENER": p.get("opener") or plat.opener,
        "GIT_BIN": git_bin,
        "PATH_PREPEND": ":".join(path_prepend),
        "VAULT": str(Path(vault).expanduser()) if vault else "",
        "PAPERLESS_URL": ep.get("paperless_url", ""),
        # THE SERVING ENDPOINT, under both names. `serve_url` is what a machine states;
        # `ollama_url` is the name it had when the box ran Ollama, which it has not since
        # 2026-07, and it is still accepted so no machine toml has to change in the same
        # commit that renames the key.
        #
        # OLLAMA_URL IS STILL EMITTED, for the same reason: `keep`, `note` and `gpu-lease`
        # read it out of resolved.env at runtime, and a rename that lands before its readers
        # do switches a capture sweep off with no signal anywhere. Both names carry the same
        # value; the old one goes when nothing reads it, which `models-check.py` asserts.
        "HATCH_SERVE_URL": serve_url,
        "OLLAMA_URL": serve_url,
        # Separate from OLLAMA_URL despite pointing at the same llama-server: that
        # value carries the OpenAI-compatible `/v1` suffix, and the transcriber's
        # client appends its own paths (`/health`, `/v1/chat/completions`) to this
        # one. Reusing OLLAMA_URL asks for /v1/health and /v1/v1/chat/completions.
        "TRANSCRIBER_LLM_ENDPOINT": ep.get("transcriber_llm_endpoint", ""),
        # Which machine diarizes a meeting. `pipeline.meeting` builds the diarizer
        # itself and takes no host argument, so this is the only way to move it.
        "TRANSCRIBER_DIARIZE_HOST": ep.get("transcriber_diarize_host", ""),
        "PI_HOST": ep.get("pi_host", "local"),
        "HATCH_HAS_DEVONTHINK": "1" if caps.get("devonthink") else "0",
        "HATCH_HAS_MACWHISPER": "1" if caps.get("macwhisper") else "0",
        # The serving tier's units and router config are symlinked verbatim and carry no
        # machine's paths of their own, so they read this instead of embedding a home
        # directory. llama-swap resolves it as `${env.HATCH_HOME}` and errors at config load
        # when it is unset, which is the failure this replaced a silent wrong path with.
        "HATCH_HOME": str(Path.home()),
    }
    env.update(_serving(serve_url))
    env.update(_registry_env())
    # A machine that deploys the serving tier and states no address would install two units
    # that start, bind the wrong thing and report `active`. Nothing downstream can catch that:
    # systemd sees no failure, and llama-swap and Open WebUI both have defaults.
    infra = config.artefacts.get("infra", []) or config.artefacts.get("infras", [])
    if "model-serving" in infra and not env.get("HATCH_SERVE_ADDR"):
        raise ValueError(
            "this machine deploys `model-serving` and states no [endpoints].serve_url, so "
            "llama-swap would bind 0.0.0.0:8080 and Open WebUI would bind 0.0.0.0:3000 with "
            "signup open. Set serve_url to this machine's own address.")
    return Resolved(role=config.role, env=env, platform=plat, config=config)
