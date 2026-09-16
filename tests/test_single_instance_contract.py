import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
MAIN = (ROOT / "nuanyu_web.py").read_text(encoding="utf-8")


class SingleInstanceContractTest(unittest.TestCase):
    def test_executable_acquires_nonblocking_process_lock(self):
        self.assertIn('open("/run/nuanyu_web_5004.lock", "w")', MAIN)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", MAIN)
        self.assertIn("raise SystemExit(73)", MAIN)

    def test_http_server_does_not_allow_parallel_port_owners(self):
        self.assertNotIn(
            "setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT",
            MAIN,
        )
        self.assertIn("SO_REUSEADDR", MAIN)


if __name__ == "__main__":
    unittest.main()
