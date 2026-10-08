#!/usr/bin/env bash
# Lumivara Sentinel - Linux / macOS / Termux one-line installer
#
#   curl -fsSL https://raw.githubusercontent.com/rygroup-dev/lumivara-sentinel/main/install.sh | bash
#
# Installs into ~/lumivara-sentinel (override with LUMIVARA_DIR=...), creates a
# virtualenv, installs dependencies and asks for your Telegram bot + game login.
# On systemd hosts it can register a user service so the bot survives reboots.
# Re-running it updates the code and keeps your .env / data.
set -euo pipefail

REPO="rygroup-dev/lumivara-sentinel"
DIR="${LUMIVARA_DIR:-$HOME/lumivara-sentinel}"
say()  { printf '\033[36m[lumivara]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[lumivara]\033[0m %s\n' "$*"; }

# ---- prerequisites -------------------------------------------------------
need_pkgs=()
command -v git >/dev/null 2>&1 || need_pkgs+=(git)
PY=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PY="$c"; break
    fi
done
[ -n "$PY" ] || need_pkgs+=(python3)

if [ ${#need_pkgs[@]} -gt 0 ] || { [ -n "$PY" ] && ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; }; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
    if [ -n "${PREFIX:-}" ] && command -v pkg >/dev/null 2>&1; then          # Termux
        pkg install -y python git
    elif command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -y && $SUDO apt-get install -y python3 python3-venv python3-pip git
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y python3 python3-pip git
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -Sy --noconfirm python python-pip git
    elif command -v brew >/dev/null 2>&1; then
        brew install python git
    else
        warn "Please install Python 3.11+ and git, then run this again."; exit 1
    fi
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            PY="$c"; break
        fi
    done
    [ -n "$PY" ] || { warn "Python 3.11+ is required (found: $(python3 --version 2>&1))."; exit 1; }
fi
say "Python: $($PY --version)"

# ---- code ------------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
    say "Updating code in $DIR"
    git -C "$DIR" pull --ff-only
else
    say "Downloading code to $DIR"
    git clone --depth 1 "https://github.com/$REPO.git" "$DIR"
fi
cd "$DIR"

# ---- virtualenv + deps -------------------------------------------------------
[ -x .venv/bin/python ] || "$PY" -m venv .venv
say "Installing dependencies..."
.venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
chmod +x run.sh

# ---- setup wizard (read answers from the terminal even when piped) ------------
if .venv/bin/python -m lumivara.setup_wizard --check; then
    say "Existing .env found - keeping your settings (refresh cookie: .venv/bin/python -m lumivara.setup_wizard --cookie)"
else
    .venv/bin/python -m lumivara.setup_wizard < /dev/tty
fi

# ---- optional: systemd user service -------------------------------------------
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    printf '[lumivara] Run the bot as a background service (auto-start on boot)? [Y/n] '
    read -r ans < /dev/tty || ans=""
    if [[ ! "$ans" =~ ^[nN] ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/lumivara-sentinel.service" <<EOF
[Unit]
Description=Lumivara Sentinel
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/main.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now lumivara-sentinel
        command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$USER" 2>/dev/null || true
        say "Service started. Logs: journalctl --user -u lumivara-sentinel -f   (or $DIR/logs/bot.log)"
        say "In Telegram, send /menu to your bot."
        exit 0
    fi
fi

say "Done. Start the bot with:  cd $DIR && ./run.sh"
say "Then send /menu to your bot in Telegram."
