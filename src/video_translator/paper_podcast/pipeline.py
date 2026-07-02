"""Generate Chinese explainer podcasts from scientific PDFs."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..commands import run_command
from ..errors import PipelineError
from ..pipeline.synthesizer import SpeechSynthesizer
from ..pipeline.translator import SegmentTranslator
from ..runtime import resolve_media_binaries
from ..settings import Settings
from ..books.pdf import extract_pdf
from .models import (
    PaperPodcastManifest,
    PaperPodcastStatus,
    PodcastScriptLine,
    PodcastStyle,
)
from .store import PaperPodcastStore


PAPER_PODCAST_PROMPT_VERSION = "paper-podcast-v1"


@dataclass(frozen=True)
class PaperText:
    markdown: str
    metadata: dict[str, Any]
    text_hash: str


class PaperPodcastPipeline:
    def __init__(self, settings: Settings, store: PaperPodcastStore):
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger("paper-podcast")

    def import_paper(
        self,
        source: Path,
        *,
        title: str | None = None,
        style: PodcastStyle = "deep_dive",
        duration_minutes: int = 8,
    ) -> PaperPodcastManifest:
        path = source.expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise PipelineError("论文播客第一版只支持存在的 PDF 文件。")
        return self.store.create(
            path,
            title=title,
            style=style,
            duration_minutes=duration_minutes,
        )

    def extract(self, manifest: PaperPodcastManifest) -> PaperPodcastManifest:
        manifest.status = PaperPodcastStatus.extracting
        self.store.save(manifest)
        try:
            blocks, pdf_metadata = extract_pdf(Path(manifest.source_path))
            lines = [
                f"# {manifest.title}",
                "",
                f"- 来源文件：{Path(manifest.source_path).name}",
                f"- 页数：{pdf_metadata.get('page_count', '未知')}",
                "",
            ]
            compact_blocks = []
            for block in blocks:
                text = _clean_text(block.source_text)
                if not text:
                    continue
                compact_blocks.append(
                    {
                        "id": block.id,
                        "page": block.page,
                        "kind": block.kind,
                        "text": text,
                    }
                )
                page = f"p.{block.page}" if block.page else "p.?"
                lines.append(f"## {page} · {block.kind}")
                lines.append(text)
                lines.append("")
            markdown = "\n".join(lines).strip() + "\n"
            text_path = self.store.job_dir(manifest.id) / "paper-text.md"
            text_path.write_text(markdown, encoding="utf-8")
            blocks_path = self.store.write_json(
                manifest,
                "paper-blocks.json",
                compact_blocks,
            )
            manifest.text_path = str(text_path)
            manifest.metadata.update(
                {
                    "page_count": pdf_metadata.get("page_count"),
                    "block_count": len(compact_blocks),
                    "paper_blocks_path": str(blocks_path),
                    "text_hash": _sha256_text(markdown),
                }
            )
            manifest.status = PaperPodcastStatus.extracted
            _mark_done(manifest, "extract")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def _read_paper_text(self, manifest: PaperPodcastManifest) -> PaperText:
        if not manifest.text_path:
            raise PipelineError("论文尚未抽取文本。")
        path = Path(manifest.text_path)
        if not path.is_file():
            raise PipelineError("论文抽取文本不存在。")
        markdown = path.read_text(encoding="utf-8")
        text_hash = _sha256_text(markdown)
        return PaperText(
            markdown=markdown,
            metadata=manifest.metadata,
            text_hash=text_hash,
        )

    def _script_key(
        self,
        manifest: PaperPodcastManifest,
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        glossary: dict[str, str],
        text_hash: str,
    ) -> str:
        payload = json.dumps(
            {
                "version": PAPER_PODCAST_PROMPT_VERSION,
                "provider": self.settings.translator_provider,
                "base_url": self.settings.translator_base_url,
                "model": self.settings.translator_model,
                "codex_model": self.settings.translator_codex_model,
                "codex_strategy": (
                    self.settings.translator_codex_strategy
                ),
                "target": target_language,
                "style": style,
                "duration": duration_minutes,
                "glossary": glossary,
                "text_hash": text_hash,
                "title": manifest.title,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return _sha256_text(payload)

    def script(
        self,
        manifest: PaperPodcastManifest,
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        glossary: dict[str, str],
    ) -> PaperPodcastManifest:
        paper = self._read_paper_text(manifest)
        key = self._script_key(
            manifest,
            target_language=target_language,
            style=style,
            duration_minutes=duration_minutes,
            glossary=glossary,
            text_hash=paper.text_hash,
        )
        if (
            manifest.script_json_path
            and Path(manifest.script_json_path).is_file()
            and manifest.metadata.get("script_key") == key
        ):
            manifest.status = PaperPodcastStatus.scripted
            _mark_done(manifest, "script")
            return self.store.save(manifest)

        manifest.status = PaperPodcastStatus.scripting
        manifest.target_language = target_language
        manifest.style = style
        manifest.duration_minutes = duration_minutes
        self.store.save(manifest)
        try:
            if self.settings.translator_provider == "passthrough":
                script = _fallback_script(
                    manifest.title,
                    paper.markdown,
                    target_language=target_language,
                    style=style,
                )
                notes: list[dict[str, Any]] = []
            else:
                notes = self._build_notes(
                    paper.markdown,
                    target_language=target_language,
                    glossary=glossary,
                )
                script = self._build_script(
                    manifest.title,
                    paper.markdown,
                    notes,
                    target_language=target_language,
                    style=style,
                    duration_minutes=duration_minutes,
                    glossary=glossary,
                )
            validated = _validate_script(script)
            notes_path = self.store.write_json(
                manifest,
                "paper-notes.json",
                notes,
            )
            script_path = self.store.write_json(
                manifest,
                "podcast-script.json",
                validated,
            )
            markdown_path = self.store.job_dir(manifest.id) / "podcast-script.md"
            markdown_path.write_text(
                _script_to_markdown(validated),
                encoding="utf-8",
            )
            manifest.notes_path = str(notes_path)
            manifest.script_json_path = str(script_path)
            manifest.script_markdown_path = str(markdown_path)
            manifest.metadata.update(
                {
                    "script_key": key,
                    "script_line_count": len(validated["lines"]),
                    "note_chunk_count": len(notes),
                    "prompt_version": PAPER_PODCAST_PROMPT_VERSION,
                }
            )
            manifest.status = PaperPodcastStatus.scripted
            _mark_done(manifest, "script")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def _build_notes(
        self,
        markdown: str,
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[dict[str, Any]]:
        chunks = _chunks(markdown, max_chars=14000, max_chunks=8)
        translator = SegmentTranslator(self.settings, self.logger)
        notes: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            self.logger.info("生成论文讲解笔记 %s/%s", index, len(chunks))
            system_prompt = (
                "你是资深论文阅读助手，擅长把论文拆成可讲解的中文要点。"
                "只基于给定原文，不编造；不确定的信息明确写“原文未说明”。"
                "输出 JSON，格式为 "
                '{"notes":["要点"],"terms":[{"term":"术语","meaning":"解释"}],'
                '"questions":["读者可能会问的问题"]}。'
            )
            user_prompt = (
                f"目标语言：{target_language}\n"
                f"术语表：{json.dumps(glossary, ensure_ascii=False)}\n"
                f"论文片段：{index}/{len(chunks)}\n\n{chunk}"
            )
            result = translator.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            notes.append(
                {
                    "chunk": index,
                    "notes": _string_list(result.get("notes"))[:18],
                    "terms": _dict_list(result.get("terms"))[:20],
                    "questions": _string_list(result.get("questions"))[:8],
                }
            )
        return notes

    def _build_script(
        self,
        title: str,
        markdown: str,
        notes: list[dict[str, Any]],
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        glossary: dict[str, str],
    ) -> dict[str, Any]:
        translator = SegmentTranslator(self.settings, self.logger)
        source_brief = markdown[:18000]
        style_instruction = (
            "双人深度讲解播客。主持人A负责搭结构、追问和转场；"
            "嘉宾B负责解释方法、实验、局限和启发。语言自然、有节奏，"
            "但不要闲聊灌水。"
            if style == "deep_dive"
            else "单人中文讲解稿。结构清晰，像一位老师在讲论文，"
            "语言准确、口语化但不油腻。"
        )
        target_lines = max(8, min(90, duration_minutes * 6))
        system_prompt = (
            "你是顶级中文科技播客编导和论文讲解者。"
            "你的任务不是逐字翻译论文，而是生成可直接朗读的中文讲解脚本。"
            "必须忠于论文，不能编造实验结果、结论或作者意图；"
            "公式可以用口语解释，不要朗读复杂 LaTeX；"
            "遇到原文缺失或抽取不清，写“这里原文信息不足”。"
            "每句适合 TTS 朗读，单句尽量不超过 120 个中文字符。"
            "输出严格 JSON，格式为："
            '{"title":"中文标题","summary":"简介",'
            '"chapters":["段落标题"],"takeaways":["收获"],'
            '"lines":[{"speaker":"主持人A","text":"台词"}]}。'
        )
        user_prompt = (
            f"论文标题：{title}\n"
            f"目标语言：{target_language}\n"
            f"风格：{style_instruction}\n"
            f"期望时长：约 {duration_minutes} 分钟\n"
            f"建议台词数量：约 {target_lines} 条\n"
            f"术语表：{json.dumps(glossary, ensure_ascii=False)}\n\n"
            "分块阅读笔记：\n"
            f"{json.dumps(notes, ensure_ascii=False)}\n\n"
            "论文原文节选（用于校验标题、摘要、方法和实验）：\n"
            f"{source_brief}"
        )
        return translator.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def synthesize(
        self,
        manifest: PaperPodcastManifest,
        *,
        voice_a: str,
        voice_b: str,
        silence_ms: int,
    ) -> PaperPodcastManifest:
        if not manifest.script_json_path:
            raise PipelineError("尚未生成论文播客脚本。")
        script_path = Path(manifest.script_json_path)
        if not script_path.is_file():
            raise PipelineError("论文播客脚本不存在。")
        manifest.status = PaperPodcastStatus.synthesizing
        self.store.save(manifest)
        try:
            script = json.loads(script_path.read_text(encoding="utf-8"))
            lines = [
                PodcastScriptLine.model_validate(item)
                for item in script.get("lines", [])
            ]
            if not lines:
                raise PipelineError("论文播客脚本没有可朗读台词。")
            tts_dir = self.store.job_dir(manifest.id) / "tts"
            raw_dir = tts_dir / "raw"
            normalized_dir = tts_dir / "normalized"
            raw_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)
            media = resolve_media_binaries(self.settings, allow_download=True)
            normalized_clips: list[Path] = []
            for index, line in enumerate(_expand_lines(lines), start=1):
                voice = _voice_for(line.speaker, voice_a=voice_a, voice_b=voice_b)
                line_settings = self.settings.model_copy(
                    update={"tts_voice": voice}
                )
                synthesizer = SpeechSynthesizer(
                    line_settings,
                    self.logger,
                )
                raw_extension = (
                    ".wav"
                    if line_settings.tts_provider == "cosyvoice"
                    else ".mp3"
                )
                raw_path = raw_dir / f"{index:04d}{raw_extension}"
                self.logger.info(
                    "生成论文播客配音 %s/%s：%s",
                    index,
                    len(lines),
                    line.text[:60],
                )
                synthesizer.synthesize(line.text, raw_path)
                normalized = normalized_dir / f"{index:04d}.wav"
                _normalize_clip(raw_path, normalized, media.ffmpeg)
                normalized_clips.append(normalized)
            output = self.store.outputs_dir / (
                f"{manifest.title}-paper-podcast-"
                f"{manifest.style}-{manifest.id[:8]}.mp3"
            )
            _concat_wav_to_mp3(
                normalized_clips,
                output,
                ffmpeg=media.ffmpeg,
                silence_ms=silence_ms,
            )
            manifest.audio_path = str(output)
            manifest.metadata.update(
                {
                    "voice_a": voice_a,
                    "voice_b": voice_b,
                    "silence_ms": silence_ms,
                    "audio_clip_count": len(normalized_clips),
                }
            )
            manifest.status = PaperPodcastStatus.completed
            _mark_done(manifest, "synthesize")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def run(
        self,
        source: Path,
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        glossary: dict[str, str],
        voice_a: str,
        voice_b: str,
        silence_ms: int,
    ) -> PaperPodcastManifest:
        manifest = self.import_paper(
            source,
            style=style,
            duration_minutes=duration_minutes,
        )
        self.extract(manifest)
        current = self.store.get(manifest.id)
        self.script(
            current,
            target_language=target_language,
            style=style,
            duration_minutes=duration_minutes,
            glossary=glossary,
        )
        current = self.store.get(manifest.id)
        return self.synthesize(
            current,
            voice_a=voice_a,
            voice_b=voice_b,
            silence_ms=silence_ms,
        )


def _mark_done(manifest: PaperPodcastManifest, step: str) -> None:
    if step not in manifest.completed_steps:
        manifest.completed_steps.append(step)


def _clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(text: str, *, max_chars: int, max_chunks: int) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if current and size + len(paragraph) > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            size = 0
            if len(chunks) >= max_chunks:
                break
        current.append(paragraph)
        size += len(paragraph)
    if current and len(chunks) < max_chunks:
        chunks.append("\n\n".join(current))
    return chunks or [text[:max_chars]]


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_list(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append({str(key): str(val) for key, val in item.items()})
    return result


def _fallback_script(
    title: str,
    markdown: str,
    *,
    target_language: str,
    style: PodcastStyle,
) -> dict[str, Any]:
    sample = " ".join(markdown.split())[:500]
    if style == "deep_dive":
        lines = [
            {
                "speaker": "主持人A",
                "text": f"欢迎来到论文讲解播客。今天我们先快速看这篇论文：{title}。",
            },
            {
                "speaker": "嘉宾B",
                "text": "当前使用的是直通测试模式，所以这里只生成一个结构示例。",
            },
            {
                "speaker": "主持人A",
                "text": f"论文抽取到的开头内容是：{sample or '这里原文信息不足'}。",
            },
        ]
    else:
        lines = [
            {
                "speaker": "旁白",
                "text": f"这是一份关于 {title} 的论文讲解稿示例。",
            },
            {
                "speaker": "旁白",
                "text": f"抽取到的开头内容是：{sample or '这里原文信息不足'}。",
            },
        ]
    return {
        "title": f"{title}｜论文讲解",
        "summary": f"使用 {target_language} 生成的论文播客测试脚本。",
        "chapters": ["论文背景", "核心方法", "启发与局限"],
        "takeaways": ["真实讲解请配置 Ollama、Codex 或其他模型。"],
        "lines": lines,
    }


def _validate_script(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError("论文播客脚本必须是 JSON 对象。")
    raw_lines = value.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise PipelineError("论文播客脚本缺少 lines。")
    lines = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker") or "旁白").strip() or "旁白"
        text = _clean_text(str(item.get("text") or ""))
        if text:
            lines.append({"speaker": speaker, "text": text})
    if not lines:
        raise PipelineError("论文播客脚本没有有效台词。")
    return {
        "title": _clean_text(str(value.get("title") or "论文讲解播客")),
        "summary": _clean_text(str(value.get("summary") or "")),
        "chapters": _string_list(value.get("chapters")),
        "takeaways": _string_list(value.get("takeaways")),
        "lines": lines,
    }


def _script_to_markdown(script: dict[str, Any]) -> str:
    lines = [f"# {script['title']}", ""]
    if script.get("summary"):
        lines.extend(["## 简介", script["summary"], ""])
    if script.get("chapters"):
        lines.extend(["## 结构", ""])
        lines.extend(f"- {item}" for item in script["chapters"])
        lines.append("")
    if script.get("takeaways"):
        lines.extend(["## 重点收获", ""])
        lines.extend(f"- {item}" for item in script["takeaways"])
        lines.append("")
    lines.extend(["## 播客台词", ""])
    for item in script["lines"]:
        lines.append(f"**{item['speaker']}**：{item['text']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _expand_lines(lines: list[PodcastScriptLine]) -> list[PodcastScriptLine]:
    expanded: list[PodcastScriptLine] = []
    for line in lines:
        parts = _split_tts_text(line.text)
        for part in parts:
            expanded.append(
                PodcastScriptLine(speaker=line.speaker, text=part)
            )
    return expanded


def _split_tts_text(text: str, *, max_chars: int = 150) -> list[str]:
    text = _clean_text(text)
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = re.split(r"(?<=[。！？!?；;])", text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) > max_chars:
            parts.append(current)
            current = ""
        if len(sentence) > max_chars:
            parts.extend(
                sentence[index : index + max_chars]
                for index in range(0, len(sentence), max_chars)
            )
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts


def _voice_for(speaker: str, *, voice_a: str, voice_b: str) -> str:
    normalized = speaker.lower()
    if any(marker in normalized for marker in ("b", "乙", "嘉宾", "guest")):
        return voice_b
    return voice_a


def _normalize_clip(input_path: Path, output_path: Path, ffmpeg: str) -> None:
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            output_path,
        ]
    )


def _concat_wav_to_mp3(
    clips: list[Path],
    output: Path,
    *,
    ffmpeg: str,
    silence_ms: int,
) -> None:
    if not clips:
        raise PipelineError("没有可合成的语音片段。")
    output.parent.mkdir(parents=True, exist_ok=True)
    combined = output.with_suffix(".wav")
    sample_rate = 44100
    silence_frames = int(sample_rate * silence_ms / 1000)
    with wave.open(str(combined), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        for index, clip in enumerate(clips):
            with wave.open(str(clip), "rb") as source:
                if (
                    source.getnchannels() != 1
                    or source.getsampwidth() != 2
                    or source.getframerate() != sample_rate
                ):
                    raise PipelineError(f"音频片段格式不一致：{clip}")
                target.writeframes(source.readframes(source.getnframes()))
            if index != len(clips) - 1 and silence_frames:
                target.writeframes(b"\x00\x00" * silence_frames)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            combined,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "3",
            output,
        ]
    )
