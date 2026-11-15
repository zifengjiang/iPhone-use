"""Local state, device discovery and readiness for the CoreDevice + AXe backend.

The server keeps one private directory: `config.json` names the target device, and
the helper log, artifacts and preview state live beside it. Discovery asks Xcode's
CoreDevice daemon, which is the same service the helpers talk to, so a device that
appears here is the device the server can drive.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from errors import PUAError

DEFAULT_STATE = Path.home() / ".local/share/iphone-use"


def state_directory(explicit=None):
    """Prefer the current identity while reusing an existing installation's state."""
    configured = explicit or os.environ.get("IPHONE_USE_STATE_DIR") or os.environ.get("WDA_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    current = Path.home() / ".local/share/iphone-use"
    previous = Path.home() / ".local/share/iphone-use-wda"
    if not current.exists() and (previous / "config.json").is_file():
        return previous
    return current


def _read_json(path, default=None):
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default
    return document if isinstance(document, type(default)) else default


def write_json(path, document):
    """Replace a state file atomically; a reader never sees half a config."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command, timeout=15):
    """Run a diagnostic command; never raise for a missing binary or a bad exit."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "stdout": "", "stderr": str(error)}
    return {"ok": completed.returncode == 0, "stdout": completed.stdout, "stderr": completed.stderr.strip()}


class SetupManager:
    """Device selection and the local prerequisites both helpers need."""

    def __init__(self, state_dir, core=None, axe=None):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.state_dir, 0o700)
        self.core = core
        self.axe = axe

    @property
    def path(self):
        return self.state_dir / "config.json"

    @property
    def config(self):
        document = _read_json(self.path, {})
        return document if isinstance(document, dict) else {}

    def save(self, **values):
        """Merge into the config; `udid=None` forgets the selection."""
        config = self.config
        for key, value in values.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        write_json(self.path, config)
        return config

    @staticmethod
    def parse_devices(document):
        """Support Xcode's legacy and current CoreDevice JSON formats."""
        devices = []
        for item in document.get("result", {}).get("devices", []):
            props = item.get("properties", {})
            hardware = props.get("hardware", item.get("hardwareProperties", {}))
            state = props.get("state", item.get("deviceProperties", {}))
            connection = props.get("connection", item.get("connectionProperties", {}))
            software = props.get("software", item.get("deviceProperties", {}))
            if hardware.get("reality") != "physical" or hardware.get("deviceType") not in {"iPhone", "iPad"}:
                continue
            version = software.get("osVersionNumber", "unknown")
            if isinstance(version, dict):
                version = version.get("stringValue", "unknown")
            developer = state.get("developerModeStatus", "unknown")
            if isinstance(developer, dict):
                developer = "enabled" if "enabled" in developer else "disabled" if "disabled" in developer else "unknown"
            devices.append({"udid": hardware.get("udid", item.get("identifier")),
                            "core_device_id": item.get("identifier"), "name": state.get("name", "iPhone"),
                            "model": hardware.get("marketingName"), "ios_version": version,
                            "pairing_state": connection.get("pairingState", "unknown"),
                            "connection_state": connection.get("state", connection.get("tunnelState", "unknown")),
                            "transport": connection.get("transportType", "unknown"),
                            "developer_mode": developer})
        return devices

    def discover(self):
        """Physical iPhones and iPads that Xcode can see, with the current selection."""
        descriptor, name = tempfile.mkstemp(prefix="devices-", suffix=".json", dir=self.state_dir)
        os.close(descriptor)
        try:
            probe = run(["xcrun", "devicectl", "list", "devices", "--json-output", name, "--timeout", "8"], timeout=12)
            document = _read_json(name, {})
            devices = self.parse_devices(document if isinstance(document, dict) else {})
            if not probe["ok"] or not devices:
                fallback = run(["xcrun", "xcdevice", "list", "--timeout", "5"], timeout=9)
                try:
                    devices = [{"udid": item["identifier"], "name": item.get("name"), "model": item.get("modelName"),
                                "ios_version": item.get("operatingSystemVersion"), "available": item.get("available"),
                                "pairing_state": "unknown", "transport": "unknown", "developer_mode": "unknown"}
                               for item in json.loads(fallback["stdout"] or "[]")
                               if not item.get("simulator") and item.get("platform") == "com.apple.platform.iphoneos"]
                except (ValueError, TypeError, KeyError):
                    pass
            selected = self.config.get("udid")
            for device in devices:
                device["selected"] = bool(selected) and device.get("udid") == selected
            return {"ok": bool(devices), "devices": devices, "selected_udid": selected,
                    "error": None if probe["ok"] else probe["stderr"] or "devicectl did not answer",
                    "next_steps": [] if devices else [
                        "Connect the iPhone by USB, unlock it, tap Trust and open Xcode > Devices and Simulators."]}
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)

    def prerequisites(self):
        """What each helper needs locally, without touching a device."""
        core = {"python": None, "available": False}
        if self.core is not None:
            core = {"python": getattr(self.core, "python", None), "available": bool(self.core.available())}
        axe = {"available": False}
        if self.axe is not None:
            axe = {"binary": getattr(self.axe, "binary", None), "available": bool(self.axe.available())}
        return {"coredevice": core, "axe": axe}

    def status(self):
        """A diagnosis a person can act on: selection, tooling and USB reach."""
        config = self.config
        udid = config.get("udid")
        report = {"ok": False, "state_dir": str(self.state_dir), "udid": udid,
                  "config": {key: config.get(key) for key in ("udid",) if key in config},
                  **self.prerequisites(), "next_steps": []}
        if not report["coredevice"]["available"]:
            report["next_steps"].append("CoreDevice needs python3 with pymobiledevice3; set IPHONE_USE_COREDEVICE_PYTHON.")
        if not report["axe"]["available"]:
            report["next_steps"].append("Element trees and selector taps need AXe; set IPHONE_USE_AXE or install it on PATH.")
        if not udid:
            report["next_steps"].append("Run pua_setup with action=discover, then action=configure and the device udid.")
            return report
        discovered = self.discover()
        report["device"] = next((device for device in discovered["devices"]
                                 if device.get("udid") == udid), None) if report["coredevice"]["available"] else None
        if report["coredevice"]["available"]:
            try:
                info = self.core.call("info")
            except PUAError as error:
                report["probe"] = {"ok": False, "code": error.code, "message": str(error)}
                report["next_steps"].append("Unlock the iPhone, reconnect USB and confirm Trust This Computer.")
            else:
                report["probe"] = {"ok": True, "transport": info.get("transport"),
                                   "point_size": [info.get("point_width"), info.get("point_height")]}
                report["ok"] = bool(report["axe"]["available"])
        return report
