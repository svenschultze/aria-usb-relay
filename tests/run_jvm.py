#!/usr/bin/env python3
"""Run transport and relay-policy tests without Android hardware or an emulator."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def java_tool(name, java_home):
    suffix = '.exe' if os.name == 'nt' else ''
    if java_home:
        executable = Path(java_home) / 'bin' / (name + suffix)
        if not executable.is_file():
            raise FileNotFoundError(f'JDK tool is missing: {name}')
        return str(executable)
    executable = shutil.which(name)
    if not executable:
        raise FileNotFoundError('Set JAVA_HOME to JDK 17+ or put its bin directory on PATH')
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java-home', default=os.environ.get('JAVA_HOME'))
    args = parser.parse_args()
    output = ROOT / 'build/jvm-tests'
    output.mkdir(parents=True, exist_ok=True)
    production = ROOT / 'app/src/main/java/dev/aria/usb'
    sources = [production / f'{name}.java' for name in
               ('AdbProtocol', 'AdbIdentity', 'AdbConnection', 'RelayPolicy')]
    tests = sorted((ROOT / 'tests').glob('*Test.java'))
    sources += tests + sorted((ROOT / 'tests/usb-stubs').rglob('*.java'))
    subprocess.run([java_tool('javac', args.java_home), '--release', '17', '-d', str(output),
                    *map(str, sources)], check=True)
    for test in tests:
        subprocess.run([java_tool('java', args.java_home), '-ea', '-cp', str(output),
                        f'dev.aria.usb.{test.stem}'], check=True)


if __name__ == '__main__':
    main()
