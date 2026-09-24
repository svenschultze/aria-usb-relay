#!/usr/bin/env python3
"""Check tracked public files, or source candidates before the first git commit."""
import re
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIRS = {'.git', '.gradle', '.idea', '.research', '.local', '.venv', 'venv',
              '.toolchain-bootstrap', 'build', 'diagnostics', '__pycache__'}
LOCAL_FILES = {'local.properties', '.DS_Store', 'Thumbs.db'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.crt', '.cer',
                    '.apk', '.apks', '.aab', '.whl', '.so', '.dll', '.dylib', '.vrs',
                    '.jpg', '.jpeg', '.png', '.mp4', '.wav', '.log', '.pid', '.leases', '.pyc'}
HOME_PATH = re.compile(r'(?:[A-Za-z]:[\\/]+Users[\\/]+|/mnt/[a-z]/Users/|/home/)([A-Za-z0-9._-]+)')
PLACEHOLDERS = {'YOUR_USER', 'YOUR_LINUX_USER', 'USER', 'USERNAME', 'user', 'example'}
PRIVATE_IP = re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')
DEVICE_SERIAL = re.compile(r'\b1WM[A-Z0-9]{10}\b')


def candidates():
    try:
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                cwd=ROOT, check=True, capture_output=True)
        return sorted({Path(name.decode('utf-8')) for name in result.stdout.split(b'\0') if name})
    except (OSError, subprocess.CalledProcessError):
        return sorted(path.relative_to(ROOT) for path in ROOT.rglob('*')
                      if path.is_file() and not (set(path.relative_to(ROOT).parts) & LOCAL_DIRS)
                      and path.name not in LOCAL_FILES and not path.name.startswith('.env'))


def findings(path):
    if (ROOT / path).is_symlink():
        return ['unexpected symlink in public source']
    if set(path.parts) & LOCAL_DIRS or path.name in LOCAL_FILES or path.name.startswith('.env'):
        return ['local runtime/configuration artifact']
    if path.suffix.lower() in PRIVATE_SUFFIXES:
        return ['private capture, credential, runtime log, or vendor/build binary']
    if path.suffix == '.jar':
        if path.as_posix() != 'gradle/wrapper/gradle-wrapper.jar':
            return ['unexpected Java archive']
        expected = '423cb469ccc0ecc31f0e4e1c309976198ccb734cdcbb7029d4bda0f18f57e8d9'
        return [] if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected else ['Gradle wrapper checksum mismatch']
    data = (ROOT / path).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        return ['unexpected source artifact larger than 2 MiB']
    try:
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return ['unexpected binary file']
    errors = []
    if PRIVATE_KEY.search(content):
        errors.append('private key material')
    if PRIVATE_IP.search(content):
        errors.append('private LAN address; use a placeholder or RFC 5737 test address')
    if DEVICE_SERIAL.search(content):
        errors.append('physical device serial')
    # These tests intentionally use a synthetic account for redaction/argv checks.
    fixture_files = {'tests/test_desktop.py', 'tests/test_linux_launchers.py'}
    placeholders = PLACEHOLDERS | ({'researcher'} if path.as_posix() in fixture_files else set())
    if any(match.group(1) not in placeholders for match in HOME_PATH.finditer(content)):
        errors.append('machine-specific home directory')
    return errors


def main():
    paths = candidates()
    errors = [(path, reason) for path in paths for reason in findings(path)]
    for path, reason in errors:
        # Report categories and filenames; never print the secret content.
        print(f'{path.as_posix()}: {reason}', file=sys.stderr)
    if errors:
        return 1
    print(f'Public source audit passed: {len(paths)} files checked.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
