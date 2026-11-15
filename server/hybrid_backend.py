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
import time

import wda_image
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
_BUTTONS = {"home": "home", "volumeup": "volumeup", "volumedown": "volumedown"}


class HybridController(IctrlController):
    """iCTRL source plus CoreDevice reach, behind one controller."""

    backend = "hybrid"

    def __init__(self, client, core, state_dir):
        super().__init__(client, state_dir)
        self.core = core
        self.passcode = (os.environ.get("IPHONE_USE_UNLOCK_PASSCODE") or "").strip()
        self._point_size = None
        self._app_cache = None

    # -- coordinates ------------------------------------------------------
    def viewport(self, max_age=0):
        try:
            viewport = super().viewport(max_age)
        except WDAError as error:
            if error.code not in _RUNNER_DOWN or not self._point_size:
                raise
            # The Runner is gone; keep the geometry the phone already proved so a
            # CoreDevice screenshot still reports the point scale it was taken at.
            width, height = self._point_size
            return {"width": width, "height": height, "units": "iPhone points"}
        self._point_size = (viewport["width"], viewport["height"])
        return viewport

    def active_app(self):
        """The Runner answers this by reading the whole source: ~0.9 s, so keep it briefly.

        Actions clear the cache (see after()), which is the only thing that can move
        the foreground app; two observations in a row reuse one read.
        """
        now = time.monotonic()
        if self._app_cache is not None and now - self._app_cache[1] < APP_CACHE_SECONDS:
            return self._app_cache[0]
        try:
            app = super().active_app()
        except WDAError as error:
            if error.code not in _RUNNER_DOWN or not self.core.available():
                raise
            # CoreDevice exposes no foreground-app query; a screenshot is still worth
            # taking, so the observation says the app is unknown instead of failing.
            app = "unknown"
        self._app_cache = (app, now)
        return app

    def after(self, expect=None, observe="none", max_nodes=100):
        self._app_cache = None  # An action may have moved the foreground app.
        return super().after(expect=expect, observe=observe, max_nodes=max_nodes)

    def point_size(self):
        if self._point_size is None:
            self.viewport(VIEWPORT_TTL)
        return self._point_size

    def _fraction(self, x, y):
        width, height = self.point_size()
        if not (0 <= x <= width and 0 <= y <= height):
            raise WDAError("invalid_argument", f"Point ({x}, {y}) is outside the {width}x{height} point viewport.")
        return x / width, y / height

    # -- screenshots ------------------------------------------------------
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
        try:
            return self.client.rpc("is_locked").get("locked")
        except WDAError:
            return None

    def unlock(self, passcode=None, interval=0.45):
        code = (passcode or self.passcode or "").strip()
        if not code:
            unsupported("Set IPHONE_USE_UNLOCK_PASSCODE or pass the passcode to unlock.")
        if not code.isdigit():
            raise WDAError("invalid_argument", "The passcode must be digits.")
        if not self.core.available():
            raise WDAError("coredevice_unavailable",
                           "Unlocking needs the CoreDevice helper; set IPHONE_USE_COREDEVICE_PYTHON.")
        reply = self.core.call("unlock", passcode=code, interval=float(interval))
        locked = self.lock_state()
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

    def run(self, op, args):
        if op == "ready":
            result = super().run(op, args)
            if isinstance(result, dict):
                result["backend"] = "hybrid"
                result["coredevice"] = self.core.available()
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
            unsupported("The hybrid backend supports observation, coordinate taps, swipes, Home and volume buttons, "
                        "app launch, type text, unlock and lockstate.")
        if args.get("selector") is not None or args.get("expect") is not None:
            unsupported("Use a coordinate tap from a fresh screenshot. Selectors and postconditions need WDA.")
        if op == "swipe" and args.get("verify"):
            unsupported("Use observe after the swipe. Tree-based swipe verification needs WDA.")
        if args.get("include_invisible") or args.get("expensive_visibility"):
            unsupported("The hybrid backend returns only the Runner's visible source.")

# Tools the hybrid backend exposes: the iCTRL pilot surface plus what CoreDevice adds.
HYBRID_MCP_TOOLS = {"pua_setup", "pua_doctor", "pua_ready", "pua_observe", "pua_batch", "pua_screen",
                    "pua_screen_action", "pua_unlock", "pua_type_text"} | {f"pua_{action}" for action in SUPPORTED_ACTIONS}
