"""The web app: `python3 studio.py --serve 8430`.

Stdlib only. One background thread runs the lane; the page polls for queue
and gallery. Load and unload are driven from the rail.
"""
import json
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import device
import media
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
                payload = studio.models_payload(tok)
            except SystemExit as e:
                return self._json({"error": str(e)}, 503)
            if isinstance(payload, dict) and "_error" in payload:
                return self._json({"error": device.refusal_text(payload)}, 503)
            return self._json(payload)
        if p == "/api/status":
            snap = studio.LANE.snapshot()
            q = urllib.parse.parse_qs(u.query)
            jid = (q.get("job") or [""])[0]
            if jid:
                snap["job"] = studio.LANE.job(jid)
            snap["loading"] = studio.LOADER.snapshot()
            try:
                snap["npu"] = studio.npu_view(device.key())
            except SystemExit:
                snap["npu"] = None
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
            if studio.LOADER.busy():
                return self._json({
                    "error": ("a model is loading; wait until it is ready "
                              "before generating")}, 409)
            job = studio.LANE.submit(spec)
            return self._json({"ok": True, "job": job}, 202)
        if u.path == "/api/upload":
            name = (body.get("name") or "upload.png").strip() or "upload.png"
            blob = studio.decode_data_url(body.get("image") or "")
            if not blob:
                return self._json({"error": "image is required"}, 400)
            try:
                item = studio.ingest_upload(blob, name)
            except media.MediaError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True, "item": item}, 201)
        if u.path in ("/api/workflow/plan", "/api/workflow"):
            try:
                tok = device.key()
            except SystemExit as e:
                return self._json({"error": str(e)}, 503)
            spec, err = studio.new_workflow_spec(tok, body)
            if err:
                return self._json({"error": err}, 400)
            if u.path == "/api/workflow/plan":
                return self._json({"ok": True, "plan": spec["plan"],
                                   "steps": spec["steps"],
                                   "honesty": spec["honesty"],
                                   "seed": spec["seed"]})
            if spec["plan"].get("swap") and not body.get("confirm"):
                return self._json({
                    "error": (
                        "this run has to unload between steps so both models "
                        "fit. That adds about %d extra seconds. Resident: %s. "
                        "Confirm if you want it to swap."
                        % (spec["plan"].get("extra_s") or 0,
                           "; ".join("%s (%s units)" % (
                               r["id"], r.get("npu_usage")
                               if r.get("npu_usage") is not None else "?")
                                     for r in spec["plan"].get("resident") or [])
                           or "nothing")),
                    "needs_confirm": True,
                    "plan": spec["plan"],
                    "honesty": spec["honesty"],
                }, 409)
            if studio.LOADER.busy():
                return self._json({
                    "error": ("a model is loading; wait until it is ready "
                              "before running a workflow")}, 409)
            job = studio.LANE.submit(spec)
            return self._json({"ok": True, "job": job}, 202)
        if u.path == "/api/load":
            model = (body.get("model") or "").strip()
            if not model:
                return self._json({"error": "model is required"}, 400)
            try:
                tok = device.key()
            except SystemExit as e:
                return self._json({"error": str(e)}, 503)
            result = studio.start_load(tok, model)
            if not result.get("ok"):
                code = 409 if result.get("busy") else 400
                return self._json({"error": result["error"]}, code)
            return self._json(result, 202)
        if u.path == "/api/unload":
            model = (body.get("model") or "").strip()
            if not model:
                return self._json({"error": "model is required"}, 400)
            try:
                tok = device.key()
            except SystemExit as e:
                return self._json({"error": str(e)}, 503)
            result = studio.stop_model(tok, model)
            if not result.get("ok"):
                code = 409 if result.get("busy") else 400
                return self._json({"error": result["error"]}, code)
            return self._json(result)
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
