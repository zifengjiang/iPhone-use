import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from ictrl_backend import IctrlClient
from iphone_use import Runtime, tool_result
from vendor import ictrl_rpc as wire
from wda_client import WDAError

DEVICE = "abcd12345678"


def png(width, height):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\xff" * (width * 3)) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class IctrlBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.key = bytes(range(32))
        self.token = hashlib.sha256(self.key).hexdigest()[:8]
        self.store = self.state / "tokens.json"
        self.store.write_text(json.dumps({f"device:{DEVICE}": {"key": self.key.hex(), "tok": self.token}}))
        self.store.chmod(0o600)
        self.methods = []
        self.app = "com.apple.Preferences"
        self.device_id = DEVICE
        self.locked = False
        self.drop_tap = False
        fixture = self

        class Runner(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == "/rpc"
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert envelope["tok"] == fixture.token
                nonce = bytes.fromhex(envelope["nonce"])
                secret = wire._hmac(fixture.key, wire.LABEL_MAC + nonce)
                data = base64.b64decode(envelope["ct"])
                signed = b"v1" + bytes.fromhex(fixture.token) + envelope["ts"].to_bytes(8, "big") + nonce + data
                assert hmac.compare_digest(wire._hmac(secret, signed).hex(), envelope["tag"])
                command = json.loads(wire._keystream_xor(wire._hmac(fixture.key, wire.LABEL_ENC_REQ + nonce), nonce, data))
                method, params = command["method"], command["params"]
                fixture.methods.append((method, params))
                if method == "tap" and fixture.drop_tap:
                    return  # Action arrived; the connection died before its result.
                if method == "activate_app":
                    fixture.app = params["bundle_id"]
                if method == "home":
                    fixture.app = "com.apple.springboard"
                result = {
                    "device_info": {"device_id": fixture.device_id, "point_width": 100, "point_height": 200},
                    "is_locked": {"locked": fixture.locked},
                    "page_source": {"source": "Application: test page", "bundle_id": fixture.app, "total_returned": 1, "truncated": False},
                    "screenshot": {"data": base64.b64encode(png(200, 400)).decode()},
                }.get(method, {"message": "ok"})
                body = json.dumps({"jsonrpc": "2.0", "id": command["id"], "result": result}).encode()
                ciphertext = wire._keystream_xor(wire._hmac(fixture.key, wire.LABEL_ENC_RESP + nonce), nonce, body)
                reply = json.dumps({"ct": base64.b64encode(ciphertext).decode(),
                                    "tag": wire._hmac(secret, b"resp" + nonce + ciphertext).hex()}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Runner)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        scan = patch.object(wire, "nearby_scan", return_value=[{"id": DEVICE, "name": "Test iPhone", "channel": "lan",
                                                                 "host": f"127.0.0.1:{server.server_port}"}])
        scan.start()
        self.addCleanup(scan.stop)
        with patch.dict(os.environ, {"IPHONE_USE_BACKEND": "ictrl", "IPHONE_USE_ICTRL_DEVICE_ID": DEVICE}):
            self.runtime = Runtime(self.state / "runtime")
        self.runtime.client.token_store = self.store
        self.addCleanup(self.runtime.close)

    def test_ready_and_phone_controls_use_authenticated_rpc(self):
        found = self.runtime.call("pua_setup", {"action": "discover"})
        self.assertEqual(found["devices"][0]["device_id"], DEVICE)
        result = tool_result(self.runtime, {"name": "pua_ready", "arguments": {"screenshot": True}})
        self.assertFalse(result["isError"])
        self.assertNotIn("_meta", result)
        self.assertEqual(result["content"][1]["type"], "image")
        ready = json.loads(result["content"][0]["text"])
        self.assertEqual((ready["ready"], ready["backend"], ready["device_id"], ready["transport"]), (True, "ictrl", DEVICE, "lan"))
        self.assertEqual(ready["observation"]["image"]["pixel_to_point"], [0.5, 0.5])
        tap = self.runtime.call("pua_tap", {"x": 12, "y": 24, "observation_id": ready["observation"]["observation_id"]})
        self.assertTrue(tap["action_complete"])
        self.runtime.call("pua_swipe", {"direction": "up"})
        self.assertIn(("tap", {"x": 12, "y": 24}), self.methods)
        self.assertIn("drag", [method for method, _ in self.methods])
        home = self.runtime.call("pua_press_button", {"name": "home", "verify": True})
        self.assertEqual(home["foreground_app"], "com.apple.springboard")
        settings = self.runtime.call("pua_launch_app", {"bundle_id": "com.apple.Preferences", "observe": "tree", "verify": True})
        self.assertEqual(settings["observation"]["app"], "com.apple.Preferences")
        self.assertIn(("activate_app", {"bundle_id": "com.apple.Preferences"}), self.methods)

    def test_failed_action_is_never_replayed(self):
        self.drop_tap = True
        with self.assertRaises(WDAError) as result:
            self.runtime.call("pua_tap", {"x": 12, "y": 24})
        self.assertEqual(result.exception.code, "action_uncertain")
        self.assertTrue(result.exception.uncertain)
        self.assertEqual(sum(method == "tap" for method, _ in self.methods), 1)

    def test_identity_and_lock_are_checked_before_actions(self):
        self.device_id = "another-phone"
        with self.assertRaises(WDAError) as result:
            self.runtime.call("pua_press_button", {"name": "home"})
        self.assertEqual(result.exception.code, "ictrl_identity_mismatch")
        self.device_id = DEVICE
        self.locked = True
        with self.assertRaises(WDAError) as result:
            self.runtime.call("pua_press_button", {"name": "home"})
        self.assertEqual(result.exception.code, "phone_locked")
        self.assertNotIn("home", [method for method, _ in self.methods])

    def test_unpaired_and_unsupported_batches_do_not_send_actions(self):
        self.store.chmod(0o644)
        with self.assertRaises(WDAError) as result:
            self.runtime.call("pua_ready", {})
        self.assertEqual(result.exception.code, "ictrl_pairing_required")
        with self.assertRaises(WDAError) as result:
            self.runtime.call("pua_batch", {"steps": [{"op": "press_button", "args": {"name": "home"}},
                                                          {"op": "tap", "args": {"selector": {"label": "About"}}}]})
        self.assertEqual(result.exception.code, "ictrl_unsupported")
        self.assertEqual(self.methods, [])

    def test_opt_in_stdio_catalog_omits_wda_only_tools_and_preview(self):
        requests = [{"jsonrpc": "2.0", "id": i, "method": method} for i, method in
                    enumerate(("initialize", "tools/list", "resources/list"))]
        result = subprocess.run([sys.executable, str(ROOT / "server/iphone_use.py"), "--state-dir", str(self.state / "stdio")],
                                input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True, check=True,
                                env={**os.environ, "IPHONE_USE_BACKEND": "ictrl"}, timeout=8)
        hello, tools, resources = [json.loads(line)["result"] for line in result.stdout.splitlines()]
        self.assertEqual(hello["serverInfo"]["name"], "iphone-use-ictrl")
        names = {tool["name"] for tool in tools["tools"]}
        self.assertIn("pua_tap", names)
        self.assertNotIn("pua_type_text", names)
        self.assertNotIn("pua_screen", names)
        self.assertEqual(resources["resources"], [])


if __name__ == "__main__":
    unittest.main()
