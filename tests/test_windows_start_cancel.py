import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
POWERSHELL = shutil.which('pwsh') or shutil.which('pwsh.exe')


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell launcher integration')
class WindowsCancellationTests(unittest.TestCase):
    def test_stop_during_preflight_prevents_phone_or_tunnel_actions(self):
        with tempfile.TemporaryDirectory(prefix='aria cancel spaces ') as folder:
            root = Path(folder)
            scripts = root / 'scripts'
            scripts.mkdir()
            for name in ('start-live.ps1', 'stop-live.ps1', 'wsl-common.ps1'):
                shutil.copyfile(SCRIPTS / name, scripts / name)
            with (scripts / 'wsl-common.ps1').open('a') as output:
                output.write('''
function Get-AriaWslContext { param($Distro,$LinuxUser,[switch]$AllowRootUser)
  [PSCustomObject]@{Executable='fake-wsl';Prefix=@();Root='/unused';User='user'} }
function Get-AriaLiveState { param($Context) [PSCustomObject]@{running=$false} }
function Invoke-AriaProcess { param($FilePath,$Arguments,$TimeoutMilliseconds)
  [PSCustomObject]@{ExitCode=0;Output='No live session';Error=''} }
''')
            (scripts / 'adb-common.ps1').write_text('''
function Find-AriaAdb { param($ExplicitPath) 'fake-adb' }
function Invoke-AriaAdbBounded { throw 'Phone mutation must not occur in this test' }
''')
            (scripts / 'doctor.ps1').write_text('''
param($Device,$Distro,$LinuxUser,$SdkPath,$AdbPath,[switch]$Json)
$root = Split-Path -Parent $PSScriptRoot
[IO.File]::WriteAllText((Join-Path $root 'doctor-entered'), 'yes')
Start-Sleep -Seconds 2
'{"ready":true,"checks":[]}'
''')
            (scripts / 'start-relay.ps1').write_text('''
$root = Split-Path -Parent $PSScriptRoot
[IO.File]::WriteAllText((Join-Path $root 'MUTATED'), 'wrong')
throw 'Phone mutation occurred'
''')
            startup = subprocess.Popen([POWERSHELL, '-NoProfile', '-File',
                str(scripts / 'start-live.ps1'), '-Device', 'PHONE'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while not (root / 'doctor-entered').exists() and time.monotonic() < deadline:
                    if startup.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertTrue((root / 'doctor-entered').exists(), startup.communicate(timeout=3) if startup.poll() is not None else 'preflight not reached')
                stopped = subprocess.run([POWERSHELL, '-NoProfile', '-File',
                    str(scripts / 'stop-live.ps1')], capture_output=True, text=True, timeout=15)
                out, error = startup.communicate(timeout=10)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertNotEqual(startup.returncode, 0)
                self.assertIn('cancelled', error + out)
                self.assertFalse((root / 'MUTATED').exists())
                self.assertFalse((root / '.research/live-run/windows-session.json').exists())
                self.assertFalse(list((root / '.research/live-run').glob('windows-cancel.*')))
            finally:
                if startup.poll() is None:
                    startup.kill()
                    startup.wait(5)


if __name__ == '__main__':
    unittest.main()
