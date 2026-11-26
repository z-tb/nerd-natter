#!/usr/bin/env bash
# Guided setup for Nerd Natter dictation. Safe to run again: it keeps what is already set.
#
# Options (all optional, it asks about anything you leave out):
#   --mode docker|venv        how dictation runs: in Docker, or in a Python virtual environment
#   --nerd-dir FOLDER         nerd-dictation is already downloaded here (it will not be downloaded again)
#   --model-dir FOLDER        a Vosk speech model is already downloaded here (it will not be downloaded again)
#   --download-model small|large|none   what to download when you have no model (default: ask)
#   --model-store FOLDER      where a downloaded model goes (default ~/.local/share/vosk-models)
#   --autostart               start dictation automatically when you log in
#   --no-packages             do not offer to install missing system packages
#   --no-applet               do not install the Cinnamon applet
#   --yes                     do not ask questions, use sensible answers
#   -h, --help                this help

set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CTL="$HERE/nn-ctl"

# The nerd-dictation version the patches were written and tested against.
ND_COMMIT="${ND_COMMIT:-41f372789c640e01bb6650339a78312661530843}"
ND_URL="https://github.com/ideasman42/nerd-dictation/archive/${ND_COMMIT}.tar.gz"
MODEL_URL_SMALL="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_URL_LARGE="https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip"

MODE=""; NERD_DIR=""; MODEL_DIR=""; DOWNLOAD=""; STORE="$HOME/.local/share/vosk-models"
AUTOSTART=""; PACKAGES=1; APPLET=1; YES=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'Note: %s\n' "$*" >&2; }
die()  { printf 'Sorry, I have to stop: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift ;;
        --nerd-dir) NERD_DIR="${2:-}"; shift ;;
        --model-dir) MODEL_DIR="${2:-}"; shift ;;
        --download-model) DOWNLOAD="${2:-}"; shift ;;
        --model-store) STORE="${2:-}"; shift ;;
        --autostart) AUTOSTART=1 ;;
        --no-packages) PACKAGES=0 ;;
        --no-applet) APPLET=0 ;;
        --yes|-y) YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "I do not know the option $1" ;;
    esac
    shift
done
case "$MODE" in ''|docker|venv) ;; *) die "--mode must be docker or venv" ;; esac
case "$DOWNLOAD" in ''|small|large|none) ;; *) die "--download-model must be small, large or none" ;; esac

interactive() { [ "$YES" = 0 ] && [ -t 0 ]; }

# ask_yes "question" default(y/n)
ask_yes() {
    local q="$1" def="${2:-y}" a
    interactive || { [ "$def" = y ]; return; }
    read -r -p "$q [$([ "$def" = y ] && echo Y/n || echo y/N)] " a
    a="${a:-$def}"
    case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ask_text "question" -> prints the answer (empty when not interactive)
ask_text() {
    local a=""
    interactive && read -r -p "$1 " a
    printf '%s' "$a"
}

expand() { local p="$1"; case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#"~/"}" ;; esac; printf '%s' "$p"; }

# ---------------------------------------------------------------- 1. the desktop
step "Checking your desktop"
session="${XDG_SESSION_TYPE:-unknown}"
if [ "$session" = wayland ]; then
    warn "You are on Wayland. Nerd Natter needs X11 because it reads one key and types text"
    warn "through X11. At the login screen, pick the Cinnamon session that is NOT marked Wayland."
    ask_yes "Carry on with the setup anyway?" n || die "log in to a Cinnamon (X11) session and run this again."
elif [ "$session" = x11 ]; then
    say "X11 session: good."
else
    say "Could not tell if this is an X11 session. Nerd Natter only works on X11."
fi
case "${XDG_CURRENT_DESKTOP:-}" in
    *innamon*) say "Cinnamon desktop: good." ;;
    *) warn "This does not look like Cinnamon. The dictation itself works, but the panel applet is for Cinnamon." ;;
esac

# ---------------------------------------------------------------- 2. how it runs
step "How should dictation run?"
docker_ok=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then docker_ok=1; fi
if [ -z "$MODE" ]; then
    saved="$("$CTL" get RUN_MODE 2>/dev/null || true)"
    default=venv; [ "$docker_ok" = 1 ] && default=docker
    if interactive; then
        say "  1) In Docker. Everything stays in a sealed container. Needs Docker installed."
        say "  2) In a Python virtual environment. A private folder, no Docker needed. Lighter."
        [ "$docker_ok" = 1 ] && say "     (Docker is installed and working on this computer.)" || say "     (Docker is not set up here yet, so option 2 is the easy one.)"
        read -r -p "Choose 1 or 2 [$([ "$default" = docker ] && echo 1 || echo 2)]: " a
        case "${a:-}" in 1) MODE=docker ;; 2) MODE=venv ;; *) MODE="$default" ;; esac
    else
        MODE="$default"
        [ -n "$saved" ] && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/nerd-natter/config.env" ] && grep -q '^RUN_MODE=' "${XDG_CONFIG_HOME:-$HOME/.config}/nerd-natter/config.env" && MODE="$saved"
    fi
fi
say "Dictation will run in: $([ "$MODE" = docker ] && echo Docker || echo "a Python virtual environment")"

# ---------------------------------------------------------------- 3. system packages
step "Checking for programs this needs"
missing=()
have() { command -v "$1" >/dev/null 2>&1; }
python3 -c 'import Xlib' 2>/dev/null || missing+=(python3-xlib)
have pactl && have paplay && have pacat || missing+=(pulseaudio-utils)
have python3 || missing+=(python3)
if [ "$MODE" = venv ]; then
    have xdotool || missing+=(xdotool)
    python3 -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3-venv)
else
    have docker || missing+=(docker.io)
fi
have curl || have wget || missing+=(curl)
if [ ${#missing[@]} -eq 0 ]; then
    say "Everything needed is already installed."
else
    say "Missing: ${missing[*]}"
    if [ "$PACKAGES" = 1 ] && have apt-get; then
        if ask_yes "Install them now? (asks for your password)" y; then
            sudo apt-get install -y "${missing[@]}" || warn "the install did not finish, install them yourself: sudo apt install ${missing[*]}"
        else
            say "Install them later with: sudo apt install ${missing[*]}"
        fi
    else
        say "Please install these with your package manager: ${missing[*]}"
    fi
fi
if [ "$MODE" = docker ] && have docker && ! docker info >/dev/null 2>&1; then
    warn "Docker is installed but your user cannot use it yet."
    if ask_yes "Add ${USER:-$(id -un)} to the docker group? (asks for your password; you must log out and in afterwards)" y; then
        sudo usermod -aG docker "${USER:-$(id -un)}" && say "Done. Log out and back in, then run this setup again."
    else
        say "You can do it later with: sudo usermod -aG docker ${USER:-$(id -un)}"
    fi
fi

# ---------------------------------------------------------------- helpers: folders
# A model folder has a folder called conf inside. Accept the folder that holds one, too.
find_model() {
    local p; p="$(expand "$1")"
    [ -d "$p/conf" ] && { printf '%s' "$p"; return 0; }
    local sub subs=()
    for sub in "$p"/*/; do [ -d "${sub}conf" ] && subs+=("${sub%/}"); done
    [ ${#subs[@]} -eq 1 ] && { printf '%s' "${subs[0]}"; return 0; }
    return 1
}

# nerd-dictation is a folder with a file called nerd-dictation inside. Accept the file itself, too.
find_nerd() {
    local p; p="$(expand "$1")"
    [ -f "$p/nerd-dictation" ] && { printf '%s' "$p"; return 0; }
    [ -f "$p" ] && [ "$(basename "$p")" = nerd-dictation ] && { printf '%s' "$(dirname "$p")"; return 0; }
    return 1
}

fetch() {   # fetch URL FILE
    if have curl; then curl -fL --progress-bar -C - -o "$2" "$1"
    elif have wget; then wget -c -O "$2" "$1"
    else return 1; fi
}

# ---------------------------------------------------------------- 4. nerd-dictation
step "nerd-dictation (the program that does the dictating)"
found=""
if [ -n "$NERD_DIR" ]; then
    found="$(find_nerd "$NERD_DIR")" || die "no file called nerd-dictation in $NERD_DIR"
else
    cur="$("$CTL" get NERD_DIR 2>/dev/null || true)"
    [ -n "$cur" ] && found="$(find_nerd "$cur")"
    if [ -n "$found" ]; then
        say "Already set up: $found"
    elif interactive; then
        say "If you already downloaded nerd-dictation, type its folder so it is not downloaded again."
        a="$(ask_text "Folder (or just press Enter to download it):")"
        if [ -n "$a" ]; then
            found="$(find_nerd "$a")" || die "I could not find a file called nerd-dictation in $a"
        fi
    fi
fi
if [ -z "$found" ]; then
    dest="$HERE/nerd-dictation"
    say "Downloading nerd-dictation (about 1 MB) to $dest"
    tmp="$(mktemp -d)"
    if fetch "$ND_URL" "$tmp/nd.tar.gz" && mkdir -p "$dest" && tar xzf "$tmp/nd.tar.gz" -C "$dest" --strip-components=1; then
        found="$dest"
    else
        rm -rf "$tmp"
        die "the download failed. Download nerd-dictation yourself (github.com/ideasman42/nerd-dictation) and run: ./install.sh --nerd-dir FOLDER"
    fi
    rm -rf "$tmp"
fi
NERD_DIR="$found"
say "nerd-dictation folder: $NERD_DIR"

# ---------------------------------------------------------------- 5. the speech model
step "Speech model (the files that let the computer understand your voice)"
found=""
if [ -n "$MODEL_DIR" ]; then
    found="$(find_model "$MODEL_DIR")" || die "$MODEL_DIR does not look like a Vosk model (it should contain a folder called conf)"
else
    cur="$("$CTL" get MODEL_DIR 2>/dev/null || true)"
    [ -n "$cur" ] && found="$(find_model "$cur")"
    if [ -n "$found" ]; then
        say "Already set up: $found"
    else
        # look in the usual places
        cands=()
        for d in "$HOME"/vosk* "$HOME"/Downloads/vosk-model* "$HOME"/.local/share/vosk* "$HOME"/.config/nerd-dictation/model /mnt/*/vosk* /opt/vosk*; do
            m="$(find_model "$d" 2>/dev/null)" && cands+=("$m")
        done
        if [ ${#cands[@]} -gt 0 ]; then
            say "Found a model already on this computer: ${cands[0]}"
            ask_yes "Use it?" y && found="${cands[0]}"
        fi
        if [ -z "$found" ] && interactive; then
            say "If you already downloaded a model, type its folder so it is not downloaded again."
            a="$(ask_text "Folder (or just press Enter to download one):")"
            if [ -n "$a" ]; then found="$(find_model "$a")" || die "$a does not look like a Vosk model (it should contain a folder called conf)"; fi
        fi
    fi
fi
if [ -z "$found" ]; then
    choice="$DOWNLOAD"
    if [ -z "$choice" ]; then
        if interactive; then
            say "  1) Small English model: about 40 MB, quick, fine for trying it out."
            say "  2) Large English model: about 1.8 GB, much more accurate, but slower to finish after you let go of the key."
            say "  3) Do not download now, I will add one later."
            read -r -p "Choose 1, 2 or 3 [1]: " a
            case "${a:-1}" in 2) choice=large ;; 3) choice=none ;; *) choice=small ;; esac
        else
            choice=none
        fi
    fi
    if [ "$choice" = none ]; then
        warn "No model yet. Download one from alphacephei.com/vosk/models and choose its folder in the applet settings (Configure, Setup, Speech model folder)."
    else
        url="$MODEL_URL_SMALL"; [ "$choice" = large ] && url="$MODEL_URL_LARGE"
        zip="$STORE/$(basename "$url")"
        mkdir -p "$STORE"
        say "Downloading $(basename "$url") to $STORE (this can take a while, and it can be resumed if it stops)"
        fetch "$url" "$zip" || die "the model download failed. Run this again, or download a model yourself and use --model-dir FOLDER"
        say "Unpacking..."
        python3 -m zipfile -e "$zip" "$STORE" || die "could not unpack $zip"
        rm -f "$zip"
        found="$(find_model "$STORE/$(basename "$url" .zip)")" || die "the unpacked model was not where I expected, look in $STORE"
    fi
fi
MODEL_DIR="$found"
[ -n "$MODEL_DIR" ] && say "Speech model folder: $MODEL_DIR"

# ---------------------------------------------------------------- 6. save the settings
step "Saving your settings"
"$CTL" set RUN_MODE "$MODE"
"$CTL" set PROJECT_DIR "$HERE"
"$CTL" set NERD_DIR "$NERD_DIR"
[ -n "$MODEL_DIR" ] && "$CTL" set MODEL_DIR "$MODEL_DIR"
chmod +x "$CTL" "$HERE/nn_watch.py" 2>/dev/null
say "Saved to ${XDG_CONFIG_HOME:-$HOME/.config}/nerd-natter/config.env"

# ---------------------------------------------------------------- 7. the environment
if [ "$MODE" = venv ]; then
    step "Creating the Python environment (a private folder, about 100 MB)"
    "$CTL" setup-venv && say "Ready: $("$CTL" get VENV_DIR)" || warn "could not create it. Fix the problem above and run: ./nn-ctl setup-venv"
elif have docker && docker info >/dev/null 2>&1; then
    step "Docker image"
    if ask_yes "Build the Docker image now? (a minute or two the first time)" y; then
        "$CTL" build || warn "the build failed. You can try again later with: ./nn-ctl build"
    else
        say "It will be built the first time you start the service."
    fi
fi

# ---------------------------------------------------------------- 8. the applet
chmod 700 "$HERE/nn-ctl" "$HERE/install.sh" "$HERE/nn_watch.py"   # only you can run the scripts
"$HERE/nn-ctl" sync-version >/dev/null 2>&1   # About dialog shows this version

if [ "$APPLET" = 1 ]; then
    step "Panel applet and icon"
    mkdir -p "$HOME/.local/share/icons/hicolor" "$HOME/.local/share/cinnamon/applets" "$HOME/.config/nerd-natter"
    cp -r "$HERE/icons/hicolor/." "$HOME/.local/share/icons/hicolor/"
    have gtk-update-icon-cache && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1
    rm -f "$HOME/.local/share/cinnamon/applets/ptt@nerd-dictation" "$HOME/.local/share/applications/nerd-dictation-ptt.desktop" \
        "$HOME/.config/autostart/nerd-dictation-ptt.desktop"   # names from before the rename
    find "$HOME/.local/share/icons/hicolor" -name 'nerd-dictation-ptt.*' -delete 2>/dev/null
    ln -sfn "$HERE/applet/nn@nerd-natter" "$HOME/.local/share/cinnamon/applets/nn@nerd-natter"
    printf '%s\n' "$HERE" > "$HOME/.config/nerd-natter/project-dir"
    mkdir -p "$HOME/.local/share/applications"
    printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Nerd Natter' \
        'Comment=Nerd Natter speech typing' "Exec=bash '$CTL' start" 'Icon=nerd-natter' \
        'Terminal=false' 'Categories=Utility;Accessibility;' 'Actions=Stop;' '' '[Desktop Action Stop]' \
        'Name=Stop Nerd Natter' "Exec=bash '$CTL' stop" > "$HOME/.local/share/applications/nerd-natter.desktop"
    say "Applet installed. It is not added to your panel yet, you do that yourself (see below)."
fi

if [ -z "$AUTOSTART" ] && ask_yes "Start dictation automatically when you log in?" n; then AUTOSTART=1; fi
if [ "$AUTOSTART" = 1 ]; then
    mkdir -p "$HOME/.config/autostart"
    printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Nerd Natter' 'Icon=nerd-natter' \
        "Exec=bash '$CTL' start" 'X-GNOME-Autostart-enabled=true' > "$HOME/.config/autostart/nerd-natter.desktop"
    say "It will start by itself at login."
fi

# ---------------------------------------------------------------- 9. the check
step "Final check"
if "$CTL" check; then
    ok=1
else
    ok=0
    warn "Something is still missing, see the message above."
fi

cat <<EOF

All done. Next steps:
  1. Right-click your panel, choose Applets, find "Nerd Natter" and add it.
     (Not listed? Press Alt+F2, type r, press Enter, and look again.)
  2. Right-click the applet and choose Start service.
  3. Hold the Menu key and talk. Open Configure on the applet to change the key,
     the folders, the sounds and more.
  Or without the applet:  $CTL start   and   $CTL stop

EOF
[ "$ok" = 1 ]
