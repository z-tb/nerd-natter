"""Shared helpers for the tests. Standard library only: run them with  make test."""
import importlib.util
import os
import stat
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path(name):
    return os.path.join(ROOT, name)


def load(filename, modname=None):
    """Import a project file by name, even if it has a dash in it (nn-config.py)."""
    spec = importlib.util.spec_from_file_location(modname or filename.replace("-", "_").replace(".py", ""),
                                                  path(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_command(folder, name, output="", exit_code=0, stderr=""):
    """Put a stand-in program in a folder, so tests can control what xdotool etc. say."""
    p = os.path.join(folder, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n")
        if stderr:
            f.write("echo '%s' >&2\n" % stderr)
        if output:
            f.write("echo '%s'\n" % output)
        f.write("exit %d\n" % exit_code)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p


def tempdir():
    return tempfile.TemporaryDirectory(prefix="nn-test-")
