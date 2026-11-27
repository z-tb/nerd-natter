"""Obvious things about the project as a whole: files parse, versions match, nn-ctl behaves."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

APPLET = helpers.path("applet/nerd-natter@z-tb")
BASH = shutil.which("bash")   # found now, so tests can change PATH later


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def ctl(*args, env=None, home=None):
    e = dict(os.environ, **(env or {}))
    e["HOME"] = home
    e["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    e["XDG_RUNTIME_DIR"] = os.path.join(home, "run")
    for k in [k for k in e if k.startswith("NN_") and k not in (env or {})]:
        del e[k]
    return subprocess.run([BASH, helpers.path("nn-ctl")] + list(args), env=e,
                          capture_output=True, text=True, timeout=30)


class FilesAreValid(unittest.TestCase):
    def test_shell_scripts_parse(self):
        for name in ("nn-ctl", "install.sh"):
            r = subprocess.run(["bash", "-n", helpers.path(name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + ": " + r.stderr)

    def test_python_files_compile(self):
        for name in ("nn_server.py", "nn_watch.py", "nn-config.py"):
            compile(read(helpers.path(name)), name, "exec")

    def test_applet_json_is_valid(self):
        for name in ("metadata.json", "settings-schema.json"):
            json.loads(read(APPLET, name))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_applet_javascript_parses(self):
        r = subprocess.run(["node", "--check", os.path.join(APPLET, "applet.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_applet_icons_exist(self):
        js = read(APPLET, "applet.js")
        for name in re.findall(r"'(nn-[a-z-]+)'", js):
            if name.endswith("symbolic"):
                self.assertTrue(os.path.exists(os.path.join(APPLET, "icons", name + ".svg")), name)

    def test_every_applet_setting_is_in_the_schema(self):
        js = read(APPLET, "applet.js")
        schema = json.loads(read(APPLET, "settings-schema.json"))
        for key in re.findall(r"settings\.bind\('([a-z-]+)'", js):
            self.assertIn(key, schema, "applet uses a setting the schema does not define: " + key)

    def test_no_machine_specific_paths(self):
        for name in ("nn-ctl", "install.sh", "Makefile", "README.md", "nn_server.py", "nn_watch.py", "nn-config.py"):
            text = read(helpers.path(name))
            for bad in ("/mnt/nerd", "usertm", "/home/claude"):
                self.assertNotIn(bad, text, name)

    def test_the_applet_shows_the_project_version(self):
        version = read(helpers.path("VERSION")).strip()
        self.assertEqual(json.loads(read(APPLET, "metadata.json"))["version"], version,
                         "run ./nn-ctl sync-version (make build and make install do this)")


class Controller(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tempdir()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_set_and_get(self):
        ctl("set", "BEEP_LEAD", "0.5", home=self.home)
        self.assertEqual(ctl("get", "BEEP_LEAD", home=self.home).stdout.strip(), "0.5")

    def test_environment_beats_settings_file(self):
        ctl("set", "BEEP_LEAD", "0.5", home=self.home)
        r = ctl("get", "BEEP_LEAD", env={"BEEP_LEAD": "0.9"}, home=self.home)
        self.assertEqual(r.stdout.strip(), "0.9")

    def test_defaults_are_generic(self):
        vosk = ctl("get", "VOSK_DIR", home=self.home).stdout.strip()
        self.assertEqual(vosk, os.path.join(self.home, ".local/share/vosk-models"))

    def test_unknown_setting_is_an_error(self):
        self.assertNotEqual(ctl("get", "NOPE", home=self.home).returncode, 0)

    def test_old_settings_are_carried_over(self):
        old = os.path.join(self.home, ".config/nerd-dictation-ptt")
        os.makedirs(old)
        with open(os.path.join(old, "config.env"), "w") as f:
            f.write("RUN_MODE=venv\nPTT_LIVE=1\nND_CONFIG=/x/nd-config.py\n")
        self.assertEqual(ctl("get", "RUN_MODE", home=self.home).stdout.strip(), "venv")
        self.assertEqual(ctl("get", "NN_LIVE", home=self.home).stdout.strip(), "1")
        self.assertEqual(ctl("get", "NN_CONFIG", home=self.home).stdout.strip(), "/x/nn-config.py")

    def test_rules_file_is_created_once(self):
        first = ctl("rules", home=self.home).stdout.strip()
        self.assertTrue(first.endswith("nerd-natter/user-rules.py"))
        with open(first, "a") as f:
            f.write("# mine\n")
        ctl("rules", home=self.home)
        self.assertIn("# mine", read(first))

    def test_docker_container_has_no_network(self):
        nd = os.path.join(self.home, "nd"); model = os.path.join(self.home, "m")
        os.makedirs(os.path.join(model, "conf")); os.makedirs(nd)
        helpers.fake_command(nd, "nerd-dictation")
        for k, v in (("RUN_MODE", "docker"), ("NERD_DIR", nd), ("MODEL_DIR", model)):
            ctl("set", k, v, home=self.home)
        r = ctl("start", env={"NN_DRYRUN": "1", "SSH_AUTH_SOCK": "/tmp/agent"}, home=self.home)
        out = r.stdout + r.stderr
        self.assertIn("--network none", out)
        self.assertNotIn(" -p ", out)              # no published port
        self.assertNotIn("/tmp/agent", out)        # the SSH agent is not handed to the container

    def test_detect_reports_the_window_class_and_mode(self):
        bin_ = os.path.join(self.home, "bin")
        os.makedirs(bin_)
        helpers.fake_command(bin_, "xdotool", output="Gnome-terminal")
        r = ctl("detect", "0", env={"PATH": bin_ + ":" + os.environ["PATH"]}, home=self.home)
        self.assertIn('window class is "Gnome-terminal"', r.stdout)
        self.assertIn("command auto", r.stdout)

    def test_detect_says_when_xdotool_is_missing(self):
        # a PATH with every normal program except xdotool, whatever is installed here
        farm = os.path.join(self.home, "farm")
        os.makedirs(farm)
        for folder in os.environ["PATH"].split(os.pathsep):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                link = os.path.join(farm, name)
                if name != "xdotool" and not os.path.lexists(link):
                    os.symlink(os.path.join(folder, name), link)
        r = ctl("detect", "0", env={"PATH": farm}, home=self.home)
        self.assertIn("xdotool is NOT installed", r.stdout)

    def test_version_command(self):
        self.assertEqual(ctl("version", home=self.home).stdout.strip(), read(helpers.path("VERSION")).strip())


class Installer(unittest.TestCase):
    def test_help_works(self):
        r = subprocess.run(["bash", helpers.path("install.sh"), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--mode", r.stdout)

    def test_bad_mode_is_refused(self):
        r = subprocess.run(["bash", helpers.path("install.sh"), "--mode", "nonsense", "--yes"],
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
