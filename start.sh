#!/bin/sh
# BurstBuster launcher for macOS and Linux: first run sets up Python packages, then opens the app.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up. This takes a few minutes and needs internet once."
  PY=""
  for p in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys; sys.exit(sys.version_info < (3, 11))"; then
      PY="$p"
      break
    fi
  done
  if [ -z "$PY" ]; then
    echo "BurstBuster needs Python 3.11 or newer. Install it (python.org, Homebrew or your package manager) and try again."
    exit 1
  fi
  if ! "$PY" -m venv .venv; then
    echo "Could not create the Python environment. On Debian/Ubuntu: sudo apt install python3-venv"
    rm -rf .venv  # a half-made one would make the next run skip setup
    exit 1
  fi
  if ! .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
    echo "Installing packages failed. See the messages above."
    rm -rf .venv
    exit 1
  fi
fi

# MediaPipe needs the system OpenGL ES library on Linux; without it every worker dies with no clear message.
if [ "$(uname)" = "Linux" ] && ! .venv/bin/python -c "import ctypes; ctypes.CDLL('libGLESv2.so.2')" 2>/dev/null; then
  echo "Missing a system library. On Debian/Ubuntu: sudo apt install libgles2 libegl1"
  exit 1
fi

# Downloads the AI models once (hash-checked); does nothing if they are already there.
if ! .venv/bin/python -c "from burstbuster.models import ensure_models; ensure_models(print)"; then
  echo "Downloading the models failed. Check your internet connection and run start.sh again."
  exit 1
fi

exec .venv/bin/python -m burstbuster "$@"
