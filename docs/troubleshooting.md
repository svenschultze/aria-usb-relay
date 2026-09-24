# Troubleshooting

Run the desktop checks or `scripts/doctor.ps1 -Device PHONE_IP:CONNECTION_PORT -Json` first. Native Linux uses `bash scripts/doctor-linux.sh --json --device PHONE_IP:CONNECTION_PORT`. Keep diagnostic exports private until reviewed; logs can contain device metadata.

| Symptom | Check or recovery |
| --- | --- |
| Phone absent from discovery | Enable Wireless debugging; confirm network reachability; connect manually to the current debugging endpoint. mDNS discovery may not cross subnets. |
| Pairing fails | Use the temporary endpoint from **Pair device with pairing code**, not the connection endpoint. Reopen the pairing dialog if its code expired. |
| Native Linux ADB has no `pair` command | Install current official Android Platform-Tools and put it first on `PATH`; older distribution packages may predate wireless pairing. Rerun the doctor. |
| ADB shows multiple entries | Select the explicit connected device. A phone may appear by both an endpoint and an mDNS name. |
| Glasses absent on phone | Use a data-capable cable, confirm phone host mode, power on glasses, and enable their ADB access in Companion. |
| Waiting for USB access | Accept the Android system dialog. Permission may be requested again after NCM re-enumeration or reconnect. |
| SDK authentication fails | Pair the official SDK as the normal Linux user. Do not delete or overwrite an existing identity as a troubleshooting shortcut. |
| SDK device-not-reachable / error 814 | Check importer and DHCP logs. The NCM interface must have a lease before network control and DDS can work. |
| Windows port 3240 is unavailable | Use the launcher; it requests a dynamic local port instead of binding a reserved Windows port. |
| Linux ADB cannot see imported glasses | Check virtual USB import and isolated server port 5038. Linux SDK and its ADB server must use the same port. |
| SDK ready but no frames | Check matching certificate mode, the NCM interface, subscription state, and DDS connectivity. Successful USB import alone is insufficient. |
| Preview is stale or drops frames | Confirm phone-to-laptop connectivity and decoder health. Queues intentionally discard older samples; reduce preview cost before assuming lossless performance. |
| Stream stops with the screen off | Check Android battery settings, wireless debugging, and Wi-Fi reachability. Foreground service operation does not guarantee an uninterrupted network. |
| Stop helper refuses a PID | It could not prove session ownership. Inspect the current launcher and local logs; do not kill broad process-name matches. |

An interrupted physical USB configuration can require unplugging and reconnecting the glasses. Stop the session, reconnect the cable, accept permission, then launch again. This recovery was needed in hardware testing; a software retry is not guaranteed to restore every USB state.

The backend logs are local runtime files, normally under `.research/live-run/` for the original launcher. The desktop preferences and diagnostics are under ignored `.local/`. Follow the current launcher output for the exact log location. Include tool versions and error categories in reports; omit pairing codes, certificates, serial numbers, local addresses, and captured images.

For developer-only direct telemetry diagnostics, first stop the laptop stream. A debug APK can explicitly enter direct USB diagnostics with:

```text
adb -s PHONE_IP:CONNECTION_PORT shell am start -n dev.aria.usb/.MainActivity --ez connect_usb true
```

Approve USB permission if requested, then use:

```powershell
.\scripts\probe.ps1 -Device 'PHONE_IP:CONNECTION_PORT' -Command 'getprop ro.product.model'
```

The command runs on the glasses through the app's current USB ADB session. It requires direct telemetry mode, not USB/IP relay mode. The private mailbox is accessed through `run-as`, has bounded request/response sizes and timeouts, and does not open an external command listener. Release builds do not process it. Use read-only commands when investigating hardware.
