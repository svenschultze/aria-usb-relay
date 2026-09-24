#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $EUID == 0 ]]; then
  exec python3 "$ROOT/scripts/wsl-session.py" "$@"
fi
if ! sudo -n true 2>/dev/null; then
  echo 'Run `sudo -v` in the dashboard launch terminal, then retry stopping. No unrelated process was signaled.' >&2
  exit 1
fi
exec sudo -n python3 "$ROOT/scripts/wsl-session.py" "$@"
