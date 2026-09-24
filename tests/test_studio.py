"""Gallery, queue, refusals, and the HTTP app, against a fake Tiiny."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import device  # noqa: E402
import serve  # noqa: E402
import studio  # noqa: E402
from tests.fake_device import PNG, FakeDevice  # noqa: E402


class StudioCase(unittest.TestCase):
    loaded = ["Tongyi-MAI/Z-Image-Turbo"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["TIINY_IMAGE_STUDIO_HOME"] = self.tmp.name
        os.environ["TIINY_KEY"] = "test-key"
        os.environ.pop("TIINY_BASE", None)
        os.environ.pop("TIINY_HOST", None)

        self.fake = FakeDevice(loaded=self.loaded).start()
        self.addCleanup(self.fake.stop)

        saved = (device.HOST, dict(device.SERVICES), dict(device.TRANSPORT),
                 device.PORT_OVERRIDE, device.SOURCE, device.PLANE)
        self.addCleanup(lambda: self._restore(saved))
        device.HOST = self.fake.host
        device.PORT_OVERRIDE = None
        device.TRANSPORT.clear()
        device.SERVICES.clear()
        device.SERVICES.update({n: (self.fake.port, None)
                                for n in ("gateway", "openai", "mgmt")})
        device.SOURCE = "test"
        device.PLANE = "given"

        self.lane = studio.Lane()
        studio.LANE = self.lane
        self.loader = studio.Loader()
        studio.LOADER = self.loader
        self._poll = (device.LOAD_POLL_S, device.LOAD_SETTLE_S, device.UNLOAD_SETTLE_S)
        device.LOAD_POLL_S = 0.05
        device.LOAD_SETTLE_S = 0.05
        device.UNLOAD_SETTLE_S = 0.05
        self.addCleanup(self._restore_poll)

    def _restore_poll(self):
        device.LOAD_POLL_S, device.LOAD_SETTLE_S, device.UNLOAD_SETTLE_S = self._poll

    def _restore(self, saved):
        device.HOST, device.PORT_OVERRIDE = saved[0], saved[3]
        device.SERVICES.clear()
        device.SERVICES.update(saved[1])
        device.TRANSPORT.clear()
        device.TRANSPORT.update(saved[2])
        device.SOURCE, device.PLANE = saved[4], saved[5]

    def tok(self):
        return "test-key"


class TestRefusalShapes(unittest.TestCase):
    """The device is not consistent about what sits under "error".

    Every route this app drives puts an object there. /v1/ocr puts a bare
    string, {"error": "Endpoint not found"}, when a model is loaded that does
    not implement the route. A string has no .get, so refusal_text used to
    raise AttributeError out of the one function whose job is turning a
    refusal into a sentence. Found 2026-09-19 while building Foolscap.
    """

    def refusal(self, body, status=503):
        return {"_error": "HTTP Error %d" % status, "_status": status,
                "_body": body}

    def test_a_bare_string_error_is_the_sentence_not_a_crash(self):
        v = self.refusal('{"error":"Endpoint not found"}', 404)
        self.assertEqual(device.refusal_text(v), "Endpoint not found")
        self.assertFalse(device.not_loaded(v))

    def test_the_ordinary_object_shape_still_reads(self):
        v = self.refusal('{"error":{"message":"No suitable model is currently '
                         'running.","type":"service_unavailable"}}')
        self.assertEqual(device.refusal_text(v),
                         "No suitable model is currently running.")
        self.assertTrue(device.not_loaded(v))

    def test_a_bare_string_still_answers_the_not_loaded_question(self):
        # the sentence is read, not discarded, because it is all there is
        v = self.refusal('{"error":"No suitable model is currently running."}')
        self.assertTrue(device.not_loaded(v))

    def test_bodies_that_are_not_an_object_fall_through_to_the_body(self):
        for body in ('{"detail":"Not Found"}', "[1, 2]", '"just a string"',
                     "not json at all", ""):
            v = self.refusal(body)
            self.assertIsInstance(device.refusal_text(v), str)
            self.assertFalse(device.not_loaded(v))


class TestGallery(StudioCase):
    def test_gallery_is_not_inside_the_install(self):
        path = str(studio.gallery_dir().resolve())
        self.assertFalse(path.startswith(str(HERE.resolve())))
        self.assertTrue(path.startswith(str(pathlib.Path(self.tmp.name).resolve())))

    def test_a_plate_survives_a_new_read(self):
        info = studio.save_item(PNG, {
            "id": "20260919-120000-deadbeef",
            "stamp": "2026-09-19T12:00:00",
            "model": "Tongyi-MAI/Z-Image-Turbo",
            "prompt": "a red apple",
            "negative_prompt": "",
            "seed": 7, "steps": 8, "width": 512, "height": 512, "wall_s": 1.2,
        })
        found = {i["id"] for i in studio.list_items()}
        self.assertIn(info["id"], found)
        item = studio.read_item(info["id"])
        self.assertEqual(item["png"].read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(item["seed"], 7)

    def test_delete_removes_png_and_sidecar(self):
        studio.save_item(PNG, {
            "id": "del-me", "stamp": "2026-09-19T12:00:00",
            "model": "x", "prompt": "p", "negative_prompt": "",
            "seed": 1, "steps": 8, "width": 512, "height": 512, "wall_s": 1,
        })
        self.assertTrue(studio.delete_item("del-me"))
        self.assertEqual(studio.list_items(), [])
        self.assertFalse(studio.delete_item("del-me"))


class TestPaint(StudioCase):
    def test_a_resident_model_returns_a_png_on_disk(self):
        result = studio.paint(self.tok(), "Tongyi-MAI/Z-Image-Turbo",
                              "a lighthouse", "", 1000, 8)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["item"]["seed"], 1000)
        png, _ = studio._item_paths(result["item"]["id"])
        self.assertTrue(png.exists())
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_a_model_that_is_not_loaded_is_a_sentence(self):
        result = studio.paint(self.tok(), "black-forest-labs/FLUX.2-klein-4B",
                              "anything", "", 1, 8)
        self.assertFalse(result["ok"])
        self.assertTrue(result["not_loaded"])
        self.assertIn("not loaded", result["error"])
        self.assertIn("Load it from the rail", result["error"])
        self.assertEqual(self.fake.state.calls, [])

    def test_json_wrapper_still_counts_as_a_png(self):
        import base64
        wrapped = json.dumps({"image": base64.b64encode(PNG).decode()}).encode()
        png, err = studio.png_from_response(wrapped)
        self.assertIsNone(err)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))


class TestLane(StudioCase):
    def test_two_jobs_never_run_on_the_device_at_once(self):
        a, _ = studio.new_job_spec({"model": "Tongyi-MAI/Z-Image-Turbo",
                                    "prompt": "one", "seed": 1, "steps": 8})
        b, _ = studio.new_job_spec({"model": "Tongyi-MAI/Z-Image-Turbo",
                                    "prompt": "two", "seed": 2, "steps": 8})
        ja = self.lane.submit(a)
        jb = self.lane.submit(b)
        deadline = time.time() + 5
        while time.time() < deadline:
            sa, sb = self.lane.job(ja["id"]), self.lane.job(jb["id"])
            if sa["status"] in ("done", "failed") and sb["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(self.lane.job(ja["id"])["status"], "done")
        self.assertEqual(self.lane.job(jb["id"])["status"], "done")
        self.assertEqual(self.fake.state.peak, 1)
        snap = self.lane.snapshot()
        self.assertEqual(snap["active"], 0)
        self.assertIsNone(snap["running"])


class TestHttp(StudioCase):
    def setUp(self):
        super().setUp()
        self.app = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.app.daemon_threads = True
        th = threading.Thread(target=self.app.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = "http://127.0.0.1:%d" % self.app.server_address[1]

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw, status = resp.read(), resp.status
                ctype = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            raw, status, ctype = exc.read(), exc.code, exc.headers.get("Content-Type")
        if ctype and "json" in ctype:
            return status, json.loads(raw.decode())
        return status, raw

    def test_the_page_loads(self):
        status, raw = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Image Studio", raw)
        self.assertIn(b"Unload", raw)
        self.assertIn(b"loadbtn", raw)
        self.assertIn(b"#4FD0C8", raw)
        self.assertIn(b"Re-rendered, not edited.", raw)
        self.assertIn(b"The box cannot modify pixels in an uploaded image", raw)
        self.assertIn(b"id=\"wf\"", raw)
        self.assertIn(b"id=\"drop\"", raw)
        for banned in (b"#EF7D22", b"#d1564a", b"#6b2f28", b"#F5F3EE"):
            self.assertNotIn(banned, raw)
        self.assertNotIn(b"does not load models", raw)
        self.assertNotIn(b"memory_total_mb", raw)
        self.assertNotIn(b" MB", raw)

    def test_models_come_from_the_device_not_a_table(self):
        status, body = self.call("/api/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body["models"]]
        self.assertIn("Tongyi-MAI/Z-Image-Turbo", ids)
        self.assertNotIn("deepreinforce-ai/Ornith-1.0-35B", ids)
        self.assertNotIn("Qwen/Qwen3.8-27B", ids)
        read_ids = [m["id"] for m in body["read"]]
        self.assertIn("Qwen/Qwen3.8-27B", read_ids)
        self.assertIn("describe", [p["id"] for p in body["presets"]])
        self.assertIn("rerender", [p["id"] for p in body["presets"]])
        self.assertIn("refine", [p["id"] for p in body["presets"]])
        self.assertIn("restyle", [p["id"] for p in body["presets"]])
        turbo = next(m for m in body["models"] if "Z-Image" in m["id"])
        self.assertTrue(turbo["loaded"])
        self.assertTrue(turbo["fits"])
        self.assertEqual(body["npu"]["total"], 100)
        self.assertEqual(body["npu"]["used"], 32)
        self.assertEqual(body["npu"]["free"], 68)
        self.assertNotIn("memory", json.dumps(body["npu"]))
        ids = [r["id"] for r in body["npu"]["resident"]]
        self.assertIn("Tongyi-MAI/Z-Image-Turbo", ids)
        huge = next(m for m in body["models"] if "Big-Plate" in m["id"])
        self.assertFalse(huge["loaded"])
        self.assertFalse(huge["fits"])
        flux = next(m for m in body["models"] if "FLUX" in m["id"])
        self.assertTrue(flux["fits"])

    def test_generate_then_gallery_then_delete(self):
        status, body = self.call("/api/generate", "POST", {
            "model": "Tongyi-MAI/Z-Image-Turbo",
            "prompt": "a bowl of oranges",
            "seed": 9, "steps": 8,
        })
        self.assertEqual(status, 202)
        jid = body["job"]["id"]
        self.assertEqual(body["job"]["seed"], 9)
        deadline = time.time() + 5
        item = None
        while time.time() < deadline:
            _, st = self.call("/api/status?job=" + jid)
            if st["job"]["status"] == "done":
                item = st["job"]["item"]
                break
            if st["job"]["status"] == "failed":
                self.fail(st["job"]["error"])
            time.sleep(0.05)
        self.assertIsNotNone(item)
        status, listing = self.call("/api/gallery")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["id"], item["id"])
        status, blob = self.call("/api/gallery/%s.png" % item["id"])
        self.assertEqual(status, 200)
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
        status, _ = self.call("/api/gallery/" + item["id"], "DELETE")
        self.assertEqual(status, 200)
        _, listing = self.call("/api/gallery")
        self.assertEqual(listing["items"], [])

    def test_a_blank_seed_is_filled_in_and_returned(self):
        spec, err = studio.new_job_spec({
            "model": "Tongyi-MAI/Z-Image-Turbo", "prompt": "x",
        })
        self.assertIsNone(err)
        self.assertIsInstance(spec["seed"], int)
        self.assertGreater(spec["seed"], 0)
        status, body = self.call("/api/generate", "POST", {
            "model": "Tongyi-MAI/Z-Image-Turbo", "prompt": "x",
        })
        self.assertEqual(status, 202)
        self.assertIsInstance(body["job"]["seed"], int)
        self.assertGreater(body["job"]["seed"], 0)
        jid = body["job"]["id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            _, st = self.call("/api/status?job=" + jid)
            if st["job"]["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(st["job"]["status"], "done")


class TestNotLoadedHttp(StudioCase):
    loaded = []

    def setUp(self):
        super().setUp()
        self.app = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.app.daemon_threads = True
        th = threading.Thread(target=self.app.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = "http://127.0.0.1:%d" % self.app.server_address[1]

    def test_generate_against_an_unloaded_model_fails_in_a_sentence(self):
        req = urllib.request.Request(
            self.base + "/api/generate", method="POST",
            data=json.dumps({"model": "Tongyi-MAI/Z-Image-Turbo",
                             "prompt": "nope"}).encode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            job = json.loads(resp.read())["job"]
        deadline = time.time() + 3
        while time.time() < deadline:
            with urllib.request.urlopen(self.base + "/api/status?job=" + job["id"],
                                        timeout=5) as resp:
                st = json.loads(resp.read())["job"]
            if st["status"] != "queued" and st["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(st["status"], "failed")
        self.assertTrue(st["not_loaded"])
        self.assertIn("Load it from the rail", st["error"])


class TestLoadHttp(StudioCase):
    def setUp(self):
        super().setUp()
        self.app = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.app.daemon_threads = True
        th = threading.Thread(target=self.app.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = "http://127.0.0.1:%d" % self.app.server_address[1]

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw, status = resp.read(), resp.status
                ctype = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            raw, status, ctype = exc.read(), exc.code, exc.headers.get("Content-Type")
        if ctype and "json" in ctype:
            return status, json.loads(raw.decode())
        return status, raw

    def wait_loaded(self, model, timeout=4):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body = self.call("/api/models")
            row = next((m for m in body["models"] if m["id"] == model), None)
            load = body.get("loading") or {}
            if row and row["loaded"] and load.get("status") != "loading":
                return body
            time.sleep(0.05)
        self.fail("did not load " + model)

    def test_load_then_unload_moves_the_unit_counts(self):
        flux = "black-forest-labs/FLUX.2-klein-4B"
        status, body = self.call("/api/load", "POST", {"model": flux})
        self.assertEqual(status, 202, body)
        payload = self.wait_loaded(flux)
        self.assertEqual(payload["npu"]["used"], 50)
        self.assertEqual(payload["npu"]["free"], 50)
        resident = {r["id"]: r["npu_usage"] for r in payload["npu"]["resident"]}
        self.assertEqual(resident[flux], 18)
        self.assertEqual(resident["Tongyi-MAI/Z-Image-Turbo"], 32)
        status, body = self.call("/api/unload", "POST", {"model": flux})
        self.assertEqual(status, 200, body)
        _, payload = self.call("/api/models")
        self.assertEqual(payload["npu"]["used"], 32)
        ids = [m["id"] for m in payload["models"] if m["loaded"]]
        self.assertNotIn(flux, ids)

    def test_a_model_that_does_not_fit_is_refused_with_what_is_resident(self):
        huge = "example/Big-Plate-90"
        status, body = self.call("/api/load", "POST", {"model": huge})
        self.assertEqual(status, 400)
        self.assertIn("needs 90 NPU units", body["error"])
        self.assertIn("only 68 of 100 are free", body["error"])
        self.assertIn("Tongyi-MAI/Z-Image-Turbo (32 units)", body["error"])
        self.assertNotIn(huge, self.fake.state.loaded)

    def test_a_vision_model_can_be_loaded_from_the_rail(self):
        status, body = self.call("/api/load", "POST", {
            "model": "deepreinforce-ai/Ornith-1.0-35B"})
        self.assertEqual(status, 202, body)
        deadline = time.time() + 4
        while time.time() < deadline:
            if "deepreinforce-ai/Ornith-1.0-35B" in self.fake.state.loaded:
                break
            time.sleep(0.05)
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", self.fake.state.loaded)

    def test_paint_refuses_a_vision_model(self):
        result = studio.paint(self.tok(), "deepreinforce-ai/Ornith-1.0-35B",
                              "anything", "", 1, 8)
        self.assertFalse(result["ok"])
        self.assertIn("not a Text-to-Image model", result["error"])
        self.assertEqual(self.fake.state.calls, [])

    def test_load_is_refused_while_a_generate_is_running(self):
        gate = threading.Event()
        self.fake.state.hold_generate = gate
        self.fake.state.generate_started.clear()
        status, body = self.call("/api/generate", "POST", {
            "model": "Tongyi-MAI/Z-Image-Turbo", "prompt": "hold",
        })
        self.assertEqual(status, 202, body)
        self.assertTrue(self.fake.state.generate_started.wait(2))
        status, body = self.call("/api/load", "POST", {
            "model": "black-forest-labs/FLUX.2-klein-4B"})
        self.assertEqual(status, 409)
        self.assertIn("a generate is running", body["error"])
        gate.set()

    def test_unloading_something_that_is_not_loaded_is_a_sentence(self):
        status, body = self.call("/api/unload", "POST", {
            "model": "black-forest-labs/FLUX.2-klein-4B"})
        self.assertEqual(status, 400)
        self.assertIn("is not loaded", body["error"])

    def test_status_carries_npu_and_no_megabytes(self):
        status, body = self.call("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["npu"]["total"], 100)
        self.assertIn("resident", body["npu"])
        blob = json.dumps(body)
        self.assertNotIn("memory_total_mb", blob)
        self.assertNotIn("memory_mb", blob)


class TestUpload(StudioCase):
    def test_png_upload_is_marked_and_kept(self):
        item = studio.ingest_upload(PNG, "photo.png")
        self.assertEqual(item["source"], "upload")
        self.assertEqual(item["name"], "photo.png")
        self.assertEqual(item["width"], 1)
        self.assertEqual(item["height"], 1)
        found = studio.read_item(item["id"])
        self.assertTrue(found["png"].read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_longest_edge_is_1536(self):
        import media
        blob = media.split_color_png(1800, 200)
        png, w, h = media.to_fitted_png(blob)
        self.assertEqual(max(w, h), 1536)
        self.assertEqual(w, 1536)
        self.assertTrue(h < 200)
        again = studio.ingest_upload(png, "wide.png")
        self.assertEqual(again["width"], 1536)

    def test_gif_is_refused(self):
        import media
        with self.assertRaises(media.MediaError) as ctx:
            media.to_fitted_png(b"GIF89a" + b"\x00" * 20)
        self.assertEqual(str(ctx.exception), media.ONLY_PNG)

    def test_jpeg_and_webp_are_refused(self):
        import media
        jpeg = b"\xff\xd8\xff" + b"\x00" * 20
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8
        for blob in (jpeg, webp):
            with self.assertRaises(media.MediaError) as ctx:
                media.to_fitted_png(blob)
            self.assertEqual(str(ctx.exception), media.ONLY_PNG)
            self.assertIn("png only", str(ctx.exception))
            self.assertIn("jpeg and webp", str(ctx.exception))


class TestWorkflow(StudioCase):
    loaded = ["Tongyi-MAI/Z-Image-Turbo", "Qwen/Qwen3.8-27B"]

    def setUp(self):
        super().setUp()
        self.app = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.app.daemon_threads = True
        th = threading.Thread(target=self.app.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = "http://127.0.0.1:%d" % self.app.server_address[1]

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw, status = resp.read(), resp.status
                ctype = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            raw, status, ctype = exc.read(), exc.code, exc.headers.get("Content-Type")
        if ctype and "json" in ctype:
            return status, json.loads(raw.decode())
        return status, raw

    def wait_job(self, jid, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, st = self.call("/api/status?job=" + jid)
            job = st.get("job") or {}
            if job.get("status") in ("done", "failed"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish" % jid)

    def test_upload_then_http_round_trip(self):
        import base64
        import media
        blob = media.split_color_png(64, 64)
        status, body = self.call("/api/upload", "POST", {
            "name": "split.png",
            "image": "data:image/png;base64," + base64.b64encode(blob).decode(),
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(body["item"]["source"], "upload")
        iid = body["item"]["id"]
        status, png = self.call("/api/gallery/%s.png" % iid)
        self.assertEqual(status, 200)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_restyle_records_parent_and_honesty(self):
        up = studio.ingest_upload(PNG, "src.png")
        status, body = self.call("/api/workflow", "POST", {
            "preset": "restyle",
            "upload_id": up["id"],
            "read_model": "Qwen/Qwen3.8-27B",
            "render_model": "Tongyi-MAI/Z-Image-Turbo",
            "seed": 11, "steps": 8,
        })
        self.assertEqual(status, 202, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["item"]["parent"], up["id"])
        self.assertIn("Re-rendered, not edited.", job["item"]["honesty"])
        self.assertTrue(self.fake.state.chat_calls)
        content = self.fake.state.chat_calls[0]["messages"][0]["content"]
        self.assertTrue(any(isinstance(p, dict) and p.get("type") == "image_url"
                            for p in content))
        self.assertTrue(self.fake.state.calls)
        gen = self.fake.state.calls[0]
        self.assertEqual(set(gen), set(studio.GENERATE_FIELDS))
        for banned in ("image", "mask", "init_image", "input_image",
                       "image_url", "strength"):
            self.assertNotIn(banned, gen)
        self.assertEqual(self.fake.state.peak, 1)

    def test_describe_returns_text(self):
        up = studio.ingest_upload(PNG, "src.png")
        status, body = self.call("/api/workflow", "POST", {
            "preset": "describe",
            "upload_id": up["id"],
            "read_model": "Qwen/Qwen3.8-27B",
        })
        self.assertEqual(status, 202, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertTrue(job.get("text"))
        self.assertIsNone(job.get("item"))

    def test_generate_body_is_only_measured_fields(self):
        status, body = self.call("/api/generate", "POST", {
            "model": "Tongyi-MAI/Z-Image-Turbo",
            "prompt": "a bowl of oranges",
            "seed": 3, "steps": 8,
            "image": "should-be-ignored-by-us-not-sent",
            "init_image": "nope",
        })
        self.assertEqual(status, 202, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        gen = self.fake.state.calls[-1]
        self.assertEqual(set(gen), set(studio.GENERATE_FIELDS))


class TestSwap(StudioCase):
    loaded = ["Tongyi-MAI/Z-Image-Turbo"]

    def setUp(self):
        super().setUp()
        self.app = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.app.daemon_threads = True
        th = threading.Thread(target=self.app.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = "http://127.0.0.1:%d" % self.app.server_address[1]

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw, status = resp.read(), resp.status
                ctype = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            raw, status, ctype = exc.read(), exc.code, exc.headers.get("Content-Type")
        if ctype and "json" in ctype:
            return status, json.loads(raw.decode())
        return status, raw

    def test_swap_is_said_up_front_then_runs(self):
        up = studio.ingest_upload(PNG, "src.png")
        body = {
            "preset": "restyle",
            "upload_id": up["id"],
            "read_model": "example/Vision-70",
            "render_model": "Tongyi-MAI/Z-Image-Turbo",
            "seed": 4, "steps": 8,
        }
        status, plan = self.call("/api/workflow/plan", "POST", body)
        self.assertEqual(status, 200, plan)
        self.assertTrue(plan["plan"]["swap"])
        self.assertEqual(plan["plan"]["extra_s"], 240)
        status, resp = self.call("/api/workflow", "POST", body)
        self.assertEqual(status, 409, resp)
        self.assertTrue(resp.get("needs_confirm"))
        self.assertIn("extra seconds", resp["error"])
        body["confirm"] = True
        status, resp = self.call("/api/workflow", "POST", body)
        self.assertEqual(status, 202, resp)
        jid = resp["job"]["id"]
        deadline = time.time() + 8
        job = None
        while time.time() < deadline:
            _, st = self.call("/api/status?job=" + jid)
            job = st.get("job") or {}
            if job.get("status") in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["item"]["parent"], up["id"])
        self.assertEqual(self.fake.state.peak, 1)


if __name__ == "__main__":
    unittest.main()
