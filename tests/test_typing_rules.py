import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers


def fresh():
    """A fresh copy of nn-config.py with no leftover state from other tests."""
    return helpers.load("nn-config.py")


class ShellRules(unittest.TestCase):
    """What gets typed in a terminal."""

    @classmethod
    def setUpClass(cls):
        cls.m = fresh()

    def check(self, spoken, typed):
        self.assertEqual(self.m.shell(spoken), typed, spoken)

    def test_paths(self):
        self.check("ls slash e t c", "ls /etc")
        self.check("c d slash e t c", "cd /etc")
        self.check("cat slash e t c slash past w d", "cat /etc/passwd")
        self.check("cat file dot txt", "cat file.txt")

    def test_flags(self):
        self.check("hail minus 100 slash var slash logs lashed his log", "tail -100 /var/log/syslog")
        self.check("grep double dash help", "grep --help")

    def test_misheard_words(self):
        self.check("system control daemon reload", "systemctl daemon-reload")
        self.check("cat flash at sea slash hosts hope to grab fu", "cat /etc/hosts | grep foo")

    def test_symbols(self):
        self.check("make double ampersand ls", "make && ls")
        self.check("ls pipe wc", "ls | wc")
        self.check("make semi colon ls", "make; ls")
        self.check("make semi-colon ls", "make; ls")
        self.check("echo quote hello world quote", 'echo "hello world"')

    def test_literal_keeps_the_word(self):
        self.assertIn("slash", self.m.shell("echo literal slash"))


class TextRules(unittest.TestCase):
    """What gets typed everywhere else."""

    @classmethod
    def setUpClass(cls):
        cls.m = fresh()

    def test_punctuation(self):
        self.assertEqual(self.m.prose("hello comma world"), "hello, world")
        self.assertEqual(self.m.prose("really question mark"), "really?")
        self.assertEqual(self.m.prose("stop period"), "stop.")

    def test_literal(self):
        self.assertEqual(self.m.prose("say literal comma"), "say comma")

    def test_symbol_words_stay_words(self):
        self.assertIn("slash", self.m.prose("slash"))


class WindowDetection(unittest.TestCase):
    """Is the window a terminal? Uses a stand-in xdotool, so no display is needed."""

    def setUp(self):
        self.tmp = helpers.tempdir()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        env = {"PATH": self.tmp.name + os.pathsep + os.environ["PATH"], "NN_STATE_DIR": self.state}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch("sys.stderr", io.StringIO())   # the rules log which mode they chose
        quiet.start()
        self.addCleanup(quiet.stop)
        self.m = fresh()

    def window(self, name):
        helpers.fake_command(self.tmp.name, "xdotool", output=name)
        self.m._class_cache.update(at=0.0, value="")

    def test_class_is_lowercased(self):
        self.window("Gnome-terminal")
        self.assertEqual(self.m.active_class(), "gnome-terminal")

    def test_terminal_windows_get_command_mode(self):
        for name in ("Gnome-terminal", "gnome-terminal-server", "XTerm", "kitty", "Alacritty"):
            self.window(name)
            self.assertEqual(self.m.nerd_dictation_process("double ampersand"), "&&", name)

    def test_other_windows_get_text_mode(self):
        for name in ("Firefox", "code", "Thunderbird"):
            self.window(name)
            self.assertNotEqual(self.m.nerd_dictation_process("double ampersand"), "&&", name)

    def test_unreadable_window_falls_back_to_text(self):
        helpers.fake_command(self.tmp.name, "xdotool", stderr="Can not open display", exit_code=1)
        self.m._class_cache.update(at=0.0, value="")
        with mock.patch("sys.stderr"):
            self.assertEqual(self.m.active_class(), "")
        self.assertNotEqual(self.m.nerd_dictation_process("double ampersand"), "&&")

    def test_missing_xdotool_does_not_crash(self):
        os.environ["PATH"] = self.tmp.name   # nothing installed here
        self.m._class_cache.update(at=0.0, value="")
        with mock.patch("sys.stderr"):
            self.assertEqual(self.m.active_class(), "")

    def test_common_terminals_are_listed(self):
        for name in ("gnome-terminal-server", "gnome-terminal", "xterm", "kitty", "alacritty",
                     "konsole", "tilix", "mate-terminal"):
            self.assertIn(name, self.m.TERMINALS)


class VoiceSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tempdir()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"NN_STATE_DIR": self.tmp.name,
                                               "PATH": self.tmp.name + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)
        helpers.fake_command(self.tmp.name, "xdotool", output="Firefox")
        self.m = fresh()

    def test_command_mode_then_back(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(self.m.nerd_dictation_process("command mode"), "")
            self.assertEqual(self.m.nerd_dictation_process("double ampersand"), "&&")
            self.assertEqual(self.m.mode_for("firefox", self.m._read_forced()), ("command", "voice"))
            self.assertEqual(self.m.nerd_dictation_process("automatic mode"), "")
        self.assertEqual(self.m.mode_for("firefox", self.m._read_forced()), ("text", "auto"))

    def test_the_alone_is_dropped(self):
        self.assertEqual(self.m.nerd_dictation_process("the"), "")


class YourOwnRules(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tempdir()
        self.addCleanup(self.tmp.cleanup)
        self.rules = os.path.join(self.tmp.name, "user-rules.py")
        patcher = mock.patch.dict(os.environ, {"NN_USER_RULES": self.rules})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_your_rules_are_applied(self):
        with open(self.rules, "w") as f:
            f.write('FIXES["bar"] = "baz"\nTERMINALS.add("my-term")\n')
        with mock.patch("sys.stderr"):
            m = fresh()
        self.assertIn("my-term", m.TERMINALS)
        self.assertEqual(m.shell("bar"), "baz")

    def test_a_mistake_in_your_rules_does_not_break_typing(self):
        with open(self.rules, "w") as f:
            f.write("this is not python (\n")
        with mock.patch("sys.stderr"):
            m = fresh()
        self.assertEqual(m.shell("ls slash e t c"), "ls /etc")

    def test_no_rules_file_is_fine(self):
        m = fresh()
        self.assertEqual(m.shell("ls slash e t c"), "ls /etc")


if __name__ == "__main__":
    unittest.main()
