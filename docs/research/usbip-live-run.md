# Gen 1 USB relay: hardware validation record

Test date: 2026-09-24. Device identifiers, account names, local endpoints, certificates, raw sensor snapshots, and camera images are intentionally omitted.

## Tested configuration

| Component | Observed configuration |
| --- | --- |
| Glasses | Project Aria Gen 1 |
| Phone | Google Pixel 9 Pro XL, Android 17 / API 37 |
| Laptop | Windows, WSL2 Ubuntu 22.04, mirrored networking |
| Linux kernel | 6.6.87.2-microsoft-standard-WSL2 |
| USB drivers | `vhci_hcd`, `cdc_ncm` |
| SDK | Official Gen 1 Client SDK 2.5.0 |
| Stream | `profile12`, USB, ephemeral streaming certificates |
| Control identity | Existing user pairing; no certificate replacement |

The glasses initially enumerated as `2833:0083` with MTP/ADB. SDK USB streaming changed the device to `2833:0300` with CDC NCM and ADB. The import watcher followed re-enumeration; scoped DHCP configured the imported interface without changing the laptop's DNS or default route. The standard SDK USB option selected NCM.

## Results

The original one-command Windows/WSL launcher reached `receiving_samples`. At 65.9 seconds, a status snapshot counted 937 decoded preview frames and 62,280 IMU samples. RGB, both SLAM cameras, and both IMUs were fresh. Displayed frames were 960 × 960 for RGB and 480 × 640 for each rotated SLAM view. Live RGB was visually verified in the original phone preview.

The 0.1 desktop integration was then tested with the revised Android app and controller. The APK was installed through the dashboard. Starting from that dashboard established the owned Windows ADB forward, Linux USB/IP import, NCM/DHCP connection, and official SDK subscription. Fresh RGB, both SLAM cameras, and both IMUs reached the live preview. The SDK/decoder ran as the normal Linux user while the transport ran as root, and the laptop's default route remained unchanged.

The revised controller also completed a clean Stop → Start with selected-phone and virtual-USB ownership checks active.

For the screen-off check, the app was left via Home. Android reported `mWakefulness=Dozing` initially and 68 seconds later, with the foreground service and partial wake lock active. Cameras and IMUs continued delivering: at 559 seconds of preview uptime, status remained `receiving_samples` with 8,732 preview images and 653,797 IMU samples. This validates a short screen-off observation, not prolonged Doze or deep-idle reliability.

A controlled wireless ADB disconnect targeted only the selected phone. An explicit `adb connect` restored that phone after approximately six seconds. The Windows launcher recreated the same owned forwarding port; the USB/IP watcher moved from disconnected/waiting back to an attached NCM device. At the next measured checkpoint, 33.2 seconds after baseline, preview uptime had advanced from 63.9 to 97.1 seconds without a decoder restart. Images increased from 967 to 1,336 and IMU samples from 82,000 to 113,780; RGB age was 51 ms and IMU age 19 ms. This validates relay recovery after an explicit phone reconnect. It does not establish automatic network reconnection or lossless delivery during the interruption.

These are observations from short runs, not throughput or reliability guarantees. DDS queue-full drops occurred. The latest-frame preview intentionally favors recent data and does not provide lossless recording. Native Linux has mocked launcher/workflow tests and offline checks; its complete hardware path has not been tested separately from WSL.

An earlier failed configuration change left the USB path stalled. Stopping the session and physically unplugging/reconnecting the glasses recovered it. Requesting the already selected USB configuration is now acknowledged without physically reconfiguring the Android-owned device; see [the USB/IP implementation notes](../usbip-protocol.md).

The laptop performs DDS subscription and image decoding. No standalone Android sensor decoder, audio path, offline recording, long-duration reliability, or native Linux hardware session was validated in this run.

## Reproduction

Use the current [setup instructions](../setup.md) and launcher rather than copying identifiers from an old run. The expected sequence is Android USB permission, scoped USB/IP import, SDK control authentication, NCM re-enumeration, DHCP, matching streaming certificates, and real sensor callbacks.

Local logs and raw snapshots remain under ignored runtime/research directories. Do not publish them without reviewing and redacting their contents.

Sources: [Linux USB/IP protocol](https://docs.kernel.org/usb/usbip_protocol.html), [official Aria SDK troubleshooting](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/sdk_troubleshooting).
