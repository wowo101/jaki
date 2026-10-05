#!/usr/bin/env bash
# test-serving-guard.sh – the guard's container sizing and its unload-before-refuse, against fakes.
#
#   bash engine/infra/model-serving/test-serving-guard.sh
#
# A fake `podman` on PATH stands in for the container launch (`run` prints RAN and exits 0;
# every other subcommand fails quietly, as `podman inspect` of nothing does), a fake router
# answers `/running` and records unloads, and a temporary registry sets `resident_gib`. The
# guard's first check is taken out of play – its GPU names are ones no process has, and a fake
# `hatch` reports no lease – so a real lease or server on the machine cannot decide the result.
# Needs no GPU; nothing on the machine is touched.
set -u
HERE=$(dirname "$(readlink -f "$0")")
GUARD="$HERE/hatch-serving-guard"
T=$(mktemp -d); trap 'kill $RPID 2>/dev/null; rm -rf "$T"' EXIT
pass=0; fails=0
check() { if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "  ok    $1"; else fails=$((fails+1)); echo "  FAIL  $1: got '$2', want '$3'"; sed 's/^/        /' "$T/err" 2>/dev/null; fi; }

mkdir -p "$T/bin"
# `container exists` answers from a flag file, and `rm` is recorded, for the orphan case.
cat > "$T/bin/podman" <<POD
#!/bin/sh
case "\$1" in
  run) echo RAN; exit 0 ;;
  container) [ -e "$T/orphan" ]; exit \$? ;;
  rm) echo "\$@" >> "$T/removed"; rm -f "$T/orphan"; exit 0 ;;
esac
exit 1
POD
chmod +x "$T/bin/podman"
printf '#!/bin/sh\necho no\nexit 1\n' > "$T/bin/hatch"; chmod +x "$T/bin/hatch"
cp "$HERE/../../lib/gpu-binaries.env" "$T/gpu.env"
sed -i -e 's/^HATCH_UNLOAD_ORDER=.*/HATCH_UNLOAD_ORDER="zimage"/' \
       -e 's/^HATCH_GPU_SERVERS=.*/HATCH_GPU_SERVERS="no-such-server"/' \
       -e 's/^HATCH_GPU_TOOLS=.*/HATCH_GPU_TOOLS="no-such-tool"/' "$T/gpu.env"
avail=$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)
reg() {   # reg <resident_gib or empty>
  { cat "$HERE/../../models.toml"
    [ -n "$1" ] && printf '\n[models.probe]\nswap_id = "probe"\n[models.probe.serve]\nresident_gib = %s\n' "$1"; } > "$T/models.toml"
}
python3 - "$T" <<'PY' & RPID=$!
import http.server, json, sys, pathlib
T = pathlib.Path(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        state = (T / "state").read_text().strip() if (T / "state").exists() else "ready"
        self._send(200, {"running": [{"model": "zimage", "state": state}]})
    def do_POST(self):
        with open(T / "unloads", "a") as f: f.write(self.path + "\n")
        self._send(200, {"msg": "ok"})
s = http.server.HTTPServer(("127.0.0.1", 0), H)
(T / "port").write_text(str(s.server_port)); s.serve_forever()
PY
for _ in $(seq 1 50); do [ -s "$T/port" ] && break; sleep 0.1; done
ADDR="127.0.0.1:$(cat "$T/port")"
run() {   # run <swap_id> -- the guard with a podman command; prints the exit code
  env PATH="$T/bin:$PATH" HATCH_GPU_BINARIES="$T/gpu.env" HATCH_MODELS_TOML="$T/models.toml" \
    HATCH_SWAP_ID="$1" HATCH_SERVE_ADDR="$ADDR" HATCH_IMAGE_MARKER="$T/marker" \
    GUARD_UNLOAD_WAIT_S=2 GUARD_MARGIN_GIB=0 \
    "$GUARD" "$T/bin/podman" run --rm --replace --name hatch-x image gufo serve > "$T/out" 2> "$T/err"
  echo $?
}

echo "1. a container entry with no resident_gib is refused"
reg ""; check "exit" "$(run probe)" 98
check "names the missing key" "$(grep -c 'no resident_gib' "$T/err")" 1

echo "2. a container entry that fits is started, sized from the registry"
reg 1; check "exit" "$(run probe)" 0
check "the command ran" "$(cat "$T/out")" RAN
check "sized by resident_gib" "$(grep -c 'need ~1 GiB' "$T/err")" 1

echo "3. one that does not fit unloads the idle entries in the order, then refuses"
rm -f "$T/unloads"; reg $((avail + 50)); check "exit" "$(run probe)" 98
check "zimage unloaded" "$(cat "$T/unloads" 2>/dev/null)" "/api/models/unload/zimage"
check "the refusal names resident_gib" "$(grep -c "resident_gib $((avail + 50)) from the registry" "$T/err")" 1

echo "4. nothing is unloaded while an image is generating"
rm -f "$T/unloads"; printf '{"pid": %d, "started": %d}' $$ "$(date +%s)" > "$T/marker"
check "exit" "$(run probe)" 98
check "no unload" "$(cat "$T/unloads" 2>/dev/null)" ""
rm -f "$T/marker"

echo "5. an entry never unloads itself"
rm -f "$T/unloads"; printf '\n[models.zi]\nswap_id = "zimage"\n[models.zi.serve]\nresident_gib = %s\n' $((avail + 50)) >> "$T/models.toml"
check "exit" "$(run zimage)" 98
check "no unload" "$(cat "$T/unloads" 2>/dev/null)" ""

echo "6. an entry still starting is not unloaded"
rm -f "$T/unloads"; reg $((avail + 50)); echo starting > "$T/state"
check "exit" "$(run probe)" 98
check "no unload" "$(cat "$T/unloads" 2>/dev/null)" ""
rm -f "$T/state"

echo "7. an orphan carrying the entry's --name is removed before sizing"
rm -f "$T/removed"; touch "$T/orphan"; reg 1
check "exit" "$(run probe)" 0
check "podman rm of the orphan" "$(cat "$T/removed" 2>/dev/null)" "rm -f -t 10 hatch-x"
rm -f "$T/removed"
check "no orphan, no removal" "$(run probe; cat "$T/removed" 2>/dev/null)" 0

echo; echo "$pass passed, $fails failed"
[ "$fails" = 0 ]
