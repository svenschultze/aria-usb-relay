#!/usr/bin/env python3
"""Owned, bounded Linux/WSL USB relay controller. SDK code runs as a normal user."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from usbip_attach import read_status

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.research/live-run'
SPEC = importlib.util.spec_from_file_location('session_state', ROOT / 'scripts/wsl-session.py')
session = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session)


class OwnedProcess:
    """Own exactly one child process group and retain at most 128 KiB of output."""
    def __init__(self, argv, name, environment=None):
        self.name = name
        self.tail = bytearray()
        self.lock = threading.Lock()
        self.path = RUNTIME / (name + '.log')
        self.process = subprocess.Popen(argv, cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            with self.path.open('ab', buffering=0) as output:
                while True:
                    chunk = os.read(self.process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    if output.tell() + len(chunk) > 1024 * 1024:
                        output.close()
                        older = self.path.with_suffix('.log.2')
                        previous = self.path.with_suffix('.log.1')
                        if older.exists(): older.unlink()
                        if previous.exists(): previous.replace(older)
                        self.path.replace(previous)
                        output = self.path.open('ab', buffering=0)
                    output.write(chunk)
                    with self.lock:
                        self.tail.extend(chunk)
                        del self.tail[:-131072]
                output.close()
        except OSError:
            pass

    def text(self):
        with self.lock:
            return self.tail.decode('utf-8', errors='replace')

    def stop(self, seconds=5):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(seconds)
            except subprocess.TimeoutExpired:
                # The process group was created by this object and is still owned.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(2)
        self.reader.join(1)
        if not self.reader.is_alive() and not self.process.stdout.closed:
            self.process.stdout.close()


def valid_port(value):
    result = int(value)
    if not 1024 <= result <= 65535:
        raise argparse.ArgumentTypeError('Port must be between 1024 and 65535')
    return result


def choose_aria(output, requested=None, owned_busid=None):
    if not owned_busid:
        return None
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == 'device' and 'model:Aria' in fields:
            if 'usb:' + owned_busid in fields[2:]:
                devices.append(fields[0])
    if requested:
        return requested if requested in devices else None
    if len(devices) > 1:
        raise RuntimeError('Multiple imported Aria devices found; specify --serial.')
    return devices[0] if devices else None


def owned_busid(record, ports):
    """Bind ADB selection to this import, not other glasses attached to Linux."""
    if record.get('vendor') != 0x2833:
        return None
    expected = (record.get('busnum', -1) << 16) | record.get('devnum', -1)
    for port in ports:
        if (port['port'] == record.get('vhci_port') and port['status'] == 6
                and port['devid'] == expected and port.get('local_busid') not in (None, '0-0')):
            return port['local_busid']
    return None


def owns_forward(output, device, port):
    expected = [device, f'tcp:{port}', 'tcp:3240']
    return any(line.split() == expected for line in output.splitlines())


class Controller:
    def __init__(self, args):
        self.args = args
        self.user = pwd.getpwnam(args.user)
        if self.user.pw_uid == 0:
            raise ValueError('Select the normal Linux user who owns the SDK pairing, not root.')
        self.stopping = threading.Event()
        self.children = []
        self.preview = None
        self.stream_attempted = False
        self.native_forward = None
        self.forward_check_at = 0
        self.forward_message = None
        self.serial = args.serial
        self.sdk = None
        self.adb = None
        # A crashed older watcher cannot publish ownership into this session.
        self.import_file = RUNTIME / ('import-' + uuid.uuid4().hex + '.json')
        if args.launch_token:
            cancellation = RUNTIME / ('windows-cancel.' + args.launch_token)
            def watch_cancel():
                while not self.stopping.wait(0.2):
                    if cancellation.exists():
                        self.stopping.set()
            threading.Thread(target=watch_cancel, daemon=True).start()

    def as_user(self, argv):
        return ['runuser', '-u', self.user.pw_name, '--', 'env',
                'HOME=' + self.user.pw_dir, 'ANDROID_ADB_SERVER_PORT=' + str(self.args.adb_port), *argv]

    def spawn(self, argv, name, user=False):
        child = OwnedProcess(self.as_user(argv) if user else argv, name)
        self.children.append(child)
        return child

    def run(self, argv, name, timeout=30, user=False, cleanup=False, check=True):
        child = self.spawn(argv, name, user)
        deadline = time.monotonic() + timeout
        try:
            while child.process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'{name} timed out; see {child.path}')
                time.sleep(0.1)
                if self.stopping.is_set() and not cleanup:
                    raise InterruptedError('Session stopped')
            child.reader.join(1)
            output = child.text()
            if check and child.process.returncode:
                raise RuntimeError(f'{name} failed: {output[-3000:]}')
            return output
        finally:
            child.stop()
            self.children.remove(child)

    def phone_adb(self, arguments, name='phone', cleanup=False):
        # The phone belongs to the normal user's existing ADB server on 5037.
        command = ['runuser', '-u', self.user.pw_name, '--', 'env', 'HOME=' + self.user.pw_dir,
                   'ANDROID_ADB_SERVER_PORT=5037', self.phone_binary, '-P', '5037',
                   '-s', self.args.device, *arguments]
        return self.run(command, name, timeout=15, cleanup=cleanup)

    def ensure_native_forward(self):
        if not self.native_forward or time.monotonic() < self.forward_check_at:
            return
        self.forward_check_at = time.monotonic() + 5
        try:
            mapping = self.phone_adb(['forward', '--list'], 'forward-check')
            if not owns_forward(mapping, self.args.device, self.native_forward):
                # A reconnect may remove our listener. Never overwrite a new owner.
                self.phone_adb(['forward', '--no-rebind', f'tcp:{self.native_forward}',
                                'tcp:3240'], 'forward-restore')
                print('Restored the owned phone USB relay forward.', flush=True)
            self.forward_message = None
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
            if isinstance(error, InterruptedError):
                raise
            message = 'Waiting for the phone ADB relay connection: ' + str(error)
            if message != self.forward_message:
                print(message, flush=True)
                self.forward_message = message

    def discover(self):
        command = [sys.executable, str(ROOT / 'scripts/sdk_environment.py')]
        if self.args.sdk_path:
            command += ['--sdk-path', self.args.sdk_path]
        output = self.run(command, 'environment', user=True, timeout=60)
        self.sdk = json.loads(output.strip().splitlines()[-1])
        missing = [key for key, present in self.sdk['dependencies'].items() if not present]
        if missing:
            raise RuntimeError('Missing preview dependencies: ' + ', '.join(missing) + '. Run setup-wsl.sh explicitly.')
        self.adb = shutil.which('adb') or self.sdk['adb']
        print(f"Official SDK {self.sdk['version']}: {self.sdk['environment']}", flush=True)

    def setup(self):
        self.discover()
        for port in (8765,):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                    raise RuntimeError(f'Preview port {port} is occupied. Stop its owner before starting another session.')
            except (ConnectionRefusedError, TimeoutError):
                pass
        self.run(['modprobe', 'vhci_hcd'], 'vhci')
        self.run(['modprobe', 'cdc_ncm'], 'ncm')
        if not self.args.port:
            if not self.args.device:
                raise ValueError('Native Linux requires --device with the connected phone ADB serial.')
            self.phone_binary = shutil.which('adb') or self.sdk['adb']
            state = self.phone_adb(['get-state'])
            if state.strip() != 'device':
                raise RuntimeError('Authorize and connect the phone using adb pair/connect first.')
            # The Activity's ensure-relay action is idempotent; never force-stop it.
            self.phone_adb(['shell', 'am', 'start', '-n', 'dev.aria.usb/.MainActivity',
                            '--ez', 'relay_usb', 'true'])
            self.args.port = valid_port(self.phone_adb(['forward', 'tcp:0', 'tcp:3240']).strip())
            self.native_forward = self.args.port
        self.import_file.write_text(json.dumps(dict(vhci_port=None)))
        self.spawn([sys.executable, 'scripts/usbip_attach.py', 'watch', '--port',
                    str(self.args.port), '--state-file', str(self.import_file)], 'usbip')
        self.run([self.adb, '-P', str(self.args.adb_port), 'start-server'], 'adb')
        self.spawn([sys.executable, 'scripts/usb_ncm_network.py', '--watch',
                    '--owner-file', str(self.import_file)], 'dhcp')

    def wait_device(self, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopping.is_set():
            self.ensure_native_forward()
            output = self.run([self.adb, '-P', str(self.args.adb_port), 'devices', '-l'],
                              'devices', timeout=5)
            try:
                imported = json.loads(self.import_file.read_text())
                busid = owned_busid(imported, read_status())
            except (OSError, ValueError, RuntimeError):
                busid = None
            serial = choose_aria(output, self.serial, busid)
            if serial:
                self.serial = serial
                return
            self.stopping.wait(1)
        if self.stopping.is_set():
            raise InterruptedError('Session stopped')
        raise TimeoutError('Aria USB did not appear. Check phone connectivity, USB permission, and usbip.log.')

    def sdk_command(self, command, name, timeout=60, cleanup=False):
        return self.run([self.sdk['cli'], '--serial', self.serial, *command], name,
                        timeout=timeout, user=True, cleanup=cleanup)

    def start_stream(self):
        self.wait_device()
        authentication = self.sdk_command(['auth', 'check'], 'auth', timeout=20)
        if 'SDK is authenticated' not in authentication:
            raise RuntimeError('Existing SDK identity is not paired. Use the official Companion pairing flow as this Linux user.')
        self.stream_attempted = True
        result = self.sdk_command(['streaming', 'start', '--interface', 'usb', '--profile',
                                  self.args.profile, '--use-ephemeral-certs'], 'start', timeout=100)
        if 'Streaming started over usb' not in result:
            raise RuntimeError('USB stream did not start: ' + result[-2500:])
        self.preview = self.spawn([self.sdk['python'], 'bridge/preview.py', '--use-ephemeral-certs'],
                                  'preview', user=True)
        print('Live decoder started at http://127.0.0.1:8765/.', flush=True)

    def healthy(self):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/status.json', timeout=2) as response:
                data = json.load(response)
            return bool(data.get('receiving'))
        except (OSError, ValueError):
            return False

    def monitor(self):
        last_good = time.monotonic()
        recoveries = 0
        while not self.stopping.wait(2):
            self.ensure_native_forward()
            if self.preview.process.poll() is not None:
                raise RuntimeError('Preview exited; see preview.log for the decoder error.')
            if self.healthy():
                last_good = time.monotonic()
                continue
            if time.monotonic() - last_good < 30:
                continue
            if recoveries >= 3:
                raise RuntimeError('No sensor data after three recovery attempts. Reconnect the phone/glasses and start again.')
            recoveries += 1
            print(f'Sensor connection interrupted; recovery {recoveries}/3.', flush=True)
            self.preview.stop()
            self.children.remove(self.preview)
            self.preview = None
            self.wait_device(seconds=90)
            self.sdk_command(['streaming', 'stop'], 'recover-stop', timeout=25)
            self.start_stream()
            last_good = time.monotonic()

    def close(self):
        errors = []
        def stop_owned(child):
            try:
                child.stop()
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f'{child.name}: {error}')
        if self.preview:
            stop_owned(self.preview)
            if self.preview in self.children: self.children.remove(self.preview)
        if self.stream_attempted and self.sdk and self.serial:
            try:
                self.sdk_command(['streaming', 'stop'], 'stop', timeout=25, cleanup=True)
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                print(f'Could not confirm stream stop: {error}', file=sys.stderr)
        for child in reversed(self.children):
            stop_owned(child)
        if self.native_forward:
            try:
                mapping = self.phone_adb(['forward', '--list'], 'forward-check', cleanup=True)
                if owns_forward(mapping, self.args.device, self.native_forward):
                    self.phone_adb(['forward', '--remove', f'tcp:{self.native_forward}'], 'forward-cleanup', cleanup=True)
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                print(f'Owned phone tunnel cleanup: {error}', file=sys.stderr)
        self.import_file.unlink(missing_ok=True)
        if errors:
            raise RuntimeError('Cleanup attempted every owned resource; errors: ' + '; '.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=valid_port)
    parser.add_argument('--device')
    parser.add_argument('--user', default=os.environ.get('SUDO_USER'))
    parser.add_argument('--sdk-path')
    parser.add_argument('--serial')
    parser.add_argument('--profile', default='profile12')
    parser.add_argument('--adb-port', type=valid_port, default=5038)
    parser.add_argument('--launch-token', type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    if args.launch_token and (RUNTIME / ('windows-cancel.' + args.launch_token)).exists():
        raise InterruptedError('Live startup was cancelled before transport initialization.')
    if os.geteuid() != 0 or not args.user or args.user == 'root':
        parser.error('Use sudo with a normal --user; only the USB transport controller runs as root.')
    if args.adb_port == 5037:
        parser.error('The imported glasses ADB port must differ from phone ADB port 5037.')
    os.chdir(ROOT)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / 'session.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This workspace already has a starting/running session.')
        if session.status()['running']:
            raise RuntimeError('This workspace already has a live session. Stop it before starting another.')
        controller = Controller(args)
        record = session.identity(os.getpid())
        temporary = session.PIDFILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(record))
        temporary.replace(session.PIDFILE)
        signal.signal(signal.SIGTERM, lambda *_: controller.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: controller.stopping.set())
        try:
            controller.setup()
            controller.start_stream()
            controller.monitor()
        except InterruptedError:
            pass
        finally:
            try:
                controller.close()
            finally:
                session.clear(record)
        print('Owned relay session stopped.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f'Aria relay: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
