#!/usr/bin/env python3
"""test-image-front.py – the delegate pause, end to end, against fakes. No GPU, no router.

    python3 engine/infra/model-serving/test-image-front.py

A fake router (/running, per-model unload, one-token load) with a fake delegate behind it, and
a fake image server that records whether the marker existed while it generated and how many
body bytes reached it. Then the guard against live markers. Every check prints what it saw.
"""
import http.client
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
GUARD = str(HERE / "hatch-serving-guard")
BINARIES = str(HERE.parent.parent / "lib" / "gpu-binaries.env")   # HATCH_IMAGE_PAUSES="qwen3.5-4b"
FAILS = 0
calls: list[tuple] = []
busy_until = [0.0]


def check(label, ok, saw=""):
    global FAILS
    FAILS += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        saw: {saw}"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def settle():
    """Clear the call log once the router has been quiet for 1.5 s: a reload from the previous
    request can land after a bare clear and pass the next check on the wrong evidence."""
    n = -1
    while n != len(calls):
        n = len(calls)
        time.sleep(1.5)
    calls.clear()


RPORT = free_port()


class Router(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/running":
            self._send({"running": [{"model": "qwen3.5-4b", "state": "ready",
                                     "proxy": f"http://127.0.0.1:{RPORT}/fake4b"}]})
        elif self.path == "/fake4b/slots":
            self._send([{"id": 0, "is_processing": time.time() < busy_until[0]}])
        else:
            self._send({}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path.startswith("/api/models/unload/"):
            calls.append(("unload", self.path.rsplit("/", 1)[1], time.time()))
        elif self.path == "/v1/chat/completions":
            calls.append(("load", json.loads(body)["model"], time.time()))
        self._send({"ok": True})


threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", RPORT), Router).serve_forever,
                 daemon=True).start()


def guard(env, swap_id):
    t0 = time.time()
    r = subprocess.run([GUARD, "true"], env=dict(env, HATCH_SWAP_ID=swap_id),
                       capture_output=True, text=True, timeout=60)
    return time.time() - t0, r


with tempfile.TemporaryDirectory() as tmp:
    marker = pathlib.Path(tmp, "hatch", "image-generating")
    seen, sdpid = pathlib.Path(tmp, "seen"), pathlib.Path(tmp, "sd.pid")
    fake_sd = pathlib.Path(tmp, "fake-sd.py")
    fake_sd.write_text(f"""
import http.server, json, os, pathlib, sys, time
pathlib.Path({str(sdpid)!r}).write_text(str(os.getpid()))
port = int(sys.argv[sys.argv.index('--listen-port') + 1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'ok')
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        pathlib.Path({str(seen)!r}).write_text(json.dumps(
            {{'marker': pathlib.Path({str(marker)!r}).exists(), 'bytes': len(body), 'path': self.path}}))
        time.sleep(1)
        b = json.dumps({{'data': [{{'b64_json': 'x'}}]}}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
""")
    fport = free_port()
    env = dict(os.environ, HATCH_SERVE_ADDR=f"127.0.0.1:{RPORT}", HATCH_IMAGE_MARKER=str(marker),
               HATCH_GPU_BINARIES=BINARIES, HATCH_IMAGE_PAUSE_WAIT_S="10",
               # admission is tested on its own below; here every image fits
               HATCH_IMAGE_NEED_COLD_GIB="0", HATCH_IMAGE_NEED_WARM_GIB="0", HATCH_IMAGE_FLOOR_GIB="0")
    front = subprocess.Popen([sys.executable, str(HERE / "hatch-image-front"), "--port", str(fport),
                              "--", sys.executable, str(fake_sd)], env=env, stderr=subprocess.PIPE, text=True)
    got = b""
    for _ in range(50):
        try:
            got = urllib.request.urlopen(f"http://127.0.0.1:{fport}/", timeout=1).read()
            break
        except Exception:                             # noqa: BLE001 - still starting
            time.sleep(0.2)
    check("a GET is forwarded to the image server", got == b"ok", got)

    def post(path, body=b'{"prompt":"x"}', timeout=60, chunked=False, port=None):
        c = http.client.HTTPConnection("127.0.0.1", port or fport, timeout=timeout)
        if chunked:
            c.request("POST", path, body=iter([body[:5], body[5:]]), encode_chunked=True,
                      headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"})
        else:
            c.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        try:
            return c.getresponse().read()
        except OSError:
            # Hang up the way a closed tab does: reset, so the server's next write fails.
            c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            c.close()
            raise

    busy_until[0] = time.time() + 2                  # the delegate is mid-request for 2 s
    t0 = time.time()
    out = json.loads(post("/v1/images/generations"))
    check("the generation's response comes back whole", out.get("data", [{}])[0].get("b64_json") == "x", out)
    unload = [c for c in calls if c[0] == "unload"]
    check("the delegate is unloaded, and only after it went idle",
          len(unload) == 1 and unload[0][2] >= t0 + 1.8, calls)
    check("the marker exists while the image server generates", json.loads(seen.read_text())["marker"], seen.read_text())
    check("the marker is gone once the response is sent", not marker.exists(), marker.exists())
    check("the delegate is reloaded after the generation",
          wait_for(lambda: sum(c[0] == "load" for c in calls) == 1, 5), calls)

    settle()
    post("/v1/images/generations", body=b'{"prompt":"a chunked request"}', chunked=True)
    check("a chunked request body reaches the image server whole",
          json.loads(seen.read_text())["bytes"] == len(b'{"prompt":"a chunked request"}'), seen.read_text())

    settle()
    post("/sdapi/v1/options", body=b'{"x":1}')
    check("a POST that is not a generation pauses nothing", not any(c[0] == "unload" for c in calls), calls)

    settle()
    try:
        post("/v1/images/generations", timeout=0.3)      # the caller hangs up mid-generation
    except (TimeoutError, OSError):
        pass
    check("a caller that hangs up still gets the delegate reloaded",
          wait_for(lambda: any(c[0] == "load" for c in calls), 8), calls)
    check("and the marker is gone after it", wait_for(lambda: not marker.exists(), 5), marker.exists())

    # Stopped mid-image, as llama-swap's ttl or a lease's unload would.
    child = int(sdpid.read_text())
    threading.Thread(target=lambda: post("/v1/images/generations", timeout=10), daemon=True).start()
    wait_for(marker.exists, 5)
    front.terminate()
    front.wait(timeout=30)
    gone = wait_for(lambda: not pathlib.Path(f"/proc/{child}").exists(), 10)
    check("stopping the front mid-image stops the image server", gone, f"pid {child} still present")
    check("and leaves no marker", not marker.exists(), marker.exists())

    # Killed outright, which no handler sees: the image server must not outlive it.
    fport2 = free_port()
    front2 = subprocess.Popen([sys.executable, str(HERE / "hatch-image-front"), "--port", str(fport2),
                               "--", sys.executable, str(fake_sd)], env=env, stderr=subprocess.DEVNULL)
    wait_for(lambda: sdpid.exists() and int(sdpid.read_text()) != child, 10)
    child2 = int(sdpid.read_text())
    front2.kill()
    front2.wait()
    check("a front killed outright takes its image server with it",
          wait_for(lambda: not pathlib.Path(f"/proc/{child2}").exists(), 10), f"pid {child2} still present")

    # Admission: an image the box has no room for is answered 503 with the reason, reaches no
    # image server, and gives the paused delegate back.
    settle()
    fport3 = free_port()
    front3 = subprocess.Popen([sys.executable, str(HERE / "hatch-image-front"), "--port", str(fport3),
                               "--", sys.executable, str(fake_sd)],
                              env=dict(env, HATCH_IMAGE_NEED_COLD_GIB="100000", HATCH_IMAGE_ADMIT_WAIT_S="1"),
                              stderr=subprocess.DEVNULL)
    wait_for(lambda: sdpid.exists() and int(sdpid.read_text()) != child2, 10)
    seen.unlink(missing_ok=True)
    status, body = None, b""
    for _ in range(50):
        try:
            c = http.client.HTTPConnection("127.0.0.1", fport3, timeout=30)
            c.request("POST", "/v1/images/generations", body=b'{"prompt":"x"}',
                      headers={"Content-Type": "application/json"})
            r = c.getresponse(); status, body = r.status, r.read()
            break
        except ConnectionRefusedError:
            time.sleep(0.2)
    check("an image with no room is answered 503", status == 503, (status, body))
    check("and says how much is available", b"GiB available" in body, body)
    check("and never reaches the image server", not seen.exists(), seen.read_text() if seen.exists() else "")
    check("and the delegate is reloaded after it",
          wait_for(lambda: any(c[0] == "load" for c in calls), 8), calls)
    front3.terminate(); front3.wait(timeout=30)

    # ── the guard ──────────────────────────────────────────────────────────────────────
    genv = dict(os.environ, HATCH_IMAGE_MARKER=str(marker), HATCH_GPU_BINARIES=BINARIES)
    marker.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(["sleep", "3"])        # left unreaped: a zombie must not hold
    marker.write_text(json.dumps({"pid": holder.pid, "started": time.time()}))
    dt, r = guard(genv, "qnext")
    check("a model not in the pause list starts at once", dt < 2.5 and "holding" not in r.stderr,
          f"{dt:.1f}s · {r.stderr.strip()[:200]}")
    dt, r = guard(genv, "qwen3.5-4b")
    check("the paused model's start is held until the marker's writer exits",
          dt >= 2.0 and "holding its start" in r.stderr and r.returncode == 0,
          f"{dt:.1f}s rc {r.returncode} · {r.stderr.strip()[:300]}")
    holder.wait()

    # Two images back to back, each 2.5 s, under a 3 s per-image cap: held, not refused.
    writer = subprocess.Popen(["sleep", "5"])
    marker.write_text(json.dumps({"pid": writer.pid, "started": time.time()}))
    threading.Timer(2.5, lambda: marker.write_text(
        json.dumps({"pid": writer.pid, "started": time.time()}))).start()
    dt, r = guard(dict(genv, GUARD_IMAGE_WAIT_S="3"), "qwen3.5-4b")
    check("back-to-back images do not trip the per-image cap",
          r.returncode == 0 and dt >= 4.0, f"{dt:.1f}s rc {r.returncode} · {r.stderr.strip()[-200:]}")
    writer.wait()

    # One image already past the cap: refused, with the reason.
    writer = subprocess.Popen(["sleep", "10"])
    marker.write_text(json.dumps({"pid": writer.pid, "started": time.time() - 20}))
    dt, r = guard(dict(genv, GUARD_IMAGE_WAIT_S="3"), "qwen3.5-4b")
    check("an image past the cap refuses the start with exit 96",
          r.returncode == 96 and "over 3s" in r.stderr, f"rc {r.returncode} · {r.stderr.strip()[-200:]}")
    writer.kill()
    writer.wait()

print("PASS" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
