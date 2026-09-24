"""account_auth_key(): the device's own account API, no TiinyOS needed.

Reverse-engineered by capturing a real TiinyOS login -- full writeup at
~/code/tiiny/tools/README-unlock.md. This only checks that device.py parses
the response the device actually sends; the discovery itself lives there.
"""
import json
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import device  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    reply = {"status": "ok", "data_state": "unlocked", "auth_key": "the-real-key"}
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps(self.reply).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestAccountAuthKey(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.addr = "127.0.0.1:%d" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def test_returns_the_auth_key_field(self):
        got = device.account_auth_key(self.addr, "TNYM000", "correct horse")
        self.assertEqual(got, "the-real-key")

    def test_wrong_password_is_empty_string_not_a_crash(self):
        _Handler.reply = {"error": "main_password_wrong"}
        _Handler.status = 403
        try:
            got = device.account_auth_key(self.addr, "TNYM000", "wrong")
        finally:
            _Handler.reply = {"status": "ok", "data_state": "unlocked", "auth_key": "the-real-key"}
            _Handler.status = 200
        self.assertEqual(got, "")

    def test_unreachable_host_is_empty_string(self):
        got = device.account_auth_key("127.0.0.1:1", "TNYM000", "x")
        self.assertEqual(got, "")


if __name__ == "__main__":
    unittest.main()
