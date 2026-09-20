"""A fake Tiiny that only speaks the image-studio surface."""
import json
import struct
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Real firmware takes tens of seconds; two polls is the same shape at a
# speed a test can wait for.
LOAD_POLLS = 2


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
    {"id": "example/Big-Plate-90", "display_name": "Big Plate",
     "type": "Text-to-Image", "params": "90B", "npu_usage": 90,
     "total_size": 40_000_000_000, "status": "available"},
    {"id": "deepreinforce-ai/Ornith-1.0-35B", "display_name": "Ornith",
     "type": "Image-Text-to-Text", "params": "35B", "npu_usage": 50,
     "total_size": 18_000_000_000, "status": "available"},
    {"id": "Qwen/Qwen3.8-27B", "display_name": "Qwen3.8 27B",
     "type": "Image-Text-to-Text", "params": "27B", "npu_usage": 55,
     "total_size": 16_000_000_000, "status": "available"},
    {"id": "example/Vision-70", "display_name": "Vision 70",
     "type": "Image-Text-to-Text", "params": "70B", "npu_usage": 70,
     "total_size": 28_000_000_000, "status": "available"},
    {"id": "example/Chat-8", "display_name": "Chat 8",
     "type": "Text Generation", "params": "8B", "npu_usage": 20,
     "total_size": 5_000_000_000, "status": "available"},
]


class State:
    def __init__(self, loaded=None):
        self.loaded = list(loaded if loaded is not None else ["Tongyi-MAI/Z-Image-Turbo"])
        self.pending = {}
        self.current = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.calls = []
        self.chat_calls = []
        self.hold_generate = None
        self.generate_started = threading.Event()
        self.npu_total = 100

    def row(self, model_id):
        for m in CATALOG:
            if m["id"] == model_id:
                return m
        return {"id": model_id, "npu_usage": 1, "type": "Text-to-Image"}

    def cost(self, model_id):
        return self.row(model_id).get("npu_usage") or 0

    def units_used(self):
        return sum(self.cost(m) for m in self.loaded)

    def status_of(self, model_id):
        return "loading" if model_id in self.pending else "running"

    def advance_loads(self):
        """A start that does not fit sits as loading, then vanishes."""
        for model_id in list(self.pending):
            job = self.pending[model_id]
            job["polls"] -= 1
            if job["polls"] > 0:
                continue
            self.pending.pop(model_id, None)
            if job["rollback"] and model_id in self.loaded:
                self.loaded.remove(model_id)


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
            instances = []
            for offset, model_id in enumerate(self.state.loaded):
                instances.append({
                    "model_id": model_id,
                    "npu_usage": self.state.cost(model_id),
                    "status": self.state.status_of(model_id),
                    "instance_id": "fake-%d" % offset,
                })
            return self._send(200, {"running": list(self.state.loaded),
                                    "instances": {"running": instances}})
        if path == "/api/v1/models/npu/status":
            if not self._auth():
                return
            with self.state.lock:
                self.state.advance_loads()
                loaded = list(self.state.loaded)
                models = [{"model_id": m, "npu_usage": self.state.cost(m),
                           "status": self.state.status_of(m)} for m in loaded]
            used = sum(self.state.cost(m) for m in loaded)
            total = self.state.npu_total
            return self._send(200, {"npu_total": total, "npu_used": used,
                                    "npu_available": max(0, total - used),
                                    "models": models})
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
        if path == "/v1/chat/completions":
            if not self._auth():
                return
            model = body.get("model")
            if model not in self.state.loaded or model in self.state.pending:
                return self._send(503, {
                    "error": {"message": "No suitable model is currently running.",
                              "type": "service_unavailable"}})
            with self.state.lock:
                self.state.current += 1
                self.state.peak = max(self.state.peak, self.state.current)
                self.state.chat_calls.append(body)
            try:
                time.sleep(0.08)
                text = ("a red apple on a wooden table, studio lighting, "
                        "detailed still life")
                if not self._sent_image(body):
                    text = ("a red apple on a wooden table, plain white "
                            "background, studio lighting")
                return self._send(200, {
                    "id": "chatcmpl-fake", "object": "chat.completion",
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": text}}],
                    "usage": {"prompt_tokens": 16, "completion_tokens": 18,
                              "total_tokens": 34},
                })
            finally:
                with self.state.lock:
                    self.state.current -= 1
        if path == "/v1/image/generate":
            if not self._auth():
                return
            model = body.get("model")
            if model not in self.state.loaded or model in self.state.pending:
                return self._send(503, {
                    "error": {"message": "No suitable model is currently running.",
                              "type": "service_unavailable"}})
            with self.state.lock:
                self.state.current += 1
                self.state.peak = max(self.state.peak, self.state.current)
                self.state.calls.append(dict(body))
            self.state.generate_started.set()
            try:
                if self.state.hold_generate is not None:
                    self.state.hold_generate.wait(30)
                else:
                    time.sleep(0.15)
                return self._send(200, PNG, "image/png")
            finally:
                with self.state.lock:
                    self.state.current -= 1
        prefix = "/api/v1/models/"
        for suffix, handler in (("/start", self._start), ("/stop", self._stop)):
            if path.startswith(prefix) and path.endswith(suffix):
                model_id = self._model_from(path, prefix, suffix)
                if model_id is None:
                    return self._send(404, {"error": {"message": "not found"}})
                if not self._auth():
                    return
                return handler(model_id)
        return self._send(404, {"error": {"message": "not found"}})

    @staticmethod
    def _sent_image(body):
        for message in body.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
        return False

    @staticmethod
    def _model_from(path, prefix, suffix=""):
        rest = path[len(prefix):]
        if suffix:
            if not rest.endswith(suffix):
                return None
            rest = rest[:-len(suffix)]
        if not rest or "/" in rest:
            return None
        return urllib.parse.unquote(rest)

    def _start(self, model_id):
        state = self.state
        with state.lock:
            if not any(m["id"] == model_id for m in CATALOG):
                return self._send(400, {"code": 400, "msg": "Error starting model.",
                                        "detail": "%s is not downloaded" % model_id})
            if model_id in state.loaded and model_id not in state.pending:
                return self._send(200, {"message": "%s already running" % model_id,
                                        "progress": 100})
            over = state.units_used() + state.cost(model_id) > state.npu_total
            if model_id not in state.loaded:
                state.loaded.append(model_id)
            state.pending[model_id] = {"polls": LOAD_POLLS, "rollback": over}
        return self._send(200, {"message": "start loading %s" % model_id,
                                "progress": 0})

    def _stop(self, model_id):
        state = self.state
        with state.lock:
            if model_id not in state.loaded:
                return self._send(400, {"code": 400, "msg": "Error stopping model.",
                                        "detail": "%s is not running" % model_id})
            state.loaded.remove(model_id)
            state.pending.pop(model_id, None)
        return self._send(200, {"removed_container_ids": ["fake"]})


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
