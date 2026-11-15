import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
from controller import DeviceController
from errors import PUAError
from fake_device import FakeAxe, FakeCore, node, tree
from iphone_use import Runtime, SCREEN_URI, result_content, serve


def payload(result):
    """The model-facing data: one JSON text block, never a second structured copy."""
    assert "structuredContent" not in result, "structuredContent would hide content blocks from the model"
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


def exchange(requests, state_dir):
    lines = "".join((json.dumps(item, ensure_ascii=False, allow_nan=False) if not isinstance(item, str) else item) + "\n"
                    for item in requests)
    process = subprocess.run([sys.executable, str(ROOT / "server" / "iphone_use.py"), "--state-dir", state_dir],
                             input=lines, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert process.returncode == 0, process.stderr
    assert process.stderr == "", process.stderr
    return [json.loads(line) for line in process.stdout.splitlines()]


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_handshake_catalog_and_annotations(self):
        responses = exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ], self.directory.name)
        self.assertEqual([response["id"] for response in responses], [1, "ping", 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"], {"name": "iphone-use", "version": "0.3.6"})
        self.assertIn("pua_doctor", responses[0]["result"]["instructions"])
        self.assertEqual(responses[1]["result"], {})
        tools = responses[2]["result"]["tools"]
        self.assertEqual(len(tools), 18)
        self.assertEqual(len({tool["name"] for tool in tools}), 18)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        apps = next(tool for tool in tools if tool["name"] == "pua_apps")
        self.assertEqual(apps["annotations"], {"readOnlyHint": True, "destructiveHint": False,
                                               "idempotentHint": True, "openWorldHint": False})
        self.assertEqual(apps["inputSchema"]["required"], ["query"])
        preview = {tool["name"]: tool for tool in tools if tool["name"] in ("pua_screen_frame", "pua_screen_action")}
        self.assertEqual(set(preview), {"pua_screen_frame", "pua_screen_action"})
        for tool in preview.values():
            self.assertEqual(tool["_meta"]["ui"]["visibility"], ["app"])

    def test_the_screen_resource_is_the_html_panel(self):
        responses = exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": SCREEN_URI}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://iphone-use/other.html"}},
        ], self.directory.name)
        resources = responses[0]["result"]["resources"]
        self.assertEqual([item["uri"] for item in resources], [SCREEN_URI])
        self.assertEqual(resources[0]["mimeType"], "text/html;profile=mcp-app")
        contents = responses[1]["result"]["contents"]
        self.assertEqual(contents[0]["uri"], SCREEN_URI)
        self.assertGreater(len(contents[0]["text"]), 5000)
        self.assertEqual(responses[2]["error"]["code"], -32602)

    def test_protocol_faults_are_jsonrpc_errors_and_never_kill_the_server(self):
        responses = exchange([
            "{not json",
            {"jsonrpc": "2.0", "id": 1, "method": "no/such/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "pua_nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "pua_tap", "arguments": {"selector": {"label": "x"}, "y": 3}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ], self.directory.name)
        by_id = {response["id"]: response for response in responses}
        self.assertEqual(set(by_id), {None, 1, 2, 3, 4})
        self.assertEqual(by_id[None]["error"]["code"], -32700)
        self.assertEqual(by_id[1]["error"]["code"], -32601)
        self.assertTrue(by_id[2]["result"]["isError"])
        self.assertEqual(payload(by_id[2]["result"])["error"]["code"], "unknown_tool")
        self.assertTrue(by_id[3]["result"]["isError"])
        tap_error = payload(by_id[3]["result"])["error"]
        self.assertEqual(tap_error["code"], "invalid_argument")
        self.assertIn("x and y go together", tap_error["message"])
        self.assertEqual(by_id[4]["result"], {})

    def test_a_tool_call_without_a_phone_is_a_normal_mcp_error(self):
        response = exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "pua_observe", "arguments": {}}}], self.directory.name)[0]
        self.assertTrue(response["result"]["isError"])
        error = payload(response["result"])["error"]
        self.assertIn(error["code"], ("device_unavailable", "coredevice_unavailable", "axe_unavailable"))
        self.assertEqual(error["uncertain"], False)


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self, nodes=None, **core):
        runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        runtime.phone.close()
        runtime.core = FakeCore(**core)
        runtime.axe = FakeAxe(nodes if nodes is not None else tree(
            node("返回", frame=(0, 0, 44, 44)),
            node("搜索", kind="TextField", role="AXTextField", frame=(20, 100, 300, 44))))
        runtime.phone = DeviceController(runtime.core, runtime.axe, Path(self.directory.name))
        runtime.setup_manager.core = runtime.core
        runtime.setup_manager.save(udid=runtime.core.udid)
        return runtime

    def call(self, runtime, name, arguments):
        request = {"jsonrpc": "2.0", "id": name, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(runtime)
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], name)
        self.assertNotIn("error", response)
        return response["result"]

    def test_ready_proves_both_channels_and_returns_the_first_screenshot(self):
        runtime = self.runtime()
        data = payload(self.call(runtime, "pua_ready", {}))
        self.assertTrue(data["ready"])
        self.assertEqual(data["state"], "ready")
        self.assertTrue(data["proof"]["coredevice"])
        self.assertTrue(data["proof"]["axe"])
        self.assertGreater(data["observation"]["total_nodes"], 0)
        self.assertEqual(runtime.core.ops().count("info"), 1)
        self.assertEqual(data["observation"]["image"]["mimeType"], "image/jpeg")

    def test_ready_reports_a_locked_phone_instead_of_failing(self):
        runtime = self.runtime()
        runtime.axe.locked = True
        result = self.call(runtime, "pua_ready", {"screenshot": False})
        data = payload(result)
        self.assertFalse(result["isError"])
        self.assertFalse(data["ready"])
        self.assertEqual(data["state"], "locked")
        self.assertIn("unlock", data["next_tool"])

    def test_observe_tap_and_type_reach_the_right_channel(self):
        runtime = self.runtime()
        observed = payload(self.call(runtime, "pua_observe", {"mode": "both"}))
        self.assertEqual(observed["app"], "SpringBoard")
        self.assertEqual(observed["viewport"]["width"], 402)
        self.assertEqual(observed["viewport"]["units"], "iPhone points")
        tap = payload(self.call(runtime, "pua_tap", {"selector": {"label": "返回"}}))
        self.assertTrue(tap["action_executed"])
        self.assertEqual([item["label"] for item in runtime.axe.taps], ["返回"])
        typed = payload(self.call(runtime, "pua_type_text", {"text": "hello"}))
        self.assertTrue(typed["typed"])
        self.assertIn("type", runtime.core.ops())

    def test_a_preview_poll_answers_with_structured_content_only(self):
        runtime = self.runtime()
        result = self.call(runtime, "pua_screen_frame", {"after_seq": 0})
        self.assertEqual(result["content"], [])
        self.assertIn("frame_available", result["structuredContent"])
        self.assertIn("structuredContent", self.call(runtime, "pua_screen", {"action": "open"}))

    def test_metrics_count_tools_and_actions_separately(self):
        runtime = self.runtime()
        self.call(runtime, "pua_tap", {"x": 10, "y": 20})
        self.call(runtime, "pua_observe", {"mode": "tree"})
        data = payload(self.call(runtime, "pua_metrics", {}))
        self.assertEqual(data["tools"]["count"], 2)
        self.assertEqual(data["device"]["accepted_actions"], 1)
        self.assertEqual(data["device"]["udid"], "TEST-DEVICE-1234")
        self.assertGreaterEqual(data["responses"]["count"], 1)

    def test_setup_status_and_doctor_never_need_a_phone_but_say_what_is_missing(self):
        runtime = self.runtime()
        status = payload(self.call(runtime, "pua_setup", {"action": "status"}))
        self.assertEqual(status["udid"], "TEST-DEVICE-1234")
        self.assertTrue(status["coredevice"]["available"])
        self.assertTrue(status["axe"]["available"])
        doctor = payload(self.call(runtime, "pua_doctor", {}))
        self.assertTrue(doctor["ok"])
        self.assertEqual(doctor["udid"], "TEST-DEVICE-1234")

    def test_a_channel_fault_is_marked_uncertain(self):
        runtime = self.runtime()
        runtime.core.failure = PUAError("coredevice_timeout", "The helper did not answer.")
        result = self.call(runtime, "pua_observe", {"mode": "screenshot"})
        error = payload(result)["error"]
        self.assertEqual(error["code"], "coredevice_timeout")
        self.assertTrue(error["uncertain"])


class ResultContentTests(unittest.TestCase):
    def test_text_is_compact_json(self):
        result = result_content({"alpha": 1, "beta": "two"})
        self.assertEqual(result["content"], [{"type": "text", "text": '{"alpha":1,"beta":"two"}'}])
        self.assertFalse(result["isError"])

    def test_images_follow_the_text_block(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        result = result_content({"ok": True, "image": {"path": str(path), "mimeType": "image/png"}})
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), path.read_bytes())

    def test_a_clean_report_is_not_an_error(self):
        # setup discover always carries the key, with None when nothing failed.
        result = result_content({"ok": True, "devices": [], "error": None})
        self.assertFalse(result["isError"])

    def test_an_error_is_a_result_not_a_transport_fault(self):
        result = result_content({"error": {"code": "phone_locked", "message": "Locked.", "uncertain": False}})
        self.assertTrue(result["isError"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_cli(self, *flags):
        return subprocess.run([sys.executable, str(ROOT / "server" / "iphone_use.py"),
                               "--state-dir", self.directory.name, *flags],
                              capture_output=True, text=True, encoding="utf-8", timeout=60,
                              env={**os.environ, "IPHONE_USE_COREDEVICE_PYTHON": "/nonexistent/python3"})

    def test_doctor_without_a_helper_exits_nonzero_with_a_reason(self):
        process = self.run_cli("--doctor")
        self.assertEqual(process.returncode, 1)
        data = json.loads(process.stdout)
        self.assertFalse(data["ok"])
        self.assertTrue(data["next_steps"])
        self.assertFalse(data["coredevice"]["available"])
        self.assertIn("IPHONE_USE_COREDEVICE_PYTHON", " ".join(data["next_steps"]))

    def test_ready_without_a_helper_exits_nonzero(self):
        process = self.run_cli("--ready")
        self.assertEqual(process.returncode, 1)
        self.assertIn("error", json.loads(process.stdout))


if __name__ == "__main__":
    unittest.main()
