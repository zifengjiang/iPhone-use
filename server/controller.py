"""iPhone control on CoreDevice and AXe.

CoreDevice owns every pixel and every touch: the device's own capture service for
screenshots, its HID service for taps and swipes, its keyboard service for text,
its app service for launches. AXe answers the accessibility tree and can tap a
selector, refusing a target that moved or is covered.

Both are local processes: the CoreDevice helper speaks JSON lines with
pymobiledevice3 installed, and AXe is a binary this module invokes.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, deque
import datetime as dt
from functools import wraps
import os
from pathlib import Path
import re
import time
import uuid

from axe_client import AxeClient
from errors import PUAError, fail, unsupported
import image

CALL_BUDGET = 90
VIEWPORT_TTL = 30
MAX_NODES = 500
MAX_TEXT = 500
ARTIFACT_LIMIT = 100
SELECTOR_FIELDS = ("label", "label_contains", "name", "value", "type", "enabled", "index")
MODES = ("none", "tree", "screenshot", "both")
# Roles that describe the screen rather than something on it.
CONTAINERS = {"AXApplication", "AXWindow"}
# Failures that a screenshot makes actionable: the model can see what it missed.
_VISUAL_CODES = {"no_such_element", "ambiguous_target", "offscreen_target", "occluded_target",
                 "stale_observation", "postcondition_failed", "phone_locked",
                 "axe_target_not_found", "axe_ambiguous_target", "axe_target_changed"}


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        fail("invalid_argument", f"{name} must be a finite number.")
    return float(value)


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        fail("invalid_argument", f"{name} must be an integer in {low}..{high}.")
    return value


def selector_spec(value, path="selector"):
    """Validate the closed selector shape shared by every tool that reads the tree."""
    if not isinstance(value, dict) or not value:
        fail("invalid_argument", f"{path} needs at least one field.")
    unknown = sorted(set(value) - set(SELECTOR_FIELDS))
    if unknown:
        fail("invalid_argument", f"{path} has unknown field(s): {', '.join(unknown)}.",
             allowed_fields=list(SELECTOR_FIELDS))
    for key in ("label", "label_contains", "name", "value", "type"):
        if key in value and not isinstance(value[key], str):
            fail("invalid_argument", f"{path}.{key} must be a string.")
    if "enabled" in value:
        allowed = value["enabled"] in (True, False, "true", "false")
        if not allowed:
            fail("invalid_argument", f"{path}.enabled must be a boolean or \"true\"/\"false\".")
    if "index" in value:
        integer(value["index"], f"{path}.index", 0, 199)
    return dict(value)


def _text(value):
    """Accessibility strings that carry no information: empty, whitespace, or a placeholder."""
    if value is None:
        return ""
    return str(value)


def _matches(element, query):
    label = query.get("label")
    if label is not None and element.get("label") != label:
        return False
    contains = query.get("label_contains")
    if contains is not None and contains not in (element.get("label") or ""):
        return False
    name = query.get("name")
    if name is not None and element.get("name") != name:
        return False
    value = query.get("value")
    if value is not None and element.get("value") != value:
        return False
    kind = query.get("type")
    if kind is not None and element.get("type") != kind.removeprefix("XCUIElementType"):
        return False
    if "enabled" in query:
        wanted = query["enabled"] in (True, "true")
        if element.get("enabled") is not wanted:
            return False
    return True


class DeviceController:
    """One iPhone, driven by CoreDevice with AXe for the element tree."""

    backend = "coredevice+axe"

    def __init__(self, core, axe=None, state_dir=None):
        self.core = core
        self.axe = axe if axe is not None else AxeClient()
        self.state_dir = Path(state_dir).expanduser() if state_dir else Path.home() / ".local/share/iphone-use"
        self.artifacts = self.state_dir / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.passcode = (os.environ.get("IPHONE_USE_UNLOCK_PASSCODE") or "").strip()
        self.snapshots = OrderedDict()
        self.tool_records = deque(maxlen=500)
        self.accepted_actions = 0
        self.screen = None
        self.call_budget = CALL_BUDGET
        self.settle_seconds = 0.8
        self._deadline = None
        self._point_size = None
        self._viewport_cache = None
        self._app = None
        self._locked = None

    # -- device -----------------------------------------------------------
    def set_device(self, udid):
        """Retarget the helper; the next call spawns it against the new device."""
        udid = self.core.set_udid(udid)
        self._point_size = None
        self._viewport_cache = None
        self._app = None
        self._locked = None
        return udid

    def close(self):
        self.core.close()

    def reset(self):
        """Forget observations that belonged to a different phone or screen."""
        self.snapshots.clear()
        self._viewport_cache = None
        self._point_size = None

    def external_action(self):
        """A hardware key or another process moved the screen; cached geometry is stale."""
        self.accepted_actions += 1
        self._viewport_cache = None

    def point_size(self):
        if self._point_size is None:
            info = self._core("info")
            size = (info.get("point_width") or 0, info.get("point_height") or 0)
            if not all(isinstance(value, (int, float)) and value > 0 for value in size):
                raise PUAError("coredevice_failed", "CoreDevice did not report a display size.")
            self._point_size = size
        return self._point_size

    def viewport(self, max_age=VIEWPORT_TTL):
        now = time.monotonic()
        if self._viewport_cache and now - self._viewport_cache[0] < max_age:
            return self._viewport_cache[1]
        width, height = self.point_size()
        viewport = {"width": width, "height": height, "units": "iPhone points"}
        self._viewport_cache = (now, viewport)
        if self.screen is not None:
            self.screen.set_viewport(viewport)
        return viewport

    def screen_event(self, kind, **arguments):
        if self.screen is None:
            return
        try:
            self.screen.gesture(kind, viewport=self._viewport_cache[1] if self._viewport_cache else None, **arguments)
        except (OSError, ValueError):
            pass

    # -- screenshots ------------------------------------------------------
    def _core(self, op, **arguments):
        """One helper call; a timeout may mean the phone already acted."""
        try:
            return self.core.call(op, **arguments)
        except PUAError as error:
            if error.code == "coredevice_timeout":
                error.uncertain = True
            raise

    def capture(self, viewport=None):
        """One JPEG straight from the device's capture service, already bounded by the helper."""
        viewport = viewport or self.viewport()
        reply = self._core("screenshot")
        native = (reply.get("width"), reply.get("height"))
        if reply.get("jpeg_b64"):
            data = base64.b64decode(reply["jpeg_b64"], validate=True)
            return self.store_jpeg(data, reply.get("encoded_width"), reply.get("encoded_height"), viewport, native=native)
        data = base64.b64decode(reply.get("png_b64") or "", validate=True)
        if not data.startswith(image.PNG_SIGNATURE):
            raise PUAError("invalid_response", "The device returned a screenshot that is not an image.")
        return self.store_png(data, viewport)

    def store_png(self, data, viewport):
        native = image.png_size(data[:32])
        viewport = self.match_orientation(native, viewport)
        path = self.write_artifact(data, ".png")
        converted, mime, width, height = image.model_image(path)
        return self.image_record(converted, mime, width, height, viewport)

    def store_jpeg(self, data, width, height, viewport, native=None):
        dimensions = (width, height) if width and height else _jpeg_size(data)
        viewport = self.match_orientation(native or dimensions, viewport)
        path = self.write_artifact(data, ".jpg")
        return self.image_record(path, "image/jpeg", dimensions[0], dimensions[1], viewport)

    def write_artifact(self, data, suffix):
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.artifacts / f"{stamp}-{uuid.uuid4().hex[:8]}{suffix}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        return path

    def match_orientation(self, native, viewport):
        """A rotated device reports swapped native dimensions; the point grid must follow."""
        if not native or not all(native):
            return viewport
        width, height = native
        if (width > height) != (viewport["width"] > viewport["height"]):
            self._point_size = None
            self._viewport_cache = None
            return self.viewport()
        return viewport

    def image_record(self, path, mime, width, height, viewport):
        """Describe an image with the exact scale from its pixels to the point grid."""
        files = list(self.artifacts.glob("*.png")) + list(self.artifacts.glob("*.jpg"))
        for candidate in sorted(files)[:-ARTIFACT_LIMIT]:
            try:
                candidate.unlink()
            except OSError:
                pass
        record = {"path": str(path), "mimeType": mime}
        if width and height:
            record.update(width=width, height=height,
                          pixel_to_point=[round(viewport["width"] / width, 4),
                                          round(viewport["height"] / height, 4)])
        return record

    # -- observation ------------------------------------------------------
    def axe_tree(self):
        """Every node AXe reports, depth first, with the foreground app name."""
        if not self.core.udid:
            raise PUAError("device_unavailable",
                           "No iPhone is pinned yet: run pua_setup discover, then configure with its udid.",
                           details={"action_executed": False,
                                    "recovery": {"next_tool": "pua_setup", "arguments": {"action": "discover"}}})
        if not self.axe.available():
            raise PUAError("axe_unavailable", "The element tree needs AXe; set IPHONE_USE_AXE or install it on PATH.")
        try:
            raw = self.axe.describe_ui(self.core.udid)
        except PUAError as error:
            if error.code == "phone_locked":
                self._locked = True
            raise
        self._locked = False
        nodes, app = [], None
        stack = list(reversed(raw if isinstance(raw, list) else [raw]))
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if app is None and node.get("role") == "AXApplication":
                app = _text(node.get("AXLabel")).strip() or None
            nodes.append(node)
            stack.extend(reversed([child for child in node.get("children") or [] if isinstance(child, dict)]))
        if app:
            self._app = app
        return nodes

    def elements(self, include_invisible=False):
        """Normalised elements: the compact node plus what a selector may match on."""
        viewport = self.viewport()
        result = []
        for raw in self.axe_tree():
            element = self.element(raw, viewport, include_invisible)
            if element is not None:
                result.append(element)
        return result

    def element(self, raw, viewport, include_invisible):
        role = str(raw.get("role") or "")
        if role in CONTAINERS:
            return None
        if raw.get("hidden") is True and not include_invisible:
            return None
        frame = raw.get("frame") or {}
        try:
            x, y = float(frame["x"]), float(frame["y"])
            width, height = float(frame["width"]), float(frame["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(value == value and abs(value) != float("inf") for value in (x, y, width, height)):
            return None
        if width <= 0 or height <= 0:
            return None
        label = _text(raw.get("AXLabel"))
        name = _text(raw.get("AXUniqueId"))
        value = _text(raw.get("AXValue"))
        title = _text(raw.get("title"))
        label = "" if not label.strip() else label
        name = "" if not name.strip() or name == label else name
        value = "" if not value.strip() or value in (label, name) else value
        kind = str(raw.get("type") or role.removeprefix("AX") or "Element")
        if not any((label, name, value, title)) and kind not in ("Alert", "Sheet"):
            return None
        visible = raw.get("hidden") is not True
        in_viewport = (x + width > 1 and y + height > 1 and x < viewport["width"] - 1
                       and y < viewport["height"] - 1)
        node = {"type": kind, "rect": [round(x, 1), round(y, 1), round(width, 1), round(height, 1)]}
        if label:
            node["label"] = label
        if name:
            node["name"] = name
        if value:
            node["value"] = value
        if raw.get("enabled") is False:
            node["enabled"] = False
        if not visible:
            node["visible"] = False
        if not in_viewport:
            node["in_viewport"] = False
        return {"type": kind, "label": label, "name": name, "value": value,
                "enabled": raw.get("enabled") is not False, "visible": visible,
                "rect": {"x": x, "y": y, "width": width, "height": height},
                "center": (x + width / 2, y + height / 2), "node": node}

    @staticmethod
    def describe(element, index=None):
        described = dict(element["node"])
        if index is not None:
            described["index"] = index
        described["tap"] = [round(element["center"][0], 1), round(element["center"][1], 1)]
        return described

    def query(self, query):
        elements = [element for element in self.elements() if _matches(element, query)]
        if "index" in query:
            index = query["index"]
            if index >= len(elements):
                raise PUAError("no_such_element",
                               f"index {index} is past the {len(elements)} matching element(s).",
                               details={"matches": len(elements), "action_executed": False})
            return [elements[index]]
        return elements

    def find(self, query, limit=10):
        query = selector_spec(query)
        integer(limit, "limit", 1, 50)
        matches = self.query(query)
        return {"matches": len(matches),
                "elements": [self.describe(element, index) for index, element in enumerate(matches[:limit])],
                "truncated": len(matches) > limit,
                "app": self._app, "viewport": self.viewport()}

    def observe(self, mode="tree", include_invisible=False, max_nodes=100):
        if mode not in MODES or mode == "none":
            fail("invalid_argument", "mode must be tree, screenshot or both.")
        integer(max_nodes, "max_nodes", 1, MAX_NODES)
        viewport = self.viewport()
        elements, nodes, image_record = [], None, None
        if mode in ("tree", "both"):
            elements = self.elements(include_invisible=include_invisible)
            nodes = [element["node"] for element in elements[:max_nodes]]
        if mode in ("screenshot", "both"):
            image_record = self.capture(viewport)
            viewport = self.viewport()
        observation_id = self.remember(nodes, viewport, self._app)
        result = {"observation_id": observation_id, "observed_at": _timestamp(), "app": self._app,
                  "viewport": viewport, "backend": self.backend}
        if nodes is not None:
            result.update(nodes=nodes, total_nodes=len(elements), truncated=len(elements) > max_nodes,
                          visibility_computed=not include_invisible)
            if not nodes:
                result["warning"] = ("Empty accessibility tree: inspect a screenshot for a lock, loading, "
                                     "Iris Mirroring conflict, or custom-rendered content before acting.")
        if image_record is not None:
            result["image"] = image_record
        return result

    def remember(self, nodes, viewport, app):
        observation_id = uuid.uuid4().hex[:12]
        self.snapshots[observation_id] = {"time": time.monotonic(), "viewport": viewport,
                                          "app": app, "nodes": nodes}
        while len(self.snapshots) > 32:
            self.snapshots.popitem(last=False)
        return observation_id

    def snapshot(self, observation_id):
        snapshot = self.snapshots.get(observation_id)
        if snapshot is None:
            self.stale("This observation is no longer usable; read the screen again.", "unknown_observation")
        if snapshot["app"] and self._app and snapshot["app"] != self._app:
            self.stale("The foreground app changed since the observation.", "foreground_changed",
                       previous_app=snapshot["app"], current_app=self._app)
        return snapshot

    @staticmethod
    def stale(message, reason, **details):
        details.update(action_executed=False, replay_action=False,
                       recovery={"next_tool": "pua_observe", "reason": reason})
        fail("stale_observation", message, **details)

    def guard(self, observation_id=None):
        if observation_id:
            snapshot = self.snapshot(observation_id)
            viewport = self.viewport(max_age=0)
            if viewport["width"] != snapshot["viewport"]["width"] or viewport["height"] != snapshot["viewport"]["height"]:
                self.stale("The device viewport changed since the observation; observe again.", "viewport_changed")
            return viewport
        return self.viewport()

    def wait(self, query, timeout_seconds=6):
        query = selector_spec(query)
        finite(timeout_seconds, "timeout_seconds")
        if not 0.1 <= timeout_seconds <= 30:
            fail("invalid_argument", "timeout_seconds must be between 0.1 and 30.")
        deadline, polls = time.monotonic() + timeout_seconds, 0
        while True:
            polls += 1
            matches = self.query(query)
            if matches:
                return {"verified": True, "matches": len(matches), "polls": polls,
                        "element": self.describe(matches[0]),
                        "criterion": "Selector exists on screen; presence alone does not prove business success."}
            if time.monotonic() >= deadline:
                fail("postcondition_failed", f"No element matched within {timeout_seconds:g}s.",
                 polls=polls, selector=query, action_executed=False,
                 recovery={"next_tool": "pua_observe",
                           "next_step": "Read the screen and check whether the app is where you expected."})
            time.sleep(0.25)

    # -- actions ----------------------------------------------------------
    def after(self, expect=None, observe="none", max_nodes=100):
        result = {"action_executed": True, "action_complete": True,
                  "verified": False, "verification_deferred": True}
        if expect:
            try:
                result["postcondition"] = self.wait(expect)
            except PUAError as error:
                result.update(postcondition_failed=error.as_dict(), verification_deferred=False)
            else:
                result.update(verified=True, verification_deferred=False)
        if observe != "none":
            result["observation"] = self.observe_after(observe, max_nodes=max_nodes)
        return result

    def observe_after(self, mode, max_nodes=100):
        if mode not in MODES:
            fail("invalid_argument", "observe must be none, tree, screenshot or both.")
        if self.settle_seconds > 0 and mode in ("tree", "both"):
            time.sleep(min(self.settle_seconds, 1.0))
        return self.observe(mode, max_nodes=max_nodes)

    def switch_to_coordinates(self, error, typing=False):
        """A visual failure is easier to act on with the screen in hand."""
        if typing or error.details.get("observation") or error.code not in _VISUAL_CODES:
            return
        if error.code == "not_editable" and error.details.get("secure_field"):
            return
        try:
            record = self.capture(self.viewport())
        except PUAError as capture_error:
            error.details["observation_error"] = str(capture_error)
            return
        error.details["observation"] = {"image": record, "viewport": self.viewport(), "app": self._app}
        recovery = error.details.setdefault("recovery", {})
        recovery.setdefault("use", "coordinates")
        recovery.setdefault("next_step", "Read the screenshot: tap the point you can see, not the stale selector.")
        error.details.setdefault("visual_check_required", True)
        error.details.setdefault("replay_action", False)

    @staticmethod
    def mutation(function):
        """Every mutation reports whether the phone could have acted before the error."""
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            try:
                return function(self, *args, **kwargs)
            except PUAError as error:
                error.details.setdefault("action_executed", False)
                if error.uncertain:
                    error.details.setdefault("recovery", {})
                    error.details["recovery"].setdefault("same_action_retry", False)
                self.switch_to_coordinates(error)
                raise
        return wrapped

    def ensure_unlocked(self):
        if self._locked is True:
            raise PUAError("phone_locked", "The iPhone is locked. Unlock it, then verify with pua_ready.",
                           details={"action_executed": False,
                                    "recovery": {"next_tool": "pua_unlock"}})

    def _fraction(self, x, y):
        width, height = self.point_size()
        if not (0 <= x <= width and 0 <= y <= height):
            fail("invalid_argument", f"Point ({x}, {y}) is outside the {width:g}x{height:g} point viewport.")
        return x / width, y / height

    def _choose(self, matches):
        """Nested duplicates share a centre; the innermost one is what a person taps."""
        centres = {(round(element["center"][0]), round(element["center"][1])) for element in matches}
        if len(centres) == 1:
            return min(matches, key=lambda element: element["rect"]["width"] * element["rect"]["height"])
        return None

    @mutation
    def tap(self, selector=None, x=None, y=None, observation_id=None, expect=None, observe="none"):
        if observe not in MODES:
            fail("invalid_argument", "observe must be none, tree, screenshot or both.")
        if expect:
            selector_spec(expect, "expect")
        if selector is not None:
            if x is not None or y is not None:
                fail("invalid_argument", "Choose a selector or coordinates, not both.")
            query = selector_spec(selector)
            matches = self.query(query)
            if not matches:
                raise PUAError("no_such_element", "Nothing on screen matches this selector.",
                               details={"selector": query, "action_executed": False,
                                        "recovery": {"next_tool": "pua_find", "next_step": "Find a live selector or tap a point from a screenshot."}})
            chosen = matches[0] if "index" in query or len(matches) == 1 else self._choose(matches)
            if chosen is None:
                raise PUAError("ambiguous_target", f"{len(matches)} elements match; add type, value or index.",
                               details={"selector": query, "action_executed": False,
                                        "candidates": [self.describe(element, index)
                                                       for index, element in enumerate(matches[:8])],
                                        "recovery": {"next_tool": "pua_find"}})
            self.ensure_unlocked()
            point = chosen["center"]
            channel = "axe"
            simple = {key: query[key] for key in ("label", "name", "value", "type") if query.get(key)}
            if simple and not (set(query) - set(simple) - {"enabled"}) and query.get("enabled", True) is not False:
                try:
                    self.axe.tap(self.core.udid, identifier=simple.get("name"), label=simple.get("label"),
                                 value=simple.get("value"), element_type=simple.get("type"))
                except PUAError as error:
                    if error.code != "axe_ambiguous_target":
                        raise
                    channel = "axe+hid"
                else:
                    self.accepted_actions += 1
                    self.screen_event("tap", point={"x": point[0], "y": point[1]})
                    return {"tapped": {"selector": query}, "backend": channel, **self.after(expect, observe)}
            self._tap_point(point[0], point[1])
            channel = "hid"
            result = {"tapped": {"selector": query}, "backend": channel}
            result.update(self.after(expect, observe))
            return result
        finite(x, "x")
        finite(y, "y")
        viewport = self.guard(observation_id)
        if x >= viewport["width"] or y >= viewport["height"]:
            fail("invalid_argument", "Coordinates are outside the iPhone viewport.")
        self.ensure_unlocked()
        reply = self._tap_point(x, y)
        return {"tapped": {"x": x, "y": y}, "backend": "coredevice",
                "duration_ms": reply.get("duration_ms"), **self.after(expect, observe)}

    def _tap_point(self, x, y):
        fraction = self._fraction(x, y)
        reply = self._core("tap", x=fraction[0], y=fraction[1])
        self.accepted_actions += 1
        self.screen_event("tap", point={"x": x, "y": y})
        return reply

    @mutation
    def swipe(self, direction="up", region=None, observation_id=None, expect=None, observe="none"):
        if direction not in ("up", "down", "left", "right"):
            fail("invalid_argument", "direction must be up, down, left or right.")
        if observe not in MODES:
            fail("invalid_argument", "observe must be none, tree, screenshot or both.")
        if expect:
            selector_spec(expect, "expect")
        viewport = self.guard(observation_id)
        area = self._region(region, viewport)
        middle = (area["x"] + area["width"] / 2, area["y"] + area["height"] / 2)
        reach_x, reach_y = area["width"] * 0.32, area["height"] * 0.32
        offset = {"up": (0, -reach_y), "down": (0, reach_y),
                  "left": (-reach_x, 0), "right": (reach_x, 0)}[direction]
        start = (middle[0] - offset[0], middle[1] - offset[1])
        end = (middle[0] + offset[0], middle[1] + offset[1])
        self.ensure_unlocked()
        width, height = viewport["width"], viewport["height"]
        reply = self._core("swipe", **{"from": [start[0] / width, start[1] / height],
                                           "to": [end[0] / width, end[1] / height]})
        self.accepted_actions += 1
        self.screen_event("drag", from_point={"x": start[0], "y": start[1]},
                          to_point={"x": end[0], "y": end[1]}, duration_ms=400)
        return {"swiped": direction, "backend": "coredevice", "duration_ms": reply.get("duration_ms"),
                **self.after(expect, observe)}

    @staticmethod
    def _region(region, viewport):
        if region is None:
            return {"x": viewport["width"] * 0.15, "y": viewport["height"] * 0.25,
                    "width": viewport["width"] * 0.7, "height": viewport["height"] * 0.5}
        if not isinstance(region, dict):
            fail("invalid_argument", "region must be an object with x, y, width and height.")
        missing = sorted({"x", "y", "width", "height"} - set(region))
        if missing:
            fail("invalid_argument", f"region is missing {', '.join(missing)}.")
        unknown = sorted(set(region) - {"x", "y", "width", "height"})
        if unknown:
            fail("invalid_argument", f"region has unknown field(s): {', '.join(unknown)}.")
        area = {key: finite(region[key], f"region.{key}") for key in ("x", "y", "width", "height")}
        if area["width"] <= 0 or area["height"] <= 0:
            fail("invalid_argument", "region width and height must be positive.")
        if (area["x"] < 0 or area["y"] < 0 or area["x"] + area["width"] > viewport["width"]
                or area["y"] + area["height"] > viewport["height"]):
            fail("invalid_argument", "region must fit inside the viewport.")
        return area

    @mutation
    def type_text(self, text, allow_newlines=False, observe="none"):
        if not isinstance(text, str) or not text:
            fail("invalid_argument", "text must be a non-empty string.")
        if len(text) > MAX_TEXT:
            fail("invalid_argument", f"text is longer than {MAX_TEXT} characters; send it in several calls.")
        if not isinstance(allow_newlines, bool):
            fail("invalid_argument", "allow_newlines must be boolean.")
        bad = [character for character in text
               if character not in "\n\t" and (ord(character) < 32 or ord(character) == 127)]
        if bad:
            fail("invalid_argument", "Typing covers printable text in any script, newline and tab. "
                                     f"Unsupported: {''.join(sorted(set(bad)))[:20]!r}.")
        if not allow_newlines and ("\n" in text or "\r" in text):
            fail("invalid_argument", "Newlines submit or start a new line; pass allow_newlines=true to confirm.")
        if "\r" in text:
            fail("invalid_argument", "Use \\n for a new line.")
        self.ensure_unlocked()
        reply = self._core("type", text=text)
        self.accepted_actions += 1
        return {"typed": len(text), "backend": "coredevice", "duration_ms": reply.get("duration_ms"),
                **self.after(None, observe)}

    @mutation
    def press_button(self, name, expect=None, observe="none"):
        if name not in ("home", "volumeup", "volumedown"):
            fail("invalid_argument", "name must be home, volumeup or volumedown.")
        if observe not in MODES:
            fail("invalid_argument", "observe must be none, tree, screenshot or both.")
        self.ensure_unlocked()
        reply = self._core("button", name=name)
        self.accepted_actions += 1
        self.external_action()
        if name == "home":
            time.sleep(0.6)
        return {"pressed": name, "backend": "coredevice", "duration_ms": reply.get("duration_ms"),
                **self.after(expect, observe)}

    @mutation
    def launch_app(self, bundle_id, expect=None, observe="none"):
        if not isinstance(bundle_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", bundle_id):
            fail("invalid_argument", "Use the app's verified bundle ID.")
        if observe not in MODES:
            fail("invalid_argument", "observe must be none, tree, screenshot or both.")
        self.ensure_unlocked()
        reply = self._core("launch", bundle_id=bundle_id)
        self.accepted_actions += 1
        self._viewport_cache = None
        self._app = None
        time.sleep(0.4)
        return {"launched": bundle_id, "backend": "coredevice", "duration_ms": reply.get("duration_ms"),
                **self.after(expect, observe)}

    # -- lock screen ------------------------------------------------------
    def locked(self):
        """Ask AXe whether the tree is readable; a locked phone answers with an error."""
        try:
            self.axe_tree()
        except PUAError as error:
            if error.code != "phone_locked":
                raise
            self._locked = True
            return True
        return False

    def unlock(self, passcode=None, interval=0.25):
        """Type the passcode only after AXe proved the phone is locked.

        The helper's flow taps and swipes before typing, so running it on an unlocked
        phone would type digits into whatever app is in front.
        """
        code = (passcode or self.passcode or "").strip()
        if not code:
            unsupported("Set IPHONE_USE_UNLOCK_PASSCODE or pass the passcode to unlock.")
        if not code.isdigit():
            fail("invalid_argument", "The passcode must be digits.")
        if not self.axe.available():
            raise PUAError("axe_unavailable", "Proving the lock state needs AXe; without it the passcode cannot be typed safely.")
        if not self.locked():
            return {"locked": False, "unlocked": False, "verified": "accessibility_tree",
                    "duration_ms": 0, "backend": "coredevice"}
        reply = self._core("unlock", passcode=code, interval=finite(interval, "interval"))
        try:
            still_locked = self.locked()
        except PUAError:
            still_locked = None
        unlocked = bool(reply.get("unlocked")) if still_locked is None else not still_locked
        return {"locked": not unlocked, "unlocked": unlocked,
                "verified": "screenshot" if still_locked is None else "accessibility_tree",
                "attempts": reply.get("attempts"), "duration_ms": reply.get("duration_ms"),
                "backend": "coredevice"}

    # -- composite --------------------------------------------------------
    def ready(self, screenshot=True):
        """Prove both channels: display geometry from CoreDevice, tree from AXe."""
        if not self.core.available():
            raise PUAError("coredevice_unavailable",
                           "CoreDevice needs python3 with pymobiledevice3; set IPHONE_USE_COREDEVICE_PYTHON.",
                           details={"action_executed": False})
        if not self.axe.available():
            raise PUAError("axe_unavailable", "Element trees need AXe; set IPHONE_USE_AXE or install it on PATH.",
                           details={"action_executed": False})
        info = self._core("info")
        width, height = info.get("point_width"), info.get("point_height")
        if not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or width <= 0 or height <= 0:
            raise PUAError("coredevice_failed", "The display geometry was not readable.", details={"action_executed": False})
        self._point_size = (float(width), float(height))
        self._viewport_cache = None
        try:
            elements = self.elements()
        except PUAError as error:
            if error.code != "phone_locked":
                raise
            return {"ready": False, "state": "locked", "device": self.core.udid,
                    "reason": "The accessibility tree is unavailable while the iPhone is locked.",
                    "action_executed": False,
                    "next_tool": "pua_unlock" if self.passcode else "unlock the iPhone by hand",
                    "recovery": {"next_tool": "pua_ready", "arguments": {"screenshot": screenshot}}}
        observation = {"app": self._app, "viewport": self.viewport(),
                       "nodes": [element["node"] for element in elements[:100]],
                       "total_nodes": len(elements), "truncated": len(elements) > 100}
        if screenshot:
            observation["image"] = self.capture(self.viewport())
        return {"ready": True, "state": "ready",
                "device": self.core.udid, "backend": self.backend,
                "proof": {"coredevice": True, "axe": True, "foreground_app": self._app,
                          "display": [info.get("point_width"), info.get("point_height")],
                          "screenshot": bool(screenshot)},
                "observation": observation}

    def batch(self, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            fail("invalid_argument", "steps must contain 1..20 operations.")
        allowed = {"tap": self.tap, "swipe": self.swipe, "type_text": self.type_text,
                   "launch_app": self.launch_app, "press_button": self.press_button,
                   "wait": self.wait, "observe": self.observe}
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"op", "args"} or step.get("op") not in allowed \
                    or not isinstance(step.get("args", {}), dict):
                fail("invalid_argument", "Each batch step needs a supported op and an args object.")
        results = []
        self._deadline = time.monotonic() + self.call_budget
        try:
            for index, step in enumerate(steps):
                if index and time.monotonic() >= self._deadline:
                    return {"completed_steps": len(results), "stopped_at": index, "stop_reason": "time_budget",
                            "results": results, "complete": False,
                            "next_step": "Send the steps from stopped_at onward in a new call. "
                                         "Completed steps were executed and must not be repeated."}
                try:
                    results.append(allowed[step["op"]](**step.get("args", {})))
                except PUAError as error:
                    return {"completed_steps": len(results), "stopped_at": index, "stop_reason": error.code,
                            "error": error.as_dict(), "results": results, "complete": False}
            return {"completed_steps": len(results), "results": results, "complete": True}
        finally:
            self._deadline = None


def _timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jpeg_size(data):
    """Read JPEG SOF dimensions from a frame the helper already encoded."""
    from screen import jpeg_dimensions
    return jpeg_dimensions(data) or (None, None)
