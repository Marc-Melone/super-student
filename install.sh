#!/usr/bin/env bash
# Super Student installer (macOS / Linux).
# Creates a private Python environment in ~/.superstudent, installs everything, then runs setup.
#   bash install.sh             install or update, then run setup
#   bash install.sh --no-setup  install or update only
#   SUPERSTUDENT_MLX=1 bash install.sh   also install the faster Apple Silicon transcriber
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${SUPERSTUDENT_HOME:-$HOME/.superstudent}"
RUN_SETUP=1
[ "${1:-}" = "--no-setup" ] && RUN_SETUP=0

say() { printf '%s\n' "$*"; }

find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    say "Super Student needs Python 3.10 or newer. Installing Python 3.12 with Homebrew…"
    brew install python@3.12
    PY="$(find_python)"
  else
    say "Super Student needs Python 3.10 or newer."
    say "Install it from https://www.python.org/downloads/ (or: brew install python), then run this again."
    exit 1
  fi
fi
say "Using $("$PY" --version) ($PY)"

mkdir -p "$APP"
if [ ! -x "$APP/venv/bin/python" ]; then
  "$PY" -m venv "$APP/venv"
fi
VPY="$APP/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip

EXTRAS="media"
[ "$(uname -s)" = "Darwin" ] && EXTRAS="media,gui"
say "Installing Super Student and lecture transcription (a few minutes the first time)…"
# --prefer-binary: take a ready-made download over a newer version that would have to be compiled here.
if ! "$VPY" -m pip install --quiet --upgrade --prefer-binary "${SRC}[$EXTRAS]"; then
  say "Lecture transcription didn't install; installing everything else (videos with captions still work)."
  "$VPY" -m pip install --quiet --upgrade --prefer-binary "$SRC"
fi
if [ "${SUPERSTUDENT_MLX:-0}" = "1" ] && [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  say "Installing the Apple Silicon transcriber (mlx-whisper)…"
  "$VPY" -m pip install --quiet --upgrade --prefer-binary "${SRC}[mlx]" || say "mlx-whisper didn't install; faster-whisper will be used."
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$APP/venv/bin/superstudent" "$HOME/.local/bin/superstudent"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    RC="$HOME/.zshrc"
    [ "${SHELL##*/}" = "bash" ] && RC="$HOME/.bash_profile"
    if ! grep -qs 'HOME/.local/bin' "$RC"; then
      printf '\n# Super Student\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
      say "Added ~/.local/bin to your PATH in $RC (open a new Terminal window to use the 'superstudent' command)."
    fi
    ;;
esac

say "Installed: $("$APP/venv/bin/superstudent" --version)"
say "Prefer a window to Terminal? Run: superstudent gui"
if [ "$RUN_SETUP" = "1" ]; then
  say ""
  exec "$APP/venv/bin/superstudent" setup
fi
