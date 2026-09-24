#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [[ $EUID == 0 ]]; then
  exec bash "$ROOT/scripts/wsl-live.sh" --user "$TARGET_USER" "$@"
fi
if ! sudo -n true 2>/dev/null; then
  echo 'USB transport needs sudo authorization. Run `sudo -v` in this terminal, then retry; or start scripts/open-dashboard.sh from this terminal.' >&2
  exit 1
fi
exec sudo -n bash "$ROOT/scripts/wsl-live.sh" --user "$TARGET_USER" "$@"
