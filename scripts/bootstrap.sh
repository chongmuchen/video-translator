#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

supports_kokoro() {
  "$1" -c 'import sys; raise SystemExit(not (3, 10) <= sys.version_info[:2] <= (3, 12))'
}

pick_python() {
  local candidate resolved
  if [[ -n "${PYTHON:-}" ]] && supports_kokoro "$PYTHON"; then
    printf '%s\n' "$PYTHON"
    return
  fi

  for candidate in \
    python3.12 python3.11 python3.10 python3 \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
      resolved="$(command -v "$candidate")"
    elif [[ -x "$candidate" ]]; then
      resolved="$candidate"
    else
      continue
    fi
    if supports_kokoro "$resolved"; then
      printf '%s\n' "$resolved"
      return
    fi
  done

  return 1
}

if [[ -x .venv/bin/python ]]; then
  if ! supports_kokoro .venv/bin/python; then
    echo "Kokoro requires Python 3.10–3.12. Move the existing .venv aside and rerun bootstrap." >&2
    exit 1
  fi
  echo "Using existing virtual environment: $PWD/.venv"
else
  PYTHON_BIN="$(pick_python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "Kokoro requires Python 3.10–3.12."
    echo "Install Python 3.12, or run: PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh"
    exit 1
  fi
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[all,dev]"

# Provision the project-local Kokoro runtime and missing media executables.
.venv/bin/python -m video_translator.bootstrap

echo
echo "Bootstrap complete."
echo "1. .venv/bin/video-translator doctor"
echo "2. .venv/bin/video-translator prepare --help"
echo "3. .venv/bin/video-translator render --help"
