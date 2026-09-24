import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


@unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0, 'Normal-user Linux launcher tests')
class NativeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aria launcher spaces ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'workspace with spaces'
        scripts = self.root / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('start-live.sh', 'stop-live.sh', 'wsl-live.sh'):
            shutil.copyfile(SCRIPTS / name, scripts / name)
        self.bin = self.root / 'mock commands'
        self.bin.mkdir()
        sudo = self.bin / 'sudo'
        sudo.write_text('#!/bin/sh\n[ "$1" = -n ] && shift\n'
                        'if [ "$1" = true ]; then exit "${TEST_SUDO_EXPIRED:-0}"; fi\nexec "$@"\n')
        sudo.chmod(0o755)
        python = self.bin / 'python3'
        python.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                          'with open(os.environ["TEST_CAPTURE"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        python.chmod(0o755)
        self.capture = self.root / 'calls.jsonl'
        self.environment = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                            'TEST_CAPTURE': str(self.capture)}
        self.environment.pop('SUDO_USER', None)

    def run_script(self, name, *args):
        return subprocess.run(['bash', str(self.root / 'scripts' / name), *args],
                              env=self.environment, capture_output=True, text=True, timeout=10)

    def test_start_and_stop_keep_argument_boundaries_in_spaced_workspace(self):
        result = self.run_script('start-live.sh', '--device', 'PHONE:5555', '--sdk-path',
                                 '/opt/SDK with spaces', '--profile', 'profile12')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_script('stop-live.sh', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual(calls[0][0], str(self.root / 'scripts/wsl_live.py'))
        self.assertIn('/opt/SDK with spaces', calls[0])
        self.assertEqual(calls[1], [str(self.root / 'scripts/wsl-session.py'), '--check'])

    def test_expired_sudo_never_starts_or_stops_a_process(self):
        self.environment['TEST_SUDO_EXPIRED'] = '1'
        for name in ('start-live.sh', 'stop-live.sh'):
            result = self.run_script(name)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('sudo -v', result.stderr)
        self.assertFalse(self.capture.exists())


class MissingDependenciesTests(unittest.TestCase):
    def test_doctor_missing_dependencies_is_machine_readable(self):
        import doctor_linux
        with patch.object(doctor_linux, 'discover', side_effect=RuntimeError('Install the official SDK')):
            with patch.object(doctor_linux.shutil, 'which', return_value=None):
                result = doctor_linux.check_environment()
        self.assertFalse(result['ready'])
        sdk = next(item for item in result['checks'] if item['id'] == 'sdk')
        self.assertEqual(sdk['status'], 'fail')
        self.assertIn('official SDK', sdk['detail'])
        self.assertEqual(set(sdk), {'id', 'label', 'status', 'detail'})
        json.dumps(result)


if __name__ == '__main__':
    unittest.main()
