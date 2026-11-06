"""Opt-in PUA control through a pre-paired iCTRL runner."""
import hashlib
import ipaddress
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from vendor import ictrl_rpc
from wda_client import WDAError
from wda_controller import PhoneController, VIEWPORT_TTL, integer


SUPPORTED_ACTIONS = {"tap", "swipe", "launch_app", "press_button"}
MCP_TOOLS = {"pua_setup", "pua_doctor", "pua_ready", "pua_observe", "pua_batch"} | {
    f"pua_{action}" for action in SUPPORTED_ACTIONS
}
_ID = re.compile(r"[a-zA-Z0-9-]{4,64}")


def unsupported(message):
    raise WDAError("ictrl_unsupported", message, details={"action_executed": False})


def _local_endpoint(endpoint):
    try:
        url = urlsplit(f"http://{endpoint}")
        addr, _, scope = (url.hostname or "").partition("%")
        ip = ipaddress.ip_address(addr)
        return (url.netloc == endpoint and not url.username and not url.password
                and not url.path and not url.query and not url.fragment
                and url.port is not None and url.port > 0
                and ((ip.version == 4 and ip.is_private and not scope)
                     or (ip.version == 6 and ip.is_link_local and scope == "awdl0")))
    except (TypeError, ValueError):
        return False


class IctrlClient:
    def __init__(self, target, token_store=None):
        self.target = (target or "").strip().lower()
        if self.target and not _ID.fullmatch(self.target):
            raise ValueError("IPHONE_USE_ICTRL_DEVICE_ID must be a full iCTRL device ID.")
        self.token_store = Path(token_store or ictrl_rpc.STORE)
        self.connection = None
        self.sequence = 0

    @staticmethod
    def scan():
        try:
            return [row for row in ictrl_rpc.nearby_scan(wait=3.0)
                    if isinstance(row.get("id"), str) and _ID.fullmatch(row["id"])
                    and row.get("channel") in ("awdl", "lan") and _local_endpoint(row.get("host"))]
        except (OSError, ValueError) as error:
            raise WDAError("ictrl_discovery_failed", "iCTRL nearby discovery failed on this Mac.") from error

    @classmethod
    def discover(cls):
        found = {}
        for row in cls.scan():
            entry = found.setdefault(row["id"], {"device_id": row["id"], "name": row.get("name"), "transports": []})
            if row["channel"] not in entry["transports"]:
                entry["transports"].append(row["channel"])
        return list(found.values())

    def _credential(self, device_id):
        try:
            info = self.token_store.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid() or info.st_size > 1024 * 1024):
                raise ValueError("unsafe token file")
            record = json.loads(self.token_store.read_text()).get(f"device:{device_id}", {})
            key, token = record["key"], record["tok"]
            if not re.fullmatch(r"[0-9a-fA-F]{64}", key) or hashlib.sha256(bytes.fromhex(key)).hexdigest()[:8] != token:
                raise ValueError("invalid token")
            return {"key": key, "tok": token}
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise WDAError("ictrl_pairing_required", "Pair the selected iCTRL device once with the upstream client and keep its token file private (0600).") from error

    def _connect(self):
        if not self.target:
            raise WDAError("ictrl_device_required", "Run pua_setup(action=discover), then set IPHONE_USE_ICTRL_DEVICE_ID for this MCP instance.")
        candidates = [row for row in self.scan() if row["id"].lower() == self.target]
        if not candidates:
            raise WDAError("ictrl_unreachable", "The selected iCTRL Runner was not discovered. Check that it is awake, running and has Nearby Discovery enabled.")
        ids = {row["id"] for row in candidates}
        if len(ids) != 1:
            raise WDAError("ictrl_device_ambiguous", "Nearby results disagree about the selected device ID.")
        device_id = next(iter(ids))
        credential = self._credential(device_id)
        last_error = None
        for row in sorted(candidates, key=lambda row: row["channel"] != "awdl"):
            try:
                info = self._send(row["host"], credential, "device_info", {}, 4, action=False)
                if info.get("device_id") != device_id:
                    raise WDAError("ictrl_identity_mismatch", "The authenticated Runner did not match the selected iCTRL device.")
                self.connection = (row, credential, time.monotonic() + 30)
                return info
            except WDAError as error:
                last_error = error
        raise last_error

    def _send(self, endpoint, credential, method, params, timeout, *, action):
        self.sequence += 1
        try:
            # One request only. The upstream nearby_call helper can replay an action after a timeout.
            status, reply = ictrl_rpc._call_with(endpoint, credential, method, params, timeout, self.sequence)
        except (OSError, ValueError, KeyError, TypeError, SystemExit) as error:
            raise WDAError("action_uncertain" if action else "ictrl_unreachable",
                           "iCTRL did not return an authenticated reply. Observe fresh state before repeating an action." if action
                           else "iCTRL did not return an authenticated reply.", uncertain=action) from error
        if status in (401, 403):
            raise WDAError("ictrl_pairing_required", "iCTRL rejected the stored pairing. Verify the device and pair it again if needed.")
        if status != 200 or not isinstance(reply, dict) or reply.get("id") != self.sequence:
            raise WDAError("ictrl_invalid_response", "Invalid iCTRL RPC response.", uncertain=action)
        if "error" in reply or not isinstance(reply.get("result"), dict):
            raise WDAError("ictrl_rpc_failed", f"iCTRL did not complete {method}.", uncertain=action)
        return reply["result"]

    def rpc(self, method, params=None, timeout=None, *, action=False):
        info = None
        if not self.connection or time.monotonic() >= self.connection[2]:
            info = self._connect()
        if method == "device_info" and info is not None:
            return info
        row, credential, _ = self.connection
        try:
            return self._send(row["host"], credential, method, params or {}, timeout or 12, action=action)
        except WDAError:
            self.close()
            raise

    def close(self):
        self.connection = None

    def request(self, method, path, payload=None, timeout=None):
        data = self.session(method, path, payload, timeout)
        return {"value": data}

    def session(self, method, path, payload=None, timeout=None):
        payload = payload or {}
        rpc = self.rpc
        if method == "GET" and path == "/window/size":
            info = rpc("device_info", timeout=timeout)
            return {"width": info.get("point_width"), "height": info.get("point_height")}
        if method == "GET" and path == "/screenshot":
            return rpc("screenshot", {"format": "png"}, timeout).get("data")
        if method == "GET" and path == "/wda/activeAppInfo":
            return {"bundleId": self.source(1, timeout).get("bundle_id")}
        if method == "POST" and path == "/wda/tap":
            return rpc("tap", {"x": payload["x"], "y": payload["y"]}, timeout, action=True)
        if method == "POST" and path == "/wda/dragfromtoforduration":
            points = {key: payload[value] for key, value in
                      [("from_x", "fromX"), ("from_y", "fromY"), ("to_x", "toX"), ("to_y", "toY")]}
            return rpc("drag", {**points, "duration": payload["duration"]}, timeout, action=True)
        if method == "POST" and path == "/wda/apps/activate":
            return rpc("activate_app", {"bundle_id": payload["bundleId"]}, timeout, action=True)
        if method == "POST" and path == "/wda/homescreen":
            return rpc("home", timeout=timeout, action=True)
        unsupported("This operation needs a WDA capability unavailable in the iCTRL pilot.")

    def source(self, limit, timeout=None):
        return self.rpc("page_source", {"max_elements": limit, "visible_only": True}, timeout)


class IctrlController(PhoneController):
    def observe(self, mode="tree", include_invisible=False, max_nodes=100, expensive_visibility=False):
        if include_invisible or expensive_visibility:
            unsupported("The iCTRL pilot returns only the Runner's visible source.")
        integer(max_nodes, "max_nodes", 1, 500)
        viewport = self.viewport(VIEWPORT_TTL)
        if mode == "screenshot":
            return self.observation_from_state([], viewport, self.active_app(), mode)
        source = self.client.source(max_nodes)
        app, tree = source.get("bundle_id"), source.get("source")
        if not isinstance(app, str) or not app or not isinstance(tree, str) or not tree.strip():
            raise WDAError("ictrl_source_unavailable", "iCTRL did not return readable foreground source. Try screenshot mode or reopen the Runner.")
        result = self.observation_from_state([], viewport, app, "screenshot" if mode == "both" else "tree")
        result.pop("warnings", None)
        result.pop("nodes", None)
        result.update(source=tree[:32_768], source_format="ictrl_text", total_nodes=source.get("total_returned"),
                      truncated=bool(source.get("truncated")) or len(tree) > 32_768)
        return result

    @staticmethod
    def check_operation(op, args):
        if op not in SUPPORTED_ACTIONS | {"observe"}:
            unsupported("The iCTRL pilot supports observation, coordinate taps, swipes, Home and app launch.")
        if args.get("selector") is not None or args.get("expect") is not None:
            unsupported("Use a coordinate tap from a fresh screenshot. WDA selectors and postconditions are unavailable in the iCTRL pilot.")
        if op == "press_button" and args.get("name") != "home":
            unsupported("The iCTRL pilot supports only the Home button.")
        if op == "swipe" and args.get("verify"):
            unsupported("Use observe after the swipe. WDA tree-based swipe verification is unavailable in the iCTRL pilot.")
        if args.get("include_invisible") or args.get("expensive_visibility"):
            unsupported("The iCTRL pilot returns only the Runner's visible source.")

    def run(self, op, args):
        if op == "setup" and args.get("action") == "discover":
            return {"backend": "ictrl", "devices": self.client.discover()}
        if op == "doctor" or (op == "setup" and args.get("action") == "status"):
            return {"backend": "ictrl", "configured": bool(self.client.target), "runner_managed": False,
                    "next_steps": ["Enable Nearby Discovery in the iCTRL Runner and pair once with the upstream client.",
                                   "Set IPHONE_USE_ICTRL_DEVICE_ID, then run pua_ready."]}
        if op == "ready":
            info = self.client.rpc("device_info")
            self.ensure_unlocked()
            screenshot = args.get("screenshot", True)
            observation = self.observe("both" if screenshot else "tree")
            return {"ready": True, "state": "ready", "backend": "ictrl", "device_id": info["device_id"],
                    "transport": self.client.connection[0]["channel"], "observation": observation,
                    "proof": {"pairing_authenticated": True, "phone_unlocked": True, "foreground_resolved": True,
                              "source_readable": True, "viewport_readable": True, "screenshot_readable": screenshot}}
        if op == "batch":
            for step in args["steps"]:
                self.check_operation(step["op"], step.get("args", {}))
            self.ensure_unlocked()
            return self.batch(args["steps"])
        self.check_operation(op, args)
        self.ensure_unlocked()
        return getattr(self, op)(**args)

    def ensure_unlocked(self):
        if self.client.rpc("is_locked").get("locked") is not False:
            raise WDAError("phone_locked", "Unlock the iPhone yourself and verify READY again.")
