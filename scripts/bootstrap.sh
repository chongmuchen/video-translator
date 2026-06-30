#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

pick_python() {
  if [[ -n "${PYTHON:-}" ]] && "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    printf '%s\n' "$PYTHON"
    return
  fi

  for candidate in \
    python3.13 python3.12 python3.11 python3.10 python3 \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12; do
    if command -v "$candidate" >/dev/null 2>&1; then
      resolved="$(command -v "$candidate")"
    elif [[ -x "$candidate" ]]; then
      resolved="$candidate"
    else
      continue
    fi
    if "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
      printf '%s\n' "$resolved"
      return
    fi
  done

  return 1
}

if [[ -x .venv/bin/python ]] &&
   .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Using existing virtual environment: $PWD/.venv"
else
  PYTHON_BIN="$(pick_python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3.10+ is required."
    echo "Install Python 3.12, or run: PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh"
    exit 1
  fi
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  .venv/bin/python -m pip install -e ".[all,books,dev,mac]"
else
  .venv/bin/python -m pip install -e ".[all,books,dev]"
fi

# Fetch a project-local ffmpeg/ffprobe only when the machine has neither.
.venv/bin/python -m video_translator.bootstrap

echo
echo "Bootstrap complete."
echo "1. cp .env.example .env"
echo "2. Configure the translation endpoint in .env"
echo "3. .venv/bin/video-translator doctor"
echo "4. .venv/bin/video-translator serve"
