"""A fake Tiiny that only speaks the image-studio surface."""
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tiny_png():
    raw = b"\x00\x00"
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


PNG = tiny_png()

CATALOG = [
    {"id": "Tongyi-MAI/Z-Image-Turbo", "display_name": "Z-Image-Turbo",
     "type": "Text-to-Image", "params": "6B", "npu_usage": 32,
     "total_size": 10_200_000_000, "status": "available"},
    {"id": "black-forest-labs/FLUX.2-klein-4B", "display_name": "FLUX.2 klein",
     "type": "Text-to-Image", "params": "4B", "npu_usage": 18,
     "total_size": 8_000_000_000, "status": "available"},
    {"id": "deepreinforce-ai/Ornith-1.0-35B", "display_name": "Ornith",
     "type": "Image-Text-to-Text", "params": "35B", "npu_usage": 50,
     "total_size": 18_000_000_000, "status": "available"},
]


class State:
    def __init__(self, loaded=None):
        self.loaded = list(loaded if loaded is not None else ["Tongyi-MAI/Z-Image-Turbo"])
        self.current = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.calls = []


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    @property
    def state(self):
        return self.server.state

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        elif not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or auth == "Bearer probe":
            self._send(401, {"error": {"message": "unauthorized", "type": "auth"}})
            return False
        return True

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/v1/models", "/v1/models/"):
            if not self._auth():
                return
            return self._send(200, {"data": CATALOG})
        if path in ("/api/v1/models/", "/api/v1/models"):
            if not self._auth():
                return
            return self._send(200, {"data": CATALOG})
        if path == "/api/v1/models/running":
            if not self._auth():
                return
            return self._send(200, {"running": list(self.state.loaded)})
        if path == "/api/v1/models/npu/status":
            if not self._auth():
                return
            used = sum(m["npu_usage"] for m in CATALOG if m["id"] in self.state.loaded)
            return self._send(200, {"npu_total": 100, "npu_used": used,
                                    "npu_available": 100 - used})
        if path == "/api/v1/sys/device_info":
            return self._send(200, {"device_name": "Fake Tiiny", "tiiny_os": "0.1.34",
                                    "version": "fake", "sn": "TESTSERIAL"})
        if path == "/device.json":
            return self._send(200, {"serial_number": "TESTSERIAL",
                                    "device_name": "Fake Tiiny"})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        if path == "/v1/image/generate":
            if not self._auth():
                return
            model = body.get("model")
            if model not in self.state.loaded:
                return self._send(503, {
                    "error": {"message": "No suitable model is currently running.",
                              "type": "service_unavailable"}})
            with self.state.lock:
                self.state.current += 1
                self.state.peak = max(self.state.peak, self.state.current)
                self.state.calls.append(dict(body))
            try:
                import time
                time.sleep(0.15)
                return self._send(200, PNG, "image/png")
            finally:
                with self.state.lock:
                    self.state.current -= 1
        return self._send(404, {"error": {"message": "not found"}})


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state, host="127.0.0.1", port=0):
        self.state = state
        super().__init__((host, port), FakeHandler)


class FakeDevice:
    def __init__(self, loaded=None):
        self.state = State(loaded=loaded)
        self.httpd = None
        self.thread = None
        self.host = "127.0.0.1"
        self.port = None

    def start(self):
        self.httpd = FakeServer(self.state)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(2)
