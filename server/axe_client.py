"""Read the accessibility tree of a physical device through AXe.

AXe talks to ``com.apple.accessibility.axAuditDaemon.remoteAXService`` over the
CoreDevice tunnel, so a device without a WebDriverAgent Runner still answers with
the real element tree. This module only drives the ``axe`` binary; AXe owns the
protocol, the per-device broker and the retry rules.

Docs: https://github.com/cameroncooke/AXe (MIT).
"""

import json
import os
import pathlib
import shutil
import subprocess

from wda_client import WDAError

BINARY_NAME = "axe"
DEFAULT_INSTALL = pathlib.Path.home() / ".local" / "share" / "iphone-use-axe" / "axe"
TREE_LIMIT = 32_768
_ERRORS = {"targetNotFound": "axe_target_not_found", "notBooted": "axe_target_not_found",
           "deviceLocked": "phone_locked", "busy": "axe_busy", "timeout": "axe_timeout",
           "treeUnavailable": "axe_tree_unavailable", "deviceNotConnected": "axe_disconnected",
           "developerModeDisabled": "axe_developer_mode", "notPaired": "axe_not_paired",
           "developerDiskImageUnavailable": "axe_disk_image", "targetChanged": "axe_target_changed"}


def _binary():
    override = os.environ.get("IPHONE_USE_AXE")
    if override:
        return override if pathlib.Path(override).is_file() else None
    found = shutil.which(BINARY_NAME)
    if found:
        return found
    return str(DEFAULT_INSTALL) if DEFAULT_INSTALL.is_file() else None


class AxeClient:
    """A thin wrapper: one process per command, JSON in and out."""

    def __init__(self, binary=None, timeout=45.0):
        self.binary = binary or _binary()
        self.timeout = timeout

    def available(self):
        return bool(self.binary)

    def _run(self, *args):
        if not self.binary:
            raise WDAError("axe_unavailable", "AXe is not installed; set IPHONE_USE_AXE to the axe binary.")
        try:
            done = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as expired:
            raise WDAError("axe_timeout", f"AXe did not answer within {self.timeout:g}s.", uncertain=True) from expired
        except OSError as error:
            raise WDAError("axe_unavailable", f"AXe could not start: {error}") from error
        if done.returncode != 0:
            message = (done.stderr or done.stdout or "").strip().splitlines()
            text = message[-1] if message else f"AXe exited with {done.returncode}."
            code = next((value for name, value in _ERRORS.items() if name in text), "axe_failed")
            raise WDAError(code, text[:300], uncertain=code not in ("axe_target_not_found", "axe_not_paired"))
        return done.stdout

    def describe_ui(self, udid, point=None):
        args = ["describe-ui", "--udid", udid]
        if point:
            args += ["--point", f"{point[0]},{point[1]}"]
        text = self._run(*args).strip()
        try:
            return json.loads(text) if text else []
        except json.JSONDecodeError as error:
            raise WDAError("axe_failed", f"AXe returned unreadable JSON: {text[:120]}") from error

    def tap(self, udid, identifier=None, label=None, value=None, timeout=None):
        args = ["tap", "--udid", udid]
        for flag, item in (("--id", identifier), ("--label", label), ("--value", value)):
            if item:
                args += [flag, item]
        if timeout:
            args += ["--wait-timeout", f"{timeout:g}"]
        self._run(*args)

    def tree_text(self, udid, limit=TREE_LIMIT):
        """Render the tree as indented text so it reads like the Runner's source."""
        nodes = self.describe_ui(udid)
        lines, app = [], ""
        stack = [(node, 0) for node in reversed(nodes if isinstance(nodes, list) else [nodes])]
        while stack and len("\n".join(lines)) < limit:
            node, depth = stack.pop()
            role = node.get("role") or node.get("type") or "?"
            label = node.get("AXLabel") or node.get("AXValue") or ""
            identifier = node.get("AXUniqueId")
            box = node.get("frame") or {}
            if role == "AXApplication" and label and not app:
                app = label
            piece = "  " * depth + role
            if label:
                piece += f" {label!r}"
            if identifier:
                piece += f" #{identifier}"
            if box:
                piece += f" {{{box.get('x', 0):.0f},{box.get('y', 0):.0f} {box.get('width', 0):.0f}x{box.get('height', 0):.0f}}}"
            lines.append(piece)
            for child in reversed(node.get("children") or []):
                stack.append((child, depth + 1))
        text = "\n".join(lines)
        return app, text[:limit], len(lines)
