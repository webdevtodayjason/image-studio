"""The web app: `python3 studio.py --serve 8430`.

Stdlib only. One background thread runs the lane; the page polls for queue
and gallery. Nothing here loads or unloads a model.
"""
import json
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import device
import studio

HERE = pathlib.Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _file(self, path, ctype):
        if not path.exists():
            return self._send(404, "not here", "text/plain")
        self._send(200, path.read_bytes(), ctype)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._file(HERE / "static" / "app.html",
                              "text/html; charset=utf-8")
        if p == "/api/device":
            key, how = "", "none"
            try:
                key = device.key()
                how = device.KEY_SOURCE or "saved"
            except SystemExit:
                pass
            w = device.where()
            return self._json({
                "host": w["host"],
                "port": w["gateway_port"],
                "device": device.identify() if w["host"] else {},
                "source": w["source"],
                "plane": w["plane"],
                "serial": w["serial"],
                "transport": w["gateway_transport"],
                "vhost": w["gateway_vhost"],
                "key_set": bool(key),
                "key_hint": (key[:4] + "..." + key[-4:]) if key else "",
                "key_source": how,
                "gallery": str(studio.gallery_dir()),
            })
        if p == "/api/models":
            try:
                tok = device.key()
                models = studio.image_models(tok)
            except SystemExit as e:
                return self._json({"error": str(e)}, 503)
            if isinstance(models, dict) and "_error" in models:
                return self._json({"error": device.refusal_text(models)}, 503)
            units = device.npu_status(tok)
            live = device.running(tok)
            return self._json({
                "models": models,
                "running": live,
                "npu": {
                    "used": units.get("npu_used"),
                    "free": units.get("npu_available"),
                    "total": units.get("npu_total") or 100,
                },
                "plate": studio.PLATE,
                "note": ("This app does not load or unload models. "
                         "Pick one that is already resident."),
            })
        if p == "/api/status":
            snap = studio.LANE.snapshot()
            q = urllib.parse.parse_qs(u.query)
            jid = (q.get("job") or [""])[0]
            if jid:
                snap["job"] = studio.LANE.job(jid)
            return self._json(snap)
        if p == "/api/gallery":
            return self._json({"items": studio.list_items()})
        if p.startswith("/api/gallery/"):
            rest = p[len("/api/gallery/"):]
            iid = rest[:-4] if rest.endswith(".png") else rest
            item = studio.read_item(iid)
            if not item:
                return self._send(404, "no such plate", "text/plain")
            if rest.endswith(".png"):
                return self._file(item["png"], "image/png")
            row = {k: v for k, v in item.items() if k != "png"}
            return self._json(row)
        return self._send(404, "no such path", "text/plain")

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        body = self._read_body()
        if u.path == "/api/generate":
            spec, err = studio.new_job_spec(body)
            if err:
                return self._json({"error": err}, 400)
            job = studio.LANE.submit(spec)
            return self._json({"ok": True, "job": job}, 202)
        if u.path == "/api/device":
            host = (body.get("host") or "").strip()
            newkey = (body.get("key") or "").strip()
            if host:
                if not device.reachable(host):
                    return self._json({
                        "error": ("nothing answering at %s. Check the address."
                                  % host)}, 400)
                device.save_config(host=host, plane="given")
                device.connect(host=host)
            if newkey:
                probe = device.api(device.gw("/api/v1/models/running"),
                                   newkey, timeout=20)
                if isinstance(probe, dict) and "_error" in probe:
                    return self._json({"error": "the device rejected that key"}, 400)
                device.save_config(key=newkey)
            w = device.where()
            return self._json({"ok": True, "host": w["host"],
                               "port": w["gateway_port"],
                               "plane": w["plane"], "source": w["source"],
                               "transport": w["gateway_transport"],
                               "device": device.identify() if w["host"] else {}})
        return self._send(404, "no such path", "text/plain")

    def do_DELETE(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/gallery/"):
            return self._send(404, "no such path", "text/plain")
        iid = u.path[len("/api/gallery/"):]
        if not studio.delete_item(iid):
            return self._json({"error": "no such plate"}, 404)
        return self._json({"ok": True, "id": iid})


def run(port=8430, host=None, serial=None, rescan=False):
    studio.gallery_dir()
    studio.LANE.start()
    w, err = device.connect_soft(host=host, serial=serial, rescan=rescan)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("\n  Image Studio %s  http://127.0.0.1:%d/" % (studio.VERSION, port))
    if w:
        print("  device      %s  (%s plane, %s, found by %s)"
              % (w["host"], w["plane"], w["gateway_transport"], w["source"]))
    else:
        print("  device      not found yet. Open the app and set the address.")
        print("              " + err.replace("\n", "\n              "))
    print("  gallery     %s\n" % studio.gallery_dir())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")
    return 0
