"""Hybrid backend: iCTRL for source and gestures, CoreDevice for speed and lock screens.

Each backend covers the other's blind spot:

* iCTRL alone needs the Runner awake and cannot see a locked phone.
* CoreDevice alone has no element source and pays for a media stream per gesture.

So the source tree and normal gestures stay on iCTRL, screenshots prefer
CoreDevice (0.07 s against 0.37 s over USB), and taps, swipes, app launches and
type text fall back to CoreDevice when the Runner is unreachable. Unlocking
needs CoreDevice outright: iCTRL can only report that the phone is locked.
"""
import base64
import os
import re
import time

import wda_image
from axe_client import TREE_LIMIT, AxeClient
from ictrl_backend import IctrlController, SUPPORTED_ACTIONS, unsupported
from wda_client import WDAError
from wda_controller import VIEWPORT_TTL

# iCTRL error codes that mean "this backend cannot act now", not "the action failed".
_RUNNER_DOWN = {"ictrl_unreachable", "ictrl_rpc_failed", "ictrl_invalid_response"}
_FALLBACK_OPS = {"tap", "swipe", "press_button", "launch_app"}
# Warm CoreDevice HID answers a tap in ~50 ms; the Runner needs ~1.1 s for the same
# tap. Gestures therefore prefer CoreDevice and keep the Runner for the tree.
_CORE_FIRST = {"tap", "swipe"}
# The Runner reads the source tree to name the foreground app; reuse it across
# observations that no action separates.
APP_CACHE_SECONDS = 2.0
_BUTTONS = {"home": "home", "volumeup": "volumeup", "volumedown": "volumedown", "lock": "lock"}


class HybridController(IctrlController):
    """iCTRL source plus CoreDevice reach, behind one controller."""

    backend = "hybrid"

    def __init__(self, client, core, state_dir, axe=None):
        super().__init__(client, state_dir)
        self.core = core
        self.axe = axe if axe is not None else AxeClient()
        self.passcode = (os.environ.get("IPHONE_USE_UNLOCK_PASSCODE") or "").strip()
        self._point_size = None
        self._app_cache = None
        self._ictrl_trusted = True

    # -- coordinates ------------------------------------------------------
    def viewport(self, max_age=0):
        """Points come from the phone that is actually being driven.

        CoreDevice reports the display's native size and UI scale, so a tap stays correct
        even when the Runner is answering for a different phone.
        """
        width, height = self.point_size()
        if width and height:
            return {"width": width, "height": height, "units": "iPhone points"}
        raise WDAError("coredevice_unavailable", "The viewport is unavailable: no Runner and no display info.")

    def point_size(self):
        if self._point_size is not None:
            return self._point_size
        if not self._ictrl_trusted and self.core.available():
            info = self.core.call("info")
            size = (info.get("point_width") or 0, info.get("point_height") or 0)
            if all(size):
                self._point_size = size
                return self._point_size
        try:
            viewport = super().viewport(VIEWPORT_TTL)
        except WDAError as error:
            if error.code not in _RUNNER_DOWN | {"ictrl_device_required", "ictrl_device_ambiguous"} \
                    or not self.core.available():
                raise
            info = self.core.call("info")
            size = (info.get("point_width") or 0, info.get("point_height") or 0)
            if all(size):
                self._point_size = size
                return self._point_size
            raise WDAError("coredevice_unavailable", "The viewport is unknown: the Runner is gone and CoreDevice "
                                                     "did not report a display size.") from error
        self._point_size = (viewport["width"], viewport["height"])
        return self._point_size

    def _fraction(self, x, y):
        width, height = self.point_size()
        if not (0 <= x <= width and 0 <= y <= height):
            raise WDAError("invalid_argument", f"Point ({x}, {y}) is outside the {width}x{height} point viewport.")
        return x / width, y / height

    # -- screenshots ------------------------------------------------------
    def set_device(self, udid, ictrl_device=None):
        """Retarget the device for the rest of the session; no restart, no env change.

        The iCTRL Runner answers for the phone it runs on. When only CoreDevice moves,
        the Runner's tree and lock state describe a different phone, so they stop being
        trusted until ``ictrl_device`` names a Runner on the new target.
        """
        previous = self.core.udid
        self.core.set_udid(udid)
        if ictrl_device:
            self.client.target = ictrl_device
            self.client.close()
            self._ictrl_trusted = True
        elif self.core.udid != previous:
            self._ictrl_trusted = False
        self._point_size = None
        self._app_cache = None
        return self.core.udid

    def capture(self, viewport):
        """CoreDevice answers a USB screenshot in ~0.07 s; iCTRL needs ~0.37 s for the same frame.

        The helper hands back a JPEG it already bounded, so the Mac skips the 0.9 s
        sips pass; a helper that only produced a PNG still works.
        """
        if not self.core.available():
            return super().capture(viewport)
        try:
            reply = self.core.call("screenshot")
        except WDAError:
            return super().capture(viewport)
        native = (reply.get("width"), reply.get("height"))
        try:
            if reply.get("jpeg_b64"):
                data = base64.b64decode(reply["jpeg_b64"], validate=True)
                return self.store_jpeg(data, reply.get("encoded_width"), reply.get("encoded_height"), viewport, native=native)
            data = base64.b64decode(reply["png_b64"], validate=True)
        except (KeyError, ValueError, TypeError):
            return super().capture(viewport)
        if not data.startswith(wda_image.PNG_SIGNATURE):
            return super().capture(viewport)
        return self.store_png(data, viewport)

    # -- lock screen ------------------------------------------------------
    def lock_state(self):
        """True/False, or None when neither backend can answer."""
        if not self._ictrl_trusted:
            return None  # Only the Runner can answer; ask the wrong phone and the answer is a lie.
        try:
            return self.client.rpc("is_locked").get("locked")
        except WDAError:
            return None

    def unlock(self, passcode=None, interval=0.25):
        code = (passcode or self.passcode or "").strip()
        if not code:
            unsupported("Set IPHONE_USE_UNLOCK_PASSCODE or pass the passcode to unlock.")
        if not code.isdigit():
            raise WDAError("invalid_argument", "The passcode must be digits.")
        if not self.core.available():
            raise WDAError("coredevice_unavailable",
                           "Unlocking needs the CoreDevice helper; set IPHONE_USE_COREDEVICE_PYTHON.")
        if self.lock_state() is False:
            # Typing a passcode into an already unlocked phone lands on whatever is
            # on screen, so report the state instead of gesture-driving the device.
            return {"locked": False, "unlocked": False, "duration_ms": 0, "backend": "coredevice"}
        reply = self.core.call("unlock", passcode=code, interval=float(interval))
        locked = self.lock_state()
        if locked is None:
            # No Runner on this phone: the helper proved it from the frame instead.
            unlocked = bool(reply.get("unlocked"))
            return {"locked": not unlocked, "unlocked": unlocked, "verified": "screenshot",
                    "attempts": reply.get("attempts"), "duration_ms": reply.get("duration_ms"),
                    "backend": "coredevice"}
        return {"locked": locked, "unlocked": locked is False, "duration_ms": reply.get("duration_ms"),
                "backend": "coredevice"}

    def ensure_unlocked(self):
        state = self.lock_state()
        if state is False:
            return
        if self.passcode and self.core.available():
            try:
                self.unlock()
            except WDAError:
                pass
            else:
                if self.lock_state() is False:
                    return
        if state is None:
            if self.core.available():
                # Neither backend can report the lock state. Refusing here would take
                # away the only channel that can still act, so CoreDevice proceeds and
                # the action itself reports verification_deferred.
                return
            raise WDAError("ictrl_unreachable",
                           "The iCTRL Runner is unreachable and the phone could not be unlocked through CoreDevice.")
        raise WDAError("phone_locked", "Unlock the iPhone yourself and verify READY again.")

    # -- actions ----------------------------------------------------------
    def type_text(self, text, **options):
        if not self.core.available():
            unsupported("Typing needs the CoreDevice helper; set IPHONE_USE_COREDEVICE_PYTHON.")
        if any(character not in "\n\t" and not 32 <= ord(character) < 127 for character in text):
            raise WDAError("invalid_argument", "CoreDevice typing covers printable ASCII, newline and tab.")
        reply = self.core.call("type", text=text)
        return {"typed": len(text), "duration_ms": reply.get("duration_ms"), "backend": "coredevice"}

    def fallback(self, op, args):
        if op == "tap":
            x, y = self._fraction(args["x"], args["y"])
            reply = self.core.call("tap", x=x, y=y)
            return {"tapped": {"x": args["x"], "y": args["y"]}, "backend": "coredevice",
                    "duration_ms": reply.get("duration_ms")}
        if op == "swipe":
            width, height = self.point_size()
            direction = args.get("direction", "up")
            middle = (width / 2, height / 2)
            reach = min(width, height) * 0.35
            offset = {"up": (0, reach), "down": (0, -reach), "left": (reach, 0), "right": (-reach, 0)}[direction]
            start = (middle[0] - offset[0], middle[1] - offset[1])
            end = (middle[0] + offset[0], middle[1] + offset[1])
            reply = self.core.call("swipe", **{"from": [start[0] / width, start[1] / height],
                                               "to": [end[0] / width, end[1] / height]})
            return {"swiped": direction, "backend": "coredevice", "duration_ms": reply.get("duration_ms")}
        if op == "press_button":
            name = _BUTTONS.get(args["name"])
            if name is None:
                unsupported("CoreDevice exposes the Home and volume buttons.")
            self.core.call("button", name=name)
            return {"pressed": args["name"], "backend": "coredevice"}
        if op == "launch_app":
            self.core.call("launch", bundle_id=args["bundle_id"])
            return {"launched": args["bundle_id"], "backend": "coredevice"}
        unsupported("This operation has no CoreDevice equivalent.")

    @staticmethod
    def axe_nodes(nodes):
        """Flatten the AXe tree, tolerant of a wrapper object instead of a list."""
        root = nodes if isinstance(nodes, list) else [nodes]
        flat = []
        stack = list(reversed(root))
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            flat.append(node)
            stack.extend(reversed([child for child in node.get("children") or [] if isinstance(child, dict)]))
        return flat

    @staticmethod
    def frame_center(frame):
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", str(frame or ""))]
        if len(numbers) < 4:
            return None
        x, y, width, height = numbers[:4]
        return round(x + width / 2, 1), round(y + height / 2, 1)

    def axe_matches(self, simple):
        matches = []
        for node in self.axe_nodes(self.axe.describe_ui(self.core.udid)):
            if simple.get("label") and node.get("AXLabel") != simple["label"]:
                continue
            if simple.get("name") and node.get("AXUniqueId") != simple["name"]:
                continue
            if simple.get("value") and node.get("AXValue") != simple["value"]:
                continue
            if simple.get("type") and str(node.get("role") or "").removeprefix("AX") != simple["type"]:
                continue
            matches.append(node)
        return matches

    @staticmethod
    def axe_selector(selector):
        """Translate the simple selector forms AXe can resolve on the device."""
        selector = selector or {}
        simple = {key: selector[key] for key in ("label", "name", "value", "type") if selector.get(key)}
        if not simple or set(selector) - set(simple) - {"index", "enabled"}:
            unsupported("AXe resolves label, name (accessibility identifier), value and type; use coordinates for "
                        "label_contains, predicates, index and enabled filters.")
        if selector.get("enabled") is not None and str(selector["enabled"]).lower() not in ("true", "1"):
            unsupported("AXe does not filter disabled elements; read the tree and pick an enabled one.")
        return simple

    def tap(self, selector=None, x=None, y=None, observation_id=None, expect=None, observe="none"):
        """A selector goes to AXe, which refuses a target that moved or got covered."""
        if selector is None:
            return super().tap(x=x, y=y, observation_id=observation_id, expect=expect, observe=observe)
        if expect:
            unsupported("Postconditions need WDA; observe after the action instead.")
        if not self.axe.available():
            raise WDAError("axe_unavailable", "A selector tap needs AXe; install it or pass x/y from a fresh screenshot.")
        simple = self.axe_selector(selector)
        self.ensure_unlocked()
        started = time.monotonic()
        channel = "axe"
        try:
            self.axe.tap(self.core.udid, identifier=simple.get("name"), label=simple.get("label"),
                         value=simple.get("value"), element_type=simple.get("type"))
        except WDAError as error:
            # Overlapping accessibility branches make AXe refuse a selector that resolves
            # to one element. Read the tree, keep a unique match and tap its centre.
            if error.code != "axe_ambiguous_target":
                raise
            matches = self.axe_matches(simple)
            points = [(node, self.frame_center(node.get("AXFrame"))) for node in matches]
            points = [(node, point) for node, point in points if point]
            if len(points) != 1:
                raise WDAError("ambiguous_target",
                               f"{len(points)} elements match this selector; add type or value, "
                               "or tap a point from the tree.",
                               details={"candidates": [{"label": node.get("AXLabel"),
                                                        "name": node.get("AXUniqueId"),
                                                        "type": node.get("role"), "point": point}
                                                       for node, point in points[:8]]}) from error
            point = points[0][1]
            self.fallback("tap", {"x": point[0], "y": point[1]})
            channel = "axe+hid"
        result = self.after(observe=observe)
        return {"tapped": {"selector": simple}, "backend": channel,
                "duration_ms": round((time.monotonic() - started) * 1000), **result}

    def run(self, op, args):
        if op == "ready":
            result = super().run(op, args)
            if isinstance(result, dict):
                result["backend"] = "hybrid"
                result["coredevice"] = self.core.available()
                if self.core.udid:
                    result["device"] = self.core.udid
            return result
        if op == "unlock":
            return self.unlock(**args)
        if op == "type_text" and not args.get("selector") and args.get("text"):
            return self.type_text(args["text"])
        if op == "press_button" and self.core.available():
            # A hardware key needs no Runner session and no source tree.
            return self.fallback(op, args)
        gesture = (op == "tap" and "x" in args) or op == "swipe"
        if op in _CORE_FIRST and gesture and self.core.available() and args.get("selector") is None:
            self.check_operation(op, args)
            self.ensure_unlocked()
            try:
                return self.fallback(op, args)
            except WDAError as error:
                # An uncertain action must never be replayed on a second channel.
                if error.uncertain or error.code in ("invalid_argument", "ictrl_unsupported"):
                    raise
        if op in _FALLBACK_OPS:
            try:
                return super().run(op, args)
            except WDAError as error:
                # An uncertain action must never be replayed on a second channel.
                if error.code not in _RUNNER_DOWN or getattr(error, "uncertain", False) \
                        or not self.core.available():
                    raise
                try:
                    return self.fallback(op, args)
                except WDAError as fallback_error:
                    if fallback_error.code == "coredevice_unavailable":
                        raise error from fallback_error
                    raise
        return super().run(op, args)

    @staticmethod
    def check_operation(op, args):
        if op not in SUPPORTED_ACTIONS | {"observe", "type_text"}:
            unsupported("The hybrid backend supports observation, taps by selector or coordinate, swipes, Home and "
                        "volume buttons, app launch, type text, unlock and lockstate.")
        if args.get("selector") is not None and op != "tap":
            unsupported("Selectors are available for taps only; read the tree and use coordinates elsewhere.")
        if args.get("expect") is not None:
            unsupported("Postconditions need WDA; observe after the action instead.")
        if op == "swipe" and args.get("verify"):
            unsupported("Use observe after the swipe. Tree-based swipe verification needs WDA.")
        if args.get("include_invisible") or args.get("expensive_visibility"):
            unsupported("The hybrid backend returns only the Runner's visible source.")

# Tools the hybrid backend exposes: the iCTRL pilot surface plus what CoreDevice adds.
HYBRID_MCP_TOOLS = {"pua_setup", "pua_doctor", "pua_ready", "pua_observe", "pua_batch", "pua_screen",
                    "pua_screen_action", "pua_unlock", "pua_type_text"} | {f"pua_{action}" for action in SUPPORTED_ACTIONS}
