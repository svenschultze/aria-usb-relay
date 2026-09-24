import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = load('session_state_test', 'wsl-session.py')


class SessionOwnershipTests(unittest.TestCase):
    def test_legacy_and_python_controller_accepted_only_in_exact_workspace(self):
        root = SCRIPTS.parent.resolve()
        for executable, script in [('bash', 'scripts/wsl-live.sh'), ('python3.12', 'scripts/wsl_live.py')]:
            self.assertEqual(state.validate_identity(42, [executable.encode(), script.encode()],
                executable, root, '999'), {'pid': 42, 'start_ticks': '999'})
        with self.assertRaises(ValueError):
            state.validate_identity(42, [b'python3', b'scripts/wsl_live.py'],
                                    'python3', root.parent, '999')
        with self.assertRaises(ValueError):
            state.validate_identity(42, [b'python3', b'bridge/preview.py'],
                                    'python3', root, '999')

    def test_reused_pid_is_not_owned(self):
        with patch.object(state, 'identity', return_value={'pid': 42, 'start_ticks': 'new'}):
            self.assertFalse(state.verified({'pid': 42, 'start_ticks': 'old'}))

    def test_cleanup_cannot_clear_a_new_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.pid'
            newer = {'pid': 43, 'start_ticks': '2'}
            path.write_text(json.dumps(newer))
            with patch.object(state, 'PIDFILE', path):
                state.clear({'pid': 42, 'start_ticks': '1'})
                self.assertEqual(json.loads(path.read_text()), newer)
                state.clear(newer)
                self.assertFalse(path.exists())

    def test_invalid_pid_record_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.pid'
            with patch.object(state, 'PIDFILE', path):
                for pid in (True, 0, 1, -2, '42'):
                    path.write_text(json.dumps({'pid': pid, 'start_ticks': '1'}))
                    with self.assertRaises(ValueError):
                        state.read_record()


@unittest.skipUnless(os.name == 'posix', 'Linux process ownership and flock')
class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = load('controller_test', 'wsl_live.py')

    def test_only_real_usb_aria_is_selected(self):
        output = ('List of devices attached\n'
                  'PHONE device product:phone model:Pixel_9 usb:1-2\n'
                  'NET device model:Aria transport_id:3\n'
                  'GLASSES device usb:2-1 product:gemini model:Aria\n')
        self.assertEqual(self.controller.choose_aria(output, owned_busid='2-1'), 'GLASSES')
        self.assertIsNone(self.controller.choose_aria(output, 'OTHER', '2-1'))
        self.assertIsNone(self.controller.choose_aria(output))
        self.assertIsNone(self.controller.choose_aria(output, 'GLASSES', '2-8'))
        with self.assertRaises(RuntimeError):
            self.controller.choose_aria(output + 'SECOND device usb:2-1 model:Aria\n', owned_busid='2-1')

    def test_import_identity_must_match_before_adb_selection(self):
        record = dict(vhci_port=8, vendor=0x2833, busnum=2, devnum=7)
        ports = [dict(port=8, status=6, devid=(2 << 16) | 7, local_busid='2-1')]
        self.assertEqual(self.controller.owned_busid(record, ports), '2-1')
        self.assertIsNone(self.controller.owned_busid({**record, 'devnum': 8}, ports))
        self.assertIsNone(self.controller.owned_busid({**record, 'vhci_port': 9}, ports))
        output = 'OTHER device usb:2-2 model:Aria\nOWNED device usb:2-1 model:Aria\n'
        self.assertEqual(self.controller.choose_aria(output, owned_busid='2-1'), 'OWNED')
        self.assertIsNone(self.controller.choose_aria(output, 'OTHER', '2-1'))

    def test_cancelled_windows_token_prevents_delayed_linux_start(self):
        token = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime / ('windows-cancel.' + token)).write_text(token)
            with patch.object(self.controller, 'RUNTIME', runtime), patch.object(
                    sys, 'argv', ['wsl_live.py', '--user', 'user', '--launch-token', token]), patch.object(
                    self.controller.Controller, 'setup') as setup:
                with self.assertRaises(InterruptedError):
                    self.controller.main()
                setup.assert_not_called()

    def test_cleanup_does_not_remove_reassigned_forward(self):
        self.assertTrue(self.controller.owns_forward('PHONE tcp:60000 tcp:3240\n', 'PHONE', 60000))
        self.assertFalse(self.controller.owns_forward('OTHER tcp:60000 tcp:3240\n', 'PHONE', 60000))
        self.assertFalse(self.controller.owns_forward('PHONE tcp:60000 tcp:1234\n', 'PHONE', 60000))

    def test_cleanup_continues_after_one_child_stop_error(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        broken = SimpleNamespace(name='broken', stop=Mock(side_effect=OSError('already gone')))
        healthy = SimpleNamespace(name='healthy', stop=Mock())
        controller = self.controller.Controller.__new__(self.controller.Controller)
        controller.preview = broken
        controller.children = [broken, healthy]
        controller.stream_attempted = False
        controller.native_forward = None
        controller.import_file = Mock()
        with self.assertRaises(RuntimeError):
            controller.close()
        healthy.stop.assert_called_once()

    def test_native_reconnect_restores_only_with_no_rebind(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        controller = self.controller.Controller.__new__(self.controller.Controller)
        controller.args = SimpleNamespace(device='PHONE')
        controller.native_forward = 60000
        controller.forward_check_at = 0
        controller.forward_message = None
        controller.phone_adb = Mock(side_effect=['', ''])
        with patch('builtins.print'):
            controller.ensure_native_forward()
        self.assertEqual(controller.phone_adb.call_args_list[1].args[0],
                         ['forward', '--no-rebind', 'tcp:60000', 'tcp:3240'])
        controller.forward_check_at = 0
        controller.phone_adb = Mock(return_value='PHONE tcp:60000 tcp:3240\n')
        controller.ensure_native_forward()
        controller.phone_adb.assert_called_once_with(['forward', '--list'], 'forward-check')

    def test_native_reconnect_does_not_delete_a_foreign_mapping(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        controller = self.controller.Controller.__new__(self.controller.Controller)
        controller.args = SimpleNamespace(device='PHONE')
        controller.native_forward = 60000
        controller.forward_check_at = 0
        controller.forward_message = None
        controller.phone_adb = Mock(side_effect=['OTHER tcp:60000 tcp:9000\n', RuntimeError('cannot rebind')])
        with patch('builtins.print'):
            controller.ensure_native_forward()
        self.assertEqual(controller.phone_adb.call_args_list[1].args[0][1], '--no-rebind')
        self.assertNotIn('--remove', str(controller.phone_adb.call_args_list))

    def test_log_rotation_and_memory_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(self.controller, 'RUNTIME', Path(folder)):
                child = self.controller.OwnedProcess(
                    [sys.executable, '-c', "import sys; sys.stdout.write('x' * (5*1024*1024))"], 'volume')
                child.process.wait(10)
                child.reader.join(5)
                child.stop()
                self.assertLessEqual(len(child.tail), 131072)
                logs = list(Path(folder).glob('volume.log*'))
                self.assertLessEqual(len(logs), 3)
                self.assertLessEqual(sum(p.stat().st_size for p in logs), 3 * 1024 * 1024)

    def test_stop_reaps_only_the_owned_child(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(self.controller, 'RUNTIME', Path(folder)):
                child = self.controller.OwnedProcess(
                    [sys.executable, '-c', 'import time; time.sleep(60)'], 'owned')
                child.stop(seconds=2)
                self.assertIsNotNone(child.process.returncode)


if __name__ == '__main__':
    unittest.main()
