#!/usr/bin/env python3
"""Host side push-to-talk key watcher for nerd-dictation (X11 / Cinnamon).

Hold the key (default Menu) to talk; release to stop. In hands-free mode (switched
on from the Cinnamon applet) a press toggles dictation instead; the server decides,
this script only reports key down / up.

The key press is captured with an X11 passive key grab (python-xlib). Applications
never receive the press, so no context menu pops up, and the keymap is never
touched: no xmodmap, nothing to restore, no stuck modifier or lock state. The X
server drops the grab by itself if this process dies for any reason.

A passive grab normally redirects the WHOLE keyboard to the grabber while the key
is held, which would swallow the dictated text. So the grab is taken in sync mode
and released the moment the press arrives (events typed in between are queued by
the server and delivered to the app in order). Release is detected by polling the
key state, and applications may see a stray Menu key-release, which they ignore.

Requires: python-xlib  (sudo apt install python3-xlib)

Usage:
  nn_watch.py            run the watcher
  nn_watch.py --debug    also print every key event seen
  nn_watch.py --observe  no grab, only watch via `xinput` (the key still reaches apps)
  nn_watch.py --send CMD talk to the container directly (on, off, down, up, status,
                          ping, handsfree on|off|toggle)

Environment:
  NN_SOCKET, NN_KEYCODES (comma separated, default 135 = Menu alone),
  NN_RELEASE_DELAY (0.3 s grace after release so trailing words are caught)

Avoid modifier keys (Alt/Ctrl/Shift) in NN_KEYCODES: a grab on them would break
every shortcut that uses them, and they stay held while dictated text is typed.
"""
import argparse
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

SOCKET = os.environ.get("NN_SOCKET") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "nerd-natter", "control.sock")
KEYS = {int(k) for k in os.environ.get("NN_KEYCODES", "135").split(",")}
RELEASE_DELAY = float(os.environ.get("NN_RELEASE_DELAY", "0.3"))

EVENT_RE = re.compile(r"EVENT type \d+ \((\w+)\)")
DETAIL_RE = re.compile(r"\s*detail:\s*(\d+)")


def send(cmd):
    # down/up can legitimately take seconds: the server waits for nerd-dictation to flush
    # the last words and stop (or to start recording) before it answers.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(25)
            s.connect(SOCKET)
            s.sendall((cmd + "\n").encode())
            return s.makefile().readline().strip()
    except OSError as e:
        return f"ERR {e}"


class Chord:
    """Tracks the keys and sends on/off with a short release grace.

    The grace also absorbs X11 autorepeat, which arrives as release+press pairs.
    """

    def __init__(self, debug=False):
        self.debug = debug
        self.down = set()
        self.active = False
        self.token = None
        self.lock = threading.Lock()

    def report(self, what, reply):
        if self.debug or reply.startswith("ERR"):
            print(f"{what}: {reply}", file=sys.stderr, flush=True)

    def key(self, code, pressed):
        if code not in KEYS:
            if self.debug:
                print(f"key {code} {'down' if pressed else 'up'}", file=sys.stderr, flush=True)
            return
        if self.debug and pressed and not self.active:
            print(f"key {code} down", file=sys.stderr, flush=True)
        with self.lock:
            if pressed:
                self.down.add(code)
            else:
                self.down.discard(code)
            want = self.down == KEYS
            if want:
                if self.token is not None:
                    self.token = None  # re-pressed during grace, still on
                elif not self.active:
                    self.active = True
                    self.report("down", send("down"))
            elif self.active and self.token is None:
                tok = object()
                self.token = tok
                t = threading.Timer(RELEASE_DELAY, self._release, args=(tok,))
                t.daemon = True
                t.start()

    def _release(self, tok):
        with self.lock:
            if self.token is not tok:
                return
            self.token = None
            self.active = False
            if self.debug:
                print("key up (confirmed)", file=sys.stderr, flush=True)
            self.report("up", send("up"))

    def force_off(self):
        with self.lock:
            self.token = None
            self.active = False
        send("off")


def run_grab(chord):
    """Passive grab: apps never see the press, keymap untouched, typing unaffected."""
    try:
        from Xlib import X, display, error
    except ImportError:
        sys.exit("missing python-xlib (sudo apt install python3-xlib)")
    import queue

    d = display.Display()
    root = d.screen().root
    errors = []
    for k in sorted(KEYS):
        err = error.CatchError(error.BadAccess)
        # Sync keyboard mode: while the grab is active the server queues other keys
        # instead of delivering them to us, so nothing is lost before we ungrab.
        root.grab_key(k, X.AnyModifier, True, X.GrabModeAsync, X.GrabModeSync, onerror=err)
        errors.append((k, err))
    d.sync()
    bad = [k for k, e in errors if e.get_error()]
    if bad:
        sys.exit(f"could not grab keycode(s) {bad}: another program already grabbed them")

    events = queue.Queue()
    held = threading.Event()

    def worker():  # talks to the container; never blocks the X loop
        while True:
            code, pressed = events.get()
            chord.key(code, pressed)

    def poller():  # key state polling: a release is not delivered once we ungrab
        pd = display.Display()  # separate connection, python-xlib is not thread safe
        while True:
            held.wait()
            time.sleep(0.025)
            km = pd.query_keymap()
            for k in sorted(KEYS):
                if not km[k // 8] & (1 << (k % 8)):
                    events.put((k, False))
            if not any(km[k // 8] & (1 << (k % 8)) for k in KEYS):
                held.clear()

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()

    while True:
        ev = d.next_event()
        if ev.type == X.KeyPress:
            d.ungrab_keyboard(X.CurrentTime)  # hand the keyboard back immediately
            d.flush()
            events.put((ev.detail, True))
            held.set()
        elif ev.type == X.KeyRelease:
            events.put((ev.detail, False))


MODIFIER_KEYCODES = {37, 105, 50, 62, 64, 108, 133, 134, 66, 77}   # Ctrl Shift Alt Super Caps NumLock
ESCAPE_KEYCODE = 9


def capture_key(timeout=20.0):
    """Wait for one key press and print its keycode (used by the applet's Choose key button).

    The whole keyboard is grabbed for the moment, so the press reaches only this program
    whatever window has focus, and no other grab (even the push-to-talk one) gets in the way.
    Exit status: 0 key printed, 1 cancelled with Escape or timed out, 2 cannot grab.
    """
    try:
        from Xlib import X, display
    except ImportError:
        sys.exit("missing python-xlib (sudo apt install python3-xlib)")
    d = display.Display()
    root = d.screen().root
    if root.grab_keyboard(True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime) != X.GrabSuccess:
        print("could not take the keyboard (another program is holding it)", file=sys.stderr)
        return 2
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            if not d.pending_events():
                select.select([d.fileno()], [], [], 0.1)
                continue
            ev = d.next_event()
            if ev.type != X.KeyPress:
                continue
            if ev.detail == ESCAPE_KEYCODE:
                return 1
            if ev.detail in MODIFIER_KEYCODES:
                print("that is a modifier key, press another", file=sys.stderr, flush=True)
                continue
            print(ev.detail, flush=True)
            return 0
        return 1
    finally:
        d.ungrab_keyboard(X.CurrentTime)
        d.flush()
        d.close()


def process(lines, chord):
    """Feed lines of `xinput test-xi2 --root` output to the chord (--observe mode)."""
    kind = None
    for line in lines:
        m = EVENT_RE.search(line)
        if m:
            kind = m.group(1)
            continue
        m = DETAIL_RE.match(line)
        if m and kind in ("RawKeyPress", "RawKeyRelease"):
            chord.key(int(m.group(1)), kind == "RawKeyPress")
            kind = None


def run_observe(chord):
    for tool in ("xinput", "stdbuf"):
        if not shutil.which(tool):
            sys.exit(f"missing {tool} (sudo apt install xinput coreutils)")
    proc = subprocess.Popen(["stdbuf", "-oL", "xinput", "test-xi2", "--root"],
                            stdout=subprocess.PIPE, text=True)
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    process(proc.stdout, chord)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", metavar="CMD", help="send on/off/status/ping and exit")
    ap.add_argument("--capture-key", action="store_true", help="print the keycode of the next key pressed")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--observe", action="store_true", help="watch via xinput without grabbing the key")
    args = ap.parse_args()

    if args.capture_key:
        sys.exit(capture_key())

    if args.send:
        reply = send(args.send)
        print(reply)
        sys.exit(0 if reply.startswith("OK") else 1)

    chord = Chord(debug=args.debug)

    def cleanup(*_):
        chord.force_off()
        os._exit(0)  # the X server releases the grab when we disconnect

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    mode = "observe" if args.observe else "grab"
    print(f"nn_watch: keycodes {sorted(KEYS)} ({mode}) -> {SOCKET}", file=sys.stderr, flush=True)
    (run_observe if args.observe else run_grab)(chord)
    cleanup()


if __name__ == "__main__":
    main()
