"""CoreDevice helper for the iPhone Use hybrid backend.

Runs under an interpreter that has ``pymobiledevice3`` installed and keeps one
RemoteServiceDiscovery tunnel, one HID media stream and one screenshot session
open for the life of the process. The CLI pays ~3 s of tunnel setup per call;
this helper pays it once.

    IPHONE_USE_COREDEVICE_PYTHON=~/.local/share/iphone-use-coredevice/venv/bin/python

Protocol: one JSON object per line on stdin, one per line on stdout.

    {"id": 1, "op": "tap", "args": {"x": 0.5, "y": 0.9}}
    {"id": 1, "ok": true, "result": {"duration_ms": 41}}

Coordinates are normalised 0..1 over the display; the HID wire wants 0..65535.
Every operation replies, including failures, so the caller never guesses.
"""
import asyncio
import base64
import json
import struct
import sys
import time

IDLE_STREAM_SECONDS = 90.0
CAPTURE_TIMEOUT = 4.0
# Mirror wda_image: the model gets 768 px on the short side and 1568 px on the long one.
MODEL_SHORT_SIDE = 768
MODEL_LONG_SIDE = 1568
JPEG_QUALITY = 80
# The passcode keypad is laid out per device width; these fractions come from
# measured taps on a 1206x2622 iPhone and hold for the 0.75/0.5/0.25 columns.
KEYPAD = {
    "1": (0.25, 0.3988), "2": (0.50, 0.3988), "3": (0.75, 0.3988),
    "4": (0.25, 0.5112), "5": (0.50, 0.5112), "6": (0.75, 0.5112),
    "7": (0.25, 0.6237), "8": (0.50, 0.6237), "9": (0.75, 0.6237),
    "0": (0.50, 0.7188), "delete": (0.75, 0.8750),
}
BUTTONS = {
    "home": (0x0C, 0x40), "lock": (0x0C, 0x30), "siri": (0x0C, 0xCF),
    "volumeup": (0x0C, 0xE9), "volumedown": (0x0C, 0xEA), "mute": (0x0C, 0xE2),
}
NAMED_KEYS = {"backspace": "backspace", "enter": "enter", "return": "enter", "tab": "tab",
              "escape": "escape", "left": "left", "right": "right", "up": "up", "down": "down"}


def fit_size(width, height):
    short, long_side = sorted((width, height))
    scale = min(1.0, MODEL_SHORT_SIDE / short, MODEL_LONG_SIDE / long_side)
    return max(1, round(width * scale)), max(1, round(height * scale))


def model_jpeg(data):
    """Downscale to the model limits here, where the pixels already are.

    sips on the Mac costs ~0.9 s per 1206x2622 frame; Pillow needs ~0.13 s. Returns
    None when Pillow is unavailable so the caller can fall back to the raw PNG.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(data)) as opened:
        width, height = opened.size
        target = fit_size(width, height)
        frame = opened if target == (width, height) else opened.resize(target, Image.LANCZOS)
        buffer = io.BytesIO()
        frame.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return {"jpeg_b64": base64.b64encode(buffer.getvalue()).decode(),
            "width": width, "height": height,
            "encoded_width": target[0], "encoded_height": target[1]}


def png_size(data):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


class Device:
    def __init__(self, udid=None):
        self.udid = udid
        self.rsd = None
        self.hid = None
        self.hid_cm = None
        self.size = None
        self.last_hid_use = 0.0
        self.last_capture_use = 0.0

    async def tunnel(self):
        if self.rsd is not None:
            return self.rsd
        from pymobiledevice3.remote.native_tunnel import establish_native_rsd
        self.rsd = await establish_native_rsd(self.udid)
        return self.rsd

    async def stream(self):
        """A live media stream is the auth gate for HID; keep it warm while acting."""
        if self.hid is not None:
            self.last_hid_use = time.monotonic()
            return self.hid
        from pymobiledevice3.remote.core_device.hid_service import touch_session
        rsd = await self.tunnel()
        self.hid_cm = touch_session(rsd)
        self.hid = await self.hid_cm.__aenter__()
        await asyncio.sleep(0.15)
        self.last_hid_use = time.monotonic()
        return self.hid

    async def drop_stream(self):
        stream, self.hid = self.hid, None
        context, self.hid_cm = self.hid_cm, None
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                pass
        return stream

    async def shot(self):
        """One frame on a session of its own.

        A ScreenCaptureService answers once: the device ends the display session
        with the frame, and a reused client blocks until the transport times out.
        A fresh session costs ~10 ms, so every capture opens one and a failure
        rebuilds the tunnel before the single retry.
        """
        from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
        error = None
        for attempt in (1, 2):
            if attempt == 2:
                self.rsd = None  # a tunnel that outlived the display session is suspect
            rsd = await self.tunnel()
            try:
                async with ScreenCaptureService(rsd) as session:
                    reply = await asyncio.wait_for(session.capture_screenshot(), timeout=CAPTURE_TIMEOUT)
            except Exception as failure:  # noqa: BLE001 - one retry, then surfaced
                error = failure
                continue
            data = reply["image"]
            self.size = png_size(data) or self.size
            self.last_capture_use = time.monotonic()
            return data
        raise RuntimeError(f"screenshot failed twice: {type(error).__name__}: {error}") from error

    async def tap(self, x, y, hold=0.05):
        from pymobiledevice3.remote.core_device.hid_service import TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE
        service = await self.stream()
        px, py = int(round(x * 65535)), int(round(y * 65535))
        await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, px, py)
        await asyncio.sleep(hold)
        await service.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, px, py)
        self.last_hid_use = time.monotonic()

    async def swipe(self, fx, fy, tx, ty, duration=0.4, steps=12):
        from pymobiledevice3.remote.core_device.hid_service import TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE
        service = await self.stream()
        await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, int(round(fx * 65535)), int(round(fy * 65535)))
        step = duration / max(steps, 1)
        for index in range(1, steps + 1):
            await asyncio.sleep(step)
            await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT,
                                           int(round((fx + (tx - fx) * index / steps) * 65535)),
                                           int(round((fy + (ty - fy) * index / steps) * 65535)))
        await service.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, int(round(tx * 65535)), int(round(ty * 65535)))
        self.last_hid_use = time.monotonic()

    async def write(self, text, char_delay=0.04):
        from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID, KEY_LEFT_SHIFT
        service = await self.stream()
        keyboard = await service.create_keyboard_service()
        for char in text:
            mapping = ASCII_TO_HID.get(char)
            if mapping is None:
                raise ValueError(f"unsupported character: {char!r}")
            usage, needs_shift = mapping
            if needs_shift:
                # iOS drops a Shift that arrives in the same report as the key.
                await service.send_keyboard(keyboard, (KEY_LEFT_SHIFT,))
                await asyncio.sleep(char_delay)
            await service.send_keyboard(keyboard, (KEY_LEFT_SHIFT, usage) if needs_shift else (usage,))
            await asyncio.sleep(char_delay)
            await service.send_keyboard(keyboard, ())
            await asyncio.sleep(char_delay / 2)
        self.last_hid_use = time.monotonic()

    async def key(self, name):
        from pymobiledevice3.remote.core_device import hid_service as keys
        usage = getattr(keys, "KEY_" + NAMED_KEYS[name].upper())
        service = await self.stream()
        keyboard = await service.create_keyboard_service()
        await service.send_keyboard(keyboard, (usage,))
        await asyncio.sleep(0.05)
        await service.send_keyboard(keyboard, ())
        self.last_hid_use = time.monotonic()

    async def button(self, name):
        from pymobiledevice3.remote.core_device.hid_service import HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP, IndigoHIDService
        page, code = BUTTONS[name]
        rsd = await self.tunnel()
        async with IndigoHIDService(rsd) as service:
            await service.send_button(page, code, HID_BUTTON_STATE_DOWN)
            await asyncio.sleep(0.05)
            await service.send_button(page, code, HID_BUTTON_STATE_UP)
            await asyncio.sleep(0.1)

    async def launch(self, bundle_id, terminate_existing=True):
        from pymobiledevice3.remote.core_device.app_service import AppServiceService
        rsd = await self.tunnel()
        async with AppServiceService(rsd) as service:
            reply = await service.launch_application(bundle_id, kill_existing=terminate_existing)
        return {"processes": len(reply) if isinstance(reply, list) else 0}

    async def idle_reaper(self):
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            if self.hid is not None and now - self.last_hid_use > IDLE_STREAM_SECONDS:
                await self.drop_stream()


async def run(device, op, args):
    if op == "info":
        await device.tunnel()
        if device.size is None:
            await device.shot()
        width, height = device.size or (0, 0)
        return {"udid": device.udid, "pixel_width": width, "pixel_height": height, "transport": "coredevice"}
    if op == "screenshot":
        started = time.monotonic()
        data = await device.shot()
        width, height = device.size or (0, 0)
        scaled = model_jpeg(data)
        if scaled is not None:
            return {**scaled, "duration_ms": round((time.monotonic() - started) * 1000)}
        return {"png_b64": base64.b64encode(data).decode(), "width": width, "height": height,
                "duration_ms": round((time.monotonic() - started) * 1000)}
    if op == "tap":
        started = time.monotonic()
        await device.tap(args["x"], args["y"], args.get("hold", 0.05))
        return {"duration_ms": round((time.monotonic() - started) * 1000)}
    if op == "tap_many":
        started = time.monotonic()
        for x, y in args["points"]:
            await device.tap(x, y, args.get("hold", 0.05))
            await asyncio.sleep(args.get("interval", 0.4))
        return {"duration_ms": round((time.monotonic() - started) * 1000), "taps": len(args["points"])}
    if op == "swipe":
        started = time.monotonic()
        await device.swipe(args["from"][0], args["from"][1], args["to"][0], args["to"][1], args.get("duration", 0.4))
        return {"duration_ms": round((time.monotonic() - started) * 1000)}
    if op == "type":
        started = time.monotonic()
        await device.write(args["text"], args.get("char_delay", 0.04))
        return {"duration_ms": round((time.monotonic() - started) * 1000)}
    if op == "key":
        await device.key(args["name"])
        return {"key": args["name"]}
    if op == "button":
        await device.button(args["name"])
        return {"button": args["name"]}
    if op == "launch":
        return await device.launch(args["bundle_id"], args.get("terminate_existing", True))
    if op == "unlock":
        started = time.monotonic()
        await device.tap(0.5, 0.4)                       # wake the display
        await asyncio.sleep(0.4)
        await device.swipe(0.5, 0.90, 0.5, 0.45, 0.35)   # reveal the passcode pad
        await asyncio.sleep(1.6)                         # keypad animation
        for digit in args["passcode"]:
            x, y = KEYPAD[digit]
            await device.tap(x, y, 0.05)
            await asyncio.sleep(args.get("interval", 0.45))
        await asyncio.sleep(0.8)
        return {"duration_ms": round((time.monotonic() - started) * 1000), "digits": len(args["passcode"])}
    raise ValueError(f"unknown op: {op}")


async def main():
    import os
    device = Device(os.environ.get("IPHONE_USE_COREDEVICE_UDID") or None)
    reaper = asyncio.create_task(device.idle_reaper())
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
            except ValueError:
                continue
            identifier = request.get("id")
            op = request.get("op")
            args = request.get("args") or {}
            if op == "quit":
                sys.stdout.write(json.dumps({"id": identifier, "ok": True, "result": {}}) + "\n")
                sys.stdout.flush()
                break
            try:
                reply = {"id": identifier, "ok": True, "result": await run(device, op, args)}
            except Exception as error:  # noqa: BLE001 - every failure must reach the caller
                reply = {"id": identifier, "ok": False,
                         "error": {"code": type(error).__name__, "message": str(error)[:400]}}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    finally:
        reaper.cancel()
        await device.drop_stream()


if __name__ == "__main__":
    asyncio.run(main())
