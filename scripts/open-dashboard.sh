#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID != 0 ]] || { echo 'Launch this dashboard as your normal Linux user; it requests sudo only for USB transport.' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Install Python 3 first.' >&2; exit 1; }
echo 'Authorize the USB transport helper. The dashboard and SDK run as your normal user.'
sudo -v
cd "$ROOT"
# Preserve this controlling TTY so later sudo -n uses the same authorization.
exec python3 desktop/server.py "$@"
