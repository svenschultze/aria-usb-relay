# Setup

## Requirements

Aria USB Relay needs Project Aria **Gen 1**, a USB data cable, and an Android phone supporting USB host mode. Enable glasses ADB through the official Companion app. Android USB access is granted through the system dialog; the phone must act as host. The phone's USB port is occupied by the glasses, so laptop control uses wireless ADB.

The laptop needs Python 3.10+, Android Platform-Tools, and the official Gen 1 SDK in Linux. Windows uses PowerShell 7 and WSL2 Ubuntu. The dashboard itself has no third-party Python dependencies; sensor decoding needs the dependencies in `bridge/requirements.txt`.

## Windows and WSL

Install WSL2 Ubuntu using Microsoft's [WSL installation instructions](https://learn.microsoft.com/windows/wsl/install). The verified setup uses Ubuntu 22.04 and mirrored networking. In your user-owned `.wslconfig`, mirrored networking is selected with:

```ini
[wsl2]
networkingMode=mirrored
```

WSL configuration changes take effect after restarting WSL. `wsl --shutdown` stops all running WSL distributions, so finish their work before using it. See [Microsoft's networking documentation](https://learn.microsoft.com/windows/wsl/networking) for Windows version requirements and firewall behavior. Do not disable the firewall globally.

Check that the Linux kernel supplies `vhci_hcd` and `cdc_ncm`. A working WSL installation does not guarantee those modules are available. The importer uses Linux's virtual USB host controller; the SDK's USB transport becomes a CDC NCM network interface.

From the repository, the explicit setup helper installs Linux packages and creates an SDK virtual environment:

```powershell
wsl -d Ubuntu-22.04 -u root --cd /mnt/c/path/to/aria-android --exec bash scripts/setup-wsl.sh --user YOUR_LINUX_USER
```

Options include `--sdk-path VENV` and `--sdk-version 2.5.0`. Review helper output before proceeding. Setup does not enable glasses ADB, pair the SDK, or grant phone USB permission. If an existing SDK environment already works, select it with `-SdkPath` rather than replacing it.

For first-time SDK pairing, temporarily connect the glasses directly to the computer. On Windows, make that specific device visible inside WSL using [Microsoft's USB passthrough instructions](https://learn.microsoft.com/windows/wsl/connect-usb): install `usbipd-win`, identify the glasses with `usbipd list`, bind only their bus ID from an administrator terminal, then attach that bus ID to WSL. Native Linux can use the direct USB connection. This one-time direct connection is separate from the phone relay.

Activate the SDK environment and run [official SDK pairing](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/setup) (`aria auth pair`) as the normal Linux user who will run the decoder. Approve the matching request in Aria Companion. Check `aria auth check`, detach the temporary WSL USB attachment if used, and reconnect the glasses to the phone. The dashboard deliberately requires an existing SDK identity before starting; phone ADB pairing alone does not satisfy this step. If this Linux user's SDK is already paired, retain that identity and skip this procedure.

Control identity and streaming certificates serve different purposes. The launcher uses your existing control pairing and ephemeral streaming certificates.

The Windows and Linux ADB servers must remain separate. Windows ADB uses 5037 for the phone's wireless connection and forwarding. The launchers use 5038 for Linux SDK access to the imported USB device. `-LinuxAdbPort` can select another unused port; it must not be 5037.

## Android build and installation

The project uses AGP 9.0.0, Gradle 9.2.1, Java 17 source compatibility, compile/target SDK 36, and Build-Tools 36.1.0. Use JDK 17+; CI uses JDK 21. Set `ANDROID_SDK_ROOT`/`ANDROID_HOME`, or create ignored `local.properties` with your SDK location. No keystore is needed for a debug build.

```powershell
.\build.ps1 assembleDebug --no-daemon
```

`build.ps1` can discover the JDK bundled with Android Studio and sets a workspace-local JDK socket directory on Windows. APK output is `app/build/outputs/apk/debug/app-debug.apk`. On Linux, use `bash gradlew assembleDebug --no-daemon`.

On the phone, enable Developer options and Wireless debugging. **Pair device with pairing code** shows a temporary pairing endpoint; the ordinary debugging page shows a different connection endpoint. The desktop dashboard can perform both operations. CLI equivalents are:

```text
adb pair PHONE_IP:PAIRING_PORT
adb connect PHONE_IP:CONNECTION_PORT
adb devices -l
```

`adb pair` asks for the code. Do not put it in a shell history or issue report. Then install with the dashboard or:

```powershell
.\scripts\install.ps1 -Device 'PHONE_IP:CONNECTION_PORT'
```

The installer chooses an explicit device and defaults to the debug APK. `-ApkPath` selects another artifact; `-AdbPath` selects Platform-Tools outside the usual SDK paths.

## Desktop and CLI

```powershell
.\scripts\open-dashboard.ps1
```

Alternatively, `python desktop/server.py --no-open` runs the server without opening a browser. The local dashboard is at `http://127.0.0.1:8766/`. It stores local preferences under ignored `.local/`; it does not require an account or cloud service.

Closing the browser tab does not stop streaming. Keep the launcher terminal running while using the dashboard. Ctrl+C requests dashboard shutdown and attempts to stop the stream that dashboard started. If the launcher is forcibly terminated or cleanup fails, use the stop helper from another terminal.

CLI checks and session control:

```powershell
.\scripts\doctor.ps1 -Device 'PHONE_IP:CONNECTION_PORT' -Json
.\scripts\start-live.ps1 -Device 'PHONE_IP:CONNECTION_PORT'
.\scripts\stop-live.ps1 -Check
.\scripts\stop-live.ps1
```

The doctor is read-only and returns `ready` plus a `checks` array in JSON mode. Each check has an identifier, label, `pass`/`warn`/`fail` status, and detail. A passing offline check is not proof that sensor frames will arrive.

## Native Linux

Native Linux uses the same phone relay, USB/IP importer, official SDK, and preview decoder. An apt-based distribution with the required kernel modules is the intended first-release path. Mocked launcher/workflow tests and offline checks pass; a complete native Linux hardware session has not been verified.

Use current [official Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools) for the phone connection, and place its directory before a distribution-provided `adb` on `PATH`. For example, [Ubuntu 22.04's packaged ADB](https://packages.ubuntu.com/en/jammy/adb) predates wireless pairing support. The doctor checks that the selected ADB exposes `pair` and reports how to correct it. Installing the Linux dependency packages alone is not sufficient for wireless pairing on those distributions.

Install prerequisites explicitly, then open the dashboard as your normal user:

```bash
bash scripts/setup-wsl.sh --user "$USER"
bash scripts/open-dashboard.sh
```

Despite its historical name, `setup-wsl.sh` also supports native apt-based Linux. `open-dashboard.sh` obtains sudo authorization in the terminal for USB import and network setup; the web server and SDK run as your normal user. Keep the terminal available. Normal-user SDK pairing is still required.

The native CLI accepts the phone's wireless ADB serial:

```bash
bash scripts/doctor-linux.sh --json --device PHONE_IP:CONNECTION_PORT
bash scripts/start-live.sh --device PHONE_IP:CONNECTION_PORT
bash scripts/stop-live.sh --check
bash scripts/stop-live.sh
```

`start-live.sh` also accepts `--sdk-path`, `--serial`, `--profile`, and `--user`. Run `sudo -v` in the terminal when authorization expires; the launcher does not collect a sudo password through the browser. Install the app with `bash scripts/install.sh --device PHONE_IP:CONNECTION_PORT`; `--apk PATH` selects another APK.

## Permissions and background operation

Allow Android USB access and foreground-service notifications. The service retains USB ownership while the UI is backgrounded and handles attach/detach events, but Android may require USB permission after reconnecting. The laptop decoder must remain running. A foreground service and wake lock do not guarantee Wi-Fi availability or exemption from Doze and vendor power controls. If background interruptions occur, inspect the app's battery settings and the phone's wireless debugging state.
