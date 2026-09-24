import threading
import time
import unittest
import http.client

from preview import LatestFrames, PreviewState, PreviewServer


class LatestFramesTests(unittest.TestCase):
    def test_slow_reader_gets_latest_without_backlog(self):
        frames = LatestFrames()
        first = frames.publish("rgb", b"frame1")
        frames.publish("rgb", b"frame2")
        newest = frames.publish("rgb", b"frame3")
        self.assertEqual(frames.wait("rgb", first.sequence, 0), newest)
        self.assertEqual(frames.status()["rgb"]["bytes"], 6)
        self.assertIsNone(frames.wait("rgb", newest.sequence, 0))

    def test_cameras_are_independent_and_payload_is_immutable(self):
        frames = LatestFrames()
        data = bytearray(b"rgb")
        frames.publish("rgb", data)
        data[:] = b"bad"
        frames.publish("slam1", b"slam")
        self.assertEqual(frames.latest("rgb").jpeg, b"rgb")
        self.assertEqual(frames.latest("slam1").jpeg, b"slam")
        self.assertIsNone(frames.latest("slam2"))

    def test_rejects_unbounded_keys_and_oversized_payloads(self):
        frames = LatestFrames(max_frame_bytes=3)
        for camera, content in (("phone", b"x"), ("rgb", b"1234"), ("rgb", b"")):
            with self.assertRaises(ValueError):
                frames.publish(camera, content)
        self.assertEqual(frames.status(), {})

    def test_shutdown_wakes_waiting_viewer(self):
        frames = LatestFrames()
        result = []
        thread = threading.Thread(target=lambda: result.append(frames.wait("rgb", 0, 30)))
        thread.start()
        frames.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])
        with self.assertRaises(RuntimeError):
            frames.publish("rgb", b"late")

    def test_independent_viewers_can_receive_same_frame(self):
        frames = LatestFrames()
        latest = frames.publish("rgb", b"image")
        self.assertEqual(frames.wait("rgb", 0, 0), latest)
        self.assertEqual(frames.wait("rgb", 0, 0), latest)

    def test_status_distinguishes_no_data_from_stale_data(self):
        state = PreviewState()
        state.update("subscribed_waiting_for_samples")
        self.assertFalse(state.status()["receiving"])
        self.assertEqual(state.status()["phase"], "subscribed_waiting_for_samples")
        state.imu["0"] = {"received_at": time.monotonic() - 10,
                          "capture_timestamp_ns": 1, "acceleration_m_s2": [1, 2, 3],
                          "angular_velocity_rad_s": None}
        self.assertFalse(state.status()["receiving"])
        self.assertEqual(state.status()["phase"], "samples_stale")
        self.assertGreater(state.status()["imu"]["0"]["age_ms"], 9000)
        state.frames.publish("rgb", b"real-frame")
        self.assertTrue(state.status()["receiving"])
        self.assertEqual(state.status()["phase"], "receiving_samples")
        state.update("sdk_error", "Connection failed")
        self.assertEqual(state.status()["phase"], "sdk_error")
        self.assertEqual(state.status()["error"], "Connection failed")


class PreviewHttpTests(unittest.TestCase):
    def test_only_local_requests_can_read_frames(self):
        state = PreviewState()
        state.frames.publish("rgb", b"test-frame")
        server = PreviewServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for headers, expected in (({}, 200), ({"Host": "attacker.invalid"}, 403),
                                      ({"Sec-Fetch-Site": "cross-site"}, 403),
                                      ({"Origin": "https://attacker.invalid"}, 403)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("GET", "/frame/rgb.jpg", headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin")
                body = response.read()
                if expected == 200:
                    self.assertEqual(body, b"test-frame")
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
