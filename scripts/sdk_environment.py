#!/usr/bin/env python3
"""Discover an installed official SDK without installing or modifying anything."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

PROBE = r'''
import importlib.metadata as m, importlib.util as u, json, pathlib, sys
import aria.sdk
base = pathlib.Path(aria.sdk.__file__).resolve().parent
print(json.dumps(dict(python=sys.executable, cli=str(pathlib.Path(sys.executable).parent/'aria'),
    adb=str(base/'tools'/'adb'), version=m.version('projectaria-client-sdk'),
    dependencies={name:u.find_spec(name) is not None for name in ('numpy','cv2')})))
'''


def candidates(explicit=None, home=None):
    if explicit:
        return [Path(explicit).expanduser()]
    home = Path(home or Path.home())
    paths = []
    for variable in ('ARIA_SDK_ENV', 'VIRTUAL_ENV'):
        if os.environ.get(variable):
            paths.append(Path(os.environ[variable]).expanduser())
    for command in ('aria', 'python3'):
        binary = shutil.which(command)
        if binary:
            paths.append(Path(binary).absolute().parent.parent)
    paths.extend([home / '.local/share/aria-usb-relay/venv',
                  home / '.local/share/aria-usb-lab/venv'])
    for pattern in ('aria*', '.venvs/*', 'venvs/*', '.virtualenvs/*'):
        paths.extend(sorted(home.glob(pattern)))
    return list(dict.fromkeys(paths))


def version_key(value):
    return tuple(int(part) for part in re.findall(r'\d+', value)[:3])


def discover(explicit=None, home=None):
    found, errors = [], []
    for environment in candidates(explicit, home):
        python = environment / 'bin/python'
        if not python.exists():
            python = environment / 'bin/python3'
        if not python.is_file():
            continue
        try:
            result = subprocess.run([str(python), '-I', '-c', PROBE],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode:
                errors.append(f'{environment}: official aria.sdk is not importable')
                continue
            data = json.loads(result.stdout.strip().splitlines()[-1])
            if not Path(data['cli']).is_file() or not Path(data['adb']).is_file():
                errors.append(f'{environment}: SDK CLI or bundled ADB missing')
                continue
            data['environment'] = str(environment.absolute())
            found.append(data)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            errors.append(f'{environment}: {error}')
    if not found:
        detail = '; '.join(errors) if explicit and errors else 'No supported environment found'
        raise RuntimeError(f'{detail}. Run scripts/setup-wsl.sh (Linux also supported), '
                           'or supply --sdk-path to an existing SDK virtual environment.')
    return max(found, key=lambda item: version_key(item['version']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-path')
    args = parser.parse_args()
    try:
        print(json.dumps(discover(args.sdk_path)))
    except RuntimeError as error:
        parser.exit(1, str(error) + '\n')
