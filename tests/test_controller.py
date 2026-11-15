import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from controller import DeviceController
from errors import PUAError
from fake_device import FakeAxe, FakeCore, node, tree


class ControllerTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.core = FakeCore()
        self.axe = FakeAxe(tree(node("返回"), node("搜索", name="search", frame=(100, 700, 60, 60)),
                                node("钱包", kind="Cell", frame=(20, 200, 362, 80))))
        self.phone = DeviceController(self.core, self.axe, self.state)

    def error(self, call, *arguments, **keywords):
        with self.assertRaises(PUAError) as caught:
            call(*arguments, **keywords)
        return caught.exception


class ObserveTests(ControllerTestCase):
    def test_tree_observation_names_the_app_and_compacts_nodes(self):
        result = self.phone.observe("tree")
        self.assertEqual(result["app"], "SpringBoard")
        self.assertEqual(result["backend"], "coredevice+axe")
        self.assertEqual(result["viewport"], {"width": 402.0, "height": 874.0, "units": "iPhone points"})
        self.assertEqual([item["label"] for item in result["nodes"]], ["返回", "搜索", "钱包"])
        self.assertEqual(result["nodes"][0]["rect"], [0, 0, 44, 44])
        self.assertNotIn("visible", result["nodes"][0])
        self.assertEqual(result["total_nodes"], 3)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["observation_id"])

    def test_max_nodes_truncates_without_losing_the_total(self):
        result = self.phone.observe("tree", max_nodes=1)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["total_nodes"], 3)
        self.assertTrue(result["truncated"])

    def test_empty_tree_says_to_look_at_a_screenshot(self):
        self.axe.nodes = tree()
        result = self.phone.observe("tree")
        self.assertIn("Empty accessibility tree", result["warning"])

    def test_screenshot_observation_records_the_pixel_scale(self):
        result = self.phone.observe("screenshot")
        image = result["image"]
        self.assertTrue(Path(image["path"]).is_file())
        self.assertEqual(image["mimeType"], "image/jpeg")
        self.assertEqual(image["width"], 600)
        self.assertEqual(image["height"], 1300)
        self.assertEqual(image["pixel_to_point"], [round(402 / 600, 4), round(874 / 1300, 4)])
        self.assertNotIn("nodes", result)

    def test_both_reads_the_tree_and_the_screen(self):
        result = self.phone.observe("both")
        self.assertEqual(len(result["nodes"]), 3)
        self.assertIn("image", result)

    def test_unknown_mode_is_rejected(self):
        error = self.error(self.phone.observe, "none")
        self.assertEqual(error.code, "invalid_argument")

    def test_hidden_nodes_are_opt_in(self):
        self.axe.nodes = tree(node("隐藏"), node("可见", hidden=True))
        visible = self.phone.observe("tree")["nodes"]
        self.assertEqual([item["label"] for item in visible], ["隐藏"])
        everything = self.phone.observe("tree", include_invisible=True)["nodes"]
        self.assertEqual([item["label"] for item in everything], ["隐藏", "可见"])
        self.assertFalse(everything[1]["visible"])
        self.assertEqual(everything[1]["rect"], [0, 0, 44, 44])

    def test_artifacts_are_pruned_to_the_limit(self):
        import controller
        self.addCleanup(setattr, controller, "ARTIFACT_LIMIT", controller.ARTIFACT_LIMIT)
        controller.ARTIFACT_LIMIT = 2
        for _ in range(4):
            self.phone.observe("screenshot")
        self.assertEqual(len(list(self.phone.artifacts.glob("*.jpg"))), 2)


class FindTests(ControllerTestCase):
    def test_labels_match_exactly_and_contain(self):
        self.assertEqual(self.phone.find({"label": "返回"})["matches"], 1)
        self.assertEqual(self.phone.find({"label_contains": "索"})["matches"], 1)
        self.assertEqual(self.phone.find({"label_contains": "z"})["matches"], 0)

    def test_name_type_and_value_filters(self):
        self.assertEqual(self.phone.find({"name": "search"})["elements"][0]["label"], "搜索")
        self.assertEqual(self.phone.find({"type": "Cell"})["elements"][0]["label"], "钱包")
        self.assertEqual(self.phone.find({"type": "XCUIElementTypeCell"})["matches"], 1)
        self.assertEqual(self.phone.find({"label": "返回", "type": "Button"})["matches"], 1)
        self.assertEqual(self.phone.find({"label": "返回", "type": "Cell"})["matches"], 0)

    def test_index_selects_one_of_several_matches(self):
        self.axe.nodes = tree(node("行", frame=(0, 0, 400, 50)), node("行", frame=(0, 100, 400, 50)))
        self.assertEqual(self.phone.find({"label": "行"})["matches"], 2)
        second = self.phone.find({"label": "行", "index": 1})
        self.assertEqual(second["matches"], 1)
        self.assertEqual(second["elements"][0]["tap"], [200.0, 125.0])
        self.assertEqual(self.phone.find({"label": "行"})["elements"][0]["tap"], [200.0, 25.0])

    def test_index_past_the_end_names_the_real_count(self):
        error = self.error(self.phone.query, {"label": "返回", "index": 3})
        self.assertEqual(error.code, "no_such_element")
        self.assertEqual(error.details["matches"], 1)

    def test_limit_truncates_the_payload(self):
        self.axe.nodes = tree(*[node(f"行{i}", frame=(0, i * 60, 400, 50)) for i in range(5)])
        result = self.phone.find({"type": "Button"}, limit=2)
        self.assertEqual(len(result["elements"]), 2)
        self.assertTrue(result["truncated"])

    def test_selector_fields_are_closed(self):
        error = self.error(self.phone.find, {"label": "返回", "predicate": "x"})
        self.assertEqual(error.code, "invalid_argument")
        self.assertIn("predicate", str(error))


class TapTests(ControllerTestCase):
    def test_selector_tap_uses_axe_with_the_native_identifier(self):
        result = self.phone.tap(selector={"label": "搜索", "name": "search"})
        self.assertEqual(result["backend"], "axe")
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.axe.taps[0]["label"], "搜索")
        self.assertEqual(self.axe.taps[0]["identifier"], "search")
        self.assertEqual(self.core.ops(), ["info"])

    def test_nested_duplicates_resolve_to_the_innermost_element(self):
        outer = node("卡片", kind="Cell", frame=(0, 0, 400, 200),
                     children=[node("卡片", kind="StaticText", frame=(0, 0, 400, 200))])
        self.axe.nodes = tree(outer)
        result = self.phone.tap(selector={"label": "卡片"})
        self.assertEqual(result["tapped"], {"selector": {"label": "卡片"}})
        self.assertEqual(self.axe.taps[0]["label"], "卡片")

    def test_two_distinct_centres_are_ambiguous_with_candidates(self):
        self.axe.nodes = tree(node("行", frame=(0, 0, 400, 50)), node("行", frame=(0, 100, 400, 50)))
        error = self.error(self.phone.tap, selector={"label": "行"})
        self.assertEqual(error.code, "ambiguous_target")
        self.assertEqual(len(error.details["candidates"]), 2)
        self.assertEqual(error.details["recovery"]["next_tool"], "pua_find")
        self.assertEqual(self.axe.taps, [])

    def test_index_disambiguates(self):
        self.axe.nodes = tree(node("行", frame=(0, 0, 400, 50)), node("行", frame=(0, 100, 400, 50)))
        result = self.phone.tap(selector={"label": "行", "index": 1})
        self.assertTrue(result["action_executed"])

    def test_missing_element_reports_no_such_element_without_acting(self):
        error = self.error(self.phone.tap, selector={"label": "缺失"})
        self.assertEqual(error.code, "no_such_element")
        self.assertEqual(error.details["action_executed"], False)
        self.assertEqual([call for call in self.core.calls if call[0] == "tap"], [])

    def test_axe_ambiguity_falls_back_to_the_matched_point(self):
        self.axe.tap_error = PUAError("axe_ambiguous_target", "Two candidates cover this point.")
        result = self.phone.tap(selector={"label": "返回"})
        self.assertEqual(result["backend"], "hid")
        self.assertIn(("tap", {"x": 22.0 / 402, "y": 22.0 / 874}), self.core.calls)

    def test_other_axe_errors_keep_the_screen_for_a_visual_check(self):
        self.axe.tap_error = PUAError("axe_target_not_found", "It vanished.")
        error = self.error(self.phone.tap, selector={"label": "返回"})
        self.assertEqual(error.code, "axe_target_not_found")
        self.assertIn("image", error.details["observation"])
        self.assertTrue(error.details["visual_check_required"])

    def test_coordinates_are_normalised_for_the_helper(self):
        result = self.phone.tap(x=201, y=437)
        self.assertEqual(result["backend"], "coredevice")
        self.assertIn(("tap", {"x": 0.5, "y": 0.5}), self.core.calls)

    def test_coordinates_outside_the_viewport_are_rejected(self):
        error = self.error(self.phone.tap, x=500, y=10)
        self.assertEqual(error.code, "invalid_argument")

    def test_selector_and_coordinates_are_mutually_exclusive(self):
        error = self.error(self.phone.tap, selector={"label": "返回"}, x=1, y=1)
        self.assertEqual(error.code, "invalid_argument")

    def test_unknown_observation_is_stale(self):
        error = self.error(self.phone.tap, x=10, y=10, observation_id="deadbeef")
        self.assertEqual(error.code, "stale_observation")
        self.assertEqual(error.details["recovery"]["next_tool"], "pua_observe")

    def test_a_locked_phone_refuses_to_tap(self):
        self.phone._locked = True
        error = self.error(self.phone.tap, x=10, y=10)
        self.assertEqual(error.code, "phone_locked")
        self.assertEqual(error.details["recovery"]["next_tool"], "pua_unlock")

    def test_expectation_verifies_or_reports_a_failure_without_raising(self):
        verified = self.phone.tap(x=10, y=10, expect={"label": "返回"})
        self.assertTrue(verified["verified"])
        missing = self.phone.tap(x=10, y=10, expect={"label": "缺失"})
        self.assertFalse(missing["verified"])
        self.assertEqual(missing["postcondition_failed"]["code"], "postcondition_failed")
        self.assertTrue(missing["action_executed"])


class SwipeTests(ControllerTestCase):
    def test_default_region_scrolls_up_from_the_middle(self):
        result = self.phone.swipe()
        self.assertEqual(result["swiped"], "up")
        _, arguments = next(call for call in self.core.calls if call[0] == "swipe")
        middle = (402 / 2, 874 * 0.5)
        reach = 874 * 0.5 * 0.32
        self.assertAlmostEqual(arguments["from"][0], middle[0] / 402, places=4)
        self.assertAlmostEqual(arguments["from"][1], (middle[1] + reach) / 874, places=4)
        self.assertAlmostEqual(arguments["to"][1], (middle[1] - reach) / 874, places=4)

    def test_a_region_keeps_the_gesture_inside_it(self):
        self.phone.swipe("down", region={"x": 0, "y": 100, "width": 400, "height": 200})
        _, arguments = next(call for call in self.core.calls if call[0] == "swipe")
        self.assertLess(arguments["from"][1], arguments["to"][1])

    def test_regions_must_fit_the_viewport(self):
        error = self.error(self.phone.swipe, region={"x": 0, "y": 0, "width": 500, "height": 200})
        self.assertEqual(error.code, "invalid_argument")
        error = self.error(self.phone.swipe, region={"x": 0, "y": 0, "width": 10})
        self.assertEqual(error.code, "invalid_argument")

    def test_direction_is_a_closed_set(self):
        error = self.error(self.phone.swipe, "diagonal")
        self.assertEqual(error.code, "invalid_argument")


class TypeTests(ControllerTestCase):
    def test_ascii_text_goes_to_the_helper(self):
        result = self.phone.type_text("hello 123")
        self.assertEqual(result["typed"], 9)
        self.assertIn(("type", {"text": "hello 123"}), self.core.calls)

    def test_newlines_need_confirmation(self):
        error = self.error(self.phone.type_text, "a\nb")
        self.assertEqual(error.code, "invalid_argument")
        self.phone.type_text("a\nb", allow_newlines=True)

    def test_non_ascii_is_rejected_before_touching_the_phone(self):
        error = self.error(self.phone.type_text, "你好")
        self.assertEqual(error.code, "invalid_argument")
        self.assertIn("ASCII", error.message)
        self.assertEqual([call for call in self.core.calls if call[0] == "type"], [])

    def test_carriage_returns_are_rejected(self):
        self.assertEqual(self.error(self.phone.type_text, "a\rb").code, "invalid_argument")

    def test_text_is_bounded(self):
        error = self.error(self.phone.type_text, "a" * 501)
        self.assertEqual(error.code, "invalid_argument")


class ButtonTests(ControllerTestCase):
    def test_home_press_marks_the_cached_geometry_stale(self):
        self.phone.viewport()
        result = self.phone.press_button("home")
        self.assertEqual(result["pressed"], "home")
        self.assertIsNone(self.phone._viewport_cache)

    def test_volume_keys_are_supported(self):
        self.assertEqual(self.phone.press_button("volumeup")["pressed"], "volumeup")
        self.assertEqual(self.phone.press_button("volumedown")["pressed"], "volumedown")

    def test_lock_is_not_a_button_anyone_can_press(self):
        error = self.error(self.phone.press_button, "lock")
        self.assertEqual(error.code, "invalid_argument")

    def test_launch_requires_a_bundle_id(self):
        self.assertEqual(self.phone.launch_app("com.tencent.xin")["launched"], "com.tencent.xin")
        self.assertEqual(self.error(self.phone.launch_app, "微信").code, "invalid_argument")


class WaitTests(ControllerTestCase):
    def test_wait_returns_as_soon_as_the_selector_exists(self):
        result = self.phone.wait({"label": "返回"}, timeout_seconds=0.5)
        self.assertTrue(result["verified"])
        self.assertEqual(result["polls"], 1)
        self.assertIn("does not prove", result["criterion"])

    def test_wait_times_out_with_a_recovery_hint(self):
        error = self.error(self.phone.wait, {"label": "缺失"}, timeout_seconds=0.2)
        self.assertEqual(error.code, "postcondition_failed")
        self.assertGreaterEqual(error.details["polls"], 1)
        self.assertEqual(error.details["recovery"]["next_tool"], "pua_observe")

    def test_timeout_is_bounded(self):
        self.assertEqual(self.error(self.phone.wait, {"label": "x"}, timeout_seconds=99).code,
                         "invalid_argument")


class UnlockTests(ControllerTestCase):
    def test_an_unlocked_phone_never_receives_the_passcode(self):
        result = self.phone.unlock("1234")
        self.assertFalse(result["locked"])
        self.assertFalse(result["unlocked"])
        self.assertEqual(result["verified"], "accessibility_tree")
        self.assertNotIn("unlock", self.core.ops())

    def test_a_locked_phone_gets_the_passcode_and_a_fresh_proof(self):
        self.axe.locked = True
        self.core.after_unlock = lambda: setattr(self.axe, "locked", False)
        result = self.phone.unlock("1234")
        self.assertTrue(result["unlocked"])
        self.assertEqual(result["verified"], "accessibility_tree")
        self.assertIn(("unlock", {"passcode": "1234", "interval": 0.25}), self.core.calls)

    def test_a_wrong_passcode_stays_locked(self):
        self.axe.locked = True
        result = self.phone.unlock("0000")
        self.assertFalse(result["unlocked"])
        self.assertTrue(result["locked"])

    def test_without_a_passcode_the_tool_explains_itself(self):
        self.axe.locked = True
        error = self.error(self.phone.unlock, "")
        self.assertEqual(error.code, "unsupported")
        self.assertIn("IPHONE_USE_UNLOCK_PASSCODE", error.message)

    def test_without_axe_the_passcode_is_never_typed(self):
        self.axe.available_flag = False
        error = self.error(self.phone.unlock, "1234")
        self.assertEqual(error.code, "axe_unavailable")
        self.assertNotIn("unlock", self.core.ops())

    def test_non_digits_are_rejected(self):
        self.assertEqual(self.error(self.phone.unlock, "12ab").code, "invalid_argument")


class ReadyTests(ControllerTestCase):
    def test_ready_proves_both_channels(self):
        result = self.phone.ready()
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["coredevice"])
        self.assertTrue(result["proof"]["axe"])
        self.assertEqual(result["proof"]["display"], [402, 874])
        self.assertIn("image", result["observation"])

    def test_ready_can_skip_the_screenshot(self):
        result = self.phone.ready(screenshot=False)
        self.assertNotIn("image", result["observation"])
        self.assertFalse(result["proof"]["screenshot"])

    def test_a_locked_phone_is_a_state_not_an_exception(self):
        self.phone.passcode = "1234"
        self.axe.locked = True
        result = self.phone.ready()
        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["next_tool"], "pua_unlock")

    def test_missing_channels_are_named(self):
        self.axe.available_flag = False
        self.assertEqual(self.error(self.phone.ready).code, "axe_unavailable")

        class Missing(FakeCore):
            def available(self):
                return False
        self.phone.core = Missing()
        self.assertEqual(self.error(self.phone.ready).code, "coredevice_unavailable")


class BatchTests(ControllerTestCase):
    def test_steps_run_in_order_and_report_completion(self):
        result = self.phone.batch([{"op": "tap", "args": {"x": 10, "y": 10}},
                                   {"op": "type_text", "args": {"text": "ok"}},
                                   {"op": "observe", "args": {"mode": "tree"}}])
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 3)
        self.assertEqual(result["results"][2]["nodes"][0]["label"], "返回")

    def test_a_failing_step_stops_the_batch_without_replaying(self):
        result = self.phone.batch([{"op": "tap", "args": {"x": 10, "y": 10}},
                                   {"op": "tap", "args": {"selector": {"label": "缺失"}}},
                                   {"op": "type_text", "args": {"text": "never"}}])
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stopped_at"], 1)
        self.assertEqual(result["stop_reason"], "no_such_element")
        self.assertNotIn(("type", {"text": "never"}), self.core.calls)

    def test_bad_step_shapes_are_rejected_before_anything_runs(self):
        error = self.error(self.phone.batch, [{"op": "tap", "args": {}, "extra": 1}])
        self.assertEqual(error.code, "invalid_argument")
        error = self.error(self.phone.batch, [{"op": "scroll_find", "args": {}}])
        self.assertEqual(error.code, "invalid_argument")
        self.assertEqual(self.core.ops(), [])

    def test_the_step_list_is_bounded(self):
        self.assertEqual(self.error(self.phone.batch, []).code, "invalid_argument")
        self.assertEqual(self.error(self.phone.batch, [{"op": "observe", "args": {}}] * 21).code,
                         "invalid_argument")


class ErrorSurfaceTests(ControllerTestCase):
    def test_uncertain_helper_failures_are_not_replayed(self):
        self.core.failure = PUAError("coredevice_timeout", "No reply.", uncertain=True)
        error = self.error(self.phone.tap, x=10, y=10)
        self.assertEqual(error.details["action_executed"], False)
        self.assertFalse(error.details["recovery"]["same_action_retry"])

    def test_records_keep_the_artifact_path_inside_the_state_directory(self):
        result = self.phone.observe("screenshot")
        self.assertTrue(Path(result["image"]["path"]).is_relative_to(self.state))
        self.assertEqual(Path(result["image"]["path"]).stat().st_mode & 0o777, 0o600)

    def test_the_json_serialises_without_a_round_trip(self):
        self.phone.tap(x=1, y=1)
        self.phone.observe("both")
        json.dumps(self.phone.find({"label": "返回"}))


if __name__ == "__main__":
    unittest.main()
