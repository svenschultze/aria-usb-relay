#!/usr/bin/env python3
"""Read-only Linux prerequisites; never install packages, pair, or start a stream."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
from sdk_environment import discover


def check_environment(sdk_path=None, device=None, serial=None, check_phone_adb=True):
    checks = []
    def add(key, label, status, detail):
        checks.append(dict(id=key, label=label, status=status, detail=detail))
    supported = platform.system() == 'Linux' and platform.machine() in ('x86_64', 'aarch64')
    add('linux', 'Linux runtime', 'pass' if supported else 'fail',
        platform.platform() if supported else 'Use supported Linux/WSL with the official Aria SDK; macOS transport is not implemented.')
    for command in ('python3', 'ip', 'modprobe', 'dhclient', 'runuser'):
        path = shutil.which(command)
        if not path:
            path = next((str(Path(base) / command) for base in ('/usr/sbin', '/sbin')
                         if (Path(base) / command).is_file()), None)
        add('tool_' + command, command, 'pass' if path else 'fail', path or
            'Missing. Run scripts/setup-wsl.sh explicitly to install prerequisites.')
    for module in ('vhci_hcd', 'cdc_ncm'):
        available = (Path('/sys/module') / module).exists()
        if not available and shutil.which('modinfo'):
            try:
                available = subprocess.run(['modinfo', '-n', module], capture_output=True,
                                           timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        add('module_' + module, module + ' kernel driver', 'pass' if available else 'fail',
            'Available; loaded only when starting.' if available else
            'Kernel module unavailable. Update WSL/kernel to one with USB/IP VHCI and CDC NCM support.')
    try:
        sdk = discover(sdk_path)
        add('sdk', 'Official Aria Client SDK', 'pass', f"{sdk['version']} in {sdk['environment']}")
        missing = [key for key, present in sdk['dependencies'].items() if not present]
        add('decoder_dependencies', 'Preview dependencies', 'fail' if missing else 'pass',
            'Missing: ' + ', '.join(missing) + '; run setup-wsl.sh for this environment.' if missing else 'NumPy and OpenCV are available.')
    except RuntimeError as error:
        add('sdk', 'Official Aria Client SDK', 'fail', str(error))
    certificate_root = Path.home() / '.aria/tls-client-certs'
    paired = certificate_root.is_dir()
    if serial:
        paired = serial not in ('.', '..') and '/' not in serial and '\\' not in serial and (certificate_root / serial).is_dir()
    add('pairing', 'Existing SDK pairing', 'pass' if paired else 'warn',
        'Certificate directory exists; authentication is checked at start without changing it.' if paired else
        'Pair this normal Linux user through the official Aria SDK/Companion flow before streaming.')
    if check_phone_adb:
        phone_adb = shutil.which('adb')
        capable = False
        if phone_adb:
            try:
                help_result = subprocess.run([phone_adb, 'help'], capture_output=True,
                                             text=True, timeout=5)
                capable = re.search(r'^\s*pair\s', help_result.stdout + help_result.stderr, re.M) is not None
            except (OSError, subprocess.TimeoutExpired):
                pass
        add('phone_adb_pair', 'Wireless phone pairing', 'pass' if capable else 'fail',
            f'Wireless pairing supported by {phone_adb}.' if capable else
            'Install current official Android SDK Platform-Tools and put its adb first on PATH. Older distro adb packages may lack wireless pairing: https://developer.android.com/tools/releases/platform-tools')
    if device and check_phone_adb:
        adb = shutil.which('adb')
        try:
            with socket.create_connection(('127.0.0.1', 5037), 0.3):
                running = True
        except OSError:
            running = False
        if adb and running:
            try:
                result = subprocess.run([adb, '-s', device, 'get-state'], capture_output=True,
                                        text=True, timeout=5)
                connected = result.returncode == 0 and result.stdout.strip() == 'device'
                add('phone', 'Phone ADB connection', 'pass' if connected else 'fail',
                    'Authorized phone connected.' if connected else 'Phone not authorized/connected; run adb pair/connect first.')
            except (OSError, subprocess.TimeoutExpired) as error:
                add('phone', 'Phone ADB connection', 'fail', str(error))
        else:
            add('phone', 'Phone ADB connection', 'fail', 'Install Android platform-tools and connect the phone with adb pair/connect. Doctor does not start ADB.')
    return dict(ready=all(item['status'] != 'fail' for item in checks), checks=checks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--sdk-path')
    parser.add_argument('--device')
    parser.add_argument('--serial')
    parser.add_argument('--skip-phone-adb', action='store_true', help='Windows wrapper checks its own phone ADB')
    args = parser.parse_args()
    result = check_environment(args.sdk_path, args.device, args.serial, not args.skip_phone_adb)
    if args.json:
        print(json.dumps(result))
    else:
        for item in result['checks']:
            print(f"{item['status'].upper():4} {item['label']}: {item['detail']}")
        print('Ready.' if result['ready'] else 'Resolve the failed checks before starting.')
