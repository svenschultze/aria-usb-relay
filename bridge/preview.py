#!/usr/bin/env python3
"""Local Gen 1 SDK subscriber and bounded HTTP preview for Aria USB Relay.

The SDK and USB network interface must already work in the Linux environment.
This process decodes real glasses samples; it is not a USB/IP server.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import math
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


CAMERAS = ("rgb", "slam1", "slam2")


@dataclasses.dataclass(frozen=True)
class Frame:
    sequence: int
    jpeg: bytes
    received_at: float
    capture_timestamp_ns: int
    width: int
    height: int


class LatestFrames:
    """One immutable JPEG per known camera; readers never own a pending queue."""

    def __init__(self, max_frame_bytes: int = 3 * 1024 * 1024):
        self.max_frame_bytes = max_frame_bytes
        self._condition = threading.Condition()
        self._frames: dict[str, Frame] = {}
        self._sequence = 0
        self._closed = False

    def publish(self, camera: str, jpeg: bytes, *, capture_timestamp_ns: int = 0,
                width: int = 0, height: int = 0) -> Frame:
        if camera not in CAMERAS:
            raise ValueError("Unknown camera")
        if not jpeg or len(jpeg) > self.max_frame_bytes:
            raise ValueError("Encoded frame exceeds the preview buffer limit")
        with self._condition:
            if self._closed:
                raise RuntimeError("Frame buffer is closed")
            self._sequence += 1
            frame = Frame(self._sequence, bytes(jpeg), time.monotonic(),
                          int(capture_timestamp_ns), int(width), int(height))
            self._frames[camera] = frame
            self._condition.notify_all()
            return frame

    def latest(self, camera: str) -> Frame | None:
        with self._condition:
            return self._frames.get(camera)

    def wait(self, camera: str, after: int, timeout: float = 1.0) -> Frame | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or (
                    camera in self._frames and self._frames[camera].sequence > after
                ), timeout=timeout,
            )
            frame = self._frames.get(camera)
            return frame if frame and frame.sequence > after else None

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._condition:
            return {
                camera: {
                    "sequence": frame.sequence,
                    "bytes": len(frame.jpeg),
                    "width": frame.width,
                    "height": frame.height,
                    "capture_timestamp_ns": frame.capture_timestamp_ns,
                    "age_ms": round((now - frame.received_at) * 1000),
                }
                for camera, frame in self._frames.items()
            }

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def finite_vector(value: Any) -> list[float | None]:
    return [float(x) if math.isfinite(float(x)) else None for x in value]


class PreviewState:
    def __init__(self):
        self.frames = LatestFrames()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.phase = "initializing"
        self.error: str | None = None
        self.sdk_version: str | None = None
        self.imu: dict[str, dict[str, Any]] = {}
        self.image_count = 0
        self.imu_count = 0
        self.started_at = time.monotonic()

    def update(self, phase: str, error: str | None = None) -> None:
        with self.lock:
            self.phase, self.error = phase, error

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            result = {
                "source": "Aria Gen 1 SDK; glasses samples only",
                "phase": self.phase,
                "error": self.error,
                "sdk_version": self.sdk_version,
                "uptime_seconds": round(now - self.started_at, 1),
                "image_count": self.image_count,
                "imu_sample_count": self.imu_count,
                "imu": {
                    key: {**{k: v for k, v in sample.items() if k != "received_at"},
                          "age_ms": round((now - sample["received_at"]) * 1000)}
                    for key, sample in self.imu.items()
                },
            }
        result["cameras"] = self.frames.status()
        result["receiving"] = any(f["age_ms"] < 3000 for f in result["cameras"].values()) or any(
            sample["age_ms"] < 3000 for sample in result["imu"].values()
        )
        if result["phase"] == "subscribed_waiting_for_samples":
            if result["receiving"]:
                result["phase"] = "receiving_samples"
            elif result["cameras"] or result["imu"]:
                result["phase"] = "samples_stale"
        return result


class SensorObserver:
    def __init__(self, state: PreviewState, aria: Any, cv2: Any, args: argparse.Namespace):
        self.state, self.cv2, self.args = state, cv2, args
        self.camera_names = {aria.CameraId.Rgb: "rgb", aria.CameraId.Slam1: "slam1",
                             aria.CameraId.Slam2: "slam2"}
        self.next_frame: dict[str, float] = {}
        self.encode_lock = threading.Lock()

    def on_image_received(self, image: Any, record: Any) -> None:
        camera = self.camera_names.get(record.camera_id)
        if camera is None or self.state.stop.is_set():
            return
        # Never let simultaneous SDK callbacks accumulate encoding work.
        if not self.encode_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if now < self.next_frame.get(camera, 0):
                return
            self.next_frame[camera] = now + 1 / self.args.max_fps
            if image.ndim not in (2, 3) or image.nbytes > 32 * 1024 * 1024:
                raise ValueError("Unexpected or oversized SDK image")
            picture = image
            if self.args.rotation:
                rotations = {90: self.cv2.ROTATE_90_CLOCKWISE, 180: self.cv2.ROTATE_180,
                             270: self.cv2.ROTATE_90_COUNTERCLOCKWISE}
                picture = self.cv2.rotate(picture, rotations[self.args.rotation])
            height, width = picture.shape[:2]
            if max(height, width) > self.args.max_size:
                scale = self.args.max_size / max(height, width)
                picture = self.cv2.resize(picture, (max(1, round(width * scale)),
                                                   max(1, round(height * scale))))
            # SDK RGB arrays use RGB channel order; OpenCV's encoder expects BGR.
            if camera == "rgb" and picture.ndim == 3 and picture.shape[2] == 3:
                picture = self.cv2.cvtColor(picture, self.cv2.COLOR_RGB2BGR)
            ok, encoded = self.cv2.imencode(".jpg", picture,
                                            [self.cv2.IMWRITE_JPEG_QUALITY, self.args.jpeg_quality])
            if not ok:
                raise ValueError("JPEG encoding failed")
            height, width = picture.shape[:2]
            self.state.frames.publish(camera, encoded.tobytes(),
                                      capture_timestamp_ns=getattr(record, "capture_timestamp_ns", 0),
                                      width=width, height=height)
            with self.state.lock:
                self.state.image_count += 1
        except Exception as exc:
            if not self.state.stop.is_set():
                self.state.update("decode_error", str(exc))
        finally:
            self.encode_lock.release()

    def on_imu_received(self, motion_data: Any, imu_idx: int) -> None:
        if not motion_data or imu_idx not in (0, 1) or self.state.stop.is_set():
            return
        try:
            sample = motion_data[-1]
            value = {
                "capture_timestamp_ns": int(sample.capture_timestamp_ns),
                "acceleration_m_s2": finite_vector(sample.accel_msec2) if sample.accel_valid else None,
                "angular_velocity_rad_s": finite_vector(sample.gyro_radsec) if sample.gyro_valid else None,
                "received_at": time.monotonic(),
            }
            with self.state.lock:
                self.state.imu[str(imu_idx)] = value
                self.state.imu_count += len(motion_data)
        except Exception as exc:
            self.state.update("decode_error", str(exc))

    def on_streaming_client_failure(self, reason: Any, message: str) -> None:
        self.state.update("sdk_error", f"{reason}: {message}")


def run_sdk(state: PreviewState, args: argparse.Namespace) -> None:
    client = manager = device_client = device = None
    subscribed = started = False
    try:
        import aria.sdk as aria
        import cv2

        with state.lock:
            state.sdk_version = importlib.metadata.version("projectaria-client-sdk")
        aria.set_log_level(aria.Level.Warning)
        if args.start_streaming:
            state.update("connecting_to_glasses")
            device_client = aria.DeviceClient()
            device_config = aria.DeviceClientConfig()
            if args.device_serial:
                device_config.device_serial = args.device_serial
            if args.device_ip:
                device_config.ip_v4_address = args.device_ip
            device_client.set_client_config(device_config)
            device = device_client.connect()
            manager = device.streaming_manager
            config = aria.StreamingConfig()
            config.profile_name = args.profile
            # SDK 2.5 maps its public Usb enum to the Gen 1 USB NCM wire mode.
            config.streaming_interface = aria.StreamingInterface.Usb
            config.security_options.use_ephemeral_certs = args.use_ephemeral_certs
            if args.certs:
                config.security_options.local_certs_root_path = args.certs
            if args.topic_prefix:
                config.topic_prefix = args.topic_prefix
            manager.streaming_config = config
            state.update("starting_streaming")
            manager.start_streaming()
            started = True
            client = manager.streaming_client
        else:
            client = aria.StreamingClient()

        config = client.subscription_config
        config.subscriber_data_type = (aria.StreamingDataType.Rgb | aria.StreamingDataType.Slam
                                       | aria.StreamingDataType.Imu)
        for kind in (aria.StreamingDataType.Rgb, aria.StreamingDataType.Slam, aria.StreamingDataType.Imu):
            config.message_queue_size[kind] = 1
        config.subscriber_name = "AriaUsbRelayPreview"
        if args.topic_prefix:
            config.subscriber_topic_prefix = args.topic_prefix
        options = aria.StreamingSecurityOptions()
        options.use_ephemeral_certs = args.use_ephemeral_certs
        if args.certs:
            options.local_certs_root_path = args.certs
        config.security_options = options
        client.subscription_config = config
        observer = SensorObserver(state, aria, cv2, args)
        client.set_streaming_client_observer(observer)
        state.update("subscribing")
        client.subscribe()
        subscribed = True
        state.update("subscribed_waiting_for_samples")
        state.stop.wait()
    except Exception as exc:
        state.update("sdk_error", f"{type(exc).__name__}: {exc}")
        print(f"Aria SDK: {exc}", flush=True)
    finally:
        for enabled, cleanup in (
            (subscribed, lambda: client.unsubscribe()),
            (started, lambda: manager.stop_streaming()),
            (device is not None, lambda: device_client.disconnect(device)),
        ):
            if enabled:
                try:
                    cleanup()
                except Exception as exc:
                    print(f"SDK cleanup: {exc}", flush=True)


PAGE = b"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aria live preview</title><style>
:root{color-scheme:dark}body{margin:0;padding:18px;background:#10130f;color:#f0f4e9;font:15px system-ui}
h1{font-size:25px;margin:6px 0}p{color:#a1ac99;line-height:1.5}small{color:#bbf77b}
.card{background:#1c211a;padding:15px;border-radius:18px;margin:12px 0}img{display:block;width:100%;border-radius:10px;background:#0b0d0a;min-height:60px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stale{opacity:.24}pre{font:12px monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.label{display:flex;justify-content:space-between;margin:4px 0 10px}.bad{color:#ffba9c}</style></head>
<body><small>PROJECT ARIA / USB PREVIEW</small><h1>Live from the glasses.</h1>
<p>The Android relay forwards USB. This computer decodes the sensor stream. Use the desktop dashboard on port 8766 for connection controls.</p>
<div class="card"><b id="state">Connecting to decoder...</b><p id="detail">Waiting for SDK status.</p></div>
<div class="card"><div class="label"><b>RGB camera</b><span id="rgb-age">No frame</span></div><img id="rgb" src="/stream/rgb.mjpg" alt="Waiting for a glasses RGB frame"></div>
<div class="row"><div class="card"><div class="label">SLAM left <span id="slam1-age"></span></div><img id="slam1" src="/stream/slam1.mjpg" alt="Waiting for SLAM"></div>
<div class="card"><div class="label">SLAM right <span id="slam2-age"></span></div><img id="slam2" src="/stream/slam2.mjpg" alt="Waiting for SLAM"></div></div>
<div class="card"><b>Motion sensors</b><pre id="imu">No IMU samples received.</pre></div>
<p id="counts"></p><script>
const ids=['rgb','slam1','slam2'];
const phases={initializing:'Preparing the decoder.',connecting_to_glasses:'Connecting to the glasses.',starting_streaming:'Starting the sensor stream.',subscribing:'Connecting to the sensor stream.',subscribed_waiting_for_samples:'Connected. Waiting for the first sensor samples.',receiving_samples:'Live USB sensor preview.',samples_stale:'The sensor stream has paused. Waiting to reconnect.'};
function vector(v){return v?v.map(x=>x===null?'?':x.toFixed(3)).join('  '):'unavailable'}
for(const id of ids){const img=document.getElementById(id);img.onerror=()=>setTimeout(()=>img.src='/stream/'+id+'.mjpg?t='+Date.now(),2000)}
async function update(){try{let r=await fetch('/status.json',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();
document.getElementById('state').textContent=s.receiving?'Receiving glasses data':s.error?'Decoder needs attention':'Waiting for glasses data';
document.getElementById('detail').textContent=s.error||phases[s.phase]||'Checking the sensor connection.';
for(let id of ids){let f=s.cameras[id],stale=!f||f.age_ms>3000;document.getElementById(id).classList.toggle('stale',stale);document.getElementById(id+'-age').textContent=f?(stale?'Stale ':'')+(f.age_ms/1000).toFixed(1)+'s':'No frame'}
let text=Object.entries(s.imu).map(([id,m])=>'IMU '+id+(m.age_ms>3000?' (stale)':'')+' | '+(m.age_ms/1000).toFixed(1)+'s ago\\nAcceleration m/s\xc2\xb2: '+vector(m.acceleration_m_s2)+'\\nRotation rad/s: '+vector(m.angular_velocity_rad_s)).join('\\n\\n');
document.getElementById('imu').textContent=text||'No IMU samples received.';document.getElementById('counts').textContent=s.image_count+' preview frames / '+s.imu_sample_count+' IMU samples / SDK '+(s.sdk_version||'unavailable');
}catch(e){document.getElementById('state').textContent='Decoder connection lost';document.getElementById('detail').textContent=e.message;for(let id of ids)document.getElementById(id).classList.add('stale')}
setTimeout(update,500)}update();</script></body></html>"""


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: PreviewState):
        self.state = state
        self.slots = threading.BoundedSemaphore(12)
        self.stream_slots = threading.BoundedSemaphore(6)
        super().__init__(address, PreviewHandler)
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.timeout = 0.5

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PreviewHandler(BaseHTTPRequestHandler):
    server: PreviewServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_: Any) -> None:
        pass

    def reply(self, code: int, content: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in self.server.hosts
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (origin and origin not in {"http://" + host for host in self.server.hosts})):
                self.reply(403, b"Local preview access only", "text/plain")
                return
            if path == "/":
                self.reply(200, PAGE, "text/html; charset=utf-8")
            elif path in ("/status.json", "/api/status"):
                data = json.dumps(self.server.state.status(), allow_nan=False).encode()
                self.reply(200, data, "application/json")
            elif path.startswith("/frame/") and path.endswith(".jpg"):
                camera = path[7:-4]
                frame = self.server.state.frames.latest(camera) if camera in CAMERAS else None
                self.reply(200, frame.jpeg, "image/jpeg") if frame else self.reply(404, b"No frame received", "text/plain")
            elif path.startswith("/stream/") and path.endswith(".mjpg") and path[8:-5] in CAMERAS:
                self.stream(path[8:-5])
            else:
                self.reply(404, b"Not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def stream(self, camera: str) -> None:
        if not self.server.stream_slots.acquire(blocking=False):
            self.reply(503, b"Too many preview viewers", "text/plain")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ariaframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            sequence = 0
            last_frame = time.monotonic()
            while not self.server.state.stop.is_set():
                frame = self.server.state.frames.wait(camera, sequence, 1)
                if frame is None:
                    # A browser may leave while no frames exist. Bound idle streams
                    # so reloads cannot exhaust the viewer slots indefinitely.
                    if time.monotonic() - last_frame > 10:
                        return
                    continue
                sequence = frame.sequence
                last_frame = time.monotonic()
                header = (f"--ariaframe\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame.jpeg)}\r\n"
                          f"X-Capture-Timestamp-Ns: {frame.capture_timestamp_ns}\r\n\r\n").encode()
                self.wfile.write(header + frame.jpeg + b"\r\n")
                self.wfile.flush()
        finally:
            self.close_connection = True
            self.server.stream_slots.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1", help="Loopback HTTP bind address")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--certs", help="Existing SDK streaming certificate directory")
    parser.add_argument("--use-ephemeral-certs", action="store_true", help="Match a session started with ephemeral certificates")
    parser.add_argument("--topic-prefix", help="Match a custom streaming topic prefix")
    parser.add_argument("--start-streaming", action="store_true", help="Also start/stop streaming through the official SDK")
    parser.add_argument("--interface", choices=("usb",), default="usb", help="Use the SDK's supported USB transport")
    parser.add_argument("--profile", default="profile12")
    parser.add_argument("--device-serial")
    parser.add_argument("--device-ip")
    parser.add_argument("--max-fps", type=float, default=10, help="Maximum preview rate per camera")
    parser.add_argument("--max-size", type=int, default=960, help="Maximum JPEG width/height")
    parser.add_argument("--jpeg-quality", type=int, default=75)
    parser.add_argument("--rotation", type=int, choices=(0, 90, 180, 270), default=90)
    args = parser.parse_args()
    if not (0 < args.max_fps <= 60 and 64 <= args.max_size <= 2048 and 10 <= args.jpeg_quality <= 95):
        parser.error("Use max-fps in (0,60], max-size in [64,2048], and jpeg-quality in [10,95].")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be in [1,65535].")
    return args


def main() -> None:
    args = parse_args()
    state = PreviewState()
    server = PreviewServer((args.host, args.port), state)

    def stop(*_: Any) -> None:
        state.stop.set()
        state.frames.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker = threading.Thread(target=run_sdk, args=(state, args), daemon=True, name="aria-sdk")
    worker.start()
    print(f"Aria preview: http://{args.host}:{args.port} (real sensor samples only)", flush=True)
    try:
        while not state.stop.is_set():
            server.handle_request()
    finally:
        stop()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
