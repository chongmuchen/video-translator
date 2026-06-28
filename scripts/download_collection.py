#!/usr/bin/env python3
"""Thin wrapper for ``main.py download-collection``."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import main  # noqa: E402


if __name__ == "__main__":
    sys.argv.insert(1, "download-collection")
    main()

