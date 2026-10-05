#!/usr/bin/env bash
# download-candidates.sh – hold the box's GGUF store at the state `engine/models.toml` declares.
#
#   download-candidates               fetch every `serving` file that is missing or short
#   download-candidates --check    compare the store to the manifest, download nothing
#
# THE MANIFEST LIVES IN THE REGISTRY, in `[[weights]]`, and nothing is repeated here. Each row
# names a file, where it comes from, its exact byte size and – where it is known – its digest.
# `hatch install` places this script on PATH as a SYMLINK into the checkout, so `readlink -f`
# finds the registry beside it; a copy would apply yesterday's manifest.
#
# ONLY `state = "serving"` ROWS ARE FETCHED. The `archived` rows are files the box keeps and
# nothing loads – retired models whose measurements cost GPU sessions. Fetching those on a
# fresh machine would be 374 GiB of metered uplink for weights no serve line names, and
# re-fetching one after it is deliberately pruned is the failure of 2026-08-06.
#
# Runs as a systemd --user service (download-candidates.service) at every boot, so it survives
# logout and reboot and finishes unattended. The unit holds `systemd-inhibit --what=sleep:idle`
# because the box powers itself off when idle and would otherwise sleep mid-download.
#
# NOTHING BOX-SIDE MAY REWRITE THIS FILE OR THE REGISTRY. Both are symlinks into the checkout,
# so a script that edits either dirties the box's repo and the next `hatch-sync` refuses its
# `git pull --ff-only`. Edit on the laptop and let the sync carry it.
#
# Transport is `curl -C -`, not `hf download`: huggingface_hub 1.x cannot resume a partial
# local-dir download across process restarts, so on a flaky link a multi-GB file restarts from
# zero every time the process dies (observed 2026-07-12: four abandoned partials for one 32.6 GB
# file). Completeness is checked by exact byte size, never by GGUF magic.
# On PATH the shim drops the extension, so the command is `download-candidates`; from the checkout it
# is this file's own name. Both are the same script.
set -uo pipefail

# GGUF store = <models>/gguf. MODELS_DIR (shared with other tools) points at <models>, so append
# /gguf – don't use it as the store directly.
STORE="${MODELS_DIR:-$HOME/.local/share/models}/gguf"
DL_MAX_RETRIES="${DL_MAX_RETRIES:-20}"

SELF_DIR=$(dirname "$(readlink -f "$0")")
# shellcheck source=../../lib/registry.sh
. "$SELF_DIR/../../lib/registry.sh"

CHECK_ONLY=0
case "${1:-}" in
  "")       ;;
  --check)  CHECK_ONLY=1 ;;
  # An unrecognised argument must not fall through to apply mode: `--dry-run` reading as
  # "fetch everything" is 120 GiB over a metered link and a rename of anything short.
  *)        echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
esac

# ONLY THE APPLYING RUN CREATES THE STORE, for the reason `verify_sha` gives below: a mode
# whose job is to say what the store looks like must not change what the store looks like.
# Creating it unconditionally left an empty `gguf` directory on the laptop after one `--check`
# there, and the taskboard read that as a store holding none of the box's models – seven
# complete models rendered as queued downloads on 2026-09-10.
[ "$CHECK_ONLY" = 1 ] || mkdir -p "$STORE"

# Fails when the read is SHORT as well as when it is empty; the terminator is what tells a
# truncated answer from a whole one. Acting on a truncated manifest would drop every row below
# the break out of the store's desired state while the run reported a match.
registry_read weights || exit 2
ROWS=("${REGISTRY_ROWS[@]}")

# dl REPO REMOTE_PATH BYTES LOCAL_NAME – curl-resume into <name>.part, verify the exact size,
# then move into place. REMOTE_PATH may include a subdirectory (split GGUFs live under their
# rung's directory); LOCAL_NAME flattens the parts side by side in $STORE, which is what
# llama.cpp expects when -m points at the 00001-of-000NN part.
dl() {
  local repo="$1" file="$2" want="$3" name="$4"
  local url="https://huggingface.co/$repo/resolve/main/$file"
  local part="$STORE/$name.part" have rc n=0
  if [ -f "$STORE/$name" ]; then
    have=$(stat -c %s "$STORE/$name")
    if [ "$have" = "$want" ]; then echo "[skip] $name complete ($want bytes)"; return 0; fi
    # QUARANTINE, NEVER DEMOTE-AND-RESUME. A file at the final name was size-verified once, at
    # whatever the pin said then – an interrupted download never gets there, it lives in .part.
    # So a mismatch means the pin moved or the file is damaged, and resuming from it eats it:
    # against a SHORTER upstream file curl 8.22 returns 0 having written nothing (measured
    # 2026-09-10), the size still mismatches, and the working model is now under a name no
    # serve line points at and no later run restores. Unsloth requantise UD quants in place,
    # so this is a live hazard and not a theoretical one.
    mv "$STORE/$name" "$STORE/$name.badsize" || {
      echo "[FAIL] $name is $have bytes, manifest says $want, AND it could not be moved aside." >&2
      return 1; }
    echo "[FAIL] $name is $have bytes, manifest says $want – quarantined as $name.badsize." >&2
    echo "       Either the pin moved (re-pin and re-pull) or the file is damaged. Nothing was" >&2
    echo "       resumed from it: resuming a requantised file destroys the copy you had." >&2
    return 1
  fi
  # BOUNDED. A permanent HTTP error – 404 on a renamed path, 401 on a repo that became gated –
  # is exit 22 with nothing downloaded, which the resume condition below never satisfies, so an
  # unbounded loop retries it forever. The unit holds a sleep:idle inhibitor for the life of the
  # process, so that also stops the box ever powering itself off. This is the mechanism behind
  # the six hours of retries on black-forest-labs in 2026-08-22; six hours is where someone
  # looked, not a bound. A flaky link is what the retries are for, and 20 at 15 s covers five
  # minutes of one.
  until curl -fsSL --http1.1 -C - \
        --connect-timeout 30 --speed-limit 10000 --speed-time 120 \
        --retry 3 --retry-delay 10 \
        -o "$part" "$url"; do
    rc=$?
    # exit 22 on HTTP 416 = the .part already spans the full range: check size and move on.
    # Kept as a belt for older curl. On 8.22 a Range past EOF is exit 0, so this never fires
    # and the loop exits normally instead (measured 2026-09-10).
    have=$(stat -c %s "$part" 2>/dev/null || echo 0)
    if [ "$rc" -eq 22 ] && [ "$have" = "$want" ]; then break; fi
    n=$((n + 1))
    if [ "$n" -ge "$DL_MAX_RETRIES" ]; then
      echo "[FAIL] $name: giving up after $n attempts (last curl exit $rc, have $(numfmt --to=iec "$have"))." >&2
      echo "       curl 22 with nothing downloaded means the URL is wrong or the repo is gated:" >&2
      echo "       $url" >&2
      return 1
    fi
    echo "[retry $n] $name (curl exit $rc, have $(numfmt --to=iec "$have")) – resuming in 15s" >&2; sleep 15
  done
  have=$(stat -c %s "$part" 2>/dev/null || echo 0)
  if [ "$have" != "$want" ]; then
    # The download completed and the file is the wrong length, which on this repo's history means
    # upstream requantised in place. Say what to do about it, and say the partial is still there:
    # nothing else in this script reports a .part, so an unmentioned 3 GB orphan is invisible.
    echo "[FAIL] $name: downloaded $have bytes, manifest pins $want." >&2
    echo "       Upstream may have replaced the artefact under the same name. Either sideload the" >&2
    echo "       pinned file, or re-pin this row and re-measure whatever was measured on it." >&2
    echo "       The partial is at $part and no later run removes it." >&2
    return 1
  fi
  # `mv` then `echo` would return the echo's status, so a failed move would print [done].
  mv "$part" "$STORE/$name" || {
    echo "[FAIL] $name: could not move $part into place" >&2; return 1; }
  echo "[done] $name ($want bytes, size-verified)"
}

# A digest is checked after a fresh pull for any row that declares one, and on EVERY run only
# where the row asks for it. Re-reading 100 GiB of resident weights at every boot would cost
# minutes of disk for a file nothing can have changed; the mirror-sourced VAE is the row that
# asks, because a re-upload at the same byte count is invisible to the size check and that
# repository is a third party.
# `--check` reports and never renames: a mode whose job is to say what the store looks like
# must not change what the store looks like. Only the applying run quarantines.
verify_sha() {
  local name="$1" want="$2" quarantine="$3" got
  # Existence and value are asserted separately. An unreadable file leaves `got` empty, which
  # compares unequal, and apply mode would then quarantine a good 50 GB artefact over a
  # transient read error and book a re-pull of it.
  if ! got=$(sha256sum "$STORE/$name"); then
    echo "[FAIL] $name could not be read to hash it; NOT quarantined – nothing about its" >&2
    echo "       contents is known either way." >&2
    return 1
  fi
  got=${got%% *}
  [ "$got" = "$want" ] && { echo "[hash] $name sha256 ok"; return 0; }
  echo "[FAIL] $name sha256 MISMATCH ($got, wanted $want)" >&2
  if [ "$quarantine" = 1 ]; then
    mv "$STORE/$name" "$STORE/$name.badhash" || {
      echo "       AND it could not be moved aside." >&2; return 1; }
    echo "       quarantined as $name.badhash; the source may have re-uploaded. The next run" >&2
    echo "       re-pulls it loudly, not silently." >&2
  fi
  return 1
}

fail=0
declared=()

if [ "$CHECK_ONLY" = 1 ]; then
  echo "== store check against $REGISTRY_PATH  ($(date -Is)) =="
else
  echo "== applying [[weights]] to $STORE  ($(date -Is)) =="
fi

for row in "${ROWS[@]}"; do
  # \x1f, never a tab: tab is an IFS *whitespace* character even when IFS is exactly a tab, so
  # consecutive tabs collapse and a row with an empty middle field reads back shifted by one.
  IFS="$REGISTRY_SEP" read -r state name repo path want sha always <<< "$row"
  declared+=("$name")

  if [ "$state" != "serving" ]; then
    # An archived row is still the store's desired state – it is just never fetched. Reporting
    # its absence keeps "deleted on purpose" and "deleted by accident" distinguishable.
    if [ ! -f "$STORE/$name" ]; then
      echo "[gone] $name – archived and not on disk; nothing fetches it"
    elif [ "$(stat -c %s "$STORE/$name")" != "$want" ]; then
      echo "[WARN] $name is archived and its size does not match the manifest" >&2
      fail=1
    fi
    continue
  fi

  if [ "$CHECK_ONLY" = 1 ]; then
    if [ ! -f "$STORE/$name" ]; then
      echo "[MISSING] $name ($want bytes, $repo)"; fail=1
    elif [ "$(stat -c %s "$STORE/$name")" != "$want" ]; then
      echo "[WRONG SIZE] $name is $(stat -c %s "$STORE/$name"), manifest says $want"; fail=1
    else
      echo "[have] $name"
      [ -n "$always" ] && [ -n "$sha" ] && { verify_sha "$name" "$sha" 0 || fail=1; }
    fi
    continue
  fi

  fresh=0
  [ -f "$STORE/$name" ] || fresh=1
  if ! dl "$repo" "$path" "$want" "$name"; then fail=1; continue; fi
  if [ -n "$sha" ] && { [ "$fresh" = 1 ] || [ -n "$always" ]; }; then
    verify_sha "$name" "$sha" 1 || fail=1
  fi
done

# A file in the store that no row declares is not an error, but it is unaccounted-for disk on a
# machine where the store is 478 GiB. Naming it is how a row that was deleted instead of being
# marked `archived` gets noticed.
while IFS= read -r f; do
  found=0
  for d in "${declared[@]}"; do [ "$d" = "$f" ] && { found=1; break; }; done
  case "$f" in
    *.badsize|*.badhash)
      echo "[quarantined] $f – moved aside by an earlier run; it is not a missing row"
      continue ;;
  esac
  [ "$found" = 0 ] && echo "[undeclared] $f – in the store, in no [[weights]] row"
# The `2>/dev/null` guards `ls`, and `cd` needs its own: on a host with no store this
# scan is the fresh-box verification step in machines/jaki/README.md, and a raw shell
# error mid-report is the first thing that box prints.
done < <(cd "$STORE" 2>/dev/null && ls -1 2>/dev/null | grep -v '\.part$')

if [ "$fail" != 0 ]; then
  echo "== FINISHED WITH FAILURES  ($(date -Is)) ==" >&2
  exit 1
fi
echo "== store matches [[weights]]  ($(date -Is)) =="
