#!/usr/bin/env python3
"""Answer one question about `engine/models.toml`, for the bash scripts that act on it.

    registry-query.py <registry.toml> weights        # one row per [[weights]] entry, plus the
                                                    # archive's rows when models.archive.toml sits beside it
    registry-query.py <registry.toml> binaries       # one row per [[binaries]] entry
    registry-query.py <registry.toml> images         # one row per [[images]] entry
    registry-query.py <registry.toml> resident-gib   # swap_id and serve.resident_gib, per model
                                                    # that declares one
    registry-query.py <registry.toml> speech-models  # one id per [models.speech].model_ids

Every query prints its rows and then `###COMPLETE`. `engine/lib/registry.sh` requires that
last line, because a reader that raises halfway through still delivers everything it had
buffered — so without a terminator a truncated answer is indistinguishable from a whole one,
and the caller acts on a desired state that is missing its tail.

Fields within a row are separated by \x1f rather than a tab: bash collapses consecutive tabs
even when IFS is exactly a tab, so a row with an empty middle field would read back shifted.

Nothing is printed until every row is built, so a malformed row aborts before any output.
"""

from __future__ import annotations

import pathlib
import sys
import tomllib

SEP = "\x1f"
ARCHIVE_PATH: pathlib.Path | None = None   # set from the registry path in main()
TERMINATOR = "###COMPLETE"


def weights(reg: dict) -> list[str]:
    """state, file, repo, path, bytes, sha256, verify_always — the fetcher's whole input.

    The archived rows live in `engine/models.archive.toml` beside the registry, and are
    appended here when that file exists: on the box the store's whole desired state is
    declared, and on a machine without the archive nothing is said about files it never had.
    """
    rows = list(reg.get("weights", []))
    archive = ARCHIVE_PATH
    if archive and archive.exists():
        with open(archive, "rb") as fh:
            rows += tomllib.load(fh).get("weights", [])
    return [SEP.join([
        r["state"], r["file"], r["repo"], r["path"], str(r["bytes"]),
        r.get("sha256", ""), "1" if r.get("verify_always") else "",
    ]) for r in rows]


def speech_models(reg: dict) -> list[str]:
    return list(reg["models"]["speech"]["model_ids"])


def binaries(reg: dict) -> list[str]:
    """name, url, sha256, bytes, unpack, dest, verify, only – the fetcher's whole input."""
    return [SEP.join([
        b["name"], b["url"], b["sha256"], str(b["bytes"]),
        b["unpack"], b["dest"], b["verify"], b.get("only", ""),
    ]) for b in reg.get("binaries", [])]


def images(reg: dict) -> list[str]:
    """name, ref, digest, version, version_cmd – the image fetcher's whole input."""
    return [SEP.join([
        i["name"], i["ref"], i["digest"], i["version"], i["version_cmd"],
    ]) for i in reg.get("images", [])]


def resident_gib(reg: dict) -> list[str]:
    """swap_id, resident_gib – what the guard sizes a container entry by."""
    return [SEP.join([m["swap_id"], str(m["serve"]["resident_gib"])])
            for m in reg.get("models", {}).values()
            if m.get("swap_id") and "resident_gib" in m.get("serve", {})]


QUERIES = {"weights": weights, "speech-models": speech_models, "binaries": binaries,
           "images": images, "resident-gib": resident_gib}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in QUERIES:
        print(f"usage: registry-query.py <registry.toml> [{'|'.join(QUERIES)}]", file=sys.stderr)
        return 2
    path, query = sys.argv[1], sys.argv[2]
    global ARCHIVE_PATH
    ARCHIVE_PATH = pathlib.Path(path).with_name("models.archive.toml")
    try:
        with open(path, "rb") as fh:
            reg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"registry-query: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    try:
        rows = QUERIES[query](reg)
    except (KeyError, TypeError) as exc:
        print(f"registry-query: {query} is malformed in {path}: missing or wrong-typed {exc}",
              file=sys.stderr)
        return 1
    sys.stdout.write("".join(f"{r}\n" for r in rows))
    sys.stdout.write(TERMINATOR + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
