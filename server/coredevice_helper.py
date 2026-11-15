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
import os
import struct
import sys
import time

IDLE_STREAM_SECONDS = 90.0
CAPTURE_TIMEOUT = 4.0
# Mirror image.py: the model gets 768 px on the short side and 1568 px on the long one.
MODEL_SHORT_SIDE = 768
MODEL_LONG_SIDE = 1568
JPEG_QUALITY = 80
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


def similar_frames(first, second, threshold=6.0):
    """Mean absolute difference of two downscaled greyscale frames.

    Two frames of the same lock screen land near 0; an unlocked phone differs by
    tens of levels because the pad is gone.
    """
    try:
        from io import BytesIO
        from PIL import Image, ImageChops, ImageStat
        small = [Image.open(BytesIO(data)).convert("L").resize((32, 32)) for data in (first, second)]
        return ImageStat.Stat(ImageChops.difference(*small)).mean[0] <= threshold
    except Exception:  # noqa: BLE001 - Pillow missing must not break the unlock attempt
        return False


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
        self.points_cache = None
        self.keyboard_id = None
        self.last_hid_use = 0.0
        self.last_capture_use = 0.0

    async def resolve(self):
        """Without a configured UDID, only a single USB device is unambiguous.

        Network devices are invisible to usbmuxd and remotepairingd hands out whichever
        device it likes when asked without a serial, so guessing is not an option.
        """
        if self.udid:
            return self.udid
        from pymobiledevice3.usbmux import list_devices
        connected = [device for device in await list_devices() if getattr(device, "is_usb", False)]
        if len(connected) == 1:
            self.udid = getattr(connected[0], "serial", None) or getattr(connected[0], "udid", None)
            return self.udid
        names = ", ".join(str(getattr(device, "serial", "")) for device in connected) or "none"
        raise RuntimeError(f"IPHONE_USE_COREDEVICE_UDID is not set and {len(connected)} USB device(s) are attached ({names}). "
                           "Pass device=<udid> or set the variable.")

    async def tunnel(self):
        if self.rsd is not None:
            return self.rsd
        # A userspace tunnel keeps its own socket stack, so Apple's remoted keeps its
        # RSD connection and Xcode-side services (which AXe needs) stay reachable.
        udid = await self.resolve()
        from pymobiledevice3.remote.userspace_tunnel import establish_userspace_rsd
        try:
            self.rsd = await establish_userspace_rsd(udid)
        except Exception:
            # A Wi-Fi-only device is not on usbmux, so the pairing daemon's tunnel is the way in.
            from pymobiledevice3.remote.native_tunnel import establish_native_rsd
            self.rsd = await establish_native_rsd(udid)
        return self.rsd

    async def points(self):
        """UI size in points: nativeSize divided by the UI scale the display reports."""
        if self.points_cache is not None:
            return self.points_cache
        from pymobiledevice3.remote.core_device.device_info import DeviceInfoService
        width, height = self.size or (0, 0)
        try:
            async with DeviceInfoService(await self.tunnel()) as service:
                info = await service.get_display_info()
            display = next((item for item in info.get("displays", []) if item.get("primary")), None) or {}
            native = display.get("nativeSize") or [width, height]
            scale = (display.get("currentMode") or {}).get("preferredUIScale") or 3
            self.points_cache = (round(native[0] / scale), round(native[1] / scale))
        except Exception:  # noqa: BLE001 - the pixel size still gives a usable fallback
            self.points_cache = (round(width / 3), round(height / 3)) if width else (0, 0)
        return self.points_cache

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
        self.keyboard_id = None
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

    async def unlock(self, passcode, interval=0.25, attempts=2):
        """Wake, open the passcode pad, type, and prove it worked from the frame.

        Keystrokes over the network transport occasionally drop (measured 4 of 6),
        and the pad screenshot is the only signal the phone gives without a Runner:
        if the frame still looks like the pad, delete and type again.
        """
        started = time.monotonic()
        await self.tap(0.5, 0.4)
        await asyncio.sleep(0.8)
        await self.swipe(0.5, 0.99, 0.5, 0.60, 0.45, steps=16)
        await asyncio.sleep(2.6)
        pad = await self.shot()
        unlocked = False
        for attempt in range(1, attempts + 1):
            await self.write(passcode, char_delay=interval)
            await asyncio.sleep(1.2)
            if not similar_frames(pad, await self.shot()):
                unlocked = True
                break
            for _ in range(len(passcode)):
                await self.key("backspace")
                await asyncio.sleep(0.12)
            await asyncio.sleep(0.8)
        return {"duration_ms": round((time.monotonic() - started) * 1000), "digits": len(passcode),
                "attempts": attempt, "unlocked": unlocked}

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

    async def keyboard(self):
        """Register the virtual keyboard once per stream; reports address that id."""
        service = await self.stream()
        if self.keyboard_id is None:
            self.keyboard_id = await service.create_keyboard_service()
        return service, self.keyboard_id

    async def write(self, text, char_delay=0.04):
        from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID, KEY_LEFT_SHIFT
        service, keyboard = await self.keyboard()
        await service.send_keyboard(keyboard, ())  # a bare report settles dtuhidd before the first key
        await asyncio.sleep(max(char_delay, 0.2))
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
        service, keyboard = await self.keyboard()
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
        points = await device.points()
        return {"udid": device.udid, "pixel_width": width, "pixel_height": height,
                "point_width": points[0], "point_height": points[1], "transport": "coredevice"}
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
        return await device.unlock(args["passcode"], interval=args.get("interval", 0.45),
                                   attempts=int(args.get("attempts", 2)))
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


class _MjpegTap:
    """Emits the device's own video stream as multipart JPEG on stdout.

    The device's encoder only sends keyframes while the host acknowledges frames, and
    VncStreamServer already answers that (RCTL) and decodes HEVC, so the tap only turns
    its BGRA frames into JPEG.
    """

    def __init__(self, rsd, fps=10, quality=60, scale=0.5):
        from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer
        tap = self
        self._fps = max(1, min(int(fps), 30))
        self._quality = max(1, min(int(quality), 100))
        self._scale = max(0.1, min(float(scale), 1.0))
        self._last = 0.0
        self._count = 0

        class Server(VncStreamServer):
            def _broadcast_frame(self, bgra, width, height, rotation):
                super()._broadcast_frame(bgra, width, height, rotation)
                tap.emit(bgra, width, height)

        self._server = Server(rsd, port=0)

    def emit(self, bgra, width, height):
        import io, resource, time
        from PIL import Image
        now = time.monotonic()
        if now - self._last < 1.0 / self._fps:
            return
        self._last = now
        if now - getattr(self, "_checked", 0.0) > 0.5:
            self._checked = now
            import gc
            # Decoded BGRA frames (~14 MB each) form reference cycles that only a
            # full collection reclaims; without this the decoder's pool grows into
            # the gigabytes while the stream runs.
            gc.collect()
            limit = float(os.environ.get("IPHONE_USE_STREAM_MAX_MB", "600"))
            used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
            if used > limit:
                # The decoder in pymobiledevice3 keeps growing while nobody consumes
                # its clients, so the stream is recycled by the parent instead.
                sys.stderr.write(f"stream memory {used:.0f}MB over {limit:.0f}MB, recycling\n")
                sys.stderr.flush()
                os._exit(75)
        image = Image.frombuffer("RGBA", (width, height), bgra, "raw", "BGRA", 0, 1)
        if self._scale < 1.0:
            image = image.resize((max(1, int(width * self._scale)), max(1, int(height * self._scale))),
                                 Image.BILINEAR)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=self._quality)
        data = buffer.getvalue()
        out = sys.stdout.buffer
        out.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode()
                  + b"\r\n\r\n" + data + b"\r\n")
        out.flush()
        self._count += 1

    async def serve(self):
        await self._server.serve()


async def stream_forever(udid, fps, quality, scale):
    from PIL import Image  # noqa: F401 - fail fast when the encoder is missing
    device = Device(udid)
    rsd = await device.tunnel()
    tap = _MjpegTap(rsd, fps=fps, quality=quality, scale=scale)
    sys.stderr.write(f"streaming {rsd.product_type} at {tap._fps}fps\n")
    sys.stderr.flush()
    await tap.serve()


def _stream_options(argv):
    values = {"udid": None, "fps": 10, "quality": 60, "scale": 0.5}
    index = 0
    while index < len(argv):
        name = argv[index]
        if name in ("--fps", "--quality", "--scale", "--udid") and index + 1 < len(argv):
            key = name[2:]
            values[key] = argv[index + 1] if key == "udid" else float(argv[index + 1])
            index += 2
        else:
            index += 1
    return values


if __name__ == "__main__":
    if "--stream" in sys.argv:
        options = _stream_options(sys.argv)
        asyncio.run(stream_forever(options["udid"], options["fps"], int(options["quality"]), options["scale"]))
    else:
        asyncio.run(main())
