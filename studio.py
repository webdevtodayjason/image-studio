#!/usr/bin/env python3
"""Image studio for a Tiiny Pocket Lab.

    python3 studio.py --serve 8430     open the page
    python3 studio.py --selfcheck      prove the install works

The box does text-to-image and image-to-text. It does not do image-to-image.
A workflow is an ordered list of those steps, one request each, one at a time.

Gallery lives in ~/.local/share/tiiny-image-studio/ so an app update does not
throw it away. Load and unload from the page. A model that does not fit the
free NPU units is refused here, because the device would accept it and roll
it back without saying so. A workflow that must swap models says so up front.
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
import media

VERSION = "0.3.1"
device.VERSION = VERSION
HERE = pathlib.Path(__file__).resolve().parent

# The gateway kills a request at 220 seconds. Matching that here means a hung
# call fails with our timeout rather than hanging the queue behind it.
GATEWAY_CAP_S = 220
PLATE = 512
DEFAULT_STEPS = 8
CHAT_MAX_TOKENS = 1024
SWAP_LOAD_S = 120

IMAGE_TYPE = "Text-to-Image"
READ_TYPE = "Image-Text-to-Text"
THINK_TYPES = ("Image-Text-to-Text", "Text Generation")

# Measured 2026-09-19 against 172.17.7.177. These are the only fields
# /v1/image/generate validates. Do not add others.
GENERATE_FIELDS = (
    "model", "prompt", "negative_prompt", "width", "height", "seed", "steps",
)

HONESTY_LINE = (
    "Re-rendered, not edited. The box cannot modify pixels in an uploaded "
    "image, so this is a new painting based on what the model saw."
)

READ_DESCRIBE = "What is in this picture. Describe it clearly."
READ_FAITHFUL = (
    "Write a faithful, detailed image-generation prompt of this picture. "
    "Describe the subject, composition, lighting, materials, colours, and "
    "style. Reply with the prompt only."
)
READ_CRITIQUE = (
    "This image was generated for this intent:\n\n{intent}\n\n"
    "What is wrong with it. Be specific about composition, subject, lighting, "
    "and missing or extra details."
)
THINK_CHANGE = (
    "Here is an image prompt:\n\n{previous}\n\n"
    "Apply this change: {change}\n\n"
    "Reply with the revised prompt only, nothing else."
)
THINK_FIX = (
    "Original prompt:\n\n{prompt}\n\n"
    "What is wrong:\n\n{previous}\n\n"
    "Rewrite the prompt to fix those issues. Reply with the revised prompt only."
)


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


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


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


def ingest_upload(blob, name=""):
    png, width, height = media.to_fitted_png(blob)
    iid = new_id()
    info = {
        "id": iid,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "upload",
        "name": pathlib.Path(name or "upload.png").name[:80],
        "width": width,
        "height": height,
        "prompt": "",
        "seed": None,
        "model": None,
    }
    return save_item(png, info)


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


def decode_data_url(value):
    if not isinstance(value, str) or not value:
        return None
    if "," in value[:80] and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value, validate=False)
    except Exception:  # noqa: BLE001
        return None


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


def catalog_rows(tok):
    cat = device.catalog(tok)
    if isinstance(cat, dict) and "_error" in cat:
        return cat
    live = set(device.running(tok))
    npu = npu_view(tok)
    free = npu["free"]
    rows = []
    for m in cat:
        row = dict(m)
        row["loaded"] = m.get("id") in live
        cost = row.get("npu_usage") or 0
        row["fits"] = bool(row.get("loaded") or cost <= free)
        rows.append(row)
    return rows


def _typed(tok, *types):
    rows = catalog_rows(tok)
    if isinstance(rows, dict) and "_error" in rows:
        return rows
    wanted = set(types)
    return [m for m in rows if (m.get("type") or "") in wanted]


def image_models(tok):
    return _typed(tok, IMAGE_TYPE)


def read_models(tok):
    return _typed(tok, READ_TYPE)


def think_models(tok):
    return _typed(tok, *THINK_TYPES)


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
                         "status": status or "running",
                         "type": (by_id.get(mid) or {}).get("type")})

    for m in device._npu_rows(units):
        add(m.get("model_id") or m.get("id"), m.get("npu_usage"), m.get("status"))
    for inst in ((detail.get("instances") or {}).get("running") or []):
        if isinstance(inst, dict):
            add(inst.get("model_id"), inst.get("npu_usage"), inst.get("status"))
    if not resident:
        for mid in device.running(tok):
            add(mid, None, "running")
    return {"used": used, "free": free, "total": total, "resident": resident}


def preset_public():
    return [
        {"id": "describe", "name": "Describe",
         "needs_upload": True, "needs_change": False, "needs_n": False,
         "needs_prompt": False, "honesty": False, "kinds": ["read"]},
        {"id": "rerender", "name": "Re-render with a change",
         "needs_upload": True, "needs_change": True, "needs_n": False,
         "needs_prompt": False, "honesty": True,
         "kinds": ["read", "think", "render"]},
        {"id": "refine", "name": "Refine",
         "needs_upload": False, "needs_change": False, "needs_n": True,
         "needs_prompt": True, "honesty": False,
         "kinds": ["render", "read", "think", "render"]},
        {"id": "restyle", "name": "Restyle",
         "needs_upload": True, "needs_change": False, "needs_n": False,
         "needs_prompt": False, "honesty": True,
         "kinds": ["read", "render"]},
        {"id": "custom", "name": "Custom",
         "needs_upload": False, "needs_change": False, "needs_n": False,
         "needs_prompt": False, "honesty": False,
         "kinds": []},
    ]


def models_payload(tok):
    rows = catalog_rows(tok)
    if isinstance(rows, dict) and "_error" in rows:
        return rows
    npu = npu_view(tok)
    render = [m for m in rows if (m.get("type") or "") == IMAGE_TYPE]
    read = [m for m in rows if (m.get("type") or "") == READ_TYPE]
    think = [m for m in rows if (m.get("type") or "") in THINK_TYPES]
    return {
        "models": render,
        "render": render,
        "read": read,
        "think": think,
        "running": [m["id"] for m in render if m.get("loaded")],
        "npu": npu,
        "plate": PLATE,
        "loading": LOADER.snapshot(),
        "presets": preset_public(),
        "honesty": HONESTY_LINE,
    }


def model_cost(tok, model):
    rows = catalog_rows(tok)
    if isinstance(rows, dict):
        return 0
    row = next((m for m in rows if m.get("id") == model), None)
    return (row.get("npu_usage") or 0) if row else 0


def refuse_load(tok, model):
    """Why this model cannot be loaded, or None.

    The NPU budget is checked here rather than left to the device because the
    device does not refuse: a start that does not fit is accepted, sits in
    npu/status as loading, and then vanishes with no error anywhere.
    """
    rows = catalog_rows(tok)
    if isinstance(rows, dict) and "_error" in rows:
        return device.refusal_text(rows)
    row = next((m for m in rows if m.get("id") == model), None)
    if row is None:
        return ("%s is not an installed model." % model)
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


def paint(tok, model, prompt, negative, seed, steps, extra=None):
    """One plate. Caller holds the lane; this never overlaps another call."""
    rows = catalog_rows(tok)
    if isinstance(rows, list):
        row = next((m for m in rows if m.get("id") == model), None)
        if row is not None and (row.get("type") or "") != IMAGE_TYPE:
            return {"ok": False, "error": (
                "%s is not a Text-to-Image model." % model)}
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
    iid = new_id()
    info = {
        "id": iid,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "render",
        "model": model,
        "prompt": prompt,
        "negative_prompt": negative or "",
        "seed": seed,
        "steps": steps,
        "width": PLATE,
        "height": PLATE,
        "wall_s": wall,
    }
    if extra:
        info.update(extra)
    save_item(png, info)
    return {"ok": True, "item": info, "wall_s": wall}


def chat_complete(tok, model, text, image_png=None):
    """One /v1/chat/completions call. Image goes as an image_url data URI."""
    live = device.running(tok)
    if model not in live:
        return {"ok": False, "not_loaded": True,
                "error": ("%s is not loaded. Load it from the rail." % model)}
    if image_png:
        b64 = base64.b64encode(image_png).decode("ascii")
        content = [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
            {"type": "text", "text": text},
        ]
    else:
        content = text
    body = {
        "model": model,
        "max_tokens": CHAT_MAX_TOKENS,
        "messages": [{"role": "user", "content": content}],
    }
    t0 = time.time()
    v = device.api(device.gw("/v1/chat/completions"), tok, body,
                   timeout=GATEWAY_CAP_S)
    wall = round(time.time() - t0, 2)
    if not isinstance(v, dict):
        return {"ok": False, "error": "unexpected chat response", "wall_s": wall}
    if v.get("_error"):
        if device.not_loaded(v):
            return {"ok": False, "not_loaded": True, "wall_s": wall,
                    "error": ("%s is not loaded. Load it from the rail. "
                              "The device said: %s"
                              % (model, device.refusal_text(v)))}
        return {"ok": False, "error": device.refusal_text(v), "wall_s": wall}
    msg = ((v.get("choices") or [{}])[0].get("message") or {})
    out = (msg.get("content") or "").strip()
    if not out:
        out = (msg.get("reasoning_content") or "").strip()
    if not out:
        return {"ok": False, "error": "the model returned no text", "wall_s": wall}
    return {"ok": True, "text": out, "wall_s": wall}


def fill_text(template, ctx):
    text = template or ""
    for key in ("previous", "change", "prompt", "intent"):
        text = text.replace("{%s}" % key, ctx.get(key) or "")
    return text


def pick_loaded(rows, fallback_id):
    if isinstance(rows, dict):
        return fallback_id
    loaded = next((m.get("id") for m in rows if m.get("loaded") and m.get("id")), None)
    if loaded:
        return loaded
    fits = next((m.get("id") for m in rows if m.get("fits") and m.get("id")), None)
    if fits:
        return fits
    if rows and rows[0].get("id"):
        return rows[0]["id"]
    return fallback_id


def expand_preset(preset, n, read_model, think_model, render_model):
    if preset == "describe":
        return [{"kind": "read", "model": read_model, "instruction": READ_DESCRIBE}]
    if preset == "rerender":
        return [
            {"kind": "read", "model": read_model, "instruction": READ_FAITHFUL},
            {"kind": "think", "model": think_model, "instruction": THINK_CHANGE},
            {"kind": "render", "model": render_model},
        ]
    if preset == "restyle":
        return [
            {"kind": "read", "model": read_model, "instruction": READ_FAITHFUL},
            {"kind": "render", "model": render_model},
        ]
    if preset == "refine":
        steps = [{"kind": "render", "model": render_model}]
        for _ in range(n):
            steps.extend([
                {"kind": "read", "model": read_model, "instruction": READ_CRITIQUE},
                {"kind": "think", "model": think_model, "instruction": THINK_FIX},
                {"kind": "render", "model": render_model},
            ])
        return steps
    return None


def plan_workflow(tok, steps):
    """What must load or unload, in order, for these steps to run.

    A start that does not fit is accepted by the device and then rolled back,
    so the plan is computed here. extra_s is 120 per load that is not already
    resident at that point in the chain.
    """
    npu = npu_view(tok)
    rows = catalog_rows(tok)
    costs = {}
    types = {}
    if isinstance(rows, list):
        for m in rows:
            if m.get("id"):
                costs[m["id"]] = m.get("npu_usage") or 0
                types[m["id"]] = m.get("type")
    sim = {r["id"]: (r.get("npu_usage") if r.get("npu_usage") is not None
                     else costs.get(r["id"]) or 0)
           for r in npu["resident"]}
    used = sum(sim.values())
    total = npu["total"] or 100
    loads, unloads = [], []
    for i, step in enumerate(steps):
        model = step.get("model")
        if not model:
            return None, "step %d is missing a model" % (i + 1)
        if model in sim:
            continue
        cost = costs.get(model)
        if cost is None:
            return None, "%s is not an installed model." % model
        if cost > total:
            return None, ("%s needs %d NPU units and the box only has %d."
                          % (model, cost, total))
        while used + cost > total and sim:
            victim = max(sim, key=lambda k: sim[k])
            unloads.append({
                "model": victim, "npu_usage": sim[victim], "before_step": i + 1,
            })
            used -= sim.pop(victim)
        if used + cost > total:
            return None, ("%s needs %d NPU units and only %d of %d would be free "
                          "after unloading."
                          % (model, cost, max(0, total - used), total))
        loads.append({"model": model, "npu_usage": cost, "before_step": i + 1})
        sim[model] = cost
        used += cost
    extra_s = SWAP_LOAD_S * len(loads)
    return {
        "swap": bool(unloads),
        "loads": loads,
        "unloads": unloads,
        "extra_s": extra_s,
        "resident": npu["resident"],
        "used": npu["used"],
        "free": npu["free"],
        "total": total,
    }, None


def chain_honesty(steps, upload_id):
    if not upload_id:
        return False
    return any(s.get("kind") == "render" for s in steps)


def _clean_custom_steps(raw):
    steps = []
    if not isinstance(raw, list) or not raw:
        return None, "custom workflow needs at least one step"
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            return None, "step %d is not an object" % (i + 1)
        kind = (row.get("kind") or "").strip()
        if kind not in ("read", "think", "render"):
            return None, "step %d kind must be read, think or render" % (i + 1)
        model = (row.get("model") or "").strip()
        if not model:
            return None, "step %d needs a model" % (i + 1)
        step = {"kind": kind, "model": model}
        if kind in ("read", "think"):
            step["instruction"] = (row.get("instruction") or "").strip()
            if not step["instruction"]:
                return None, "step %d needs an instruction" % (i + 1)
        if kind == "render" and row.get("prompt"):
            step["prompt"] = str(row.get("prompt")).strip()
        steps.append(step)
    return steps, None


def new_workflow_spec(tok, body):
    preset = (body.get("preset") or "custom").strip()
    upload_id = (body.get("upload_id") or "").strip() or None
    prompt = (body.get("prompt") or "").strip()
    change = (body.get("change") or "").strip()
    n = _int(body.get("n"), 1, lo=1, hi=8)
    read_model = (body.get("read_model") or "").strip()
    think_model = (body.get("think_model") or "").strip()
    render_model = (body.get("render_model") or body.get("model") or "").strip()
    if not read_model:
        read_model = pick_loaded(read_models(tok), "")
    if not think_model:
        think_model = pick_loaded(think_models(tok), read_model)
    if not render_model:
        render_model = pick_loaded(image_models(tok), "")

    if preset == "custom":
        steps, err = _clean_custom_steps(body.get("steps"))
        if err:
            return None, err
    else:
        known = {p["id"] for p in preset_public()}
        if preset not in known or preset == "custom":
            return None, "unknown preset"
        if preset == "describe" and not upload_id:
            return None, "Describe needs an upload"
        if preset == "rerender":
            if not upload_id:
                return None, "Re-render needs an upload"
            if not change:
                return None, "Re-render needs a change"
        if preset == "restyle" and not upload_id:
            return None, "Restyle needs an upload"
        if preset == "refine" and not prompt:
            return None, "Refine needs a prompt"
        need_read = preset in ("describe", "rerender", "restyle", "refine")
        need_think = preset in ("rerender", "refine")
        need_render = preset in ("rerender", "restyle", "refine")
        if need_read and not read_model:
            return None, "no Image-Text-to-Text model is installed"
        if need_think and not think_model:
            return None, "no chat model is installed"
        if need_render and not render_model:
            return None, "no Text-to-Image model is installed"
        steps = expand_preset(preset, n, read_model, think_model, render_model)
        if not steps:
            return None, "unknown preset"

    if upload_id and not read_item(upload_id):
        return None, "no such upload"
    if any(s["kind"] == "read" for s in steps) and not upload_id:
        first_read = next(i for i, s in enumerate(steps) if s["kind"] == "read")
        if not any(s["kind"] == "render" for s in steps[:first_read]):
            return None, "a read step needs an upload or an earlier render"

    seed = body.get("seed")
    if seed in (None, "", -1, "-1"):
        seed = random.randint(1, 2 ** 31 - 1)
    else:
        seed = _int(seed, random.randint(1, 2 ** 31 - 1))
    render_steps = _int(body.get("steps"), DEFAULT_STEPS, lo=1, hi=200)

    plan, err = plan_workflow(tok, steps)
    if err:
        return None, err
    honesty = chain_honesty(steps, upload_id)
    return {
        "kind": "workflow",
        "preset": preset,
        "steps": steps,
        "upload_id": upload_id,
        "prompt": prompt,
        "change": change,
        "negative_prompt": (body.get("negative_prompt") or "").strip(),
        "seed": seed,
        "render_steps": render_steps,
        "n": n,
        "plan": plan,
        "honesty": honesty,
        "allow_swap": bool(body.get("confirm")) if plan.get("swap") else True,
    }, None


def ensure_model(tok, model, job, step_i):
    live = device.running(tok)
    if model in live:
        return None
    npu = npu_view(tok)
    cost = model_cost(tok, model)
    total = npu["total"] or 100
    if npu["used"] + cost > total:
        if not job.get("allow_swap"):
            return ("%s needs %d NPU units and only %d of %d are free. "
                    "This run did not confirm a swap."
                    % (model, cost, npu["free"], total))
        plan = job.get("plan") or {}
        for u in plan.get("unloads") or []:
            if u.get("before_step") != step_i:
                continue
            if u.get("model") in device.running(tok) and u.get("model") != model:
                job["step_label"] = "unloading " + u["model"]
                device.unload(tok, u["model"])
        npu = npu_view(tok)
        if npu["used"] + cost > total:
            for r in sorted(npu["resident"],
                            key=lambda x: -(x.get("npu_usage") or 0)):
                if r["id"] == model:
                    continue
                job["step_label"] = "unloading " + r["id"]
                device.unload(tok, r["id"])
                npu = npu_view(tok)
                if npu["used"] + cost <= total:
                    break
        npu = npu_view(tok)
        if npu["used"] + cost > total:
            return ("%s still does not fit after unloading. Resident: %s"
                    % (model, "; ".join(r["id"] for r in npu["resident"]) or "nothing"))
    job["step_label"] = "loading " + model
    if not device.load(tok, model):
        return ("%s did not come up. The device accepts a load that does "
                "not fit and then rolls it back without saying so, or the "
                "start timed out." % model)
    return None


def run_workflow(tok, job):
    steps = job["steps"]
    ctx = {
        "prompt": job.get("prompt") or "",
        "change": job.get("change") or "",
        "intent": job.get("prompt") or "",
        "previous": "",
        "text": "",
        "image_id": job.get("upload_id"),
        "honesty": job.get("honesty") or False,
    }
    job["step_n"] = len(steps)
    for i, step in enumerate(steps):
        job["step_i"] = i + 1
        job["step_kind"] = step["kind"]
        job["model"] = step["model"]
        job["step_label"] = step["kind"]
        err = ensure_model(tok, step["model"], job, i + 1)
        if err:
            return {"ok": False, "error": err, "not_loaded": "not loaded" in err}
        job["step_label"] = step["kind"]
        kind = step["kind"]
        if kind == "read":
            iid = ctx.get("image_id")
            item = read_item(iid) if iid else None
            if not item:
                return {"ok": False, "error": "no image to read"}
            png = item["png"].read_bytes()
            instruction = fill_text(step.get("instruction") or READ_DESCRIBE, ctx)
            result = chat_complete(tok, step["model"], instruction, png)
            if not result.get("ok"):
                return result
            ctx["text"] = result["text"]
            ctx["previous"] = result["text"]
            job["text"] = result["text"]
        elif kind == "think":
            instruction = fill_text(step.get("instruction") or THINK_CHANGE, ctx)
            result = chat_complete(tok, step["model"], instruction, None)
            if not result.get("ok"):
                return result
            ctx["text"] = result["text"]
            ctx["previous"] = result["text"]
            job["text"] = result["text"]
        elif kind == "render":
            prompt = (step.get("prompt") or ctx.get("text")
                      or ctx.get("prompt") or "").strip()
            if not prompt:
                return {"ok": False, "error": "render needs a prompt"}
            extra = {
                "parent": ctx.get("image_id"),
                "preset": job.get("preset"),
                "source": "workflow",
            }
            if ctx.get("honesty"):
                extra["honesty"] = HONESTY_LINE
            result = paint(tok, step["model"], prompt,
                           job.get("negative_prompt") or "",
                           job.get("seed"),
                           job.get("render_steps") or DEFAULT_STEPS,
                           extra=extra)
            if not result.get("ok"):
                return result
            job["item"] = result["item"]
            ctx["image_id"] = result["item"]["id"]
            ctx["prompt"] = prompt
            if not ctx.get("intent"):
                ctx["intent"] = prompt
        else:
            return {"ok": False, "error": "unknown step kind %s" % kind}
    return {"ok": True, "item": job.get("item"), "text": job.get("text")}


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
        if spec.get("kind") == "workflow":
            job = {
                "id": uuid.uuid4().hex[:12],
                "kind": "workflow",
                "status": "queued",
                "preset": spec.get("preset"),
                "steps": spec["steps"],
                "upload_id": spec.get("upload_id"),
                "prompt": spec.get("prompt") or "",
                "change": spec.get("change") or "",
                "negative_prompt": spec.get("negative_prompt") or "",
                "seed": spec["seed"],
                "render_steps": spec.get("render_steps") or DEFAULT_STEPS,
                "plan": spec.get("plan") or {},
                "honesty": spec.get("honesty") or False,
                "allow_swap": spec.get("allow_swap", True),
                "model": (spec["steps"][0].get("model") if spec["steps"] else ""),
                "step_i": 0,
                "step_n": len(spec["steps"]),
                "step_kind": None,
                "step_label": None,
                "text": None,
                "item": None,
                "error": None,
                "not_loaded": False,
                "created": time.time(),
                "started": None,
                "finished": None,
            }
        else:
            job = {
                "id": uuid.uuid4().hex[:12],
                "kind": "generate",
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
        return dict(self._public(job))

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
        kind = j.get("kind") or "generate"
        if kind == "workflow":
            out = {k: j.get(k) for k in (
                "id", "kind", "status", "preset", "model", "prompt", "change",
                "seed", "error", "item", "not_loaded", "honesty", "text",
                "step_i", "step_n", "step_kind", "step_label", "upload_id")}
            out["plan"] = j.get("plan") or {}
            out["steps"] = [
                {"kind": s.get("kind"), "model": s.get("model")}
                for s in (j.get("steps") or [])
            ]
        else:
            out = {k: j[k] for k in (
                "id", "status", "model", "prompt", "negative_prompt", "seed",
                "steps", "error", "item", "not_loaded")}
            out["kind"] = "generate"
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
                if job.get("kind") == "workflow":
                    result = run_workflow(tok, job)
                else:
                    result = paint(tok, job["model"], job["prompt"],
                                   job["negative_prompt"], job["seed"], job["steps"])
                if result.get("ok"):
                    job["status"] = "done"
                    if result.get("item"):
                        job["item"] = result["item"]
                    if result.get("text"):
                        job["text"] = result["text"]
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


def _wait_job(jid, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = LANE.job(jid)
        if row and row["status"] in ("done", "failed"):
            return row
        time.sleep(0.2)
    return LANE.job(jid)


def selfcheck():
    """Prove the install works, in the same spirit as tiiny-bench --selfcheck."""
    ok = True

    def step(label, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print("  %-4s %-34s %s" % ("ok" if passed else "FAIL", label, detail))

    print("\n  Image Studio %s selfcheck" % VERSION)

    page = HERE / "static" / "app.html"
    html = page.read_bytes() if page.is_file() else b""
    step("app page present", page.is_file(), str(page))
    step("honesty line on the page", HONESTY_LINE.encode() in html,
         "next to the result, not a tooltip")
    step("no megabytes on the page", b" MB" not in html and b"memory_total_mb" not in html)

    try:
        probe = gallery_dir() / ".selfcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        step("gallery writable", True, str(gallery_dir()))
    except Exception as exc:  # noqa: BLE001
        step("gallery writable", False, str(exc)[:80])

    try:
        blob = media.split_color_png(64, 64)
        up = ingest_upload(blob, "selfcheck.png")
        found = read_item(up["id"])
        step("upload round-trip", bool(found) and found.get("source") == "upload",
             "%s  %sx%s" % (up["id"], up["width"], up["height"]))
    except Exception as exc:  # noqa: BLE001
        up = None
        step("upload round-trip", False, str(exc)[:80])

    try:
        big = media.split_color_png(1800, 200)
        fitted, w, h = media.to_fitted_png(big)
        step("upload downscales to 1536", max(w, h) == 1536 and min(w, h) > 1,
             "%sx%s  %d bytes" % (w, h, len(fitted)))
    except Exception as exc:  # noqa: BLE001
        step("upload downscales to 1536", False, str(exc)[:80])

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
            served = r.read()
            code = r.status
        httpd.shutdown()
        th.join(2)
        step("page serves", code == 200 and b"<title>" in served,
             "http://127.0.0.1:%d/  %d bytes" % (port, len(served)))
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

    rows = catalog_rows(tok)
    if isinstance(rows, dict) and "_error" in rows:
        step("api key accepted", False, rows["_error"])
        print("")
        return 1
    step("api key accepted", True, "%d models installed" % len(rows))
    step("catalog is from the device", bool(rows) and all(m.get("id") for m in rows),
         ", ".join(sorted({(m.get("type") or "?") for m in rows})))

    vision = [m for m in rows
              if (m.get("type") or "") == READ_TYPE and m.get("loaded")]
    plates = [m for m in rows
              if (m.get("type") or "") == IMAGE_TYPE and m.get("loaded")]
    step("a vision model is loaded", bool(vision),
         (", ".join(m["id"] for m in vision) or "none resident; load one from the page"))
    step("an image model is loaded", bool(plates),
         (", ".join(m["id"].split("/")[-1] for m in plates)
          or "none resident; load one from the page"))

    if vision and up:
        result = chat_complete(tok, vision[0]["id"], READ_DESCRIBE,
                               read_item(up["id"])["png"].read_bytes())
        step("read step against vision model", bool(result.get("ok")),
             result.get("error") or ("%s  %ss  %s" % (
                 vision[0]["id"].split("/")[-1], result.get("wall_s"),
                 (result.get("text") or "")[:80].replace("\n", " "))))
    else:
        step("read step against vision model", False,
             "need a resident Image-Text-to-Text model and an upload")

    if vision and plates and up:
        spec, spec_err = new_workflow_spec(tok, {
            "preset": "restyle",
            "upload_id": up["id"],
            "read_model": vision[0]["id"],
            "render_model": plates[0]["id"],
            "seed": 42,
            "steps": DEFAULT_STEPS,
        })
        if spec_err:
            step("two-step chain with parent", False, spec_err)
        elif spec["plan"].get("swap") and not spec.get("allow_swap"):
            step("two-step chain with parent", False,
                 "would unload to fit: " + json.dumps(spec["plan"].get("unloads")))
        else:
            spec["allow_swap"] = False
            job = LANE.submit(spec)
            row = _wait_job(job["id"], timeout=GATEWAY_CAP_S * 2 + 30)
            item = (row or {}).get("item") or {}
            parent_ok = item.get("parent") == up["id"]
            step("two-step chain with parent",
                 bool(row and row["status"] == "done" and parent_ok),
                 (row or {}).get("error") or "%s  parent %s  honesty %s" % (
                     item.get("id"), item.get("parent"),
                     "yes" if item.get("honesty") else "no"))
    else:
        step("two-step chain with parent", False,
             "need a resident vision model, a resident image model, and an upload")

    if plates:
        target = plates[0]["id"]
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
                   help="prove the install works: page, gallery, upload, read, one chain")
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
