"""CoreDevice client: a long-lived helper process behind a JSON line protocol.

The helper owns the tunnel and the HID stream, so the expensive part of
CoreDevice happens once per session instead of once per call. Nothing here can
run without an interpreter that has pymobiledevice3 installed; when that is
missing the hybrid backend simply keeps using iCTRL.
"""
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from wda_client import WDAError

HELPER = Path(__file__).with_name("coredevice_helper.py")
DEFAULT_PYTHON = Path.home() / ".local/share/iphone-use-coredevice/venv/bin/python"
FAILURE_CODES = {"coredevice_unavailable", "coredevice_failed", "coredevice_timeout"}


class CoreDeviceClient:
    def __init__(self, udid=None, state_dir=None, python=None, timeout=25.0):
        self.udid = udid or os.environ.get("IPHONE_USE_COREDEVICE_UDID") or None
        self.state_dir = Path(state_dir).expanduser() if state_dir else Path.home() / ".local/share/iphone-use-ictrl"
        self.python = python or os.environ.get("IPHONE_USE_COREDEVICE_PYTHON") or str(DEFAULT_PYTHON)
        self.timeout = timeout
        self.process = None
        self.log = None
        self.replies = queue.Queue()
        self.sequence = 0

    def available(self):
        """True when a helper can actually be started; the device itself is probed on first call."""
        return bool(self.python) and Path(self.python).is_file() and HELPER.is_file()

    def _spawn(self):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log = (self.state_dir / "coredevice-helper.log").open("ab")
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        if self.udid:
            environment["IPHONE_USE_COREDEVICE_UDID"] = self.udid
        try:
            self.process = subprocess.Popen([self.python, str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.log, text=True, bufsize=1, env=environment)
        except OSError as error:
            self.process = None
            raise WDAError("coredevice_unavailable",
                           f"The CoreDevice helper could not start ({error}). Set IPHONE_USE_COREDEVICE_PYTHON to an interpreter with pymobiledevice3.") from error
        threading.Thread(target=self._pump, args=(self.process.stdout,), daemon=True).start()

    def _pump(self, stream):
        try:
            for line in stream:
                self.replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)

    def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
            try:
                process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        log, self.log = self.log, None
        if log is not None:
            log.close()

    def call(self, op, **args):
        if not self.available():
            raise WDAError("coredevice_unavailable",
                           "CoreDevice needs an interpreter with pymobiledevice3; set IPHONE_USE_COREDEVICE_PYTHON.")
        if self.process is None or self.process.poll() is not None:
            self._stop()
            self._spawn()
        self.sequence += 1
        request = {"id": self.sequence, "op": op, "args": args}
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            self._stop()
            raise WDAError("coredevice_failed", f"The CoreDevice helper stopped before {op}.") from error
        try:
            line = self.replies.get(timeout=self.timeout)
        except queue.Empty:
            self._stop()
            raise WDAError("coredevice_timeout",
                           f"CoreDevice did not answer {op} within {self.timeout:g}s. Observe before repeating an action.",
                           uncertain=op not in ("info", "screenshot")) from None
        if line is None:
            self._stop()
            raise WDAError("coredevice_failed", f"The CoreDevice helper exited during {op}.")
        try:
            reply = json.loads(line)
        except ValueError as error:
            self._stop()
            raise WDAError("coredevice_failed", "The CoreDevice helper returned unreadable output.") from error
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise WDAError("coredevice_failed", str(error.get("message") or "CoreDevice failed the request.")[:300],
                           uncertain=op not in ("info", "screenshot"))
        return reply.get("result") or {}

    def close(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write(json.dumps({"id": 0, "op": "quit", "args": {}}) + "\n")
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self._stop()
