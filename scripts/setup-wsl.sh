#!/usr/bin/env bash
# Explicit setup for Ubuntu/Debian Linux or WSL. Nothing calls this automatically.
set -euo pipefail
TARGET_USER="${SUDO_USER:-$(id -un)}"
SDK=""
SDK_VERSION=2.5.0
while (($#)); do
  case "$1" in
    --user) TARGET_USER="${2:?Missing user}"; shift 2 ;;
    --sdk-path) SDK="${2:?Missing SDK environment}"; shift 2 ;;
    --sdk-version) SDK_VERSION="${2:?Missing official SDK version}"; shift 2 ;;
    *) echo 'Usage: bash scripts/setup-wsl.sh [--user USER] [--sdk-path VENV] [--sdk-version 2.5.0]' >&2; exit 2 ;;
  esac
done
[[ $TARGET_USER != root ]] || { echo 'Specify --user for the normal Linux user who will own the SDK and pairing.' >&2; exit 2; }
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -n $USER_HOME ]] || { echo 'Unknown user.' >&2; exit 2; }
SDK="${SDK:-$USER_HOME/.local/share/aria-usb-relay/venv}"
[[ $SDK = /* ]] || { echo '--sdk-path must be absolute.' >&2; exit 2; }
command -v apt-get >/dev/null || { echo 'This setup supports apt-based Linux. Install python3-venv, adb, iproute2, isc-dhcp-client and kmod manually, then create an official SDK venv.' >&2; exit 2; }
as_root() { if [[ $EUID == 0 ]]; then "$@"; else sudo "$@"; fi; }
as_user() { if [[ $(id -un) == "$TARGET_USER" ]]; then "$@"; else runuser -u "$TARGET_USER" -- env HOME="$USER_HOME" "$@"; fi; }
echo "Installing Linux prerequisites and official SDK $SDK_VERSION in $SDK. Existing Aria pairing certificates are not changed."
as_root apt-get update
as_root apt-get install -y python3 python3-venv python3-pip adb iproute2 isc-dhcp-client kmod
as_user python3 -m venv "$SDK"
as_user "$SDK/bin/python" -m pip install "projectaria-client-sdk==$SDK_VERSION" numpy opencv-python-headless
echo 'Setup complete. Run scripts/doctor-linux.sh --json as the normal user.'
echo 'For native Linux phone pairing, install current official Android SDK Platform-Tools and put its adb first on PATH; distro adb can be too old.'
echo 'Official download: https://developer.android.com/tools/releases/platform-tools'
echo 'The host kernel must independently provide vhci_hcd and cdc_ncm. Setup does not replace kernels or alter sudo policy.'
