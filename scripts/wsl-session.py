#!/usr/bin/env python3
"""Record or stop only this workspace's verified WSL live launcher."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
PIDFILE = ROOT / ".research/live-run/session.pid"


def identity(pid):
    proc = Path("/proc") / str(pid)
    argv = proc.joinpath("cmdline").read_bytes().split(b"\0")[:-1]
    cwd = proc.joinpath("cwd").resolve(strict=True)
    executable = proc.joinpath("exe").resolve(strict=True).name
    start_ticks = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
    return validate_identity(pid, argv, executable, cwd, start_ticks)


def validate_identity(pid, argv, executable, cwd, start_ticks, root=ROOT):
    if len(argv) < 2 or not (executable == "bash" or executable.startswith("python")):
        raise ValueError("Recorded process is not a supported live launcher")
    script = Path(os.fsdecode(argv[1]))
    if not script.is_absolute():
        script = cwd / script
    allowed = {root / "scripts/wsl-live.sh", root / "scripts/wsl_live.py"}
    if cwd != root or script.resolve() not in allowed:
        raise ValueError("Recorded process belongs to a different command or workspace")
    return {"pid": pid, "start_ticks": start_ticks}


def verified(record):
    try:
        return identity(record['pid']) == {key: record[key] for key in ('pid', 'start_ticks')}
    except (OSError, ValueError, KeyError):
        return False


def status():
    if not PIDFILE.exists():
        return dict(running=False, stale=False)
    recorded = read_record()
    running = verified(recorded)
    return dict(running=running, stale=not running, pid=recorded['pid'])


def read_record():
    value = json.loads(PIDFILE.read_text())
    if type(value.get("pid")) is not int or value["pid"] <= 1:
        raise ValueError("Invalid session PID")
    return value


def clear(expected):
    if PIDFILE.exists() and read_record() == expected:
        PIDFILE.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--register", type=int, metavar="PID")
    actions.add_argument("--clear", type=int, metavar="PID")
    actions.add_argument("--check", action="store_true", help="Validate the session without stopping it")
    actions.add_argument("--status-json", action="store_true", help="Read machine-readable session state")
    args = parser.parse_args()
    if args.status_json:
        print(json.dumps(status()))
        return
    if args.register is not None:
        current = identity(args.register)
        if PIDFILE.exists():
            previous = read_record()
            try:
                still_running = verified(previous)
            except (OSError, ValueError):
                still_running = False
            if still_running and previous != current:
                raise ValueError("This workspace already has a live session; stop it first")
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PIDFILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(current) + "\n")
        temporary.replace(PIDFILE)
        print(f"Registered live session {current['pid']}")
        return
    if args.clear is not None:
        if PIDFILE.exists():
            recorded = read_record()
            if recorded["pid"] == args.clear:
                clear(recorded)
        return
    if not PIDFILE.exists():
        print("No live session is registered for this workspace.")
        return
    recorded = read_record()
    if not verified(recorded):
        if args.check:
            print('The session record is stale; no files changed and no process was signaled.')
            return
        clear(recorded)
        print('Removed a stale session record; no process was signaled.')
        return
    # A pidfd prevents signaling a replacement process if the launcher exits
    # between identity validation and the signal.
    with os.fdopen(os.pidfd_open(recorded["pid"]), "rb", buffering=0) as process:
        if not verified(recorded):
            raise ValueError("Session identity changed; refusing to signal it")
        if args.check:
            print(f"Verified live session {recorded['pid']} in {ROOT}; no signal sent.")
            return
        signal.pidfd_send_signal(process.fileno(), signal.SIGTERM)
        print(f"Requested clean shutdown of live session {recorded['pid']}.", flush=True)
        if not select.select([process], [], [], 45)[0]:
            raise TimeoutError("Cleanup is still running after 45 seconds; no forced kill was sent")
    clear(recorded)
    print("Live session stopped.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Session check failed: {error}", file=sys.stderr)
        sys.exit(1)
