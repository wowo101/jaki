#!/usr/bin/env bash
# speaches-models.sh – hold the speech server's model cache at its declared contents.
#
# The `speech` entry in llama-swap.yaml runs with HF_HUB_OFFLINE=1, so it never downloads
# anything: a model that is not in the cache fails the request instead of pulling gigabytes
# over a slow uplink while someone waits. This script is the only path by which those
# models arrive, the way `download-candidates.sh` is for the GGUF store.
#
#   speaches-models            fetch every declared model that is not already cached
#   speaches-models --check    report what is cached; exit 1 if a model is missing,
#                              exit 2 if the check itself could not run
#
# It fetches by asking speaches itself (POST /v1/models/<id>), because speaches knows which
# files each model needs – faster-whisper wants five specific files out of its repo, Piper
# wants two. A hand-written file list here would be a second copy of that knowledge and would
# go stale the next time the executor changes.
#
# It runs its own short-lived container on a scratch port rather than talking to the serving
# one, which is offline by design. The cache is a named podman volume shared between the two.
# On PATH the shim drops the extension, so the command is `speaches-models`; from the checkout it
# is this file's own name. Both are the same script.
set -uo pipefail

IMAGE="${SPEACHES_IMAGE:-ghcr.io/speaches-ai/speaches:0.8.3-cpu}"
VOLUME="${SPEACHES_VOLUME:-speaches-hf-cache}"
PORT="${SPEACHES_FETCH_PORT:-18234}"
NAME="speaches-fetch"

# THE DECLARED SET COMES FROM THE REGISTRY, `[models.speech].model_ids`, and is not repeated
# here. `hatch install` places this script on PATH as a SYMLINK into the checkout, so
# `engine/lib/registry.sh` resolves beside it the way `gpu-binaries.env` resolves for
# hatch-serving-guard; a copy would silently fetch yesterday's list.
#
# The same list is aliased onto the `speech` entry in llama-swap.yaml, which is what makes an id
# routable, and models-check.py compares the registry against both the router's alias list and
# what the running server holds. Adding a model is two edits: the registry and the router.
SELF_DIR=$(dirname "$(readlink -f "$0")")
# shellcheck source=../../lib/registry.sh
. "$SELF_DIR/../../lib/registry.sh"

# Fails when the read is SHORT as well as when it is empty. A reader that raises halfway still
# delivers what it had buffered, so without the terminator registry.sh requires, a partial list
# would read as a complete one and this script would report a cache it had not finished checking.
registry_read speech-models || exit 2
MODELS=("${REGISTRY_ROWS[@]}")

CHECK_ONLY=0
case "${1:-}" in
  "")       ;;
  --check)  CHECK_ONLY=1 ;;
  # An unrecognised argument must not fall through to fetch mode: `--dry-run` reading as
  # "download everything" is a 2 GB surprise on a metered link.
  *)        echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
esac

PULLBODY=$(mktemp -t speaches-pull.XXXXXX)
cleanup() { podman rm -f "$NAME" >/dev/null 2>&1 || true; rm -f "$PULLBODY"; }
trap cleanup EXIT

podman rm -f "$NAME" >/dev/null 2>&1 || true

# --check runs offline too, so it reports on the cache rather than on the network.
offline=()
[ "$CHECK_ONLY" = 1 ] && offline=(-e HF_HUB_OFFLINE=1)

# NO --rm. A detached container that exits is removed immediately, taking with it the logs the
# health-timeout path below exists to print – and a crash at startup is the likelier failure
# than a hang. `cleanup` removes it instead, after anything worth reading has been read. The
# known crash is a permission error on the cache mount when the volume is not owned by uid 1000.
if ! podman run -d --name "$NAME" \
      -p "127.0.0.1:$PORT:$PORT" \
      -e UVICORN_PORT="$PORT" \
      -e ENABLE_UI=false \
      "${offline[@]}" \
      -v "$VOLUME:/home/ubuntu/.cache/huggingface/hub" \
      "$IMAGE" >/dev/null; then
  echo "speaches-models: could not start $IMAGE – is it pulled? (podman pull $IMAGE)" >&2
  exit 2
fi

base="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
  curl -fsS "$base/health" >/dev/null 2>&1 && break
  sleep 2
done
if ! curl -fsS "$base/health" >/dev/null 2>&1; then
  echo "speaches-models: $NAME never answered /health on :$PORT after 120 s. Last log lines:" >&2
  podman logs --tail 20 "$NAME" >&2 || true
  exit 2
fi

# What is cached is read from speaches' own local listing, not from a directory walk: the
# cache layout is huggingface_hub's and a blob present is not a snapshot complete.
cached() { curl -fsS "$base/v1/models" | python3 -c '
import json,sys
print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))'; }

have=$(cached) || { echo "speaches-models: /v1/models did not answer" >&2; exit 2; }

missing=0
for m in "${MODELS[@]}"; do
  if printf '%s\n' "$have" | grep -qxF "$m"; then
    echo "[have] $m"
    continue
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    echo "[MISSING] $m"
    missing=1
    continue
  fi
  echo "[pull] $m"
  # Synchronous, and it costs a remote listing per executor before it reaches the cache: the
  # handler asks kokoro, then piper, then whisper whether they publish this id, and each of
  # those is a live huggingface_hub call. So this NEEDS THE NETWORK even for a cached model –
  # which is why the container above runs online and the serving one does not.
  # 200 means downloaded and 201 means already present; both are success, inverted from the
  # reading you would guess.
  code=$(curl -sS -o "$PULLBODY" -w '%{http_code}' -X POST "$base/v1/models/$m")
  if [ "$code" != "200" ] && [ "$code" != "201" ] && [ "$code" != "204" ]; then
    echo "[FAIL] $m – HTTP $code: $(head -c 400 "$PULLBODY")" >&2
    missing=1
  fi
done

if [ "$CHECK_ONLY" = 1 ]; then
  [ "$missing" = 0 ] && echo "speaches-models: all ${#MODELS[@]} declared models are cached."
  exit "$missing"
fi

# Assert on the artefact rather than on the exit codes above: re-read the listing and require
# every declared id back. A download that reported 200 and wrote nothing fails here.
# Guarded, because an unreadable listing would otherwise report every model as absent and send
# the reader after the downloads instead of after the server.
have=$(cached) || { echo "speaches-models: /v1/models did not answer after the pull" >&2; exit 2; }
for m in "${MODELS[@]}"; do
  printf '%s\n' "$have" | grep -qxF "$m" || {
    echo "[FAIL] $m is still absent from /v1/models after the pull" >&2
    missing=1
  }
done
[ "$missing" = 0 ] && echo "speaches-models: all ${#MODELS[@]} declared models are cached."
exit "$missing"
