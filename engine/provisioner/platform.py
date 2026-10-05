"""Platform detection — the single place OS divergence is resolved.

Everything else in the provisioner (and every artefact downstream) consumes
the flat result; nothing else branches on the operating system.
"""
import platform as _sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PlatformInfo:
    os: str                # "darwin" | "linux" | other (lowercased platform.system())
    opener: str            # the URL/file opener: "open" (darwin) / "xdg-open" (linux)
    git_candidates: list   # ordered absolute-path guesses for the git binary
    path_dirs: list        # default PATH-prepend dirs for spawned-process env
    capabilities: dict     # {"devonthink": bool, "macwhisper": bool}


def _app(name: str) -> bool:
    """True if a macOS application bundle of this name is installed."""
    return Path(f"/Applications/{name}.app").exists()


def detect() -> PlatformInfo:
    system = _sys.system().lower()
    if system == "darwin":
        return PlatformInfo(
            os="darwin",
            opener="open",
            git_candidates=["/opt/homebrew/bin/git", "/usr/local/bin/git", "/usr/bin/git"],
            path_dirs=["/opt/homebrew/bin", "/usr/local/bin"],
            capabilities={
                "devonthink": _app("DEVONthink") or _app("DEVONthink 3"),
                "macwhisper": _app("MacWhisper"),
            },
        )
    return PlatformInfo(
        os=system,
        opener="xdg-open",
        git_candidates=["/usr/bin/git", "/usr/local/bin/git"],
        path_dirs=["/usr/local/bin"],
        capabilities={"devonthink": False, "macwhisper": False},
    )
