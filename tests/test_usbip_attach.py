import importlib.util
from pathlib import Path
import socket
import struct
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "usbip_attach", Path(__file__).resolve().parents[1] / "scripts" / "usbip_attach.py")
usbip = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usbip)


class UsbIpTests(unittest.TestCase):
    def test_fragmented_device_list(self):
        descriptor = usbip.DEVICE.pack(
            b"/dev/bus/usb/001/002", b"1-2", 1, 2, 3, 0x2833, 0x0083,
            0x0100, 0, 0, 0, 1, 1, 2)
        response = usbip.COMMON.pack(usbip.VERSION, 5, 0) + struct.pack("!I", 1)
        response += descriptor + bytes([6, 1, 1, 0, 255, 66, 1, 0])
        requests = []
        errors = []
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            def serve():
                try:
                    with server.accept()[0] as peer:
                        requests.append(usbip.recv_exact(peer, 8))
                        for start in range(0, len(response), 7):
                            peer.sendall(response[start:start+7])
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=serve)
            worker.start()
            result = usbip.list_devices("127.0.0.1", server.getsockname()[1])
            worker.join(2)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        self.assertEqual(requests, [usbip.COMMON.pack(usbip.VERSION, 0x8005, 0)])
        self.assertEqual(result[0]["busid"], "1-2")
        self.assertEqual(result[0]["vendor"], 0x2833)
        self.assertEqual(result[0]["interface_descriptors"], [[6, 1, 1], [255, 66, 1]])

    def test_wrong_reply_rejected(self):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(usbip.COMMON.pack(usbip.VERSION, 3, 0))
            with self.assertRaises(ValueError):
                usbip.receive_common(left, 5)

    def test_truncated_reply_rejected(self):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(b"ab")
            right.shutdown(socket.SHUT_WR)
            with self.assertRaises(ConnectionError):
                usbip.recv_exact(left, 8)

    def test_port_matches_speed_and_is_free(self):
        ports = [dict(hub="hs", port=0, status=6), dict(hub="hs", port=1, status=4),
                 dict(hub="ss", port=8, status=4)]
        self.assertEqual(usbip.select_port(3, ports), 1)
        self.assertEqual(usbip.select_port(5, ports), 8)
        with self.assertRaises(RuntimeError):
            usbip.select_port(3, ports, 0)
        with self.assertRaises(RuntimeError):
            usbip.select_port(3, ports, 8)


if __name__ == "__main__":
    unittest.main()
