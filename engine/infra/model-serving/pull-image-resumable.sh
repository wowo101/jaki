#!/usr/bin/env bash
# pull-image-resumable.sh – fetch a container image over a slow, flaky link.
#
# `podman pull` restarts the ENTIRE copy on any blob error and gives up after 3 tries, so on
# a ~0.3 MB/s uplink a 1.8 GB image never lands: each attempt times out ~25 minutes
# in and the next starts from zero. This fetches each blob separately with `curl -C -`, so
# progress is cumulative across restarts and interruptions – re-run it as often as needed.
#
# Assembles an OCI layout, then hands it to podman. Same reasoning as the model downloads in
# engine/infra/model-store/download-candidates.sh: on this link, prefer byte-exact resume over any
# tool that restarts.
#
# Usage:  pull-image-resumable.sh ghcr.io/open-webui/open-webui:main [workdir]
set -uo pipefail

IMAGE="${1:?image required, e.g. ghcr.io/open-webui/open-webui:main}"
WORK="${2:-/var/tmp/oci-pull}"

REGISTRY="${IMAGE%%/*}"
REST="${IMAGE#*/}"
REPO="${REST%%:*}"
TAG="${REST##*:}"
[ "$TAG" = "$REST" ] && TAG=latest

BLOBS="$WORK/blobs/sha256"
mkdir -p "$BLOBS"

ACCEPT='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'

token() {   # ghcr/docker anonymous pull token; short-lived, so refetch per blob
  curl -sf "https://${REGISTRY}/token?scope=repository:${REPO}:pull&service=${REGISTRY}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))'
}

say() { echo "$(date -Is) | $*"; }

TOK=$(token)
[ -n "$TOK" ] || { say "FATAL: no registry token"; exit 1; }

# --- resolve the amd64 manifest ----------------------------------------------------------
say "resolving ${IMAGE}"
INDEX=$(curl -sf -H "Authorization: Bearer $TOK" -H "Accept: $ACCEPT" \
        "https://${REGISTRY}/v2/${REPO}/manifests/${TAG}")

MANIFEST_DIGEST=$(printf '%s' "$INDEX" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "manifests" in d:
    for m in d["manifests"]:
        p = m.get("platform", {})
        if p.get("architecture") == "amd64" and p.get("os") == "linux":
            print(m["digest"])
            break
')

# Fetch straight to the blob file. A command substitution strips trailing newlines, and the
# stored bytes must hash to MANIFEST_DIGEST or podman rejects the layout with an error that
# names neither this script nor the newline – so assert the digest here rather than let it
# surface as an opaque import failure three steps later.
if [ -n "$MANIFEST_DIGEST" ]; then
  mf="$BLOBS/${MANIFEST_DIGEST#sha256:}"
  if ! curl -sS --fail-with-body -H "Authorization: Bearer $TOK" -H "Accept: $ACCEPT" \
            -o "$mf" "https://${REGISTRY}/v2/${REPO}/manifests/${MANIFEST_DIGEST}"; then
    say "FATAL: manifest fetch failed for ${MANIFEST_DIGEST}"; rm -f "$mf"; exit 4
  fi
  actual=$(sha256sum "$mf" | cut -d' ' -f1)
  if [ "$actual" != "${MANIFEST_DIGEST#sha256:}" ]; then
    say "FATAL: manifest digest mismatch – got ${actual:0:12}, want ${MANIFEST_DIGEST:7:12}"
    rm -f "$mf"; exit 3
  fi
  MANIFEST=$(cat "$mf")
else
  MANIFEST="$INDEX"
  MANIFEST_DIGEST="sha256:$(printf '%s' "$MANIFEST" | sha256sum | cut -d' ' -f1)"
  printf '%s' "$MANIFEST" > "$BLOBS/${MANIFEST_DIGEST#sha256:}"
fi
MEDIATYPE=$(python3 -c 'import sys,json; print(json.load(sys.stdin).get("mediaType","application/vnd.oci.image.manifest.v1+json"))' <<<"$MANIFEST")

# --- fetch every blob, resumably ---------------------------------------------------------
mapfile -t WANTED < <(printf '%s' "$MANIFEST" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for b in [d["config"]] + d["layers"]:
    print(b["digest"], b["size"])
')

total=${#WANTED[@]}
# An empty blob list means the manifest did not parse. Without this the script "succeeds"
# in two seconds, having downloaded nothing, and only podman notices.
[ "$total" -gt 0 ] || { say "FATAL: no blobs resolved from the manifest"; exit 4; }
say "manifest ${MANIFEST_DIGEST:0:19} – $total blobs"
n=0
for entry in "${WANTED[@]}"; do
  n=$((n+1))
  digest=${entry%% *}; want=${entry##* }
  hex=${digest#sha256:}
  out="$BLOBS/$hex"

  have=$(stat -c %s "$out" 2>/dev/null || echo 0)
  if [ "$have" = "$want" ]; then
    say "[$n/$total] ${hex:0:12} already complete ($want bytes)"
    continue
  fi

  for attempt in 1 2 3 4 5 6 7 8; do
    TOK=$(token)
    say "[$n/$total] ${hex:0:12} attempt $attempt, have $have/$want"
    curl -fL --http1.1 -C - \
      --connect-timeout 30 --speed-limit 20000 --speed-time 120 \
      -H "Authorization: Bearer $TOK" \
      -o "$out" \
      "https://${REGISTRY}/v2/${REPO}/blobs/${digest}"
    have=$(stat -c %s "$out" 2>/dev/null || echo 0)
    [ "$have" = "$want" ] && break
    sleep 5
  done

  if [ "$have" != "$want" ]; then
    say "[$n/$total] ${hex:0:12} INCOMPLETE ($have/$want) – re-run to resume"
    exit 2
  fi
  say "[$n/$total] ${hex:0:12} complete"
done

# --- verify, then assemble the OCI layout ------------------------------------------------
say "verifying digests"
for entry in "${WANTED[@]}"; do
  digest=${entry%% *}; hex=${digest#sha256:}
  actual=$(sha256sum "$BLOBS/$hex" | cut -d' ' -f1)
  # Delete it: the fetch loop short-circuits on size alone, so a corrupt blob of the right
  # length is skipped as "already complete" on every retry and the script's own re-run-to-
  # resume contract can never recover. The caller retries in a loop and would spin forever.
  [ "$actual" = "$hex" ] || { say "FATAL: digest mismatch for ${hex:0:12} – discarding it, re-run to refetch"
                              rm -f "$BLOBS/$hex"; exit 3; }
done

printf '{"imageLayoutVersion":"1.0.0"}' > "$WORK/oci-layout"
python3 - "$WORK/index.json" "$MANIFEST_DIGEST" "$MEDIATYPE" "$TAG" \
         "$(stat -c %s "$BLOBS/${MANIFEST_DIGEST#sha256:}")" <<'PY'
import sys, json
path, digest, mediatype, tag, size = sys.argv[1:6]
json.dump({
    "schemaVersion": 2,
    "manifests": [{
        "mediaType": mediatype,
        "digest": digest,
        "size": int(size),
        "annotations": {"org.opencontainers.image.ref.name": tag},
    }],
}, open(path, "w"))
PY

say "importing into podman"
IMGID=$(podman pull --quiet "oci:${WORK}:${TAG}") || { say "FATAL: podman import failed"; exit 5; }
podman tag "$IMGID" "$IMAGE" || { say "FATAL: could not tag $IMGID as $IMAGE"; exit 6; }
say "imported ${IMGID:0:12} as $IMAGE"
say "DONE"
