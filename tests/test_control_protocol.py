"""The service's control socket: what the key watcher and the applet talk to."""
import os
import socket
import stat
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers


class ModelLoadingLine(unittest.TestCase):
    def test_says_loading_until_the_model_is_ready(self):
        ctl = helpers.load("nn_server.py").Controller()
        ctl.model_ready = False
        self.assertEqual(ctl.model_line(), "model loading")
        ctl.model_ready = True
        self.assertEqual(ctl.model_line(), "model ready")

    def test_new_watchers_are_told_the_model_state(self):
        import queue
        ctl = helpers.load("nn_server.py").Controller()
        ctl.model_ready = False
        q = queue.Queue()
        ctl.subscribe(q)
        self.assertEqual(q.get_nowait(), "state idle 0")
        self.assertEqual(q.get_nowait(), "model loading")


class FakeController:
    """Stands in for the real controller, which needs nerd-dictation and a model."""

    def __init__(self):
        self.calls = []
        self.queues = []

    def down(self): self.calls.append("down"); return "OK"
    def up(self): self.calls.append("up"); return "OK"
    def on(self): self.calls.append("on"); return "OK"
    def off(self): self.calls.append("off"); return "OK"
    def status(self): return "OK idle"

    def set_handsfree(self, value):
        self.calls.append(("handsfree", value))
        return "OK"

    def subscribe(self, q):
        self.queues.append(q)
        q.put("state idle 0")

    def unsubscribe(self, q):
        self.queues.remove(q)


class ControlSocket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = helpers.load("nn_server.py")
        cls.tmp = helpers.tempdir()
        cls.path = os.path.join(cls.tmp.name, "control.sock")
        cls.fake = FakeController()
        cls.server_module.ctl = cls.fake
        old = os.umask(0o177)
        try:
            cls.server = cls.server_module.Server(cls.path, cls.server_module.Handler)
        finally:
            os.umask(old)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def ask(self, text):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(self.path)
            s.sendall((text + "\n").encode())
            return s.makefile().readline().strip()

    def test_ping(self):
        self.assertEqual(self.ask("ping"), "OK pong")

    def test_status(self):
        self.assertEqual(self.ask("status"), "OK idle")

    def test_press_and_release(self):
        self.fake.calls.clear()
        self.assertEqual(self.ask("down"), "OK")
        self.assertEqual(self.ask("up"), "OK")
        self.assertEqual(self.fake.calls, ["down", "up"])

    def test_hands_free_switch(self):
        self.fake.calls.clear()
        self.ask("handsfree on")
        self.ask("handsfree off")
        self.ask("handsfree toggle")
        self.assertEqual(self.fake.calls, [("handsfree", True), ("handsfree", False), ("handsfree", None)])

    def test_unknown_commands_are_refused(self):
        self.assertTrue(self.ask("rm -rf").startswith("ERR"))
        self.assertTrue(self.ask("handsfree maybe").startswith("ERR"))
        self.assertTrue(self.ask("").startswith("ERR"))

    def test_watch_streams_state(self):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(self.path)
            s.sendall(b"watch\n")
            self.assertEqual(s.makefile().readline().strip(), "state idle 0")

    def test_only_you_can_open_the_socket(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode) & 0o077, 0)

    def test_there_is_no_network_port(self):
        # the server class is file-socket only
        self.assertEqual(self.server.address_family, socket.AF_UNIX)


class PatchesToNerdDictation(unittest.TestCase):
    def setUp(self):
        self.m = helpers.load("nn_server.py")

    def test_an_unknown_version_is_skipped_not_broken(self):
        source = "print('some other nerd-dictation')\n"
        patched, report = self.m.apply_patches(source)
        self.assertEqual(patched, source)
        self.assertTrue(report and not any(ok for _, ok in report))

    @unittest.skipUnless(os.environ.get("NN_TEST_ND") and os.path.isfile(os.environ.get("NN_TEST_ND", "")),
                         "set NN_TEST_ND=/path/to/nerd-dictation (the file) to check the real one")
    def test_all_patches_apply_to_the_tested_version(self):
        with open(os.environ["NN_TEST_ND"], encoding="utf-8") as f:
            patched, report = self.m.apply_patches(f.read())
        self.assertEqual([n for n, ok in report if not ok], [], "patches that no longer apply")
        compile(patched, "nerd-dictation", "exec")   # and the result is still valid Python


if __name__ == "__main__":
    unittest.main()
