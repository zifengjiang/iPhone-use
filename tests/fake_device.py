"""Deterministic stand-ins for the two device channels.

FakeCore speaks the helper's JSON-lines operations as if a phone answered;
FakeAxe returns a fixed accessibility tree. Both record what they were asked
to do so tests can assert on the exact device command.
"""
import base64

from errors import PUAError


def node(label="返回", value="", name="", role="AXButton", kind="Button", frame=(0, 0, 44, 44),
         enabled=True, hidden=False, children=None):
    x, y, width, height = frame
    return {"role": role, "type": kind, "AXLabel": label, "AXValue": value, "AXUniqueId": name,
            "AXFrame": f"{{{{{x}, {y}}}, {{{width}, {height}}}}}",
            "frame": {"x": x, "y": y, "width": width, "height": height},
            "enabled": enabled, "hidden": hidden, "children": children or []}


def tree(*children, app="SpringBoard"):
    return [{"role": "AXApplication", "type": "Application", "AXLabel": app, "AXValue": "",
             "AXUniqueId": "", "frame": {"x": 0, "y": 0, "width": 402, "height": 874},
             "enabled": True, "hidden": False, "children": list(children)}]


class FakeCore:
    """Answers CoreDevice helper calls and remembers them."""

    def __init__(self, udid="TEST-DEVICE-1234", point=(402, 874), pixel=(1206, 2622)):
        self.udid = udid
        self.point = point
        self.pixel = pixel
        self.calls = []
        self.closed = False
        self.failure = None
        self.after_unlock = None

    def available(self):
        return True

    def set_udid(self, udid):
        self.udid = udid
        return udid

    def close(self):
        self.closed = True

    def call(self, op, **arguments):
        self.calls.append((op, arguments))
        if self.failure is not None:
            raise self.failure
        if op == "info":
            return {"udid": self.udid, "point_width": self.point[0], "point_height": self.point[1],
                    "pixel_width": self.pixel[0], "pixel_height": self.pixel[1], "transport": "coredevice"}
        if op == "screenshot":
            frame = b"\xff\xd8\xff" + bytes(range(64)) + b"\xff\xd9"
            return {"jpeg_b64": base64.b64encode(frame).decode(), "width": self.pixel[0],
                    "height": self.pixel[1], "encoded_width": 600, "encoded_height": 1300,
                    "duration_ms": 12}
        if op == "tap":
            return {"duration_ms": 20}
        if op == "swipe":
            return {"duration_ms": 400}
        if op == "type":
            return {"duration_ms": len(arguments.get("text", "")), "typed": arguments.get("text", "")}
        if op == "button":
            return {"duration_ms": 5}
        if op == "launch":
            return {"duration_ms": 5, "bundle_id": arguments.get("bundle_id")}
        if op == "unlock":
            if self.after_unlock is not None:
                self.after_unlock()
            return {"unlocked": True, "attempts": 1, "duration_ms": 30}
        raise PUAError("coredevice_failed", f"unsupported fake operation {op}")

    def ops(self):
        return [op for op, _ in self.calls]


class FakeAxe:
    """Returns a fixed tree and records taps; can pretend to be locked."""

    def __init__(self, nodes=None):
        self.nodes = nodes if nodes is not None else tree()
        self.taps = []
        self.locked = False
        self.tap_error = None
        self.available_flag = True

    def available(self):
        return self.available_flag

    def close(self):
        pass

    def describe_ui(self, udid, point=None):
        if self.locked:
            raise PUAError("phone_locked", "The device is locked.", details={"action_executed": False})
        return self.nodes

    def tap(self, udid, identifier=None, label=None, value=None, element_type=None, timeout=20):
        self.taps.append({"udid": udid, "identifier": identifier, "label": label,
                          "value": value, "element_type": element_type, "timeout": timeout})
        if self.tap_error is not None:
            raise self.tap_error
        return {"duration_ms": 8}
