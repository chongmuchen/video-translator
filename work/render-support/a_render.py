"""Shared draft and sentence synthesis helpers for project renderers."""

from __future__ import annotations

import json
import math
from pathlib import Path

from video_translator.models import Segment
from video_translator.pipeline.synthesizer import SpeechSynthesizer


class BatchSynthesizer(SpeechSynthesizer):
    """Compatibility entry point for renderers using the shared Kokoro backend."""


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def filenames(folder: Path, title: str) -> tuple[Path, Path, Path]:
    return (
        folder / f"{title}.mp4",
        folder / f"{title}.zh-CN.srt",
        folder / f"{title}.en.srt",
    )


def streams_of(probe: dict, kind: str) -> list[dict]:
    return [stream for stream in probe.get("streams", []) if stream["codec_type"] == kind]


def parse_draft(path: Path, duration: float) -> tuple[list[Segment], list[dict]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["segments"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("译稿必须有 segments 列表")
    original = []
    translations = []
    prior_end = 0.0
    ids = set()
    for row in rows:
        index = row["id"]
        if type(index) is not int or index < 0 or index in ids:
            raise ValueError(f"无效或重复片段编号 {index}")
        start = round(float(row["start"]), 3)
        end = round(min(float(row["end"]), duration), 3)
        if not math.isfinite(start) or not math.isfinite(end) or start < prior_end or end <= start:
            raise ValueError(f"片段 {index} 时间无效或重叠：{start}–{end}")
        for field in ("ja", "zh", "en"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"片段 {index} 缺少 {field}")
        original.append(Segment(index=index, start=start, end=end, source_text=row["ja"].strip()))
        translations.append({"id": index, "text": row["zh"].strip(), "en": row["en"].strip()})
        ids.add(index)
        prior_end = end
    return original, translations
