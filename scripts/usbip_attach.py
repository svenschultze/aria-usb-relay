#!/usr/bin/env python3
"""Minimal USB/IP importer for Linux/WSL, using only Python's standard library.

The kernel owns all URBs after attach; this process does not decode USB traffic.
Protocol: https://docs.kernel.org/usb/usbip_protocol.html
Sysfs ABI: Linux tools/usb/usbip/libsrc/vhci_driver.c
"""

import argparse
import json
import os
from pathlib import Path
import socket
import signal
import struct
import sys
import time


VERSION = 0x0111
DEVICE = struct.Struct("!256s32sIIIHHHBBBBBB")
COMMON = struct.Struct("!HHI")
DEFAULT_VHCI = Path("/sys/devices/platform/vhci_hcd.0")


def recv_exact(sock, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise ConnectionError(f"USB/IP connection closed after {len(chunks)}/{count} bytes")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_common(sock, expected_code):
    version, code, status = COMMON.unpack(recv_exact(sock, COMMON.size))
    if version != VERSION or code != expected_code:
        raise ValueError(f"Unexpected USB/IP reply version={version:#x}, code={code:#x}")
    if status:
        raise RuntimeError(f"USB/IP server rejected request with status {status}")


def decode_device(data):
    fields = DEVICE.unpack(data)
    names = ("path", "busid", "busnum", "devnum", "speed", "vendor", "product",
             "bcd_device", "device_class", "device_subclass", "device_protocol",
             "configuration", "configurations", "interfaces")
    result = dict(zip(names, fields))
    for key in ("path", "busid"):
        result[key] = result[key].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if not 1 <= result["speed"] <= 6:
        raise ValueError(f"Unsupported USB speed {result['speed']}")
    if not 0 <= result["busnum"] <= 0xFFFF or not 0 <= result["devnum"] <= 0xFFFF:
        raise ValueError("USB/IP device ID exceeds 16-bit bus/device fields")
    return result


def connect(host, port, timeout):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return sock


def list_devices(host, port=3240, timeout=10):
    with connect(host, port, timeout) as sock:
        sock.sendall(COMMON.pack(VERSION, 0x8005, 0))
        receive_common(sock, 0x0005)
        count, = struct.unpack("!I", recv_exact(sock, 4))
        if count > 64:
            raise ValueError(f"Server returned unreasonable device count {count}")
        devices = []
        for _ in range(count):
            device = decode_device(recv_exact(sock, DEVICE.size))
            device["interface_descriptors"] = [
                list(recv_exact(sock, 4)[:3]) for _ in range(device["interfaces"])
            ]
            devices.append(device)
        return devices


def read_status(vhci=DEFAULT_VHCI):
    status_file = vhci / "status"
    if not status_file.exists():
        raise RuntimeError("vhci_hcd is not loaded. Run: sudo modprobe vhci_hcd")
    result = []
    for line in status_file.read_text().splitlines()[1:]:
        values = line.split()
        if len(values) >= 5:
            result.append(dict(hub=values[0], port=int(values[1]), status=int(values[2]),
                               speed=int(values[3]), devid=int(values[4], 16),
                               local_busid=values[6] if len(values) > 6 else None))
    return result


def select_port(speed, ports, requested=None):
    hub = "ss" if speed in (5, 6) else "hs"
    available = [p for p in ports if p["hub"] == hub and p["status"] == 4]
    if requested is not None:
        available = [p for p in available if p["port"] == requested]
    if not available:
        raise RuntimeError(f"No free {hub} VHCI port" +
                           (f" numbered {requested}" if requested is not None else ""))
    return available[0]["port"]


def write_sysfs(path, value):
    # One write is required by this sysfs control interface.
    with path.open("wb", buffering=0) as stream:
        payload = value.encode("ascii")
        if stream.write(payload) != len(payload):
            raise OSError(f"Short write to {path}")


def attach(host, busid, port=3240, timeout=10, vhci=DEFAULT_VHCI, vhci_port=None):
    if os.geteuid() != 0:
        raise PermissionError("Attach needs root for the VHCI sysfs interface; list does not.")
    encoded = busid.encode("ascii")
    if not encoded or len(encoded) > 31 or b"\0" in encoded:
        raise ValueError("Bus ID must contain 1–31 ASCII characters without NUL")
    ports = read_status(vhci)
    with connect(host, port, timeout) as sock:
        sock.sendall(COMMON.pack(VERSION, 0x8003, 0) + encoded.ljust(32, b"\0"))
        receive_common(sock, 0x0003)
        device = decode_device(recv_exact(sock, DEVICE.size))
        if device["busid"] != busid:
            raise ValueError("USB/IP server returned a different bus ID")
        if device["vendor"] != 0x2833:
            raise ValueError(f"Expected Meta vendor 2833, received {device['vendor']:04x}")
        selected = select_port(device["speed"], ports, vhci_port)
        devid = (device["busnum"] << 16) | device["devnum"]
        # Hand a blocking socket to the kernel; it retains its own reference.
        sock.settimeout(None)
        write_sysfs(vhci / "attach", f"{selected} {sock.fileno()} {devid} {device['speed']}")
    return dict(vhci_port=selected, host=host, tcp_port=port, **device)


def detach(port, vhci=DEFAULT_VHCI):
    if os.geteuid() != 0:
        raise PermissionError("Detach needs root for the VHCI sysfs interface.")
    selected = next((p for p in read_status(vhci) if p["port"] == port), None)
    if selected is None:
        raise ValueError(f"VHCI port {port} does not exist")
    if selected["status"] == 4:
        return dict(vhci_port=port, detached=False, reason="already free")
    write_sysfs(vhci / "detach", str(port))
    return dict(vhci_port=port, detached=True)


def watch(host, port, timeout, busid=None, state_file=None):
    """Re-import after the device changes from ADB/MTP to USB Ethernet mode."""
    if os.geteuid() != 0:
        raise PermissionError("Watch needs root for the VHCI sysfs interface.")
    owned_port = None
    last_error = None
    def save_state(value):
        if state_file:
            target = Path(state_file)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(value))
            temporary.replace(target)
    try:
        while True:
            try:
                if owned_port is not None:
                    state = next(p for p in read_status() if p["port"] == owned_port)
                    if state["status"] in (5, 6):
                        time.sleep(1)
                        continue
                    detach(owned_port)
                    print(json.dumps(dict(event="disconnected", vhci_port=owned_port)), flush=True)
                    owned_port = None
                    save_state(dict(vhci_port=None))
                devices = [d for d in list_devices(host, port, timeout)
                           if d["vendor"] == 0x2833 and (busid is None or d["busid"] == busid)]
                if len(devices) != 1:
                    raise RuntimeError(f"Waiting for exactly one Meta USB device; found {len(devices)}")
                result = attach(host, devices[0]["busid"], port, timeout)
                owned_port = result["vhci_port"]
                save_state(result)
                print(json.dumps(dict(event="attached", **result)), flush=True)
                last_error = None
            except (OSError, ValueError, RuntimeError) as error:
                message = str(error)
                if message != last_error:
                    print(json.dumps(dict(event="waiting", detail=message)), flush=True)
                    last_error = message
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if owned_port is not None:
            result = detach(owned_port)
            print(json.dumps(dict(event="stopped", **result)), flush=True)
        save_state(dict(vhci_port=None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "attach", "detach", "status", "watch"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3240, help="USB/IP TCP port")
    parser.add_argument("--busid", help="Exact busid from list")
    parser.add_argument("--vhci-port", type=int)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--state-file", help="Optional atomic state file for the owned imported port")
    args = parser.parse_args()
    if args.action == "attach" and not args.busid:
        parser.error("attach requires --busid from list")
    if args.action == "detach" and args.vhci_port is None:
        parser.error("detach requires --vhci-port from status")
    if args.timeout <= 0 or not 1 <= args.port <= 65535:
        parser.error("timeout must be positive and port must be 1–65535")
    try:
        if args.action == "watch":
            def stop_watch(_signum, _frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_watch)
            watch(args.host, args.port, args.timeout, args.busid, args.state_file)
            return 0
        elif args.action == "list":
            result = list_devices(args.host, args.port, args.timeout)
        elif args.action == "attach":
            result = attach(args.host, args.busid, args.port, args.timeout,
                            vhci_port=args.vhci_port)
        elif args.action == "detach":
            result = detach(args.vhci_port)
        else:
            result = read_status()
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"USB/IP: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
