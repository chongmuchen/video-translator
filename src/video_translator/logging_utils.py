"""Per-job logging."""

from __future__ import annotations

import logging
from pathlib import Path


def create_job_logger(job_id: str, job_dir: Path) -> logging.Logger:
    logger = logging.getLogger(f"video_translator.job.{job_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(
        job_dir / "pipeline.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger

