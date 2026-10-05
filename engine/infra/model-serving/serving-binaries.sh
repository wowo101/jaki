#!/usr/bin/env bash
# serving-binaries.sh – fetch the upstream releases and container images this repo does not carry.
#
# On PATH the shim drops the extension, so the command is `serving-binaries`; from the
# checkout it is this file's own name. Both are the same script.
#
#   serving-binaries            fetch and unpack anything missing
#   serving-binaries --check    report what is in place; exit 1 if something is missing,
#                               exit 2 if the check itself could not run
#
# The engine, the router and the image server are upstream builds. `[[binaries]]` in
# engine/models.toml pins each one's URL, digest and where it unpacks, and those paths are the
# ones `llama-swap.yaml` reaches through `${env.HATCH_BIN_<NAME>}`, which `hatch install` emits
# from the same rows. Nothing here is a second copy of a version: change the registry, run this,
# re-run `hatch install` so the router's environment names the new path, then restart the router.
#
# THE DIGEST IS CHECKED ON EVERY FETCH. These are executables and they are tens of megabytes,
# so the reason the weights are checked by size alone does not apply.
#
# `[[images]]` rows are container images a GPU entry runs from. Each is pulled as
# `ref@digest`, so podman itself refuses content that does not match; the digest podman then
# reports and the version the image prints are checked as well, because an image that is
# present under a digest can still be the wrong build if the row was edited by hand.
set -uo pipefail

SELF_DIR=$(dirname "$(readlink -f "$0")")
# shellcheck source=../../lib/registry.sh
. "$SELF_DIR/../../lib/registry.sh"

CHECK_ONLY=0
case "${1:-}" in
  "")       ;;
  --check)  CHECK_ONLY=1 ;;
  *)        echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
esac

# Fails when the read is SHORT as well as when it is empty; see registry.sh for why a
# truncated answer would otherwise read as a complete one.
registry_read binaries || exit 2

# `|| exit` because an unchecked failure leaves TMP empty, which makes the trap `rm -rf ""`
# and every download target an absolute path at the filesystem root, reported as a failed
# download instead of as a failed mktemp.
TMP=$(mktemp -d -t serving-binaries.XXXXXX) || { echo "cannot make a temp dir" >&2; exit 2; }
trap 'rm -rf "$TMP"' EXIT

fail=0
echo "== serving binaries against $REGISTRY_PATH  ($(date -Is)) =="

for row in "${REGISTRY_ROWS[@]}"; do
  IFS="$REGISTRY_SEP" read -r name url sha want unpack dest verify only <<< "$row"
  dest=${dest/#\~/$HOME}
  target="$dest/$verify"

  # The artefact is the unpacked binary, not the archive: an archive that downloaded and
  # unpacked to the wrong shape leaves the previous binary in place, and the router goes on
  # working until its next restart. `-x` and not `-f`, because a file that unpacked without
  # its execute bit is a start that fails with `upstream command exited prematurely`.
  #
  # PRESENT IS NOT ENOUGH: the pin has to match too. For the engine and the image server the
  # version is in `dest`, so a bump changes the path and an old binary reads as absent. THE
  # ROUTER HAS NO VERSION IN ITS PATH – it unpacks to ~/.local/bin/llama-swap – so a bump used
  # to print `[have]`, fetch nothing and exit 0, which is what the guide's own change table
  # tells you to run. The sidecar records which pin produced the file that is there.
  # `verify` may carry a directory (`vulkan/llama-server`), so the stamp name flattens it:
  # `.vulkan_llama-server.sha256`. With the slash kept, the stamp path named a directory
  # that does not exist, the write failed after `[done]`, and every later run read the
  # engine as `[STALE]` and re-fetched it – the state the box was in until 2026-09-11.
  stamp="$dest/.${verify//\//_}.sha256"
  if [ -x "$target" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$sha" ]; then
    echo "[have] $name -> $target"
    continue
  fi
  if [ -x "$target" ] && [ "$CHECK_ONLY" = 1 ]; then
    echo "[STALE] $name -> $target was not unpacked from the pinned archive ($sha)"
    fail=1
    continue
  fi
  if [ -x "$target" ]; then
    echo "[bump] $name: replacing the binary at $target, which is not from the pinned archive"
  fi
  if [ -e "$target" ] && [ ! -x "$target" ]; then
    echo "[FAIL] $name: $target exists and is not executable" >&2
    fail=1
    continue
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    echo "[MISSING] $name -> $target  ($(numfmt --to=iec "$want") from $url)"
    fail=1
    continue
  fi

  archive="$TMP/$name.archive"
  echo "[fetch] $name  $(numfmt --to=iec "$want")"
  # No `-C -`: the scratch directory is new on every run, so there is never a partial to
  # resume from and the flag would only imply one. curl's own --retry covers a flaky link
  # within a run; a run that dies restarts the archive, which is tens of megabytes and not
  # the tens of gigabytes download-candidates.sh resumes byte-exact.
  if ! curl -fsSL --http1.1 --connect-timeout 30 --retry 5 --retry-delay 10 \
        -o "$archive" "$url"; then
    echo "[FAIL] $name: download failed – $url" >&2
    fail=1
    continue
  fi

  have=$(stat -c %s "$archive" 2>/dev/null || echo 0)
  if [ "$have" != "$want" ]; then
    echo "[FAIL] $name: downloaded $have bytes, registry pins $want. Upstream may have" >&2
    echo "       replaced the asset; re-pin the row after checking what changed." >&2
    fail=1
    continue
  fi
  # Existence and value asserted separately: an unreadable file hashes to the empty string,
  # which compares unequal and would report a tamper where there was a read error.
  if ! got=$(sha256sum "$archive"); then
    echo "[FAIL] $name: could not read the download to hash it" >&2; fail=1; continue
  fi
  got=${got%% *}
  if [ "$got" != "$sha" ]; then
    echo "[FAIL] $name: sha256 $got, registry pins $sha – NOT unpacked." >&2
    fail=1
    continue
  fi
  echo "[hash] $name sha256 ok"

  mkdir -p "$dest"
  # `only` extracts one member, for an archive whose other files have no business at the
  # destination – llama-swap ships a README and a licence beside its binary, and its
  # destination is a PATH directory.
  unpack_ok=0
  case "$unpack" in
    tar) if [ -n "$only" ]; then tar -xzf "$archive" -C "$dest" "$only" && unpack_ok=1
         else tar -xzf "$archive" -C "$dest" && unpack_ok=1; fi ;;
    zip) if [ -n "$only" ]; then unzip -oq "$archive" "$only" -d "$dest" && unpack_ok=1
         else unzip -oq "$archive" -d "$dest" && unpack_ok=1; fi ;;
    *)   echo "[FAIL] $name: unknown unpack format $unpack" >&2; fail=1; continue ;;
  esac
  if [ "$unpack_ok" != 1 ]; then
    echo "[FAIL] $name: unpacking into $dest failed" >&2; fail=1; continue
  fi

  chmod +x "$target" 2>/dev/null
  if [ ! -x "$target" ]; then
    echo "[FAIL] $name: unpacked, but $verify is not there or is not executable." >&2
    echo "       The archive shape is not what the registry expects; look inside before re-pinning." >&2
    ls -1 "$dest" | head -10 >&2
    fail=1
    continue
  fi
  # Written only after the artefact is in place and executable, so a stamp never claims a
  # file that is not there. A missing stamp reads as "not from this pin", which is what a
  # machine that installed before 2026-09-10 truthfully is.
  if ! printf '%s\n' "$sha" > "$stamp"; then
    echo "[FAIL] $name: unpacked, but the stamp $stamp could not be written; --check will read it as STALE" >&2
    fail=1
    continue
  fi
  echo "[done] $name -> $target"
done

# ── container images ────────────────────────────────────────────────────────────────
if ! registry_read images; then
  fail=1
else
  for row in "${REGISTRY_ROWS[@]}"; do
    IFS="$REGISTRY_SEP" read -r name ref digest version version_cmd <<< "$row"
    target="$ref@$digest"
    if ! podman image exists "$target" 2>/dev/null; then
      if [ "$CHECK_ONLY" = 1 ]; then
        echo "[MISSING] image $name -> $target"
        fail=1
        continue
      fi
      echo "[pull] image $name  $target"
      if ! podman pull -q "$target" >/dev/null; then
        echo "[FAIL] image $name: podman pull $target failed" >&2
        fail=1
        continue
      fi
    fi
    got=$(podman image inspect --format '{{.Digest}}' "$target" 2>/dev/null)
    if [ "$got" != "$digest" ]; then
      echo "[FAIL] image $name: podman reports digest '${got:-nothing}', registry pins $digest" >&2
      fail=1
      continue
    fi
    # shellcheck disable=SC2086  # version_cmd is a command line, split on purpose
    said=$(timeout 60 podman run --rm "$target" $version_cmd 2>&1 | head -1)
    if [ "$said" != "$version" ]; then
      echo "[FAIL] image $name: \`$version_cmd\` printed '${said:-nothing}', registry pins '$version'" >&2
      fail=1
      continue
    fi
    echo "[have] image $name -> $target ($version)"
  done
fi

if [ "$fail" != 0 ]; then
  echo "== FINISHED WITH FAILURES  ($(date -Is)) ==" >&2
  exit 1
fi
echo "== every [[binaries]] and [[images]] row is in place  ($(date -Is)) =="
