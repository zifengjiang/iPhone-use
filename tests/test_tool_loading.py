import copy
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from errors import PUAError
from iphone_use import (BATCH_OPS, DESCRIPTIONS, INSTRUCTIONS, SCHEMAS, SCREEN_URI, TOOLS,
                        validate, validate_semantics)

MUTATIONS = {"tap", "swipe", "type_text", "press_button", "launch_app", "batch", "setup",
             "unlock", "screen_action"}


def schema_of(name):
    return next(tool for tool in TOOLS if tool["name"] == "pua_" + name)


class CatalogTests(unittest.TestCase):
    def test_every_schema_is_exactly_one_tool(self):
        self.assertEqual(len(TOOLS), len(SCHEMAS))
        self.assertEqual({tool["name"] for tool in TOOLS}, {"pua_" + name for name in SCHEMAS})
        self.assertEqual(len(TOOLS), 18)

    def test_the_final_tool_list_is_the_whole_product(self):
        self.assertEqual([tool["name"] for tool in TOOLS],
                         ["pua_observe", "pua_find", "pua_tap", "pua_swipe", "pua_type_text",
                          "pua_press_button", "pua_launch_app", "pua_wait", "pua_batch", "pua_apps",
                          "pua_doctor", "pua_ready", "pua_metrics", "pua_setup", "pua_unlock",
                          "pua_screen", "pua_screen_frame", "pua_screen_action"])

    def test_titles_descriptions_and_annotations_are_complete(self):
        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["title"].startswith("Pua ") or tool["name"] == "pua_screen")
                self.assertGreater(len(tool["description"]), 60)
                self.assertEqual(set(tool["annotations"]),
                                 {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
                mutation = tool["name"].removeprefix("pua_") in MUTATIONS
                self.assertEqual(tool["annotations"]["readOnlyHint"], not mutation)
                self.assertEqual(tool["annotations"]["destructiveHint"], mutation)
                self.assertFalse(tool["annotations"]["openWorldHint"])

    def test_preview_tools_are_hidden_from_the_model_and_the_panel_is_not(self):
        for name in ("screen_frame", "screen_action"):
            self.assertEqual(schema_of(name)["_meta"]["ui"]["visibility"], ["app"])
        ready = schema_of("ready")
        self.assertEqual(ready["_meta"]["ui"]["resourceUri"], SCREEN_URI)
        self.assertEqual(schema_of("screen")["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])

    def test_no_description_promises_a_backend_that_no_longer_exists(self):
        blob = json.dumps(TOOLS, ensure_ascii=False) + INSTRUCTIONS + json.dumps(DESCRIPTIONS)
        for word in ("WDA", "WebDriverAgent", "iCTRL", "ictrl", "appium", "iproxy", "Appium"):
            self.assertNotIn(word, blob, word)

    def test_the_instructions_teach_the_real_loop(self):
        self.assertIn("pua_doctor", INSTRUCTIONS)
        self.assertIn("pua_observe", INSTRUCTIONS)
        self.assertIn("observation_id", INSTRUCTIONS)
        self.assertIn("Any script", INSTRUCTIONS)
        self.assertIn("pua_unlock", INSTRUCTIONS)

    def test_descriptions_state_the_constraints_the_code_enforces(self):
        self.assertIn("Any script", DESCRIPTIONS["type_text"])
        self.assertIn("500", DESCRIPTIONS["type_text"])
        self.assertIn("selector", DESCRIPTIONS["tap"])
        self.assertIn("locked", DESCRIPTIONS["unlock"])
        self.assertIn("must not be repeated", DESCRIPTIONS["batch"])
        self.assertIn("Discover", DESCRIPTIONS["setup"])


class SchemaShapeTests(unittest.TestCase):
    def walk(self, schema, path="arguments"):
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "object" and "properties" in schema:
            self.assertFalse(schema.get("additionalProperties", True), f"{path} accepts unknown fields")
        for key, value in schema.items():
            if key in ("properties", "$defs"):
                for name, child in value.items():
                    self.walk(child, f"{path}.{name}")
            elif key in ("items",):
                self.walk(value, path + "[]")
            elif key in ("oneOf", "anyOf", "allOf"):
                for index, child in enumerate(value):
                    self.walk(child, f"{path}<{index}>")

    def test_every_published_object_is_closed(self):
        for tool in TOOLS:
            self.walk(tool["inputSchema"], tool["name"])

    def test_repeated_selector_and_observe_definitions_are_shared(self):
        tap = schema_of("tap")["inputSchema"]
        self.assertIn("selector", tap["$defs"])
        self.assertIn("observe", tap["$defs"])
        self.assertNotIn("document", json.dumps(tap))
        self.assertEqual(tap["properties"]["selector"], {"$ref": "#/$defs/selector"})

    def test_the_selector_has_no_backend_specific_fields(self):
        selector = schema_of("tap")["inputSchema"]["$defs"]["selector"]["properties"]
        self.assertEqual(set(selector), {"label", "label_contains", "name", "value", "type", "enabled", "index"})
        self.assertEqual(selector["index"]["minimum"], 0)

    def test_press_button_cannot_lock_the_phone(self):
        self.assertEqual(SCHEMAS["press_button"]["properties"]["name"]["enum"],
                         ["home", "volumeup", "volumedown"])

    def test_type_text_has_no_field_that_needs_a_webdriver(self):
        self.assertEqual(set(SCHEMAS["type_text"]["properties"]), {"text", "allow_newlines", "observe"})

    def test_batch_steps_are_the_supported_operations(self):
        steps = SCHEMAS["batch"]["properties"]["steps"]
        self.assertEqual([step["properties"]["op"]["const"] for step in steps["items"]["oneOf"]],
                         list(BATCH_OPS))
        self.assertTrue(set(BATCH_OPS) <= set(SCHEMAS))
        self.assertNotIn("scroll_find", SCHEMAS)


class ValidationTests(unittest.TestCase):
    def rejected(self, name, arguments):
        with self.assertRaises(PUAError) as caught:
            validate(arguments, SCHEMAS[name])
            validate_semantics(name, arguments)
        return caught.exception

    def accepted(self, name, arguments):
        validate(arguments, SCHEMAS[name])
        validate_semantics(name, arguments)

    def test_unknown_fields_are_named_with_the_allowed_ones(self):
        error = self.rejected("tap", {"selector": {"label": "x"}, "password": "hunter2"})
        self.assertEqual(error.code, "invalid_argument")
        self.assertEqual(error.details["unknown_fields"], ["password"])
        self.assertIn("selector", error.details["allowed_fields"])
        self.assertFalse(error.details["action_executed"])

    def test_a_selector_cannot_be_empty(self):
        self.assertEqual(self.rejected("find", {"selector": {}}).code, "invalid_argument")

    def test_tap_needs_exactly_one_target(self):
        self.assertEqual(self.rejected("tap", {}).code, "invalid_argument")
        self.assertEqual(self.rejected("tap", {"selector": {"label": "x"}, "x": 1, "y": 2}).code,
                         "invalid_argument")
        self.assertEqual(self.rejected("tap", {"x": 1}).code, "invalid_argument")
        self.accepted("tap", {"selector": {"label": "返回"}})
        self.accepted("tap", {"x": 12, "y": 34})

    def test_newlines_need_an_explicit_intent(self):
        self.assertEqual(self.rejected("type_text", {"text": "a\nb"}).code, "invalid_argument")
        self.accepted("type_text", {"text": "a\nb", "allow_newlines": True})

    def test_numbers_are_bounded(self):
        self.assertEqual(self.rejected("observe", {"max_nodes": 0}).code, "invalid_argument")
        self.assertEqual(self.rejected("observe", {"max_nodes": 501}).code, "invalid_argument")
        self.assertEqual(self.rejected("wait", {"selector": {"label": "x"}, "timeout_seconds": -1}).code,
                         "invalid_argument")

    def test_ready_udid_is_a_coredevice_identifier(self):
        self.accepted("ready", {})
        self.accepted("ready", {"device": "00008150-001A55461AE1401C"})
        self.assertEqual(self.rejected("ready", {"device": "my phone"}).code, "invalid_argument")

    def test_setup_needs_a_udid_to_configure(self):
        self.assertEqual(self.rejected("setup", {"action": "configure"}).code, "invalid_argument")
        self.accepted("setup", {"action": "status"})
        self.accepted("setup", {"action": "configure", "udid": "00008150-001A55461AE1401C"})

    def test_batch_validates_every_step(self):
        self.accepted("batch", {"steps": [{"op": "tap", "args": {"x": 1, "y": 2}},
                                          {"op": "type_text", "args": {"text": "ok"}}]})
        error = self.rejected("batch", {"steps": [{"op": "tap", "args": {}}]})
        self.assertEqual(error.code, "invalid_argument")
        self.assertEqual(self.rejected("batch", {"steps": []}).code, "invalid_argument")
        self.assertEqual(self.rejected("batch", {"steps": [{"op": "scroll_find", "args": {}}]}).code,
                         "invalid_argument")

    def test_observations_are_optional_but_typed(self):
        self.accepted("tap", {"x": 1, "y": 2, "observation_id": "abc123"})
        self.assertEqual(self.rejected("tap", {"x": 1, "y": 2, "observation_id": 5}).code,
                         "invalid_argument")


class InstructionTests(unittest.TestCase):
    def test_the_system_prompt_is_one_behavioural_brief(self):
        self.assertLess(len(INSTRUCTIONS), 4000)
        self.assertNotIn("http://", INSTRUCTIONS)
        self.assertIn("expect", INSTRUCTIONS)

    def test_no_tool_advertises_a_removed_feature(self):
        blob = INSTRUCTIONS + json.dumps(DESCRIPTIONS)
        for gone in ("scroll_find", "collect_list", "continue_token", "max_attempts", "predicate"):
            self.assertNotIn(gone, blob, gone)


if __name__ == "__main__":
    unittest.main()
