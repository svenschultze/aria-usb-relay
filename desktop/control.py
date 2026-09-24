"""Bounded local operations. Every subprocess uses argument arrays, never a shell."""
from __future__ import annotations

from collections import deque
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"device": "", "distro": "Ubuntu-22.04", "linux_user": "",
            "sdk_path": "", "aria_serial": "", "profile": "profile12"}
SAFE_ID = re.compile(r"[A-Za-z0-9_.:\[\]-]{1,200}\Z")


def validate_config(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Unknown connection setting")
    result = dict(DEFAULTS)
    for key, item in value.items():
        if not isinstance(item, str) or len(item) > 400 or any(ord(c) < 32 for c in item):
            raise ValueError(f"Invalid {key.replace('_', ' ')}")
        result[key] = item.strip()
    for key in ("device", "distro", "aria_serial", "profile"):
        if result[key] and (not SAFE_ID.fullmatch(result[key]) or result[key].startswith("-")):
            raise ValueError(f"Invalid {key.replace('_', ' ')}")
    if not result["profile"]:
        raise ValueError("Choose a streaming profile")
    if result["linux_user"] and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", result["linux_user"]):
        raise ValueError("Invalid Linux user name")
    if result["sdk_path"] and (not result["sdk_path"].startswith("/") or any(c in result["sdk_path"] for c in "`$;\"'\\")):
        raise ValueError("SDK path must be an absolute Linux path")
    return result


def validate_address(address: object) -> str:
    if not isinstance(address, str) or len(address) > 200:
        raise ValueError("Enter the address and port shown in Wireless debugging")
    address = address.strip()
    host, separator, port = address.rpartition(":")
    if not separator or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Use the IP address and connection port shown in Wireless debugging")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError as error:
        raise ValueError("Use the IP address shown on the phone") from error
    return address


def parse_devices(output: str) -> list[dict]:
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] not in ("device", "offline", "unauthorized", "recovery"):
            continue
        details = dict(x.split(":", 1) for x in fields[2:] if ":" in x)
        devices.append({"id": fields[0], "state": fields[1],
                        "model": details.get("model", "Android device").replace("_", " "),
                        "connection": "wireless" if ":" in fields[0] or "._tcp" in fields[0] else "USB"})
    return devices


def parse_mdns(output: str) -> list[dict]:
    found = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-2].rstrip(".") in ("_adb-tls-connect._tcp", "_adb-tls-pairing._tcp"):
            try:
                address = validate_address(fields[-1])
            except ValueError:
                continue
            kind = "pairing" if "pairing" in fields[-2] else "connect"
            found[(kind, address)] = {"kind": kind, "address": address}
    return list(found.values())


def redact(text: str, config: dict | None = None, known_identifiers=()) -> str:
    for path in (str(ROOT), str(Path.home()), str(ROOT).replace("\\", "/")):
        text = text.replace(path, "<local-path>")
    for key in ("device", "aria_serial", "linux_user", "sdk_path"):
        value = (config or {}).get(key, "")
        if value:
            text = text.replace(value, f"<{key}>")
    for value in sorted(known_identifiers, key=len, reverse=True):
        if len(value) >= 3:
            text = text.replace(value, "<identifier>")
    text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
    text = re.sub(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+", "<user-home>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "<address>", text)
    text = re.sub(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b", "<mac>", text)
    text = re.sub(r"\b(?:1WM|2WM)[A-Za-z0-9]+\b", "<glasses-serial>", text)
    def ipv6(match):
        candidate = match.group(0)
        try:
            ipaddress.IPv6Address(candidate.strip("[]"))
            return "<address>"
        except ValueError:
            return candidate
    text = re.sub(r"\[?[0-9a-fA-F]*:[0-9a-fA-F:%._-]+\]?", ipv6, text)
    return text


class Controller:
    def __init__(self, root: Path = ROOT, windows: bool | None = None):
        self.root = root
        self.local = root / ".local"
        self.local.mkdir(exist_ok=True)
        self.windows = os.name == "nt" if windows is None else windows
        self.lock = threading.RLock()
        self.closing = False
        self.logs = deque(maxlen=180)
        self.config = dict(DEFAULTS)
        try:
            self.config = validate_config(json.loads((self.local / "desktop.json").read_text()))
        except (OSError, ValueError):
            pass
        self.identifiers = {value for key, value in self.config.items() if key in ("device", "aria_serial", "linux_user", "sdk_path") and value}
        self.identifiers.update(value for value in (Path.home().name, os.environ.get("USER", ""), os.environ.get("USERNAME", "")) if len(value) >= 3)
        self.logger = logging.getLogger(f"aria.desktop.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.local / "desktop.log", maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(handler)
        self.job = {"running": False, "action": "", "message": "Ready", "error": None}
        self.devices: list[dict] = []
        self.discovered: list[dict] = []
        self.distros: list[str] = []
        self.checks: dict = {"ready": False, "checks": []}
        self.session: subprocess.Popen | None = None
        self.stopping_session: subprocess.Popen | None = None
        self.session_config: dict | None = None
        self.session_exit: int | None = None
        self.preview: dict | None = None
        self.preview_error: str | None = None
        self.stop_event = threading.Event()
        self.opener = build_opener(ProxyHandler({}))
        self.monitor = threading.Thread(target=self._monitor, daemon=True, name="aria-status")

    def log(self, message: str) -> None:
        clean = re.sub(r"\x1b\[[0-9;]*[mK]", "", message).strip()[:3000]
        if clean:
            with self.lock:
                self.logs.append({"time": time.strftime("%H:%M:%S"), "text": clean})
            self.logger.info(clean)

    def start_monitor(self) -> None:
        self.monitor.start()

    def begin_close(self) -> None:
        with self.lock:
            self.closing = True
            self.stop_event.set()

    def close(self) -> None:
        self.begin_close()
        if self.monitor.is_alive():
            self.monitor.join(timeout=3)
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)

    @staticmethod
    def process_options() -> dict:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def run(self, arguments: list[str], timeout: float = 20, input_text: str | None = None) -> str:
        try:
            completed = subprocess.run(arguments, cwd=self.root, input=input_text,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                       **self.process_options())
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Operation timed out. Check the connection and try again.") from error
        output = completed.stdout.strip()
        if completed.returncode:
            detail = (completed.stderr.strip() or output)[-5000:]
            raise RuntimeError(detail or f"Operation exited with code {completed.returncode}")
        return output

    def adb(self) -> str:
        names = [os.environ.get("ARIA_ADB", "")]
        for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            if os.environ.get(key):
                names.append(str(Path(os.environ[key]) / "platform-tools" / ("adb.exe" if self.windows else "adb")))
        if self.windows and os.environ.get("LOCALAPPDATA"):
            names.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk/platform-tools/adb.exe"))
        names.append(shutil.which("adb") or "")
        for name in names:
            if name and Path(name).is_file():
                return name
        raise RuntimeError("Android Platform-Tools is missing. Install adb and add it to PATH, or set ANDROID_SDK_ROOT.")

    def script(self, name: str, config: dict, *, device: bool = False) -> list[str]:
        if self.windows:
            pwsh = shutil.which("pwsh")
            if not pwsh:
                raise RuntimeError("Install PowerShell 7 and add pwsh to PATH.")
            result = [pwsh, "-NoProfile", "-NonInteractive", "-File", str(self.root / "scripts" / f"{name}.ps1")]
            if name in ("start-live", "stop-live", "doctor"):
                result += ["-Distro", config["distro"]]
            if name in ("start-live", "doctor"):
                for key, flag in (("linux_user", "LinuxUser"), ("sdk_path", "SdkPath"), ("aria_serial", "AriaSerial")):
                    if config[key]:
                        result += [f"-{flag}", config[key]]
            if name == "start-live":
                result += ["-Profile", config["profile"]]
            if device and config["device"]:
                result += ["-Device", config["device"]]
            if name == "doctor":
                result += ["-Json"]
            return result
        actual = "doctor-linux" if name == "doctor" else name
        result = ["bash", str(self.root / "scripts" / f"{actual}.sh")]
        if name in ("start-live", "doctor"):
            for key, flag in (("sdk_path", "--sdk-path"), ("aria_serial", "--serial")):
                if config[key]:
                    result += [flag, config[key]]
        if device and config["device"]:
            result += ["--device", config["device"]]
        if name == "start-live":
            result += ["--profile", config["profile"]]
        if name == "doctor":
            result += ["--json"]
        return result

    def save(self, config: dict) -> None:
        with self.lock:
            active = self.preview is not None or (self.session is not None and self.session.poll() is None)
            if active and config != self.config:
                raise RuntimeError("Stop the stream before changing its connection settings.")
        temporary = self.local / "desktop.tmp"
        temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.local / "desktop.json")
        with self.lock:
            self.config = dict(config)
            self.identifiers.update(value for key, value in config.items() if key in ("device", "aria_serial", "linux_user", "sdk_path") and value)

    def submit(self, action: str, payload: dict) -> None:
        if action not in ("save", "discover", "pair", "connect", "install", "doctor", "start", "stop"):
            raise ValueError("Unknown action")
        if action == "stop":
            # Stopping must work even if the form contains unfinished edits.
            with self.lock:
                config = dict(self.session_config or self.config)
        else:
            config = validate_config(payload.get("config", self.config))
        if action in ("start", "install") and not config["device"]:
            raise ValueError("Select or connect a phone first")
        if action in ("pair", "connect"):
            payload = {**payload, "address": validate_address(payload.get("address"))}
        if action == "pair" and not re.fullmatch(r"\d{6}", str(payload.get("code", ""))):
            raise ValueError("Enter the six-digit pairing code shown on the phone")
        with self.lock:
            if self.closing:
                raise RuntimeError("The dashboard is shutting down")
            if self.job["running"]:
                raise RuntimeError("An operation is already running. Wait for it to finish.")
            self.job = {"running": True, "action": action, "message": "Working…", "error": None}
        threading.Thread(target=self._job, args=(action, payload, config), daemon=True,
                         name=f"aria-{action}").start()

    def _job(self, action: str, payload: dict, config: dict) -> None:
        try:
            if action not in ("stop", "pair"):
                self.save(config)
            result = self._perform(action, payload, config)
            with self.lock:
                self.job = {"running": False, "action": action, "message": result, "error": None}
            self.log(result)
        except Exception as error:
            detail = str(error)
            # Pairing codes never reach logs or saved settings, including tool errors.
            if action == "pair":
                detail = detail.replace(str(payload.get("code", "")), "<pairing-code>")
            with self.lock:
                self.job = {"running": False, "action": action, "message": "Needs attention", "error": detail}
            self.log(detail)

    def _perform(self, action: str, payload: dict, config: dict) -> str:
        if action == "save":
            return "Connection settings saved on this computer."
        if action == "discover":
            adb = self.adb()
            devices = parse_devices(self.run([adb, "devices", "-l"]))
            # ADB often lists one phone both by IP and by its mDNS alias.
            # Compare a real device identifier instead of merging identical models.
            unique = {}
            for entry in devices[:16]:
                identity = entry["id"]
                if entry["state"] == "device":
                    try:
                        serial = self.run([adb, "-s", entry["id"], "shell", "getprop", "ro.serialno"], timeout=3).strip()
                        if serial and serial.lower() != "unknown":
                            identity = serial
                    except RuntimeError:
                        pass
                prior = unique.get(identity)
                if prior is None or ("._tcp" in prior["id"] and "._tcp" not in entry["id"]):
                    unique[identity] = entry
            devices = list(unique.values())
            try:
                discovered = parse_mdns(self.run([adb, "mdns", "services"], timeout=8))
            except RuntimeError:
                discovered = []
            distros = []
            if self.windows and shutil.which("wsl"):
                raw = self.run(["wsl", "--list", "--quiet"], timeout=8)
                distros = [x.replace("\x00", "").strip("\ufeff� \r") for x in raw.splitlines() if x.replace("\x00", "").strip()]
            with self.lock:
                self.devices, self.discovered, self.distros = devices, discovered, distros
                self.identifiers.update(device["id"] for device in devices)
                self.identifiers.update(unique.keys())
                self.identifiers.update(device["address"] for device in discovered)
            return f"Found {len(devices)} ADB device(s). Select the phone you want to use."
        if action == "pair":
            response = self.run([self.adb(), "pair", payload["address"]], timeout=25,
                                input_text=payload["code"] + "\n")
            if "successfully paired" not in response.lower():
                raise RuntimeError(response or "Pairing was not confirmed. Keep the pairing dialog open and try again.")
            return "Phone paired. Connect using the address on the main Wireless debugging screen."
        if action == "connect":
            address = payload["address"]
            response = self.run([self.adb(), "connect", address], timeout=15)
            if "connected to" not in response.lower():
                raise RuntimeError(response or "Phone connection failed")
            connected = parse_devices(self.run([self.adb(), "devices", "-l"]))
            if not any(d["id"] == address and d["state"] == "device" for d in connected):
                raise RuntimeError("The phone is not ready. Check Wireless debugging and accept authorization on the phone.")
            self.save({**config, "device": address})
            with self.lock:
                self.devices = connected
            return "Phone connected and selected."
        if action == "doctor":
            result = json.loads(self.run(self.script("doctor", config, device=True), timeout=90))
            if not isinstance(result, dict) or not isinstance(result.get("checks"), list):
                raise RuntimeError("Setup checker returned an invalid result")
            with self.lock:
                self.checks = result
            return "Setup checks complete." if result.get("ready") else "Setup checks found items to resolve."
        if action == "install":
            apk = self.root / "app/build/outputs/apk/debug/app-debug.apk"
            release = self.root / "dist/aria-usb-relay.apk"
            if release.exists():
                apk = release
            if not apk.is_file():
                raise RuntimeError("Download the release APK to dist/aria-usb-relay.apk, or build the Android app first.")
            self.run([self.adb(), "-s", config["device"], "install", "-r", str(apk)], timeout=90)
            self.run([self.adb(), "-s", config["device"], "shell", "am", "start", "-n", "dev.aria.usb/.MainActivity"])
            return "Aria USB Relay installed. Open it and allow USB access on the phone."
        if action == "start":
            with self.lock:
                if self.session is not None and self.session.poll() is None:
                    return "This dashboard already has a running session."
                if self.preview is not None:
                    return "A decoder is already running. Use Stop stream before starting a new session."
            arguments = self.script("start-live", config, device=True)
            with self.lock:
                # Publish the child atomically with the shutdown check. Shutdown
                # can then either stop this exact child or prevent its creation.
                if self.closing:
                    raise RuntimeError("The dashboard is shutting down")
                process = subprocess.Popen(arguments, cwd=self.root,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           **self.process_options())
                self.session_config = dict(config)
                self.session_exit = None
                self.stopping_session = None
                self.session = process
            threading.Thread(target=self._read_session, args=(process,), daemon=True, name="aria-session-log").start()
            return "Starting the stream. Open the phone app, tap Start relay if needed, and accept USB access."
        if action == "stop":
            with self.lock:
                self.stopping_session = self.session
            target = self.session_config or config
            deadline = time.monotonic() + 110
            while True:
                self.run(self.script("stop-live", target), timeout=max(1, deadline - time.monotonic()))
                process = self.session
                if process is None or process.poll() is not None:
                    break
                # A click can arrive before the native controller registers its
                # PID. Keep requesting an owned stop until this launcher exits.
                if time.monotonic() >= deadline:
                    raise RuntimeError("Shutdown is still pending. Check Diagnostics; no unrelated process was terminated.")
                time.sleep(0.5)
            return "Stream stopped. The phone can remain ready for the next session."
        raise ValueError("Unknown action")

    def _read_session(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in iter(lambda: process.stdout.readline(4096), b""):
            self.log(line.decode("utf-8", errors="replace"))
        process.stdout.close()
        code = process.wait()
        with self.lock:
            requested_stop = self.stopping_session is process
            if self.session is process:
                self.session_exit = 0 if requested_stop else code
        self.log("Session stopped." if requested_stop else "Session ended." if code in (0, 130, 143) else f"Session exited with code {code}. Check Diagnostics for recovery instructions.")

    def fetch_preview(self, path: str, limit: int) -> tuple[bytes, str]:
        with self.opener.open("http://127.0.0.1:8765" + path, timeout=2) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError("Preview response exceeds its buffer limit")
            return content, response.headers.get_content_type()

    def _monitor(self) -> None:
        while not self.stop_event.is_set():
            try:
                data, _ = self.fetch_preview("/status.json", 128 * 1024)
                preview = json.loads(data)
                if not isinstance(preview, dict) or "receiving" not in preview:
                    raise ValueError("Unexpected preview service")
                with self.lock:
                    self.preview, self.preview_error = preview, None
            except (OSError, ValueError, URLError) as error:
                with self.lock:
                    self.preview, self.preview_error = None, str(error)
            self.stop_event.wait(0.8)

    def status(self) -> dict:
        with self.lock:
            running = self.session is not None and self.session.poll() is None
            return {"platform": "windows" if self.windows else "linux", "config": dict(self.config),
                    "job": dict(self.job), "devices": list(self.devices), "discovered": list(self.discovered),
                    "distros": list(self.distros), "checks": self.checks, "preview": self.preview,
                    "session_running": running, "session_exit": self.session_exit,
                    "logs": list(self.logs)[-70:]}

    def diagnostics(self) -> dict:
        status = self.status()
        # No images, raw IMU samples, pairing codes, environment variables or certificates.
        preview = status["preview"] or {}
        report = {"application": "Aria USB Relay", "platform": status["platform"],
                  "setup": status["checks"], "session_exit": status["session_exit"],
                  "stream": {key: preview.get(key) for key in ("phase", "sdk_version", "uptime_seconds", "image_count", "imu_sample_count")},
                  "logs": status["logs"]}
        with self.lock:
            identifiers = set(self.identifiers)
        def clean(value):
            if isinstance(value, str):
                return redact(value, self.config, identifiers)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        return clean(report)
