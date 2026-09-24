#!/usr/bin/python3
"""DHCP for the owned imported Aria interface; never install DNS/default routes."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from usbip_attach import read_status

ROOT = Path(__file__).resolve().parents[1]


def aria_interface(name, owner_file=None):
    path = Path('/sys/class/net') / name / 'device'
    if not path.exists():
        return False
    device = path.resolve()
    driver = device / 'driver'
    if not driver.exists() or driver.resolve().name != 'cdc_ncm':
        return False
    parents = [device, *device.parents]
    if not any((parent / 'idVendor').exists()
               and (parent / 'idVendor').read_text().strip() == '2833' for parent in parents):
        return False
    if owner_file:
        try:
            port = json.loads(Path(owner_file).read_text())['vhci_port']
            state = next(item for item in read_status() if item['port'] == port)
            return state['status'] == 6 and state['local_busid'] in {p.name for p in parents}
        except (OSError, ValueError, KeyError, StopIteration):
            return False
    return any(parent.name == 'vhci_hcd.0' for parent in parents)


def lease_hook():
    name = os.environ.get('interface', '')
    owner_file = os.environ.get('ARIA_USB_OWNER_FILE')
    if not aria_interface(name, owner_file):
        return 1
    reason = os.environ.get('reason', '')
    if reason in ('BOUND', 'RENEW', 'REBIND', 'REBOOT'):
        address = ipaddress.IPv4Address(os.environ['new_ip_address'])
        network = ipaddress.IPv4Network(f"{address}/{os.environ['new_subnet_mask']}", strict=False)
        subprocess.run(['ip', 'link', 'set', 'dev', name, 'up'], check=True, timeout=5)
        subprocess.run(['ip', 'address', 'replace', f'{address}/{network.prefixlen}',
                        'dev', name], check=True, timeout=5)
        print(json.dumps(dict(event='lease', interface=name,
                              address=f'{address}/{network.prefixlen}')), flush=True)
    return 0


def main():
    if os.geteuid() != 0:
        raise PermissionError('DHCP and interface setup require root')
    if 'reason' in os.environ:
        return lease_hook()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-file')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    runtime = ROOT / '.research/ncm-dhcp'
    runtime.mkdir(parents=True, exist_ok=True)
    stopping = False
    def stop(_number, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = None
    identity = None
    last_start = 0
    try:
        while not stopping:
            interfaces = []
            for path in Path('/sys/class/net').iterdir():
                try:
                    if aria_interface(path.name, args.owner_file):
                        interfaces.append((path.name, (path / 'ifindex').read_text().strip()))
                except (OSError, ValueError):
                    # A USB mode change can remove an interface during this scan.
                    continue
            selected = interfaces[0] if len(interfaces) == 1 else None
            if child and (selected != identity or child.poll() is not None):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(3)
                    except subprocess.TimeoutExpired:
                        child.kill(); child.wait()
                child = None
            if selected and child is None and time.monotonic() - last_start > 3:
                name = selected[0]
                subprocess.run(['ip', 'link', 'set', 'dev', name, 'up'], check=True, timeout=5)
                environment = os.environ.copy()
                if args.owner_file:
                    environment['ARIA_USB_OWNER_FILE'] = str(Path(args.owner_file).resolve())
                print(json.dumps(dict(event='dhcp', interface=name)), flush=True)
                command = ['/usr/sbin/dhclient', '-d', '-v']
                if args.owner_file:
                    command += ['-e', 'ARIA_USB_OWNER_FILE=' + str(Path(args.owner_file).resolve())]
                child = subprocess.Popen([*command,
                    '-sf', str(Path(__file__).resolve()), '-pf', str(runtime / f'{name}.pid'),
                    '-lf', str(runtime / f'{name}.leases'), name], env=environment)
                identity = selected
                last_start = time.monotonic()
            time.sleep(0.5)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(3)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
