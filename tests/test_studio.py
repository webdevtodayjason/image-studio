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

    def _restore(self, saved):
        device.HOST, device.PORT_OVERRIDE = saved[0], saved[3]
        device.SERVICES.clear()
        device.SERVICES.update(saved[1])
        device.TRANSPORT.clear()
        device.TRANSPORT.update(saved[2])
        device.SOURCE, device.PLANE = saved[4], saved[5]

    def tok(self):
        return "test-key"


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
        self.assertIn("does not load models", result["error"])
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

    def test_models_come_from_the_device_not_a_table(self):
        status, body = self.call("/api/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body["models"]]
        self.assertIn("Tongyi-MAI/Z-Image-Turbo", ids)
        self.assertNotIn("deepreinforce-ai/Ornith-1.0-35B", ids)
        turbo = next(m for m in body["models"] if "Z-Image" in m["id"])
        self.assertTrue(turbo["loaded"])

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
        self.assertIn("does not load models", st["error"])


if __name__ == "__main__":
    unittest.main()
