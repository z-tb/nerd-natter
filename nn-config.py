"""nerd-dictation config: prose punctuation normally, shell symbols in a terminal.

Loaded through the nerd-dictation arguments (applet: Configure > Setup, or NN_ARGS in
config.env; the default already contains --config /home/appuser/nn-config.py). Keep
--numbers-as-digits there so "two" types 2 (good for commands).
"""
import re
import subprocess
import sys
import time

# Window classes treated as a terminal (lowercase). Check yours with:
#   xdotool getactivewindow getwindowclassname
TERMINALS = {
    "gnome-terminal-server", "gnome-terminal", "xterm", "tilix", "kitty",
    "alacritty", "xfce4-terminal", "konsole", "terminator", "urxvt", "st",
    "mate-terminal", "lxterminal", "qterminal", "foot", "wezterm", "wezterm-gui",
    "guake", "yakuake", "terminology", "sakura", "xvt", "uxterm", "rxvt", "ptyxis",
    "kgx", "cool-retro-term", "deepin-terminal", "hyper", "tabby", "x-terminal-emulator",
}
# Programs that are not terminals but contain one (VS Code's terminal panel, for example)
# cannot be told apart from the window. Say "command mode" to switch to command typing and
# "text mode" to go back, or "automatic mode" to follow the window again.
MODE_PHRASES = {
    "command mode": "shell",
    "shell mode": "shell",
    "text mode": "prose",
    "normal mode": "prose",
    "automatic mode": None,
    "auto mode": None,
}

# --- prose mode -----------------------------------------------------------
# Say "literal <phrase>" to type the words instead of the symbol.
PROSE = {
    "question mark": "?",
    "exclamation point": "!",
    "comma": ",",
    "period": ".",
    "colon": ":",
    "semicolon": ";",
}

# --- shell mode -----------------------------------------------------------
# (spoken phrase, symbol, join)   join: "both" glues to the words on each side,
# "next" glues to the following word only, "prev" to the word before only,
# "none" leaves spacing alone.
# Longer phrases first so "leading slash" wins over "slash". Dashes: see DASHES.
SHELL = [
    ("double ampersand", "&&", "none"),
    ("double greater than", ">>", "none"),
    ("double less than", "<<", "none"),
    ("double pipe", "||", "none"),
    ("leading slash", "/", "next"),   # absolute path: "cat leading slash etc slash hosts"
    ("leading dot", ".", "next"),     # "ls dot git" -> "ls .git"
    ("slash", "/", "both"),
    ("backslash", "\\", "both"),
    ("dot", ".", "both"),
    ("tilde", "~", "next"),
    ("underscore", "_", "both"),
    ("equals", "=", "both"),
    ("dollar", "$", "next"),
    ("star", "*", "both"),
    ("pipe", "|", "none"),
    ("greater than", ">", "none"),
    ("less than", "<", "none"),
    ("semicolon", ";", "prev"),       # "ls semicolon ls" -> "ls; ls"
    ("open paren", "(", "next"),
    ("close paren", ")", "prev"),
    ("open bracket", "[", "next"),
    ("close bracket", "]", "prev"),
    ("open brace", "{", "next"),
    ("close brace", "}", "prev"),
    ("hash", "#", "next"),
    ("at sign", "@", "both"),
    ("ampersand", "&", "none"),
    ("colon", ":", "both"),
    ("comma", ",", "both"),
]

# Quotes come in pairs: the first one opens ("next" join), the second closes ("prev" join).
# 'echo quote hello world quote' -> echo "hello world"
QUOTES = [("single quote", "'"), ("double quote", '"'), ("quote", '"')]

# "minus 100" -> "-100", "double dash help" -> "--help": glued to the next word.
DASHES = [("double dash", "--"), ("double minus", "--"), ("minus", "-"), ("dash", "-")]

# Words Vosk tends to mishear in commands (what it typed -> what you meant). Matched as
# whole words after spoken letters are joined ("p w d" -> "pwd"). Add your own as you
# find them; this is the main way to teach it your vocabulary.
FIXES = {
    "pseudo": "sudo",
    "app get": "apt",
    "system ctl": "systemctl",
    "semi colon": "semicolon",
    "semi-colon": "semicolon",
    "system control": "systemctl",
    "journal control": "journalctl",
    "hail": "tail",
    "grab": "grep",
    "grip": "grep",
    "hope to": "pipe",
    "lashed": "slash",
    "flash": "slash",
    "logs slash": "log slash",
    "logs lashed": "log slash",
    "at sea": "etc",
    "his log": "syslog",
    "sis log": "syslog",
    "past wd": "passwd",
    "pass wd": "passwd",
    "fu": "foo",
}

# Words that Vosk splits that belong together with a hyphen, as in systemctl subcommands.
COMPOUNDS = [
    "daemon reload", "daemon reexec", "list units", "list unit files", "is active",
    "is enabled", "is failed", "reset failed",
]

# "slash" or "dot" that starts an argument is a leading one: "ls slash etc" -> "ls /etc",
# "ls dot git" -> "ls .git". That is the case right after a command, after a flag
# ("tail minus 100 slash var") or at the start of a phrase. Anywhere else it joins a path
# together: "etc slash hosts" -> "etc/hosts", "file dot txt" -> "file.txt".
# These words are followed by another command, so what comes next starts a command:
SEPARATORS = {"pipe", "ampersand", "semicolon", "sudo", "time", "xargs", "watch", "nohup", "env"}


_class_cache = {"at": 0.0, "value": "", "warned": False}


def active_class():
    # Called for every recognition update, so don't start xdotool each time
    now = time.monotonic()
    if now - _class_cache["at"] < 0.5:
        return _class_cache["value"]
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowclassname"],
            capture_output=True, text=True, timeout=1,
        )
        value = out.stdout.strip().lower()
        if not value and not _class_cache["warned"]:
            _class_cache["warned"] = True
            log("could not read the focused window (xdotool said: %s). Terminals cannot be "
                "recognised until this works. Say \"command mode\" to switch by hand."
                % ((out.stderr or "nothing").strip().splitlines() or ["nothing"])[0])
    except Exception as e:
        value = ""
        if not _class_cache["warned"]:
            _class_cache["warned"] = True
            log("could not run xdotool to read the focused window: %s" % e)
    _class_cache.update(at=now, value=value)
    return value


_forced = None            # set by "command mode" / "text mode"; None follows the window
_logged = {"key": None}


def _state_file():
    import os
    folder = os.environ.get("NN_STATE_DIR") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "nerd-natter")
    return os.path.join(folder, "mode")


def _save_forced():
    # lets the panel icon show a mode you chose by voice; failing to write is harmless
    try:
        with open(_state_file(), "w") as f:
            f.write(_forced or "")
    except OSError:
        pass


def _read_forced():
    try:
        with open(_state_file()) as f:
            return f.read().strip() or None
    except OSError:
        return None


def mode_for(window_class, forced):
    """("command" or "text", "voice" or "auto") for a window class."""
    if forced:
        return ("command" if forced == "shell" else "text"), "voice"
    return ("command" if (window_class or "").lower() in TERMINALS else "text"), "auto"


def log(message):
    # nerd-dictation's messages end up in the service log
    sys.stderr.write("[nn-config] %s\n" % message)
    sys.stderr.flush()


def current_mode():
    """"shell" (command typing) or "prose" (normal text)."""
    cls = active_class()
    mode = _forced or ("shell" if cls in TERMINALS else "prose")
    key = (cls, mode, _forced)
    if key != _logged["key"]:
        _logged["key"] = key
        why = ("chosen by voice" if _forced else "terminal window" if mode == "shell"
               else "not a known terminal" if cls else "focused window unknown")
        log('window class "%s": %s mode (%s)' % (cls, "command" if mode == "shell" else "text", why))
    return mode


def protect(text, phrases):
    """"literal <phrase>" becomes a letterless token so no rule can touch it."""
    for i, words in enumerate(phrases):
        text = text.replace("literal " + words, "\0%d\0" % i)
    return text


def restore(text, phrases):
    for i, words in enumerate(phrases):
        text = text.replace("\0%d\0" % i, words)
    return text


def prose(text):
    phrases = list(PROSE)
    text = protect(text, phrases)
    for words, symbol in PROSE.items():
        text = re.sub(r"\s*\b" + words + r"\b", lambda m, s=symbol: s, text)
    return restore(text, phrases)


def join_letters(text):
    """Spoken letters become a word: "e t c" -> "etc". A single letter is left alone."""
    out, run = [], []

    def flush():
        if len(run) > 1:
            out.append("".join(run))
        else:
            out.extend(run)
        run.clear()

    for tok in text.split():
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
        else:
            flush()
            out.append(tok)
    flush()
    return " ".join(out)


def mark_leading(text):
    """"slash" / "dot" that start an argument become "leading slash" / "leading dot"."""
    out = []
    at_cmd = True      # the next word is a command
    prev_cmd = False   # the previous word was a command
    for tok in text.split():
        if tok in ("slash", "dot") and (not out or at_cmd or prev_cmd or out[-1].startswith("-")):
            tok = "leading " + tok
        out.append(tok)
        if tok in SEPARATORS:
            at_cmd, prev_cmd = True, False
        else:
            at_cmd, prev_cmd = False, at_cmd
    return " ".join(out)


def pair_quotes(text):
    for words, symbol in QUOTES:
        count = [0]

        def sub(m, symbol=symbol, count=count):
            count[0] += 1
            return m.group(1) + symbol if count[0] % 2 else symbol + m.group(2)

        text = re.sub(r"(\s*)\b%s\b(\s*)" % words, sub, text)
    return text


def shell(text):
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    text = text.strip()
    phrases = [w for w, _, _ in SHELL] + [w for w, _ in QUOTES]
    text = protect(text, phrases)
    text = join_letters(text)
    for words, fixed in FIXES.items():
        text = re.sub(r"\b" + words + r"\b", fixed, text)
    for words in COMPOUNDS:
        text = re.sub(r"\b" + words + r"\b", words.replace(" ", "-"), text)
    for words, symbol in DASHES:
        text = re.sub(r"\b" + words + r"\b\s*", lambda m, s=symbol: s, text)
    text = pair_quotes(text)
    text = mark_leading(text)
    for words, symbol, join in SHELL:
        pattern = {"both": r"\s*\b%s\b\s*", "next": r"\b%s\b\s*", "prev": r"\s*\b%s\b",
                   "none": r"\b%s\b"}[join] % words
        text = re.sub(pattern, lambda m, s=symbol: s, text)
    return lead + restore(text, phrases) + trail


# Vosk sometimes hears a click, pop or beep as a one word sentence. If the whole
# phrase is just one of these words it is thrown away. Empty the set to switch off.
DROP_ALONE = {"the"}


def nerd_dictation_process(text):
    global _forced
    phrase = text.strip().lower()
    if phrase in MODE_PHRASES:
        _forced = MODE_PHRASES[phrase]
        _save_forced()
        log("switched by voice: %s" % ("follows the window" if _forced is None else
                                        "command mode" if _forced == "shell" else "text mode"))
        return ""
    if phrase in DROP_ALONE:
        return ""
    return shell(text) if current_mode() == "shell" else prose(text)


def load_user_rules():
    """Run your own rules file, if you have one, so your changes survive updates.

    Default place: ~/.config/nerd-natter/user-rules.py (create it with: nn-ctl rules).
    It runs inside this file, so it can change the lists above directly, for example
        FIXES["pseudo"] = "sudo"          TERMINALS.add("my-terminal")
        SHELL.append(("hash tag", "#", "next"))
    Changes take effect when the service restarts.
    """
    import os
    path = os.environ.get("NN_USER_RULES") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "nerd-natter", "user-rules.py")
    if not os.path.isfile(path):
        return
    try:
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), globals())
        log("loaded your rules from %s" % path)
    except Exception as e:      # a typo in your file must never stop dictation
        log("could not use %s: %s (the built-in rules are still working)" % (path, e))


load_user_rules()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--mode":
        # Used by the panel icon: which mode would a window of this class be typed in?
        print(*mode_for(_sys.argv[2] if len(_sys.argv) > 2 else "", _read_forced()))
        raise SystemExit(0)
    # Try the rules without speaking:  python3 nn-config.py "ls slash e t c"
    for line in _sys.argv[1:]:
        print("command mode:", repr(shell(line)))
        print("text mode:   ", repr(prose(line)))
