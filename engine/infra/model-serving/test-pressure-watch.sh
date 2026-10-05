#!/usr/bin/env bash
# test-pressure-watch.sh – the watcher against a fake router: it acts below the floor, not above
# it, not while an image generates, and once per model inside the cooldown. No GPU.
#
#   bash engine/infra/model-serving/test-pressure-watch.sh
set -u
T=$(mktemp -d); cd "$(dirname "$(readlink -f "$0")")"; fails=0
python3 - "$T" <<'PY' & R=$!
import http.server, json, sys, pathlib
T = pathlib.Path(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, b):
        b = json.dumps(b).encode(); self.send_response(200)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send({"running": [{"model": "zimage", "state": "ready"}, {"model": "qnext", "state": "ready"}]})
    def do_POST(self):
        open(T / "unloads", "a").write(self.path + "\n"); self._send({})
s = http.server.HTTPServer(("127.0.0.1", 0), H); (T / "port").write_text(str(s.server_port)); s.serve_forever()
PY
for _ in $(seq 50); do [ -s $T/port ] && break; sleep 0.1; done
E="env HATCH_SERVE_ADDR=127.0.0.1:$(cat $T/port) HATCH_IMAGE_MARKER=$T/marker"
res() { cat $T/unloads 2>/dev/null | tr '\n' ' '; rm -f $T/unloads; }
expect() { if [ "$2" = "$3" ]; then echo "  ok    $1"; else echo "  FAIL  $1: got [$2], want [$3]"; fails=$((fails+1)); fi; }
$E HATCH_PRESSURE_FLOOR_GIB=0 ./hatch-pressure-watch --once >/dev/null; expect "above the floor, nothing" "$(res)" ""
$E HATCH_PRESSURE_FLOOR_GIB=100000 ./hatch-pressure-watch --once >/dev/null; expect "below it, zimage and not qnext" "$(res)" "/api/models/unload/zimage "
printf '{"pid": %d, "started": 0}' $$ > $T/marker
$E HATCH_PRESSURE_FLOOR_GIB=100000 ./hatch-pressure-watch --once >/dev/null; expect "during an image, nothing" "$(res)" ""
rm -f $T/marker
$E HATCH_PRESSURE_FLOOR_GIB=100000 HATCH_PRESSURE_INTERVAL_S=0.2 timeout 2 ./hatch-pressure-watch >/dev/null; expect "ten readings in the cooldown, one unload" "$(res)" "/api/models/unload/zimage "
kill $R; rm -rf $T
[ "$fails" = 0 ] && echo PASS || { echo "$fails FAILED"; exit 1; }
