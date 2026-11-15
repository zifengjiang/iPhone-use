import base64
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from hybrid_backend import HybridController
from wda_client import WDAError


def png(width, height):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\xff" * (width * 3)) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


RUNNER_FRAME = png(40, 80)
DEVICE_FRAME = png(60, 120)


class StubClient:
    """iCTRL client surface: everything the controller touches, and nothing else."""

    def __init__(self, locked=True):
        self.locked = locked
        self.calls = []
        self.taps = []
        self.failures = {}
        self.uncertain = False

    def _maybe_fail(self, key):
        code = self.failures.get(key)
        if code:
            raise WDAError(code, "runner down", uncertain=self.uncertain)

    def rpc(self, method, params=None, timeout=None, *, action=False):
        self.calls.append(method)
        self._maybe_fail(method)
        if method == "is_locked":
            return {"locked": self.locked}
        return {}

    def session(self, method, path, payload=None, timeout=None):
        self.calls.append(path)
        self._maybe_fail(path)
        if path == "/window/size":
            return {"width": 402, "height": 874}
        if path == "/wda/tap":
            self.taps.append(payload)
            return {}
        return {}

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append(path)
        self._maybe_fail(path)
        if path == "/screenshot":
            return {"value": base64.b64encode(RUNNER_FRAME).decode()}
        return {}

    def source(self, limit, timeout=None):
        return {"bundle_id": "com.example.app", "source": "text", "total_returned": 1}

    def close(self):
        pass


class CoreStub:
    def __init__(self, available=True, fails=None):
        self.is_available = available
        self.fails = fails or set()
        self.calls = []
        self.client = None

    def available(self):
        return self.is_available

    def call(self, op, **args):
        self.calls.append((op, args))
        if op in self.fails:
            raise WDAError(self.fails[op], "coredevice failed", uncertain=getattr(self, "uncertain", False))
        if op == "screenshot":
            return {"png_b64": base64.b64encode(DEVICE_FRAME).decode(), "width": 60, "height": 120}
        if op == "unlock":
            if self.client is not None:
                self.client.locked = False
            return {"duration_ms": 5200, "digits": len(args.get("passcode", ""))}
        return {"duration_ms": 12}


class HybridBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.client = StubClient()
        self.core = CoreStub()
        self.core.client = self.client
        patcher = patch.dict(os.environ, {"IPHONE_USE_UNLOCK_PASSCODE": "1234"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.phone = HybridController(self.client, self.core, self.state)

    def test_capture_prefers_coredevice_frame(self):
        image = self.phone.capture({"width": 402, "height": 874})
        self.assertTrue(Path(image["path"]).is_file())
        self.assertEqual([op for op, _ in self.core.calls], ["screenshot"])
        self.assertNotIn("/screenshot", self.client.calls)

    def test_capture_falls_back_to_runner_when_coredevice_fails(self):
        self.core.fails = {"screenshot": "coredevice_failed"}
        image = self.phone.capture({"width": 402, "height": 874})
        self.assertTrue(Path(image["path"]).is_file())
        self.assertIn("/screenshot", self.client.calls)

    def test_unlock_reports_the_resulting_lock_state(self):
        result = self.phone.run("unlock", {})
        self.assertEqual(result["locked"], False)
        self.assertEqual(self.core.calls, [("unlock", {"passcode": "1234", "interval": 0.25})])

    def test_ensure_unlocked_raises_without_a_passcode(self):
        with patch.dict(os.environ, {"IPHONE_USE_UNLOCK_PASSCODE": ""}):
            self.phone.passcode = ""
            with self.assertRaises(WDAError) as caught:
                self.phone.ensure_unlocked()
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(self.core.calls, [])

    def test_ensure_unlocked_returns_early_when_already_unlocked(self):
        self.client.locked = False
        self.phone.ensure_unlocked()
        self.assertEqual(self.core.calls, [])

    def test_tap_prefers_coredevice_over_the_slower_runner(self):
        result = self.phone.run("tap", {"x": 100, "y": 400})
        self.assertEqual(result["backend"], "coredevice")
        self.assertEqual(self.client.taps, [])
        x, y = self.core.calls[-1][1]["x"], self.core.calls[-1][1]["y"]
        self.assertAlmostEqual(x, 100 / 402, places=4)
        self.assertAlmostEqual(y, 400 / 874, places=4)

    def test_tap_falls_back_to_the_runner_when_coredevice_fails(self):
        self.core.fails = {"tap": "coredevice_failed"}
        result = self.phone.run("tap", {"x": 100, "y": 400})
        self.assertEqual(result["action_executed"], True)
        self.assertEqual(self.client.taps, [{"x": 100, "y": 400}])

    def test_tap_is_never_replayed_after_an_uncertain_coredevice_failure(self):
        self.core.fails = {"tap": "coredevice_timeout"}
        self.core.uncertain = True
        with self.assertRaises(WDAError):
            self.phone.run("tap", {"x": 100, "y": 400})
        self.assertEqual(self.client.taps, [])

    def test_tap_uses_the_runner_when_coredevice_is_unavailable(self):
        self.core.is_available = False
        self.client.locked = False
        self.phone.run("tap", {"x": 100, "y": 400})
        self.assertEqual(self.client.taps, [{"x": 100, "y": 400}])

    def test_press_button_uses_hardware_buttons_in_hybrid(self):
        result = self.phone.run("press_button", {"name": "volumedown"})
        self.assertEqual(result["backend"], "coredevice")
        self.assertEqual(self.core.calls[-1], ("button", {"name": "volumedown"}))

    def test_type_text_goes_to_the_coredevice_keyboard(self):
        result = self.phone.run("type_text", {"text": "hello"})
        self.assertEqual(result["typed"], 5)
        self.assertEqual(self.core.calls[-1], ("type", {"text": "hello"}))

    def test_type_text_rejects_non_ascii(self):
        with self.assertRaises(WDAError) as caught:
            self.phone.run("type_text", {"text": "你好"})
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_point_size_is_cached_across_taps(self):
        self.phone.run("tap", {"x": 10, "y": 10})
        self.phone.run("tap", {"x": 20, "y": 20})
        self.assertEqual(self.client.calls.count("/window/size"), 1)

    def test_launch_app_falls_back_to_coredevice(self):
        self.client.failures = {"/wda/apps/activate": "ictrl_unreachable"}
        result = self.phone.run("launch_app", {"bundle_id": "com.example.app"})
        self.assertEqual(result["backend"], "coredevice")
        self.assertEqual(self.core.calls[-1], ("launch", {"bundle_id": "com.example.app"}))

    def test_unavailable_coredevice_keeps_every_call_on_ictrl(self):
        self.core.is_available = False
        self.client.locked = False
        self.phone.run("tap", {"x": 10, "y": 10})
        self.assertEqual(self.client.taps, [{"x": 10, "y": 10}])
        self.assertEqual(self.core.calls, [])


if __name__ == "__main__":
    unittest.main()

    def test_set_device_rebinds_coredevice_and_drops_cached_size(self):
        self.phone.run("tap", {"x": 10, "y": 10})
        self.assertEqual(self.phone.set_device("another-udid"), "another-udid")
        self.assertEqual(self.core.udid, "another-udid")
        self.assertIsNone(self.phone._point_size)
