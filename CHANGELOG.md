# Changelog

## 0.1.0 — release candidate

- Android foreground USB relay for Project Aria Gen 1, with bounded USB/IP transfers and device connection state.
- Windows/WSL and native Linux launchers, explicit device selection, scoped networking, and session cleanup helpers.
- Official SDK camera/IMU preview with bounded latest-sample buffers and stale-data reporting.
- Local desktop dashboard for device setup, checks, session controls, and live viewing.
- Debug-only direct USB telemetry and private diagnostic mailbox.
- Offline protocol/lifecycle/desktop tests, Android build/lint CI, publication audit, and optional external release signing.

The current APK was installed through the desktop dashboard, and the new Windows/WSL controller pipeline delivered all three cameras and both IMUs on the tested Gen 1/Pixel setup. Clean Stop → Start, a 68-second screen-off observation, and recovery after a brief ADB interruption followed by explicit phone reconnect were verified. Native Linux has mocked workflow and offline validation only. Prolonged Doze reliability, automatic network reconnection, durable sensor recording/replay, audio, and standalone Android sensor decoding remain outstanding. See [compatibility](docs/compatibility.md). No published-release date is claimed here.
