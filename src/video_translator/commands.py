"""Safe subprocess helpers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable

from .errors import PipelineError


def run_command(
    args: Iterable[str | Path],
    *,
    logger: logging.Logger | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without invoking a shell."""

    argv = [str(arg) for arg in args]
    if logger:
        logger.info("运行命令: %s", " ".join(argv))
    result = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if logger and details:
            logger.error(details)
        raise PipelineError(
            f"命令执行失败（退出码 {result.returncode}）: {argv[0]}\n"
            f"{details[-4000:]}"
        )
    return result

