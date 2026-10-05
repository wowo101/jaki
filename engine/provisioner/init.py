"""`hatch init` — scaffold this machine's config from the template.

Run once per fresh host; the user reviews/edits the file before `install`.
Never overwrites an existing machine config.
"""
import socket
from pathlib import Path

from . import platform
from .config import machine_config_path


def init_machine(root: Path) -> Path:
    dest = machine_config_path(root, socket.gethostname())
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; edit it directly")
    template_path = root / "machines" / "_template.toml"
    if not template_path.exists():
        raise FileNotFoundError(
            f"{template_path} is not in this checkout; a jaki checkout has none. "
            "`./jaki install <address>` writes the machine config instead.")
    template = template_path.read_text()
    plat = platform.detect()
    dest.write_text(template.replace(
        'description = ""',
        f'description = "{socket.gethostname()} ({plat.os})"'))
    return dest
