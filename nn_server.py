#!/usr/bin/env python3
"""Nerd Natter control server for nerd-dictation. Runs INSIDE the container.

Listens on a TCP port (published to 127.0.0.1 only on the host). One command
per connection, except `watch`, which stays open and streams state changes.

Commands:
  down / up        key press / release, as sent by nn_watch.py. Hold mode:
                   down starts dictation, up stops it. Hands-free mode:
                   down toggles dictation, up is ignored.
  on / off         explicit start / stop, whatever the mode
  handsfree on|off|toggle   switch hands-free mode (ends dictation in progress)
  status           "OK <idle|active> <hold|handsfree>"
  watch            stream "state <idle|active> <0|1>" lines (1 = hands-free),
                   the current state first, then every change, plus "ping"
                   keepalives. Used by the Cinnamon applet.
  ping

Just before the mic opens a "starting <0|1>" line is sent and the server pauses
NN_BEEP_LEAD seconds (default 0.35, 0 disables) so the watcher's start beep is
over before recording begins; otherwise the microphone hears the beep.

A state line is sent only after the transition really completed: "active" means
nerd-dictation is recording (the mic is hot), "idle" means it has flushed its last
words and stopped.

How suspend and resume are done (default mode): nerd-dictation is started once
with --suspend-on-start so the Vosk model stays loaded. `nerd-dictation suspend`
and `resume` only send signals and return at once, so this server sends the
signals itself and then WAITS until the process really is stopped (state T in
/proc) or really recording again (a parec child exists). Without the waiting a
quick release and press can desynchronise it from nerd-dictation, and a slow
flush of the last words gets typed after the next key press.

nerd-dictation itself is run from a patched temporary copy (see PATCHES; your
checkout is never modified):
  - suspend happens at a safe point in its main loop instead of inside the signal
    handler, where it could interrupt typing and leave stale text state behind
  - partial results are not typed (no typing and deleting while you speak, which
    shows as flicker); finished sentences are typed as soon as Vosk finalises
    them, and whatever is left when you release the key is typed then
If an anchor is not found (a different nerd-dictation version) that patch is
skipped and a warning is logged.

Environment:
  ND_SCRIPT, ND_COOKIE, ND_BEGIN_ARGS, NN_MODE, ND_DELAY_EXIT
  NN_SOCKET             control socket path (default: <state folder>/control.sock). There is no
                        network port: the container can run with --network none
  NN_TMP                folder for the patched copy of nerd-dictation (/tmp/nn)
  NN_MAX_ON             safety: auto-stop held dictation after N s (300, 0 disables)
  NN_MAX_ON_HANDSFREE   same for hands-free dictation (1800, 0 disables)
  NN_PING               keepalive interval for `watch` in seconds (10)
  NN_LIVE=1             type partial results live again (flicker returns)
  NN_PATCH=0            run the original nerd-dictation unpatched
"""
import os
import queue
import shlex
import signal
import socketserver
import subprocess
import sys
import threading
import time

ND = os.environ.get("ND_SCRIPT", "/home/appuser/nerd-dictation/nerd-dictation")
COOKIE = os.environ.get("ND_COOKIE", "/tmp/nerd-dictation.cookie")
BEGIN_ARGS = shlex.split(os.environ.get("ND_BEGIN_ARGS", ""))
MODE = os.environ.get("NN_MODE", "suspend")
DELAY_EXIT = os.environ.get("ND_DELAY_EXIT", "0.4")
MAX_ON = float(os.environ.get("NN_MAX_ON", "300"))
MAX_ON_HANDSFREE = float(os.environ.get("NN_MAX_ON_HANDSFREE", "1800"))
SOCKET = os.environ.get("NN_SOCKET") or os.path.join(
    os.environ.get("NN_STATE_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "nerd-natter"),
    "control.sock")
PATCH_DIR = os.environ.get("NN_TMP", "/tmp/nn")   # where the patched copy of nerd-dictation goes
PING = float(os.environ.get("NN_PING", "10"))
LIVE = os.environ.get("NN_LIVE") == "1"
PATCH = os.environ.get("NN_PATCH", "1") != "0"

NOT_READY_WAIT = 3.0    # s to wait for a still loading nerd-dictation to reach "suspended"
SUSPEND_TIMEOUT = 15.0  # s, flushing the last words with a big model can take a while
RESUME_TIMEOUT = 5.0    # s for the recording process to come up
BEEP_LEAD = float(os.environ.get("NN_BEEP_LEAD", "0.35"))  # s the start beep gets before the mic opens
SETTLE = 0.05           # s after parec appears, before we call the mic hot


def log(msg):
    print(f"[nn] {msg}", flush=True)


# --- patches for nerd-dictation (applied to a temporary copy) ---------------------
PATCHES = [
    ("finals-only flag",
     "SIMULATE_INPUT_CODE_COMMAND = -1\n",
     "SIMULATE_INPUT_CODE_COMMAND = -1\n\n"
     "# nn patch: skip partial results, so nothing is typed and deleted again while speaking\n"
     "FINALS_ONLY = bool(os.environ.get(\"NN_FINALS_ONLY\"))\n"),

    ("finals-only check",
     "    def handle_fn_wrapper(text: str, is_partial_arg: bool) -> None:\n"
     "        nonlocal handled_any\n"
     "        nonlocal text_prev\n",
     "    def handle_fn_wrapper(text: str, is_partial_arg: bool) -> None:\n"
     "        nonlocal handled_any\n"
     "        nonlocal text_prev\n\n"
     "        if is_partial_arg and FINALS_ONLY:\n"
     "            return\n"),

    ("suspend request flag",
     "    def handle_sig_suspend_from_usr1(_signum: int, _frame: Optional[FrameType]) -> None:\n"
     "        nonlocal suspend\n"
     "        if suspend:\n"
     "            return\n"
     "        suspend = True\n"
     "        do_suspend_pause()\n"
     "        # Use when Py3.6 compatibility is dropped.\n"
     "        # `signal.raise_signal(signal.SIGSTOP)`\n"
     "        os.kill(os.getpid(), signal.SIGSTOP)\n",
     "    suspend_pending = False\n\n"
     "    def handle_sig_suspend_from_usr1(_signum: int, _frame: Optional[FrameType]) -> None:\n"
     "        # nn patch: only flag the request. Suspending from inside the signal handler can\n"
     "        # interrupt a typing call half way and leave stale text state behind, so the main\n"
     "        # loop does the suspending at a safe point.\n"
     "        nonlocal suspend_pending\n"
     "        if suspend:\n"
     "            return\n"
     "        suspend_pending = True\n"),

    ("suspend at safe point",
     "        if suspend:\n"
     "            continue\n"
     "\n"
     "        if idle_time > 0.0:\n",
     "        if suspend_pending:\n"
     "            suspend_pending = False\n"
     "            suspend = True\n"
     "            do_suspend_pause()\n"
     "            os.kill(os.getpid(), signal.SIGSTOP)\n"
     "            continue\n"
     "\n"
     "        if suspend:\n"
     "            continue\n"
     "\n"
     "        if idle_time > 0.0:\n"),
]


def apply_patches(src):
    """Returns (patched source, [(name, applied)]). A patch whose anchor is not found
    exactly once is skipped, so a different nerd-dictation version never breaks."""
    report = []
    for name, old, new in PATCHES:
        if src.count(old) == 1:
            src = src.replace(old, new)
            report.append((name, True))
        else:
            report.append((name, False))
    return src, report


def prepare_nd():
    """Point ND at a patched temporary copy of nerd-dictation."""
    global ND
    if not PATCH:
        log("patches disabled, running nerd-dictation as it is")
        return
    try:
        with open(ND, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        log(f"cannot read {ND} to patch it: {e}")
        return
    patched, report = apply_patches(src)
    for name, ok in report:
        if not ok:
            log(f"WARNING patch skipped, anchor not found (different nerd-dictation version?): {name}")
    if not any(ok for _, ok in report):
        return
    os.makedirs(PATCH_DIR, exist_ok=True)
    path = os.path.join(PATCH_DIR, "nerd-dictation")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(patched)
    os.chmod(path, 0o755)
    ND = path
    log("running patched copy: " + ", ".join(n for n, ok in report if ok))


# --- /proc helpers ----------------------------------------------------------------
def proc_stat(pid):
    """(state, ppid) from /proc/<pid>/stat, or (None, None) when gone."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return None, None
    rest = data[data.rindex(")") + 2:].split()
    return rest[0], int(rest[1])


def proc_state(pid):
    return proc_stat(pid)[0]


def has_parec(pid):
    """True when `pid` has a parec child, i.e. nerd-dictation is recording."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        state, ppid = proc_stat(entry)
        if ppid != pid or state in (None, "Z"):
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if any(os.path.basename(a) == b"parec" for a in argv[:2]):
            return True
    return False


def wait_for(cond, timeout, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class Controller:
    def __init__(self):
        self.lock = threading.Lock()  # serialises transitions
        self.active = False
        self.handsfree = False
        self.stopping = False
        self.child = None
        self.watchdog = None
        self.subs = set()
        self.subs_lock = threading.Lock()

    # --- state broadcast ---------------------------------------------------
    def state_line(self):
        return f"state {'active' if self.active else 'idle'} {1 if self.handsfree else 0}"

    def announce_start(self):
        """Tell watchers the mic is about to open, and give their beep time to finish.

        The beep must not play into a hot mic: Vosk happily hears it as a stray "the".
        """
        with self.subs_lock:
            if not self.subs or BEEP_LEAD <= 0:
                return
            for q in self.subs:
                q.put(f"starting {1 if self.handsfree else 0}")
        time.sleep(BEEP_LEAD)

    def broadcast(self):
        with self.subs_lock:  # line built inside the lock so the last one sent is current
            line = self.state_line()
            for q in self.subs:
                q.put(line)

    def subscribe(self, q):
        with self.subs_lock:
            q.put(self.state_line())
            self.subs.add(q)

    def unsubscribe(self, q):
        with self.subs_lock:
            self.subs.discard(q)

    # --- nerd-dictation process --------------------------------------------
    def nd_env(self):
        env = dict(os.environ)
        if not LIVE:
            env["NN_FINALS_ONLY"] = "1"
        return env

    def nd(self, *args, timeout=15):
        """Run a nerd-dictation sub-command (restart mode: end)."""
        cmd = [sys.executable, ND, *args, "--cookie", COOKIE]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"exit {r.returncode}")

    def begin_cmd(self, *extra):
        return [sys.executable, ND, "begin", "--cookie", COOKIE, *extra, *BEGIN_ARGS]

    def supervise(self):
        """Suspend mode: keep one suspended nerd-dictation process alive."""
        while not self.stopping:
            try:
                os.remove(COOKIE)
            except FileNotFoundError:
                pass
            log("starting nerd-dictation (suspended, model loading)")
            self.child = subprocess.Popen(self.begin_cmd("--suspend-on-start"), env=self.nd_env())
            self.child.wait()
            changed = False
            with self.lock:
                if self.active:
                    self.active = False
                    changed = True
            if changed:
                self.broadcast()
            if self.stopping:
                break
            log("nerd-dictation exited, restarting in 2s")
            time.sleep(2)

    def nd_pid(self):
        if self.child is None or self.child.poll() is not None:
            raise RuntimeError("nerd-dictation is not running")
        return self.child.pid

    def resume_nd(self):
        """Wake the suspended process and return once it is recording again."""
        pid = self.nd_pid()
        if proc_state(pid) != "T" and not has_parec(pid):
            # still loading the model: wait briefly for it to park itself
            if not wait_for(lambda: proc_state(pid) == "T", NOT_READY_WAIT):
                raise RuntimeError("not ready, nerd-dictation is probably still loading the model")
        if proc_state(pid) == "T":
            os.kill(pid, signal.SIGCONT)
        if not wait_for(lambda: has_parec(pid), RESUME_TIMEOUT):
            raise RuntimeError("nerd-dictation did not start recording")
        time.sleep(SETTLE)

    def suspend_nd(self):
        """Ask the process to suspend and return once it has flushed and stopped."""
        pid = self.nd_pid()
        if proc_state(pid) != "T":
            os.kill(pid, signal.SIGUSR1)
            if not wait_for(lambda: proc_state(pid) == "T", SUSPEND_TIMEOUT):
                raise RuntimeError("nerd-dictation did not suspend in time")
        # it signals parec and stops without waiting for it: a recorder that is still exiting
        # must not be mistaken for a new one by the next resume, nor count as a hot mic
        wait_for(lambda: not has_parec(pid), 2.0)

    # --- dictation on/off ----------------------------------------------------
    def arm_watchdog(self):
        limit = MAX_ON_HANDSFREE if self.handsfree else MAX_ON
        if limit > 0:
            self.watchdog = threading.Timer(limit, self.off)
            self.watchdog.daemon = True
            self.watchdog.start()

    def on(self):
        with self.lock:
            if self.active:
                return "OK on"
            if MODE == "suspend":
                try:
                    pid = self.nd_pid()
                except RuntimeError:
                    pid = None
                if pid is None or (proc_state(pid) != "T" and not has_parec(pid)):
                    # still loading the model: say so instead of beeping and then failing
                    with self.subs_lock:
                        for q in self.subs:
                            q.put("loading")
                    log("key pressed while the model is still loading, ignored")
                    return "ERR model still loading"
            self.announce_start()
            t0 = time.monotonic()
            try:
                if MODE == "suspend":
                    self.resume_nd()
                else:
                    self.child = subprocess.Popen(self.begin_cmd("--delay-exit", DELAY_EXIT), env=self.nd_env())
            except Exception as e:
                log(f"on failed: {e}")
                return f"ERR {e}"
            self.active = True
            self.arm_watchdog()
            log(f"on, recording after {time.monotonic() - t0:.2f}s")
        self.broadcast()
        return "OK on"

    def off(self):
        with self.lock:
            if self.watchdog:
                self.watchdog.cancel()
                self.watchdog = None
            if not self.active:
                return "OK off"
            t0 = time.monotonic()
            self.active = False
            try:
                if MODE == "suspend":
                    self.suspend_nd()
                else:
                    self.nd("end")
            except Exception as e:
                log(f"off failed: {e}")
                self.broadcast()
                return f"ERR {e}"
            log(f"off, stopped after {time.monotonic() - t0:.2f}s")
        self.broadcast()
        return "OK off"

    # --- modes and key events ----------------------------------------------------
    def set_handsfree(self, value):
        if value is None:
            value = not self.handsfree
        if value != self.handsfree:
            self.off()  # a mode change always ends dictation in progress
            self.handsfree = value
            log(f"hands-free {'on' if value else 'off'}")
        self.broadcast()
        return f"OK handsfree {1 if self.handsfree else 0}"

    def down(self):
        if self.handsfree and self.active:
            return self.off()
        return self.on()

    def up(self):
        if self.handsfree:
            return "OK ignored"
        return self.off()

    def status(self):
        return f"OK {'active' if self.active else 'idle'} {'handsfree' if self.handsfree else 'hold'}"

    def shutdown(self):
        self.stopping = True
        try:
            self.off()
        except Exception:
            pass
        if self.child and self.child.poll() is None:
            if proc_state(self.child.pid) == "T":
                os.kill(self.child.pid, signal.SIGCONT)  # a stopped process ignores SIGTERM
            self.child.terminate()


ctl = Controller()


class Handler(socketserver.StreamRequestHandler):
    def reply(self, text):
        try:
            self.wfile.write((text + "\n").encode())
        except OSError:
            pass  # client already gone, nothing to tell it

    def watch(self):
        q = queue.Queue()
        ctl.subscribe(q)
        try:
            while True:
                try:
                    line = q.get(timeout=PING)
                except queue.Empty:
                    line = "ping"
                self.wfile.write((line + "\n").encode())
        except OSError:
            pass
        finally:
            ctl.unsubscribe(q)

    def handle(self):
        parts = self.rfile.readline(64).decode(errors="replace").strip().lower().split()
        cmd = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "watch":
            return self.watch()
        if cmd == "down":
            reply = ctl.down()
        elif cmd == "up":
            reply = ctl.up()
        elif cmd == "on":
            reply = ctl.on()
        elif cmd == "off":
            reply = ctl.off()
        elif cmd == "handsfree" and arg in ("on", "off", "toggle"):
            reply = ctl.set_handsfree({"on": True, "off": False, "toggle": None}[arg])
        elif cmd == "status":
            reply = ctl.status()
        elif cmd == "ping":
            reply = "OK pong"
        else:
            reply = "ERR unknown command"
        self.reply(reply)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    prepare_nd()
    # A file socket instead of a network port: nothing can reach it from outside this
    # computer, only your own user can open it, and the container needs no network at all.
    os.makedirs(os.path.dirname(SOCKET), mode=0o700, exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o177)
    try:
        server = Server(SOCKET, Handler)
    finally:
        os.umask(old_umask)
    os.chmod(SOCKET, 0o600)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if MODE == "suspend":
        threading.Thread(target=ctl.supervise, daemon=True).start()
    log(f"listening on {SOCKET}, mode={MODE}, partial results {'typed live' if LIVE else 'not typed'}")
    try:
        server.serve_forever()
    finally:
        ctl.shutdown()
        try:
            os.unlink(SOCKET)
        except OSError:
            pass


if __name__ == "__main__":
    main()
