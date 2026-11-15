import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import coredevice_client
from coredevice_client import CoreDeviceClient
from wda_client import WDAError

STUB = textwrap.dedent(
    """
    import json, sys, time
    for line in sys.stdin:
        request = json.loads(line)
        op = request["op"]
        if op == "quit":
            print(json.dumps({"id": request["id"], "ok": True, "result": {}}), flush=True)
            break
        if op == "slow":
            time.sleep(5)
        if op == "boom":
            print(json.dumps({"id": request["id"], "ok": False,
                              "error": {"code": "ValueError", "message": "device said no"}}), flush=True)
            continue
        print(json.dumps({"id": request["id"], "ok": True, "result": {"op": op, "args": request["args"]}}), flush=True)
    """
)


class CoreDeviceClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.helper = self.state / "stub_helper.py"
        self.helper.write_text(STUB)
        patcher = patch.object(coredevice_client, "HELPER", self.helper)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = CoreDeviceClient(state_dir=self.state, python=sys.executable, timeout=2)

    def test_round_trip_keeps_one_process(self):
        first = self.client.call("tap", x=0.5, y=0.5)
        second = self.client.call("info")
        self.assertEqual(first, {"op": "tap", "args": {"x": 0.5, "y": 0.5}})
        self.assertEqual(second["op"], "info")
        self.assertIsNotNone(self.client.process)

    def test_helper_error_becomes_a_wda_error(self):
        with self.assertRaises(WDAError) as caught:
            self.client.call("boom")
        self.assertEqual(caught.exception.code, "coredevice_failed")
        self.assertIn("device said no", str(caught.exception))

    def test_timeout_is_reported_as_uncertain_for_actions(self):
        with self.assertRaises(WDAError) as caught:
            self.client.call("slow")
        self.assertEqual(caught.exception.code, "coredevice_timeout")
        self.assertTrue(caught.exception.uncertain)

    def test_missing_interpreter_degrades_instead_of_crashing(self):
        broken = CoreDeviceClient(state_dir=self.state, python=str(self.state / "nope"), timeout=1)
        self.assertFalse(broken.available())
        with self.assertRaises(WDAError) as caught:
            broken.call("info")
        self.assertEqual(caught.exception.code, "coredevice_unavailable")

    def test_close_stops_the_helper(self):
        self.client.call("info")
        process = self.client.process
        self.client.close()
        self.assertIsNone(self.client.process)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
