#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEVICE=""
APK="$ROOT/app/build/outputs/apk/debug/app-debug.apk"
while (($#)); do
  case "$1" in
    --device) DEVICE="${2:?Missing phone serial}"; shift 2 ;;
    --apk) APK="${2:?Missing APK path}"; shift 2 ;;
    *) echo 'Usage: bash scripts/install.sh --device PHONE [--apk PATH]' >&2; exit 2 ;;
  esac
done
[[ -n $DEVICE ]] || { echo '--device is required.' >&2; exit 2; }
[[ -f $APK ]] || { echo "APK not found: $APK. Build assembleDebug first." >&2; exit 2; }
command -v adb >/dev/null || { echo 'Install Android platform-tools first.' >&2; exit 2; }
if [[ -f $ROOT/.research/live-run/session.pid ]]; then
  echo 'A live session record exists. Stop the transport before updating the phone app.' >&2
  exit 1
fi
timeout 120 adb -s "$DEVICE" install -r "$APK"
timeout 15 adb -s "$DEVICE" shell am start -n dev.aria.usb/.MainActivity
echo 'Phone bridge installed and opened.'
