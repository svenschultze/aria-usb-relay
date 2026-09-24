# Compatibility and validation

| Area | Supported or tested scope |
| --- | --- |
| Glasses | Aria Gen 1; Gen 2 is a different SDK/transport and is unsupported |
| Android API | Minimum 28, compile/target 36; hardware tested on Pixel 9 Pro XL Android 17/API 37 |
| Windows | PowerShell 7, Python 3.10+, Platform-Tools, WSL2 Ubuntu with mirrored networking |
| Linux SDK | Official `projectaria-client-sdk==2.5.0`; users install it separately |
| WSL hardware path | Dashboard APK installation and new controller startup verified on Ubuntu 22.04; real data from three cameras and two IMUs |
| Background observation | App left via Home; screen-off observations 68 seconds apart while sensor delivery continued |
| Brief interruption | Approximately 6-second wireless ADB interruption, explicit phone reconnect, automatic restoration of the owned tunnel/import, no decoder restart |
| Native Linux | Mocked launcher/workflow and offline tests pass; complete native hardware run still needed |
| macOS | No supported phone-relay launcher in this release |
| Sensors | RGB, both SLAM cameras, both IMUs; audio is not implemented |
| Storage | Bounded preview and phone connection journal; no offline sensor recording/replay |

The Gen 1 SDK's public USB option selected NCM on the tested SDK/firmware. The observed USB product IDs were `2833:0083` before streaming and `2833:0300` in NCM mode. Other firmware may enumerate differently. Do not use device model names alone as evidence that every interface is compatible.

The current Windows/WSL pipeline has delivered live data after APK installation through the desktop dashboard, clean Stop → Start, and the brief checks above. This does not establish lossless throughput, multi-hour stability, prolonged Doze reliability, all Android vendors, or automatic restoration of the phone's network/ADB connection. Report additional validated configurations with software versions and sanitized observations rather than private device identifiers.

Offline CI tests cover protocol framing, transport behavior, lifecycle policy, bounded preview state, orchestration checks, and local desktop request handling. CI does not run the official SDK against physical glasses or simulate real sensor capture. See [the validation record](research/usbip-live-run.md).
