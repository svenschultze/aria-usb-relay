"""Offline boundaries: HTTP origin, bounded input, no shell interpolation, privacy."""
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.control import Controller, parse_devices, parse_mdns, validate_address, validate_config
from desktop.server import DashboardServer


class ConfigurationTests(unittest.TestCase):
    def test_rejects_shell_like_device_and_unknown_fields(self):
        for value in ({"device": "x; touch /tmp/oops"}, {"device": "--help"},
                      {"sdk_path": "/tmp/$(id)"}, {"shell": "anything"},
                      {"linux_user": "root;id"}, {"profile": ""}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(value)
        self.assertEqual(validate_config({"device": "192.0.2.20:37213"})["profile"], "profile12")

    def test_pairing_endpoint_requires_valid_ip_and_port(self):
        for value in ("--help", "example.invalid:3", "192.0.2.1:65536", "192.0.2.1:0", "192.0.2.1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_address(value)
        self.assertEqual(validate_address("[2001:db8::1]:37123"), "[2001:db8::1]:37123")

    def test_discovery_ignores_banners_and_deduplicates_mdns(self):
        devices = parse_devices("List of devices attached\n192.0.2.20:37123 device product:phone model:Example_Phone\nabc unauthorized\n* daemon started successfully *")
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["model"], "Example Phone")
        services = "one _adb-tls-connect._tcp. 192.0.2.20:37123\ntwo _adb-tls-connect._tcp. 192.0.2.20:37123\nthree _adb-tls-pairing._tcp. 192.0.2.20:38123"
        self.assertEqual(len(parse_mdns(services)), 2)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.control = Controller(Path(self.directory.name), windows=True)

    def tearDown(self):
        self.control.close()
        self.directory.cleanup()

    def test_pairing_code_is_stdin_only_and_never_logged_on_failure(self):
        calls = []
        def fail(arguments, **kwargs):
            calls.append((arguments, kwargs))
            raise RuntimeError("Failed pairing code 123456")
        with patch.object(self.control, "adb", return_value="adb"), patch.object(self.control, "run", side_effect=fail):
            self.control._job("pair", {"address": "192.0.2.20:37123", "code": "123456"}, validate_config({}))
        self.assertEqual(calls[0][0], ["adb", "pair", "192.0.2.20:37123"])
        self.assertEqual(calls[0][1]["input_text"], "123456\n")
        self.assertNotIn("123456", json.dumps(self.control.status()))
        self.assertNotIn("123456", (Path(self.directory.name) / ".local/desktop.log").read_text())
        self.assertFalse((Path(self.directory.name) / ".local/desktop.json").exists())

    def test_second_mutation_cannot_overlap_running_operation(self):
        self.control.job["running"] = True
        with self.assertRaises(RuntimeError):
            self.control.submit("save", {"config": {}})

    def test_requested_startup_cancellation_is_not_an_unexpected_failure(self):
        process = SimpleNamespace(stdout=io.BytesIO(b"Live startup was cancelled\n"), wait=lambda: 1)
        self.control.session = process
        self.control.stopping_session = process
        self.control._read_session(process)
        self.assertEqual(self.control.session_exit, 0)
        self.assertEqual(self.control.logs[-1]["text"], "Session stopped.")

    def test_stop_ignores_unfinished_invalid_form_settings(self):
        completed = threading.Event()
        received = []
        def operation(action, payload, config):
            received.append((action, config))
            completed.set()
            return "Stopped"
        with patch.object(self.control, "_perform", side_effect=operation):
            self.control.submit("stop", {"config": {"profile": "", "sdk_path": "bad"}})
            self.assertTrue(completed.wait(2))
        self.assertEqual(received[0][0], "stop")
        self.assertEqual(received[0][1]["profile"], "profile12")

    def test_active_connection_cannot_be_retargeted_by_saving_settings(self):
        self.control.preview = {"receiving": True}
        self.control.save(dict(self.control.config))
        with self.assertRaises(RuntimeError):
            self.control.save({**self.control.config, "distro": "DifferentDistro"})

    def test_shutdown_fences_a_start_before_process_creation(self):
        preparing, release = threading.Event(), threading.Event()
        def prepare(*args, **kwargs):
            preparing.set()
            self.assertTrue(release.wait(2))
            return ["unused"]
        thread = threading.Thread(target=self.control._job, args=("start", {}, validate_config({"device": "EXAMPLE-PHONE"})))
        with patch.object(self.control, "script", side_effect=prepare), patch("subprocess.Popen") as process:
            thread.start()
            self.assertTrue(preparing.wait(2))
            self.control.begin_close()
            release.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            process.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.control.submit("discover", {})

    def test_export_redacts_previous_phone_and_ipv6_identifiers(self):
        self.control.save(validate_config({"device": "OLD-PHONE-SERIAL"}))
        self.control.log("Phone OLD-PHONE-SERIAL at [2001:db8::1234]:34567")
        self.control.save(validate_config({"device": "NEW-PHONE-SERIAL"}))
        report = json.dumps(self.control.diagnostics())
        self.assertNotIn("OLD-PHONE-SERIAL", report)
        self.assertNotIn("2001:db8::1234", report)

    def test_connect_checks_actual_device_state_before_saving(self):
        with patch.object(self.control, "adb", return_value="adb"), patch.object(self.control, "run", side_effect=["connected to 192.0.2.20:37123", "192.0.2.20:37123 offline"]):
            with self.assertRaises(RuntimeError):
                self.control._perform("connect", {"address": "192.0.2.20:37123"}, validate_config({}))
        self.assertEqual(self.control.config["device"], "")

    def test_export_removes_identifiers_paths_and_sensor_data(self):
        self.control.config = validate_config({"device": "192.0.2.20:37123", "aria_serial": "EXAMPLE-SERIAL", "linux_user": "researcher"})
        self.control.log(r"C:\Users\researcher\private EXAMPLE-SERIAL 192.0.2.20:37123 /home/researcher/aria")
        self.control.preview = {"phase": "receiving_samples", "imu": {"private_sample": [1, 2, 3]}}
        report = json.dumps(self.control.diagnostics())
        for private in ("EXAMPLE-SERIAL", "192.0.2.20", "private_sample", "researcher"):
            self.assertNotIn(private, report)

    def test_mdns_and_ip_aliases_merge_only_when_hardware_id_matches(self):
        outputs = ["192.0.2.20:37123 device model:Same_Phone\nadb-alias._tcp device model:Same_Phone\n192.0.2.21:37123 device model:Same_Phone", "one", "one", "two", ""]
        with patch.object(self.control, "adb", return_value="adb"), patch.object(self.control, "run", side_effect=outputs), patch("shutil.which", return_value=None):
            self.control._perform("discover", {}, validate_config({}))
        self.assertEqual([device["id"] for device in self.control.devices], ["192.0.2.20:37123", "192.0.2.21:37123"])


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.control = Controller(Path(self.directory.name))
        self.server = DashboardServer(("127.0.0.1", 0), self.control)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.control.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        data = response.read()
        code = response.status
        client.close()
        return code, data

    def test_dns_rebinding_and_cross_site_reads_are_rejected(self):
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status")[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"X-Aria-Token": self.server.token})[0], 200)

    def test_mutations_require_token_and_matching_origin(self):
        body = json.dumps({"config": {}})
        for headers in ({}, {"Origin": self.origin}, {"X-Aria-Token": self.server.token},
                        {"X-Aria-Token": self.server.token, "Origin": "https://attacker.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/action/save", body, {"Content-Type": "application/json", **headers})[0], 403)
        self.assertFalse((Path(self.directory.name) / ".local/desktop.json").exists())

    def test_oversized_json_and_path_traversal_are_rejected(self):
        headers = {"Content-Type": "application/json", "Origin": self.origin, "X-Aria-Token": self.server.token}
        self.assertEqual(self.request("POST", "/api/action/save", " " * 8200, headers)[0], 413)
        self.assertEqual(self.request("GET", "/../README.md")[0], 404)
        self.assertEqual(self.request("GET", "/api/frame/../../private", headers={"X-Aria-Token": self.server.token})[0], 404)


if __name__ == "__main__":
    unittest.main()
