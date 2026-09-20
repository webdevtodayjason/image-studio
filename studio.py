#!/usr/bin/env python3
"""Image studio for a Tiiny Pocket Lab.

    python3 studio.py --serve 8430     open the page
    python3 studio.py --selfcheck      prove the install works

Generate 512x512 plates from whatever Text-to-Image model is resident, keep
them in ~/.local/share/tiiny-image-studio/ so an app update does not throw
the gallery away, and never fire two inferences at once.

Load and unload from the page. A model that does not fit the free NPU units
is refused here, because the device would accept it and roll it back without
saying so. Nothing is evicted unless the user asks.
"""
import argparse
import base64
import json
import os
import pathlib
import random
import re
import sys
import threading
import time
import uuid
from collections import deque
from http.server import ThreadingHTTPServer

import device

VERSION = device.VERSION
HERE = pathlib.Path(__file__).resolve().parent

# The gateway kills a request at 220 seconds. Matching that here means a hung
# call fails with our timeout rather than hanging the queue behind it.
GATEWAY_CAP_S = 220
PLATE = 512
DEFAULT_STEPS = 8
IMAGE_TYPE = "Text-to-Image"


def data_dir():
    """Gallery root. Not inside the install directory.

    TiinyBench used to write results next to the code. The farm stages each
    release in a new folder, so eight updates later the history was gone.
    This path is the one that survives that.
    """
    override = os.environ.get("TIINY_IMAGE_STUDIO_HOME")
    if override:
        d = pathlib.Path(override).expanduser()
    else:
        d = pathlib.Path(os.environ.get("XDG_DATA_HOME")
                         or pathlib.Path.home() / ".local" / "share")
        d = d / "tiiny-image-studio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gallery_dir():
    d = data_dir() / "gallery"
    d.mkdir(parents=True, exist_ok=True)
    return d


SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _item_paths(iid):
    if not SAFE_ID.match(iid or ""):
        return None, None
    root = gallery_dir()
    return root / ("%s.png" % iid), root / ("%s.json" % iid)


def list_items():
    items = []
    for meta in sorted(gallery_dir().glob("*.json"), reverse=True):
        try:
            row = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        iid = row.get("id") or meta.stem
        png, _ = _item_paths(iid)
        if png is None or not png.exists():
            continue
        row["id"] = iid
        row["bytes"] = png.stat().st_size
        items.append(row)
    items.sort(key=lambda r: r.get("stamp") or "", reverse=True)
    return items


def read_item(iid):
    png, meta = _item_paths(iid)
    if png is None or not png.exists() or not meta.exists():
        return None
    try:
        row = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        row = {"id": iid}
    row["id"] = iid
    row["bytes"] = png.stat().st_size
    row["png"] = png
    return row


def save_item(blob, info):
    iid = info["id"]
    png, meta = _item_paths(iid)
    tmp = png.with_suffix(".png.tmp")
    tmp.write_bytes(blob)
    tmp.replace(png)
    meta.write_text(json.dumps(info, indent=2), encoding="utf-8")
    info["bytes"] = png.stat().st_size
    return info


def delete_item(iid):
    png, meta = _item_paths(iid)
    if png is None:
        return False
    gone = False
    for p in (png, meta):
        if p.exists():
            p.unlink()
            gone = True
    return gone


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _b64_blob(value):
    if not isinstance(value, str) or len(value) < 32:
        return None
    if "," in value[:40] and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        raw = base64.b64decode(value, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return raw if raw.startswith(PNG_MAGIC) else None


def png_from_response(raw):
    """A PNG, or a dict carrying the refusal the device sent.

    Firmware returns the file. A JSON wrapper showed up on other classes in
    the 19 September 2026 sweep, so if the body is JSON we look for the
    image in it rather than recording a failure for a plate that arrived.
    """
    if isinstance(raw, dict) and "_error" in raw:
        return None, raw
    if not isinstance(raw, (bytes, bytearray)):
        return None, {"_error": "unexpected response", "_body": str(raw)[:device.MAX_CAPTURE]}
    raw = bytes(raw)
    if raw.startswith(PNG_MAGIC):
        return raw, None
    text = raw[:device.MAX_CAPTURE].decode("utf-8", "replace")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, {"_error": "the device did not return a PNG",
                      "_body": text, "_status": 200}
    if not isinstance(obj, dict):
        return None, {"_error": "the device did not return a PNG",
                      "_body": text, "_status": 200}
    for key in ("image", "image_data", "png", "b64_json"):
        found = _b64_blob(obj.get(key))
        if found:
            return found, None
    for row in obj.get("data") or obj.get("images") or []:
        if isinstance(row, str):
            found = _b64_blob(row)
            if found:
                return found, None
        if isinstance(row, dict):
            for key in ("b64_json", "image", "url"):
                found = _b64_blob(row.get(key))
                if found:
                    return found, None
    return None, {"_error": "the device did not return a PNG",
                  "_body": text, "_status": 200}


def image_models(tok):
    cat = device.catalog(tok)
    if isinstance(cat, dict) and "_error" in cat:
        return cat
    live = set(device.running(tok))
    rows = []
    for m in cat:
        if (m.get("type") or "") != IMAGE_TYPE:
            continue
        row = dict(m)
        row["loaded"] = m.get("id") in live
        rows.append(row)
    return rows


def npu_view(tok):
    """Units used and free, and what is resident. Megabytes are not here.

    The device's memory_total_mb is not a total; it jumps around on a box
    whose memory did not change. Unit counts are what we trust.
    """
    units = device.npu_status(tok)
    detail = device.running_detail(tok)
    cat = device.catalog(tok)
    by_id = {}
    if isinstance(cat, list):
        by_id = {m.get("id"): m for m in cat if isinstance(m, dict) and m.get("id")}
    total = units.get("npu_total") or 100
    used = units.get("npu_used")
    free = units.get("npu_available")
    if used is None:
        used = 0
    if free is None:
        free = max(0, total - used)
    resident = []
    seen = set()

    def add(mid, cost, status):
        if not mid or mid in seen:
            return
        seen.add(mid)
        if cost is None:
            cost = (by_id.get(mid) or {}).get("npu_usage")
        resident.append({"id": mid, "npu_usage": cost,
                         "status": status or "running"})

    for m in device._npu_rows(units):
        add(m.get("model_id") or m.get("id"), m.get("npu_usage"), m.get("status"))
    for inst in ((detail.get("instances") or {}).get("running") or []):
        if isinstance(inst, dict):
            add(inst.get("model_id"), inst.get("npu_usage"), inst.get("status"))
    if not resident:
        for mid in device.running(tok):
            add(mid, None, "running")
    return {"used": used, "free": free, "total": total, "resident": resident}


def models_payload(tok):
    models = image_models(tok)
    if isinstance(models, dict) and "_error" in models:
        return models
    npu = npu_view(tok)
    free = npu["free"]
    for m in models:
        cost = m.get("npu_usage") or 0
        m["fits"] = bool(m.get("loaded") or cost <= free)
    return {
        "models": models,
        "running": [m["id"] for m in models if m.get("loaded")],
        "npu": npu,
        "plate": PLATE,
        "loading": LOADER.snapshot(),
    }


def refuse_load(tok, model):
    """Why this model cannot be loaded, or None.

    The NPU budget is checked here rather than left to the device because the
    device does not refuse: a start that does not fit is accepted, sits in
    npu/status as loading, and then vanishes with no error anywhere.
    """
    models = image_models(tok)
    if isinstance(models, dict) and "_error" in models:
        return device.refusal_text(models)
    row = next((m for m in models if m.get("id") == model), None)
    if row is None:
        return ("%s is not an installed Text-to-Image model." % model)
    if row.get("loaded"):
        return None
    npu = npu_view(tok)
    cost = row.get("npu_usage") or 0
    free, total, used = npu["free"], npu["total"], npu["used"]
    if total and used + cost > total:
        bits = ["%s (%s units)" % (r["id"], r["npu_usage"]
                                   if r["npu_usage"] is not None else "?")
                for r in npu["resident"]]
        who = "; ".join(bits) if bits else "nothing"
        return ("%s needs %d NPU units and only %d of %d are free. Resident: %s. "
                "Unload one of those from the rail if you want this one instead. "
                "The device accepts a load that does not fit and then rolls it "
                "back without saying so, so it is refused here."
                % (model, cost, free, total, who))
    return None


def generate_busy():
    snap = LANE.snapshot()
    return snap.get("active") or snap.get("running")


def load_busy_reason():
    if generate_busy():
        return ("a generate is running; a load would sit behind it. "
                "Wait until the plate is done.")
    if LOADER.busy():
        return "already loading %s" % LOADER.model
    return None


class Loader:
    """One load at a time. The wait lives here so the HTTP handler does not freeze."""

    def __init__(self):
        self.lock = threading.Lock()
        self.model = None
        self.status = "idle"
        self.started = None
        self.finished = None
        self.error = None

    def busy(self):
        with self.lock:
            return self.status == "loading"

    def snapshot(self):
        with self.lock:
            if self.status == "idle":
                return None
            elapsed = 0
            if self.started:
                end = self.finished or time.time()
                elapsed = round(end - self.started, 1)
            return {
                "model": self.model,
                "status": self.status,
                "elapsed_s": elapsed,
                "error": self.error,
            }

    def begin(self, model):
        with self.lock:
            if self.status == "loading":
                return "already loading %s" % self.model
            self.model = model
            self.status = "loading"
            self.started = time.time()
            self.finished = None
            self.error = None
            return None

    def finish(self, ok, error=None):
        with self.lock:
            self.status = "ready" if ok else "failed"
            self.error = None if ok else (error or "load failed")
            self.finished = time.time()


LOADER = Loader()


def start_load(tok, model):
    reason = load_busy_reason()
    if reason:
        return {"ok": False, "error": reason, "busy": True}
    refusal = refuse_load(tok, model)
    if refusal:
        return {"ok": False, "error": refusal, "refused": True}
    conflict = LOADER.begin(model)
    if conflict:
        return {"ok": False, "error": conflict, "busy": True}

    def _run(t=tok, m=model):
        try:
            ok = device.load(t, m)
            if ok:
                LOADER.finish(True)
            else:
                LOADER.finish(False, (
                    "%s did not come up. The device accepts a load that does "
                    "not fit and then rolls it back without saying so, or the "
                    "start timed out." % m))
        except Exception as exc:  # noqa: BLE001
            LOADER.finish(False, "%s: %s" % (type(exc).__name__, exc))

    threading.Thread(target=_run, name="studio-load", daemon=True).start()
    return {"ok": True, "model": model, "loading": LOADER.snapshot()}


def stop_model(tok, model):
    if generate_busy():
        running = LANE.snapshot().get("running") or {}
        if running.get("model") == model:
            return {"ok": False, "error": (
                "%s is generating a plate. Wait until it finishes, then unload."
                % model), "busy": True}
    if LOADER.busy() and LOADER.model == model:
        return {"ok": False, "error": "%s is still coming up." % model,
                "busy": True}
    live = device.running(tok)
    if model not in live:
        return {"ok": False, "error": "%s is not loaded." % model}
    device.unload(tok, model)
    return {"ok": True, "model": model}


def paint(tok, model, prompt, negative, seed, steps):
    """One plate. Caller holds the lane; this never overlaps another call."""
    live = device.running(tok)
    if model not in live:
        return {"ok": False, "not_loaded": True,
                "error": ("%s is not loaded. Load it from the rail." % model)}
    body = {
        "model": model,
        "prompt": prompt,
        "negative_prompt": negative or "",
        "width": PLATE,
        "height": PLATE,
        "seed": seed,
        "steps": steps,
    }
    t0 = time.time()
    raw = device.api_raw(device.gw("/v1/image/generate"), tok, body,
                         timeout=GATEWAY_CAP_S)
    wall = round(time.time() - t0, 2)
    png, err = png_from_response(raw)
    if err:
        if device.not_loaded(err):
            msg = ("%s is not loaded. Load it from the rail. The device said: %s"
                   % (model, device.refusal_text(err)))
            return {"ok": False, "not_loaded": True, "error": msg,
                    "wall_s": wall, "raw": err}
        return {"ok": False, "error": device.refusal_text(err),
                "wall_s": wall, "raw": err}
    iid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    info = {
        "id": iid,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "prompt": prompt,
        "negative_prompt": negative or "",
        "seed": seed,
        "steps": steps,
        "width": PLATE,
        "height": PLATE,
        "wall_s": wall,
    }
    save_item(png, info)
    return {"ok": True, "item": info, "wall_s": wall}


class Lane:
    """One inference at a time. The hardware queues; so does this."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.pending = deque()
        self.current = None
        self.jobs = {}
        self.active = 0
        self.worker = threading.Thread(target=self._loop, name="studio-lane",
                                       daemon=True)
        self.started = False

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
            self.worker.start()

    def submit(self, spec):
        self.start()
        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "queued",
            "model": spec["model"],
            "prompt": spec["prompt"],
            "negative_prompt": spec.get("negative_prompt") or "",
            "seed": spec["seed"],
            "steps": spec["steps"],
            "created": time.time(),
            "started": None,
            "finished": None,
            "error": None,
            "item": None,
            "not_loaded": False,
        }
        with self.cv:
            self.jobs[job["id"]] = job
            self.pending.append(job)
            self.cv.notify()
        return dict(job)

    def snapshot(self):
        with self.lock:
            waiting = [self._public(j) for j in self.pending]
            current = self._public(self.current) if self.current else None
            return {
                "running": current,
                "queue": waiting,
                "depth": len(waiting) + (1 if current else 0),
                "active": self.active,
            }

    def job(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return self._public(j) if j else None

    def _public(self, j):
        if not j:
            return None
        out = {k: j[k] for k in (
            "id", "status", "model", "prompt", "negative_prompt", "seed",
            "steps", "error", "item", "not_loaded")}
        out["elapsed_s"] = round(
            (time.time() - j["started"]) if j.get("started") and not j.get("finished")
            else ((j["finished"] - j["started"]) if j.get("started") and j.get("finished")
                  else 0), 1)
        return out

    def _loop(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                job = self.pending.popleft()
                self.current = job
                self.active = 1
                job["status"] = "running"
                job["started"] = time.time()
            try:
                tok = device.key()
                result = paint(tok, job["model"], job["prompt"],
                               job["negative_prompt"], job["seed"], job["steps"])
                if result.get("ok"):
                    job["status"] = "done"
                    job["item"] = result["item"]
                else:
                    job["status"] = "failed"
                    job["error"] = result.get("error") or "failed"
                    job["not_loaded"] = bool(result.get("not_loaded"))
            except SystemExit as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                job["status"] = "failed"
                job["error"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["finished"] = time.time()
                with self.lock:
                    self.current = None
                    self.active = 0


LANE = Lane()


def _int(value, default, lo=None, hi=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


def new_job_spec(body):
    model = (body.get("model") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    if not model:
        return None, "model is required"
    if not prompt:
        return None, "prompt is required"
    seed = body.get("seed")
    if seed in (None, "", -1, "-1"):
        seed = random.randint(1, 2 ** 31 - 1)
    else:
        seed = _int(seed, random.randint(1, 2 ** 31 - 1))
    steps = _int(body.get("steps"), DEFAULT_STEPS, lo=1, hi=200)
    return {
        "model": model,
        "prompt": prompt,
        "negative_prompt": (body.get("negative_prompt") or "").strip(),
        "seed": seed,
        "steps": steps,
    }, None


def selfcheck():
    """Prove the install works, in the same spirit as tiiny-bench --selfcheck."""
    ok = True

    def step(label, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print("  %-4s %-34s %s" % ("ok" if passed else "FAIL", label, detail))

    print("\n  Image Studio %s selfcheck" % VERSION)

    page = HERE / "static" / "app.html"
    step("app page present", page.is_file(), str(page))

    try:
        probe = gallery_dir() / ".selfcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        step("gallery writable", True, str(gallery_dir()))
    except Exception as exc:  # noqa: BLE001
        step("gallery writable", False, str(exc)[:80])

    # Boot the web app on an ephemeral port and fetch the page, so a missing
    # static file or a handler that 500s is caught without a browser.
    try:
        import serve
        LANE.start()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        httpd.daemon_threads = True
        th = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        port = httpd.server_address[1]
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as r:
            html = r.read()
            code = r.status
        httpd.shutdown()
        th.join(2)
        step("page serves", code == 200 and b"<title>" in html,
             "http://127.0.0.1:%d/  %d bytes" % (port, len(html)))
    except Exception as exc:  # noqa: BLE001
        step("page serves", False, str(exc)[:80])

    w, err = device.connect_soft()
    if not w:
        print("  device   not found")
        print("           " + (err or "").strip().replace("\n", "\n           "))
        step("device reachable", False, "no box; set TIINY_BASE or --host")
        print("")
        return 0 if ok else 1

    print("  device   %s   found by %s   %s plane"
          % (w["host"], w["source"], w["plane"])
          + (("   serial %s" % w["serial"]) if w.get("serial") else ""))
    print("  gateway  " + (
        "port 80 with Host: %s" % w["gateway_vhost"]
        if w["gateway_transport"] == "vhost"
        else "direct on port %s" % w["gateway_port"])
          + "   (%s)\n" % w["gateway_transport"])

    t0 = time.time()
    try:
        tok = device.key()
        info = device.api(device.mgmt("/api/v1/sys/device_info"), tok, timeout=20)
        step("device reachable", "_error" not in info,
             info.get("_error", "") or "TiinyOS %s  %0.0fms" % (
                 info.get("tiiny_os", "?"), (time.time() - t0) * 1000))
    except SystemExit as exc:
        step("api key accepted", False, str(exc))
        print("")
        return 1

    models = image_models(tok)
    if isinstance(models, dict) and "_error" in models:
        step("api key accepted", False, models["_error"])
        print("")
        return 1
    step("api key accepted", True, "%d Text-to-Image models installed" % len(models))

    live = [m for m in models if m.get("loaded")]
    step("an image model is loaded", bool(live),
         (", ".join(m["id"].split("/")[-1] for m in live)
          or "none resident; load one from the page"))

    if live:
        target = live[0]["id"]
        result = paint(tok, target, "a red apple on a wooden table, studio light",
                       "", 42, DEFAULT_STEPS)
        step("inference returns", bool(result.get("ok")),
             result.get("error") or "%s  %s  %ss  seed %s" % (
                 target.split("/")[-1], result["item"]["id"],
                 result.get("wall_s"), result["item"]["seed"]))
        if result.get("ok"):
            listed = {i["id"] for i in list_items()}
            step("gallery keeps the plate", result["item"]["id"] in listed,
                 str(gallery_dir() / (result["item"]["id"] + ".png")))
    else:
        step("inference returns", False,
             "nothing loaded that this can paint")

    print("")
    if ok:
        print("  All good. Open the app with:  python3 studio.py --serve 8430")
    else:
        print("  Something above needs fixing before a generate will mean anything.")
    print("")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="studio.py")
    p.add_argument("--serve", nargs="?", const=8430, type=int, metavar="PORT",
                   help="run the web app (default port 8430)")
    p.add_argument("--selfcheck", action="store_true",
                   help="prove the install works: page, gallery, one real plate if loaded")
    p.add_argument("--host", help="the Tiiny's address, instead of finding it")
    p.add_argument("--serial", help="pick a box by serial when more than one answers")
    p.add_argument("--rescan", action="store_true",
                   help="ignore the saved address and look for boxes again")
    p.add_argument("--version", action="version", version="image-studio " + VERSION)
    a = p.parse_args(argv)

    if a.selfcheck:
        return selfcheck()

    if a.serve is not None:
        import serve
        return serve.run(a.serve, host=a.host, serial=a.serial, rescan=a.rescan)

    p.print_help()
    print("\n  start with:  python3 studio.py --serve 8430")
    return 0


if __name__ == "__main__":
    sys.exit(main())
