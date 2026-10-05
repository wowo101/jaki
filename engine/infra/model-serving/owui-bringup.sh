#!/usr/bin/env bash
# owui-bringup.sh – land the Open WebUI image on a flaky link, then start the chat surface.
#
# Retries pull-image-resumable.sh until the image is present. Each attempt resumes from the
# blobs already on disk, so a dropped connection costs the current blob, not the pull. Then
# enables open-webui.service and waits for it to answer.
#
# Run detached, holding an inhibitor so the box does not idle-power-off underneath it:
#   systemd-run --user --unit=owui-bringup --collect \
#     systemd-inhibit --what=idle:shutdown --who=owui --why=bringup \
#     "$HOME"/.local/bin/owui-bringup
# Watch:  tail -f /tmp/owui-bringup.log
set -u

# The image is whatever open-webui.service runs; the tag is pinned there and nowhere else.
UNIT="$(dirname "$(readlink -f "$0")")/open-webui.service"
IMAGE=$(grep -o -m1 'ghcr\.io/open-webui/open-webui:[^[:space:]]*' "$UNIT")
if [ -z "$IMAGE" ]; then
  echo "owui-bringup: no open-webui image named in $UNIT" >&2
  exit 2
fi
WORK=/var/tmp/owui
LOG=/tmp/owui-bringup.log

# THIS FILE NAMES NO MACHINE. It carried this box's tailnet address as a literal until
# 2026-09-10, which on any other machine polls the wrong host for thirty minutes and then
# reports the bring-up failed. The address comes from the same place the units take theirs:
# `hatch install` writes it from `[endpoints].serve_url`.
ENV_FILE="${HATCH_ENV_FILE:-$HOME/.config/hatch/env}"
BOX_TS=$(grep -m1 '^HATCH_SERVE_HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
WEBUI_URL=$(grep -m1 '^HATCH_WEBUI_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
if [ -z "$BOX_TS" ]; then
  echo "owui-bringup: no HATCH_SERVE_HOST in $ENV_FILE – run \`hatch install\` first." >&2
  exit 2
fi
WEBUI_URL="${WEBUI_URL:-http://$BOX_TS:3000}"

say() { echo "$(date -Is) | $*" >> "$LOG"; }
have_image() { podman image exists "$IMAGE"; }

say "### bringup start"

for attempt in $(seq 1 40); do
  have_image && { say "image already present"; break; }
  say "--- pull attempt $attempt ---"
  "$HOME"/.local/bin/pull-image-resumable "$IMAGE" "$WORK" >> "$LOG" 2>&1
  rc=$?
  have_image && { say "image landed on attempt $attempt"; break; }
  say "attempt $attempt did not complete (rc=$rc); retrying in 30s"
  sleep 30
done

if ! have_image; then
  say "FATAL: image never landed after 40 attempts"
  exit 1
fi

say "starting open-webui"
systemctl --user daemon-reload
systemctl --user enable --now open-webui >> "$LOG" 2>&1

# Open WebUI's first boot builds its database and loads the bundled embedding model before
# it binds, which takes well over ten minutes on this box. A short window reports a healthy
# bring-up as failed.
for i in $(seq 1 360); do
  if curl -sf -m 5 "http://${BOX_TS}:3000/health" >/dev/null 2>&1; then
    say "open-webui healthy at $WEBUI_URL"
    say "### bringup done"
    exit 0
  fi
  sleep 5
done

say "open-webui did not answer within 30 min; state=$(systemctl --user is-active open-webui)"
say "### bringup done (unhealthy)"
exit 2
