# Architecture

Aria USB Relay separates physical USB access from sensor decoding. The phone owns the attached glasses through Android's USB Host API; Linux runs the official SDK unchanged.

| Component | Responsibility | Boundary |
| --- | --- | --- |
| Android foreground service | USB permission, USB/IP export, attach/detach state, bounded journal | Phone USB device; loopback exporter |
| Windows/native launcher | Device selection, ADB forwarding, session ownership and cleanup | Authenticated wireless ADB |
| Linux transport helpers | Virtual USB import and scoped NCM DHCP | Root only for USB/network operations |
| `bridge/preview.py` | SDK subscription, image decoding, latest camera/IMU samples | Normal Linux user; HTTP loopback 8765 |
| `desktop/server.py` | Local controls, checks, diagnostics, live dashboard | Laptop loopback 8766 |

The Android exporter listens on phone loopback port 3240. ADB maps a dynamically allocated laptop port to that endpoint. Linux's `vhci_hcd` driver imports the real composite USB device. SDK control authenticates through the imported ADB interface. Starting USB streaming causes the glasses to re-enumerate into CDC NCM mode; the watcher reimports them and DHCP configures only the corresponding Meta USB network interface. The helper does not replace the laptop's default route or DNS.

The SDK and preview use the normal Linux user's existing control identity. Ephemeral certificates match stream startup and subscription. Pairing remains the official Companion-approved process. Windows ADB 5037 and Linux ADB 5038 are separate to avoid mirrored-localhost conflicts.

The decoder subscribes to RGB, SLAM, and IMU. It keeps one JPEG per camera and the newest sample for each IMU. Preview defaults cap each camera at 10 encoded frames/s and 960 pixels on its longest side. Those caps are display limits, not sensor acquisition or recording rates. Slow consumers do not accumulate an unbounded queue. Freshness comes from actual callbacks, not generated samples.

Android direct telemetry is a separate mode using a small app-owned ADB implementation over USB. It cannot share physical USB ownership with the USB/IP relay. Telemetry journals retain two approximately 4 MiB segments; they do not contain durable camera/IMU recordings. The private diagnostic mailbox is restricted to debug builds and runs through authenticated `adb shell run-as`.

The USB/IP implementation is described in [protocol notes](usbip-protocol.md). Isochronous transfers are unsupported, and Android's public USB APIs do not expose exact native URB errno values. Those limits matter for extending this tool to other devices or sensors.

Session helpers record the orchestrator identity and verify its process start time and exact workspace command before signaling it. Cleanup requests stream stop, terminates owned helpers, detaches owned virtual USB state, and removes the launcher's tunnels. Closing the browser tab leaves the stream active. A graceful dashboard-launcher shutdown attempts to stop the stream it started; it does not automatically stop an independently launched session. Forced process termination can interrupt cleanup, so a verified stop helper remains available.
