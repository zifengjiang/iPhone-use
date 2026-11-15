#!/usr/bin/env python3
"""MCP stdio entrypoint for PUA (Phone Use Agent).

Two channels drive the phone. The CoreDevice helper owns the display, touch,
keyboard, hardware buttons and app launches; AXe owns the accessibility tree and
selector taps. Both are local processes this server starts and owns.
"""
import argparse
import base64
import fcntl
import json
import math
import os
import queue
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from apps import AppCatalog
from axe_client import AxeClient
from controller import CALL_BUDGET, MODES, DeviceController, selector_spec
from coredevice_client import CoreDeviceClient
from errors import PUAError, fail
from screen import ScreenHub
from setup import SetupManager, state_directory

VERSION = "0.3.6"
SCREEN_URI = f"ui://iphone-use/phone-{VERSION}.html"
# Codex scopes reuse to the host, chat, server and UI resource. A stable result
# ID keeps repeated READY/open/pause/resume calls in that chat on one panel,
# including after the MCP process reconnects; no device identifiers are needed.
SCREEN_SESSION_ID = "iphone-use-screen"
SCREEN_META = {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}, "prefersBorder": False},
               "openai/ui": {"availableDisplayModes": ["fullscreen"], "preferredDisplayMode": "fullscreen"}}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Seconds PUA may wait for animations to end before a post-action tree read.
SETTLE_SECONDS = 0.8


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def string(description="", max_length=1000, enum=None):
    result = {"type": "string", "minLength": 1, "maxLength": max_length}
    if description:
        result["description"] = description
    if enum:
        result["enum"] = enum
    return result


def num(low, high, kind="number"):
    return {"type": kind, "minimum": low, "maximum": high}


BOOL = {"type": "boolean"}
SEL = {
    "type": "object",
    "description": "Match one element on screen. Exact fields, at least one; the first match wins unless index is set.",
    "properties": {
        "label": string("Exact accessibility label.", max_length=200),
        "label_contains": string("Substring of the accessibility label.", max_length=200),
        "name": string("Accessibility identifier (AXUniqueId).", max_length=200),
        "value": string("Exact value, such as the text inside a field.", max_length=300),
        "type": string("Element type, such as Button or StaticText.", max_length=40),
        "enabled": {"oneOf": [{"type": "boolean"}, {"type": "string", "enum": ["true", "false"]}]},
        "index": num(0, 199, "integer"),
    },
    "minProperties": 1,
    "additionalProperties": False,
    "examples": [{"label": "返回", "type": "Button"}],
}
OBS = {"type": "string", "enum": list(MODES), "default": "none",
       "description": "Read the phone after acting: none, tree (cheap), screenshot (pixels) or both."}
EXPECT = {"expect": SEL, "observe": OBS}
REGION = obj({
    "x": num(0, 10000), "y": num(0, 10000),
    "width": num(1, 10000), "height": num(1, 10000),
}, ("x", "y", "width", "height"))
REGION["description"] = "Rectangle in iPhone points to swipe inside; defaults to the middle of the screen."

SCHEMAS = {
    "observe": obj({
        "mode": {"type": "string", "enum": ["tree", "screenshot", "both"], "default": "tree"},
        "include_invisible": {**BOOL, "description": "Keep elements the system reports as hidden."},
        "max_nodes": num(1, 500, "integer"),
    }),
    "find": obj({"selector": SEL, "limit": num(1, 50, "integer")}),
    "tap": obj({
        "selector": SEL, "x": num(0, 10000), "y": num(0, 10000),
        "observation_id": string("Observation the point came from; rejects a changed screen.", max_length=64),
        **EXPECT,
    }),
    "swipe": obj({
        "direction": string(enum=["up", "down", "left", "right"]),
        "region": REGION,
        "observation_id": string("Observation the gesture belongs to.", max_length=64),
        **EXPECT,
    }),
    "type_text": obj({
        "text": string("Text for the focused field, in any script: newline and tab included.", max_length=500),
        "allow_newlines": {**BOOL, "description": "Required when text contains a newline."},
        "observe": OBS,
    }, ("text",)),
    "press_button": obj({
        "name": string(enum=["home", "volumeup", "volumedown"]),
        **EXPECT,
    }, ("name",)),
    "launch_app": obj({
        "bundle_id": string("Verified bundle ID, such as com.tencent.xin.", max_length=200),
        **EXPECT,
    }, ("bundle_id",)),
    "wait": obj({"selector": SEL, "timeout_seconds": num(0.1, 30)}, ("selector",)),
    "apps": obj({
        "query": string("App name, alias or bundle ID.", max_length=100),
        "country": string("Two-letter store country.", max_length=2),
        "source": string(enum=["auto", "catalog", "installed", "apple"]),
        "limit": num(1, 30, "integer"),
    }, ("query",)),
    "doctor": obj({}),
    "ready": obj({
        "device": string("CoreDevice UDID; switches the session to that phone.", max_length=64),
        "screenshot": {**BOOL, "description": "Include the first screenshot with the proof."},
    }),
    "metrics": obj({"reset": BOOL}),
    "setup": obj({
        "action": string(enum=["discover", "configure", "status"]),
        "udid": string("CoreDevice UDID reported by discover.", max_length=64),
    }, ("action",)),
    "unlock": obj({
        "passcode": string("Digits; defaults to IPHONE_USE_UNLOCK_PASSCODE.", max_length=32),
        "interval": num(0.05, 2),
    }),
    "screen": obj({"action": string(enum=["open", "pause", "resume"])}),
    "screen_frame": obj({"after_seq": num(0, 1000000000, "integer"),
                         "last_event_id": num(0, 1000000000, "integer")}),
    "screen_action": obj({"action": string(enum=["refresh", "home", "screenshot"])}, ("action",)),
}
SCHEMAS["observe"]["properties"]["mode"]["description"] = "tree lists elements, screenshot returns pixels, both does both."
BATCH_OPS = ("tap", "swipe", "type_text", "launch_app", "press_button", "wait", "observe")
SCHEMAS["batch"] = obj({"steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object",
    "oneOf": [obj({"op": {"const": name}, "args": SCHEMAS[name]}, ("op", "args")) for name in BATCH_OPS]}}},
    ("steps",))
SCHEMAS = {name: SCHEMAS[name] for name in ("observe", "find", "tap", "swipe", "type_text", "press_button",
                                            "launch_app", "wait", "batch", "apps", "doctor", "ready", "metrics",
                                            "setup", "unlock", "screen", "screen_frame", "screen_action")}

DESCRIPTIONS = {
    "doctor": ("Check the USB phone, the CoreDevice helper and AXe before doing anything else. Returns the UDID, "
               "iOS version, display size, helper paths and the next step when something is missing."),
    "setup": ("Discover the connected iPhone, pin one UDID for this state directory, or show the current status. "
              "configure writes the UDID to config.json so later sessions and the preview use it."),
    "ready": ("Prove both channels before a task: CoreDevice reports the display geometry, AXe returns the "
              "accessibility tree, and the foreground app is resolved. Returns the first screenshot. If the phone "
              "is locked it says so and points at pua_unlock instead of failing."),
    "observe": ("Read the phone: the accessibility tree, a screenshot, or both. Every observation records a viewport "
                "and an observation_id that later taps and swipes can check against."),
    "find": ("List elements matching a selector without tapping. Use it when a label is uncertain; each element "
             "carries its tap point, so a coordinate tap is one call away."),
    "tap": ("Tap an element by selector or by iPhone points from a screenshot. Selectors are resolved against the "
            "AXe tree; a selector tap uses AXe directly when it is unambiguous, otherwise the element centre. Pass "
            "expect to prove the result, observe to see it."),
    "swipe": ("Swipe once up, down, left or right inside an optional region. The gesture goes through CoreDevice "
              "HID; pass expect or observe when the new screen matters."),
    "type_text": ("Type into the field that already has focus; tap the field first. Any script, newline and tab "
                  "only, up to 500 characters. CoreDevice cannot clear a field or read it back, so verify the result "
                  "with pua_observe."),
    "press_button": ("Press Home, volume up or volume down through CoreDevice. Locking the phone is deliberately not "
                     "offered: pua_unlock would have to type into the lock screen to undo it."),
    "launch_app": ("Launch an installed app by bundle ID with CoreDevice. The app must already be installed; use "
                   "pua_apps to find the ID."),
    "wait": ("Wait until a selector exists on screen. A present element does not prove the business result, only "
             "that the page arrived."),
    "batch": ("Run up to 20 steps in order with one shared time budget. A failing step stops the batch; completed "
              "steps are reported and must not be repeated."),
    "apps": ("Look up an app name, alias or bundle ID. Sources: the curated catalog, the apps installed on the "
             "phone, both, or the Apple store search."),
    "metrics": ("Call counts, latency and response sizes for this session. Device actions the phone accepted are "
                "counted separately from tool calls."),
    "unlock": ("Type the configured passcode, but only after AXe proves the phone is locked; on an unlocked phone "
               "this is a no-op. The phone must have a passcode set."),
    "screen": ("Open, pause or resume the live phone panel. Pausing keeps the stream off while the phone stays "
               "usable."),
    "screen_frame": ("Preview App only: the latest frame, gesture events and viewport, with long-poll semantics."),
    "screen_action": ("Preview App only: refresh the stream, press Home, or copy a screenshot to the Mac clipboard."),
}
APP_TOOLS = ("screen_frame", "screen_action")
READS = {"doctor", "ready", "observe", "find", "wait", "metrics", "apps", "screen", "screen_frame"}


def undocumented(value):
    """Drop prose from a schema; the tool description already covers the shape."""
    if isinstance(value, dict):
        return {key: undocumented(item) for key, item in value.items()
                if key not in ("description", "examples", "$comment")}
    if isinstance(value, list):
        return [undocumented(item) for item in value]
    return value


SEL_BRIEF = {key: value for key, value in SEL.items() if key != "description"}
OBS_BRIEF = {key: value for key, value in OBS.items() if key != "description"}


def published_schema(name):
    """Compact the wire schema: repeated prose lives in the tool description instead."""
    schema = SCHEMAS[name]
    if name == "find":
        return undocumented(schema)
    published = undocumented(schema)
    if name == "batch":
        return published
    definitions = {}
    if name in ("tap", "swipe", "press_button", "launch_app", "wait"):
        definitions["selector"] = SEL_BRIEF
    if name in ("tap", "swipe", "press_button", "launch_app", "type_text"):
        definitions["observe"] = OBS_BRIEF
    properties = published["properties"]
    if "selector" in properties:
        properties["selector"] = {"$ref": "#/$defs/selector"}
    if "expect" in properties:
        properties["expect"] = {"$ref": "#/$defs/selector"}
    if properties.get("observe") == undocumented(OBS):
        properties["observe"] = {"$ref": "#/$defs/observe"}
    if definitions:
        published["$defs"] = definitions
    return published


TOOLS = [{
    "name": "pua_" + name,
    "title": "Pua " + name.replace("_", " "),
    "description": DESCRIPTIONS[name],
    "inputSchema": published_schema(name),
    "annotations": {"readOnlyHint": name in READS, "destructiveHint": name not in READS,
                    "idempotentHint": name in READS, "openWorldHint": False},
} for name in SCHEMAS]
for _tool in TOOLS:
    if _tool["name"] in ("pua_ready", "pua_screen"):
        _tool["_meta"] = {"ui": {"resourceUri": SCREEN_URI}}
    if _tool["name"] == "pua_screen":
        _tool["title"] = "手机屏幕"
        _tool["_meta"] = {**_tool["_meta"], "openai/ui": {"entrypoints": [{"type": "thread"}]}}
    if _tool["name"][4:] in APP_TOOLS:
        _tool["_meta"] = {"ui": {"visibility": ["app"]}}


def validate(value, schema, path="arguments"):
    if "oneOf" in schema:
        if isinstance(value, dict) and "op" in value:
            # Surface the chosen batch step's precise argument error instead
            # of hiding it behind the union of unrelated operation schemas.
            chosen = [candidate for candidate in schema["oneOf"]
                      if candidate.get("properties", {}).get("op", {}).get("const") == value["op"]]
            if len(chosen) == 1:
                return validate(value, chosen[0], path)
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate(value, candidate, path)
                matches += 1
            except PUAError:
                pass
        if matches != 1:
            raise PUAError("invalid_argument", f"{path} must match exactly one allowed operation schema.")
        return
    kind = schema.get("type")
    valid = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
             "string": lambda: isinstance(value, str), "boolean": lambda: isinstance(value, bool),
             "number": lambda: not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value),
             "integer": lambda: not isinstance(value, bool) and isinstance(value, int)}
    if kind and not valid[kind]():
        raise PUAError("invalid_argument", f"{path} must be {kind}.")
    if "const" in schema and value != schema["const"]:
        raise PUAError("invalid_argument", f"Invalid {path} operation.")
    if "enum" in schema and value not in schema["enum"]:
        raise PUAError("invalid_argument", f"Invalid {path} option.")
    if kind == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise PUAError("invalid_argument",
                           f"Unknown fields in {path}: {', '.join(sorted(set(value) - set(props)))}. "
                           "Use only the declared fields.",
                           details={"action_executed": False, "argument_path": path,
                                    "unknown_fields": sorted(set(value) - set(props)),
                                    "allowed_fields": sorted(props),
                                    "recovery": {"next_step": "Correct these fields using the current tool "
                                                                "schema, then call once; no device action was executed."}})
        if set(schema.get("required", [])) - set(value):
            raise PUAError("invalid_argument", f"Missing required fields in {path}.")
        if len(value) < schema.get("minProperties", 0):
            raise PUAError("invalid_argument", f"{path} cannot be empty.")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], path + "." + key)
    if kind in ("number", "integer") and not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
        raise PUAError("invalid_argument", f"{path} is out of range.")
    if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
        raise PUAError("invalid_argument", f"{path} has invalid length.")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100000):
            raise PUAError("invalid_argument", f"{path} has invalid number of items.")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")


def validate_semantics(name, args):
    """Constraints JSON Schema cannot express, checked before the phone is touched."""
    def selector_field(key):
        if args.get(key) is not None:
            selector_spec(args[key], "arguments." + key)

    if name in ("find", "wait"):
        selector_field("selector")
    if name in ("tap", "swipe", "press_button", "launch_app"):
        selector_field("expect")
    if name == "tap":
        selector_field("selector")
        has_selector = args.get("selector") is not None
        has_point = args.get("x") is not None and args.get("y") is not None
        if (args.get("x") is None) != (args.get("y") is None):
            fail("invalid_argument", "x and y go together.")
        if has_selector == has_point:
            fail("invalid_argument", "Give either a selector or an x/y point.")
    if name == "type_text":
        text = args.get("text") or ""
        if not args.get("allow_newlines") and ("\n" in text or "\r" in text):
            fail("invalid_argument", "Newlines submit or start a new line; pass allow_newlines=true to confirm.")
        if "\r" in text:
            fail("invalid_argument", "Use \n for a new line.")
    if name == "type_text":
        text = args.get("text") or ""
        if not args.get("allow_newlines") and ("\n" in text or "\r" in text):
            fail("invalid_argument", "Newlines submit or start a new line; pass allow_newlines=true to confirm.")
        if "\r" in text:
            fail("invalid_argument", "Use \n for a new line.")
    if name == "batch":
        for index, step in enumerate(args["steps"]):
            validate_semantics(step["op"], step.get("args", {}))
    if name == "ready" and args.get("device"):
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", args["device"]):
            fail("invalid_argument", "device must be a CoreDevice UDID.")
    if name == "setup" and args["action"] == "configure":
        if not args.get("udid"):
            fail("invalid_argument", "configure needs the udid that discover reported.")
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", args["udid"]):
            fail("invalid_argument", "udid must be a CoreDevice UDID.")


def copy_png(directory, data):
    """Put a PNG on the macOS clipboard; the file exists only for the duration of the copy."""
    fd, name = tempfile.mkstemp(prefix=".clipboard-", suffix=".png", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        # The path travels as an argument, never inside the script text.
        done = subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                               "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)",
                               "-e", "end run", name],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=8, check=False)
        if done.returncode != 0:
            raise PUAError("clipboard_unavailable", "The screenshot was captured but macOS did not accept it on the clipboard.")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PUAError("clipboard_unavailable", "The screenshot could not be copied to the clipboard.") from exc
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def setting(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, low), high) if math.isfinite(value) else default


class Runtime:
    """One MCP session: device state, preview hub, catalog and call accounting."""

    backend = "coredevice+axe"

    def __init__(self, state_dir=None, core=None, axe=None):
        self.state_dir = state_directory(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.core = core or CoreDeviceClient(state_dir=self.state_dir)
        self.axe = axe or AxeClient()
        self.phone = DeviceController(self.core, self.axe, self.state_dir)
        self.setup_manager = SetupManager(self.state_dir, core=self.core, axe=self.axe)
        self.apps = AppCatalog(self.state_dir, self.setup_manager)
        self.screen = ScreenHub(self.state_dir)
        self.phone.screen = self.screen
        self.phone.settle_seconds = setting("IPHONE_USE_SETTLE_SECONDS", SETTLE_SECONDS, 0.0, 5.0)
        self.phone.call_budget = setting("IPHONE_USE_CALL_BUDGET", CALL_BUDGET, 5, 300)
        config = self.setup_manager.config
        udid = config.get("udid")
        if udid and not self.core.udid:
            self.core.set_udid(udid)
        self.name_device(config)
        self.responses = deque(maxlen=500)
        self._replied_at = None
        self._lock = None

    def name_device(self, config):
        """The panel header shows a model only when a previous setup discovered one."""
        if config.get("model"):
            self.screen.device = {"model": f'{config["model"]} · iOS {config.get("ios_version", "?")}'[:60]}

    def retarget(self, udid, device=None):
        self.core.set_udid(udid)
        self.phone.reset()
        self.setup_manager.save(udid=udid)
        if device:
            self.setup_manager.save(model=device.get("model"), ios_version=device.get("ios_version"))
            self.name_device(self.setup_manager.config)

    # -- dispatch ---------------------------------------------------------
    def call(self, name, args=None):
        args = dict(args or {})
        op = name[4:] if isinstance(name, str) and name.startswith("pua_") else name
        if op not in SCHEMAS:
            raise PUAError("unknown_tool", "Unknown PUA tool.")
        validate(args, SCHEMAS[op])
        validate_semantics(op, args)
        started = time.monotonic()
        error = None
        activity = None
        try:
            if op not in ("doctor", "setup", "metrics", "apps", "screen", "screen_frame", "screen_action"):
                activity = self.screen.begin(op)
            if op == "doctor":
                return self.doctor()
            if op == "setup":
                return self.setup_action(**args)
            if op == "metrics":
                data = self.metrics()
                if args.get("reset"):
                    self.reset_metrics()
                return data
            if op == "apps":
                return self.apps.lookup(**args)
            if op == "screen":
                return self.screen_control(args.get("action", "open"))
            if op == "screen_frame":
                return self.screen.frame(**args)
            if op == "screen_action":
                return self.screen_action(args["action"])
            self.acquire()
            try:
                if op == "ready":
                    return self.ready(**args)
                return getattr(self.phone, op)(**args)
            finally:
                self.release()
        except PUAError as exc:
            error = exc.code
            if exc.code == "phone_locked":
                try:
                    self.screen.set_paused(True, "device_locked")
                except Exception:
                    pass
            if op in READS:
                exc.details.setdefault("action_executed", False)
            raise
        finally:
            if activity is not None:
                try:
                    self.screen.end(activity)
                except Exception:
                    pass
            self.phone.tool_records.append({"tool": "pua_" + op,
                                            "seconds": round(time.monotonic() - started, 4),
                                            "error": error})

    def acquire(self):
        """One phone operation at a time, across processes."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.state_dir / "operation.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise PUAError("device_busy", "Another iPhone Use operation is running; wait for it to finish.",
                           details={"action_executed": False})
        self._lock = handle

    def release(self):
        if self._lock is None:
            return
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock.close()
            self._lock = None

    def doctor(self):
        data = self.setup_manager.status()
        data["backend"] = self.backend
        data["preview"] = {"paused": self.screen.paused(), **self.screen.pause_status()}
        return data

    def setup_action(self, action, udid=None):
        if action == "discover":
            return self.setup_manager.discover()
        if action == "configure":
            report = self.setup_manager.discover()
            device = next((item for item in report.get("devices", []) if item["udid"] == udid), None)
            if device is None:
                fail("no_such_device", "udid was not in the discover report; run pua_setup discover again.")
            self.retarget(udid, device)
            return {"ok": True, "udid": udid, "device": device, "config": self.setup_manager.config}
        return self.setup_manager.status()

    def ready(self, device=None, screenshot=True):
        if device:
            self.retarget(device)
        try:
            self.screen.start()
        except Exception:
            pass
        data = self.phone.ready(screenshot=screenshot)
        if data.get("ready"):
            try:
                self.screen.resume_after_unlock(self.screen.locked_pause_id(), explicit=True)
            except Exception:
                pass
            try:
                self.screen.set_paused(False)
                self.screen.set_viewport(data["observation"]["viewport"])
            except Exception:
                pass
        else:
            try:
                self.screen.set_paused(True, "device_locked")
            except Exception:
                pass
        return data

    def screen_control(self, action):
        if action == "pause":
            self.screen.set_paused(True, "authentication")
        elif action == "resume":
            self.screen.set_paused(False)
        else:
            self.screen.start()
        return self.screen.frame()

    def screen_action(self, action):
        if action == "home":
            self.acquire()
            try:
                self.phone.core.call("button", name="home")
                self.phone.external_action()
            finally:
                self.release()
            return {"ok": True, "action": "home", **self.screen.frame()}
        if action == "screenshot":
            record = self.phone.capture(self.phone.viewport())
            copy_png(self.state_dir, Path(record["path"]).read_bytes())
            return {"ok": True, "action": "screenshot", "copied": True,
                    "width": record["width"], "height": record["height"]}
        pause_id = self.screen.reconnect_pause_id()
        self.screen.restart()
        try:
            probe = self.phone.ready(screenshot=False)
        except PUAError as error:
            self.screen.set_paused(True, "device_locked" if error.code == "phone_locked" else "authentication")
            return {"ok": False, "action": "refresh", "service_ready": False,
                    "error": error.as_dict(), **self.screen.frame()}
        if probe.get("ready"):
            self.screen.resume_after_unlock(pause_id, explicit=True)
            self.screen.set_viewport(probe["observation"]["viewport"])
        else:
            self.screen.set_paused(True, "device_locked")
        return {"ok": bool(probe.get("ready")), "action": "refresh", "service_ready": bool(probe.get("ready")),
                "state": probe.get("state"), **self.screen.frame()}

    # -- accounting -------------------------------------------------------
    def metrics(self):
        records = list(self.phone.tool_records)
        responses = list(self.responses)

        def grouped(items, fields):
            groups = {}
            for item in items:
                entry = groups.setdefault(item["tool"], {"count": 0, **{field: 0 for field in fields}})
                entry["count"] += 1
                for field in fields:
                    entry[field] = round(entry[field] + (item[field] or 0), 4)
            return groups

        waits = [item["wait_seconds"] for item in responses if item["wait_seconds"] is not None]
        return {
            "device": {"backend": self.phone.backend, "udid": self.core.udid,
                       "accepted_actions": self.phone.accepted_actions,
                       "observations": len(self.phone.snapshots)},
            "tools": {"count": len(records), "seconds": round(sum(item["seconds"] for item in records), 4),
                      "errors": sum(bool(item["error"]) for item in records),
                      "by_tool": grouped([{**item, "errors": int(bool(item["error"]))} for item in records],
                                         ("seconds", "errors"))},
            "responses": {"count": len(responses),
                          "text_bytes": sum(item["text_bytes"] for item in responses),
                          "image_bytes": sum(item["image_bytes"] for item in responses),
                          "by_tool": grouped(responses, ("text_bytes", "image_bytes"))},
            "rounds": {"count": len(responses), "waits": len(waits),
                       "wait_seconds": round(sum(waits), 3),
                       "median_wait_seconds": round(statistics.median(waits), 3) if waits else None,
                       "max_wait_seconds": round(max(waits), 3) if waits else None,
                       "scope": "Time from one tool response to the next tool request: host, model and user time "
                                "together. A long wait may be the user, not the model."},
            "latency_scope": "Tool time and response sizes are measured here. The wait between calls is observed, "
                             "not controlled; this plugin cannot split it into model, host and user time.",
        }

    def reset_metrics(self):
        self.phone.tool_records.clear()
        self.responses.clear()
        self._replied_at = None

    def note_response(self, name, result, arrived):
        """Record what one model-facing response cost and how long the previous one waited for it."""
        if name == "pua_screen_frame":
            return
        content = result.get("content", [])
        self.responses.append({"tool": name if isinstance(name, str) else "invalid",
                               "text_bytes": sum(len(item["text"].encode()) for item in content
                                                 if item.get("type") == "text"),
                               "image_bytes": sum(len(item["data"]) for item in content
                                                  if item.get("type") == "image"),
                               "wait_seconds": None if self._replied_at is None
                               else max(0.0, arrived - self._replied_at)})

    def replied(self):
        self._replied_at = time.monotonic()

    def close(self):
        self.release()
        try:
            self.screen.close()
        finally:
            self.core.close()


INSTRUCTIONS = """\
PUA (Phone Use Agent) drives one USB-connected iPhone from this Mac.

Two local channels: CoreDevice serves the display, touch, keyboard, hardware buttons, \
unlocking and app launches; AXe serves the accessibility tree and selector taps. Both \
are processes this server starts itself.

Start
1. pua_doctor — proves the USB phone, the CoreDevice helper and AXe. Read next_steps when anything fails.
2. pua_setup {action:"discover"} then {action:"configure", udid} when no phone is pinned yet.
3. pua_ready — returns the UDID, display size, foreground app and a screenshot. If it reports \
ready:false with state:"locked", call pua_unlock or ask the user to unlock.

Work loop
- pua_observe {mode:"both"} shows the screen and the tree. Prefer it over guessing.
- pua_find lists live elements for a selector: label, label_contains, name, value, type, enabled, index.
- pua_tap {selector} taps an element. When several match, add type/value/index; when the tree is empty, \
tap {x,y} points taken from the screenshot. Screenshot image pixels map to points with pixel_to_point.
- pua_swipe once per call; pua_wait proves a selector appeared; pass expect on any mutation to verify.
- pua_type_text writes into the focused field only: tap the field first. Any script plus newline \
and tab, at most 500 characters.
- pua_press_button and pua_launch_app go through CoreDevice; pua_unlock types the passcode only after \
AXe proved the phone is locked.

Rules
- An error with uncertain:true means the phone may have acted. Observe before retrying; never replay a tap \
blindly, and never repeat a batch step that already completed.
- Selectors are matched locally against the AXe tree. Anything outside the listed fields is rejected, and a \
selector that showed nothing is answered with the current screenshot instead of a guess.
- Secure text fields and passcodes are not readable; screenshots of them are the only evidence.
- Keep one observation_id from observe to tap when the screen could change in between.
- The live panel (pua_screen) mirrors the same CoreDevice stream the screenshots come from."""


def result_content(data, structured=False):
    """One compact JSON text block, plus the screenshot when the result carries one.

    structuredContent is left out on purpose: a host that receives it may give the model
    only that object and drop every content block, including the image.
    """
    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False, allow_nan=False,
                                                   separators=(",", ":"))}]
    record = data.get("image") or data.get("observation", {}).get("image")
    if not record and isinstance(data.get("error"), dict):
        record = data["error"].get("observation", {}).get("image")
    if not record and data.get("results"):
        last = data["results"][-1]
        record = last.get("image") or last.get("observation", {}).get("image")
    if record and Path(record["path"]).is_file():
        content.append({"type": "image",
                        "data": base64.b64encode(Path(record["path"]).read_bytes()).decode(),
                        "mimeType": record.get("mimeType", "image/png")})
    result = {"content": content, "isError": bool(data.get("error"))}
    if structured:
        result["structuredContent"] = data
    return result


def tool_result(runtime, params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
        data = runtime.call(name, arguments)
    except PUAError as exc:
        return result_content({"error": exc.as_dict()})
    except Exception as exc:  # noqa: BLE001 - the host must get a structured error, not a traceback
        return result_content({"error": {"code": "internal_error", "message": str(exc), "uncertain": True,
                                         "action_executed": False}})
    if name == "pua_screen_frame":
        return {"content": [], "structuredContent": data, "isError": False}
    result = result_content(data, structured=name in ("pua_screen", "pua_screen_action"))
    if name in ("pua_ready", "pua_screen"):
        result["_meta"] = {"openai/widgetSessionId": SCREEN_SESSION_ID}
    return result


def serve(runtime):
    """Read requests on this thread; run phone tools in order on one worker.

    The preview App polls frames several times a second. Answering those, pings and the
    catalog here keeps the preview live and the host responsive while a long phone
    operation is still running. Phone tools stay strictly sequential.
    """
    writing = threading.Lock()

    def send(response):
        line = json.dumps(response, ensure_ascii=False, allow_nan=False)
        with writing:
            print(line, flush=True)

    jobs = queue.Queue()

    def work():
        while True:
            job = jobs.get()
            if job is None:
                return
            ident, params, arrived = job
            result = tool_result(runtime, params)
            runtime.note_response(params.get("name"), result, arrived)
            send({"jsonrpc": "2.0", "id": ident, "result": result})
            runtime.replied()

    worker = threading.Thread(target=work, name="pua-tools", daemon=True)
    worker.start()
    try:
        for line in sys.stdin:
            request = None
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("Request exceeds 1 MiB")
                request = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                        or not isinstance(request.get("method"), str)):
                    raise ValueError("Invalid request")
                if "id" not in request:
                    continue
                ident = request["id"]
                if isinstance(ident, bool) or not isinstance(ident, (str, int)):
                    raise ValueError("Invalid identifier")
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise PUAError("invalid_argument", "params must be an object.")
                method = request["method"]
                if method == "initialize":
                    offered = params.get("protocolVersion")
                    result = {"protocolVersion": offered if offered in PROTOCOLS else PROTOCOLS[0],
                              "capabilities": {"tools": {"listChanged": False},
                                               "resources": {"listChanged": False}},
                              "serverInfo": {"name": "iphone-use", "version": VERSION},
                              "instructions": INSTRUCTIONS}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "resources/list":
                    result = {"resources": [{"uri": SCREEN_URI, "name": "iPhone Use Screen", "title": "手机屏幕",
                                             "mimeType": "text/html;profile=mcp-app", "_meta": SCREEN_META}]}
                elif method == "resources/read":
                    if params.get("uri") != SCREEN_URI:
                        raise PUAError("invalid_argument", "Unknown screen resource URI.")
                    html = (Path(__file__).resolve().parents[1] / "assets/phone-screen.html").read_text()
                    result = {"contents": [{"uri": SCREEN_URI, "mimeType": "text/html;profile=mcp-app",
                                            "text": html, "_meta": SCREEN_META}]}
                elif method == "tools/call":
                    arrived = time.monotonic()
                    if params.get("name") not in ("pua_screen_frame", "pua_screen", "pua_screen_action"):
                        jobs.put((ident, params, arrived))
                        continue
                    # Preview polls, its toolbar and opening or pausing the panel never wait behind a phone operation.
                    result = tool_result(runtime, params)
                    if params.get("name") == "pua_screen":
                        runtime.note_response("pua_screen", result, arrived)
                        runtime.replied()
                else:
                    send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
                    continue
                response = {"jsonrpc": "2.0", "id": ident, "result": result}
            except PUAError as exc:
                response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                            "error": {"code": -32602, "message": str(exc)}}
            except (ValueError, TypeError, KeyError):
                response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                            "error": {"code": -32700 if request is None else -32600,
                                      "message": "Parse error" if request is None else "Invalid request"}}
            send(response)
    finally:
        # Finish the phone operation already accepted, then stop: its outcome must be reported.
        jobs.put(None)
        worker.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    runtime = Runtime(args.state_dir)
    try:
        if args.doctor or args.ready:
            try:
                data = runtime.call("pua_doctor" if args.doctor else "pua_ready", {})
            except PUAError as exc:
                data = {"error": exc.as_dict()}
            print(json.dumps(data, ensure_ascii=False))
            failed = "error" in data or (args.doctor and not data.get("ok")) \
                or (args.ready and data.get("ready") is not True)
            return 1 if failed else 0
        serve(runtime)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.exit(main())
