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
from ..control import mark_canceled, raise_if_canceled
from ..errors import PipelineCanceled, PipelineError
from ..metrics import record_step_metric
from ..pipeline.media import probe_duration
from ..pipeline.synthesizer import SpeechSynthesizer
from ..pipeline.translator import SegmentTranslator
from ..runtime import resolve_media_binaries
from ..settings import Settings
from ..books.pdf import extract_pdf
from .models import (
    PaperPodcastManifest,
    PaperPodcastStatus,
    PodcastScriptBackend,
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
            with record_step_metric(manifest, self.store, "extract"):
                raise_if_canceled(manifest, self.store)
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
                raise_if_canceled(manifest, self.store)
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
        except PipelineCanceled:
            mark_canceled(manifest, self.store)
            raise
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
        script_backend: PodcastScriptBackend = "builtin",
        script_compare_models: list[str] | None = None,
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
                "script_backend": script_backend,
                "script_compare_models": _unique_models(
                    script_compare_models or []
                ),
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
        script_backend: PodcastScriptBackend = "builtin",
        script_compare_models: list[str] | None = None,
    ) -> PaperPodcastManifest:
        paper = self._read_paper_text(manifest)
        comparison_models = _unique_models(script_compare_models or [])
        key = self._script_key(
            manifest,
            target_language=target_language,
            style=style,
            script_backend=script_backend,
            script_compare_models=comparison_models,
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
            raise_if_canceled(manifest, self.store)
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
                raise_if_canceled(manifest, self.store)
                if comparison_models:
                    selected = self._build_script_comparison(
                        manifest,
                        paper.markdown,
                        notes,
                        target_language=target_language,
                        style=style,
                        script_backend=script_backend,
                        duration_minutes=duration_minutes,
                        glossary=glossary,
                        model_names=comparison_models,
                    )
                    script = selected["raw_script"]
                    validated = selected["validated"]
                    quality = selected["quality"]
                    raw_script_path = Path(selected["raw_script_path"])
                    repaired = bool(selected["repaired"])
                    manifest.metadata["script_comparison_path"] = selected[
                        "comparison_path"
                    ]
                    manifest.metadata["script_selected_model"] = selected[
                        "model"
                    ]
                    manifest.metadata["script_compare_models"] = selected[
                        "models"
                    ]
                else:
                    script = self._build_script(
                        manifest.title,
                        paper.markdown,
                        notes,
                        target_language=target_language,
                        style=style,
                        script_backend=script_backend,
                        duration_minutes=duration_minutes,
                        glossary=glossary,
                    )
                    raise_if_canceled(manifest, self.store)
                    raw_script_path = self.store.write_json(
                        manifest,
                        "podcast-script-raw.json",
                        script,
                    )
                    validated, repaired, repaired_path = (
                        self._validate_or_repair_script(
                            manifest,
                            script,
                            notes,
                            target_language=target_language,
                            style=style,
                            duration_minutes=duration_minutes,
                            repair_filename="podcast-script-repaired.json",
                        )
                    )
                    if repaired_path:
                        manifest.metadata["script_repaired_path"] = str(
                            repaired_path
                        )
                    quality = self._score_script(validated, notes)
            if self.settings.translator_provider == "passthrough":
                raw_script_path = self.store.write_json(
                    manifest,
                    "podcast-script-raw.json",
                    script,
                )
                validated, repaired, repaired_path = (
                    self._validate_or_repair_script(
                        manifest,
                        script,
                        notes,
                        target_language=target_language,
                        style=style,
                        duration_minutes=duration_minutes,
                        repair_filename="podcast-script-repaired.json",
                    )
                )
                if repaired_path:
                    manifest.metadata["script_repaired_path"] = str(
                        repaired_path
                    )
                quality = self._score_script(validated, notes)
            quality_path = self.store.write_json(
                manifest,
                "script-quality.json",
                quality,
            )
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
                    "raw_script_path": str(raw_script_path),
                    "script_repaired": repaired,
                    "script_backend": script_backend,
                    "script_quality_path": str(quality_path),
                    "script_quality_score": quality["score"],
                }
            )
            manifest.status = PaperPodcastStatus.scripted
            _mark_done(manifest, "script")
            manifest.error = None
            return self.store.save(manifest)
        except PipelineCanceled:
            mark_canceled(manifest, self.store)
            raise
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
        script_backend: PodcastScriptBackend,
        duration_minutes: int,
        glossary: dict[str, str],
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        translator = SegmentTranslator(settings or self.settings, self.logger)
        source_brief = markdown[:18000]
        style_instruction = (
            "双人深度讲解播客。主持人A负责搭结构、追问和转场；"
            "嘉宾B负责解释方法、实验、局限和启发。语言自然、有节奏，"
            "但不要闲聊灌水。"
            if style == "deep_dive"
            else "单人中文讲解稿。结构清晰，像一位老师在讲论文，"
            "语言准确、口语化但不油腻。"
        )
        backend_instruction = {
            "builtin": (
                "结构采用：问题引入、论文贡献、方法拆解、实验与局限、"
                "实践启发。"
            ),
            "notebooklm": (
                "模仿高质量双主持人论文导读：开场解释为什么重要，"
                "持续用追问推动解释，每 2～3 分钟做一次小结，"
                "把公式和实验翻译成听众能跟上的例子。"
            ),
            "podcastfy": (
                "采用音频博客编排：hook、背景、核心机制、证据、"
                "反方观点、takeaway；转场自然，适合直接发布。"
            ),
        }[script_backend]
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
            f"脚本编排策略：{backend_instruction}\n"
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

    def _build_script_comparison(
        self,
        manifest: PaperPodcastManifest,
        markdown: str,
        notes: list[dict[str, Any]],
        *,
        target_language: str,
        style: PodcastStyle,
        script_backend: PodcastScriptBackend,
        duration_minutes: int,
        glossary: dict[str, str],
        model_names: list[str],
    ) -> dict[str, Any]:
        models = _comparison_models(self.settings, model_names)
        candidates: list[dict[str, Any]] = []
        for index, model_name in enumerate(models, start=1):
            raise_if_canceled(manifest, self.store)
            candidate_settings = _settings_for_script_model(
                self.settings,
                model_name,
            )
            self.logger.info(
                "生成论文播客候选脚本 %s/%s：%s",
                index,
                len(models),
                model_name,
            )
            raw_script = self._build_script(
                manifest.title,
                markdown,
                notes,
                target_language=target_language,
                style=style,
                script_backend=script_backend,
                duration_minutes=duration_minutes,
                glossary=glossary,
                settings=candidate_settings,
            )
            prefix = f"script-candidate-{index:02d}-{_safe_filename(model_name)}"
            raw_path = self.store.write_json(
                manifest,
                f"{prefix}-raw.json",
                raw_script,
            )
            validated, repaired, repaired_path = self._validate_or_repair_script(
                manifest,
                raw_script,
                notes,
                target_language=target_language,
                style=style,
                duration_minutes=duration_minutes,
                repair_filename=f"{prefix}-repaired.json",
                settings=candidate_settings,
            )
            quality = self._score_script(validated, notes)
            script_path = self.store.write_json(
                manifest,
                f"{prefix}.json",
                validated,
            )
            quality_path = self.store.write_json(
                manifest,
                f"{prefix}-quality.json",
                quality,
            )
            markdown_path = self.store.job_dir(manifest.id) / f"{prefix}.md"
            markdown_path.write_text(
                _script_to_markdown(validated),
                encoding="utf-8",
            )
            candidates.append(
                {
                    "model": model_name,
                    "raw_script": raw_script,
                    "raw_script_path": str(raw_path),
                    "validated": validated,
                    "script_path": str(script_path),
                    "markdown_path": str(markdown_path),
                    "quality": quality,
                    "quality_path": str(quality_path),
                    "repaired": repaired,
                    "repaired_path": str(repaired_path)
                    if repaired_path
                    else None,
                }
            )
        if not candidates:
            raise PipelineError("没有可用于对比的论文播客脚本候选模型。")
        selected = max(
            candidates,
            key=lambda item: (
                float(item["quality"].get("score", 0)),
                int(item["quality"].get("line_count", 0)),
            ),
        )
        report = {
            "selected_model": selected["model"],
            "selection_rule": "按脚本质量评分最高选择；同分时优先台词更多的候选。",
            "candidates": [
                {
                    "model": item["model"],
                    "score": item["quality"].get("score"),
                    "line_count": item["quality"].get("line_count"),
                    "warnings": item["quality"].get("warnings", []),
                    "script_path": item["script_path"],
                    "markdown_path": item["markdown_path"],
                    "quality_path": item["quality_path"],
                    "repaired": item["repaired"],
                }
                for item in candidates
            ],
        }
        comparison_path = self.store.write_json(
            manifest,
            "script-comparison.json",
            report,
        )
        selected["comparison_path"] = str(comparison_path)
        selected["models"] = models
        return selected

    def _validate_or_repair_script(
        self,
        manifest: PaperPodcastManifest,
        script: dict[str, Any],
        notes: list[dict[str, Any]],
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        repair_filename: str,
        settings: Settings | None = None,
    ) -> tuple[dict[str, Any], bool, Path | None]:
        selected_settings = settings or self.settings
        try:
            return _validate_script(script), False, None
        except PipelineError as original:
            if selected_settings.translator_provider == "passthrough":
                raise
            self.logger.warning(
                "论文播客脚本格式不完整，尝试自动修复：%s",
                original,
            )
            repaired_script = self._repair_script(
                manifest.title,
                script,
                notes,
                target_language=target_language,
                style=style,
                duration_minutes=duration_minutes,
                settings=selected_settings,
            )
            repaired_path = self.store.write_json(
                manifest,
                repair_filename,
                repaired_script,
            )
            try:
                validated = _validate_script(repaired_script)
            except PipelineError:
                validated = _script_from_notes(
                    manifest.title,
                    notes,
                    target_language=target_language,
                    style=style,
                    duration_minutes=duration_minutes,
                )
                manifest.metadata["script_fallback_reason"] = (
                    "模型没有生成可朗读台词，已根据阅读笔记生成兜底讲解稿。"
                )
            return validated, True, repaired_path

    def _repair_script(
        self,
        title: str,
        raw_script: dict[str, Any],
        notes: list[dict[str, Any]],
        *,
        target_language: str,
        style: PodcastStyle,
        duration_minutes: int,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        translator = SegmentTranslator(settings or self.settings, self.logger)
        speaker_rule = (
            "双人模式：speaker 只能在“主持人A”和“嘉宾B”之间交替。"
            if style == "deep_dive"
            else "单人模式：speaker 使用“旁白”。"
        )
        target_lines = max(8, min(90, duration_minutes * 6))
        system_prompt = (
            "你是严格的 JSON 格式修复器和中文播客编导。"
            "把输入的论文讲解草稿改写成可直接 TTS 朗读的台词。"
            "必须输出 JSON，且必须包含非空 lines 数组。"
            "每条台词对象必须只有 speaker 和 text 两个关键字段。"
            "不要输出 Markdown，不要解释。"
        )
        user_prompt = (
            f"论文标题：{title}\n"
            f"目标语言：{target_language}\n"
            f"预计台词数量：约 {target_lines} 条\n"
            f"{speaker_rule}\n\n"
            "原始模型输出如下，它缺少或没有正确填写 lines。请保留其有用内容，"
            "转成标准结构：\n"
            f"{json.dumps(raw_script, ensure_ascii=False)}\n\n"
            "阅读笔记可作为补充事实来源，不能编造笔记外的信息：\n"
            f"{json.dumps(notes[:8], ensure_ascii=False)}\n\n"
            "目标 JSON 格式："
            '{"title":"中文标题","summary":"简介",'
            '"chapters":["段落标题"],"takeaways":["收获"],'
            '"lines":[{"speaker":"主持人A","text":"台词"}]}'
        )
        return translator.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def _score_script(
        self,
        script: dict[str, Any],
        notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        lines = script.get("lines") or []
        line_count = len(lines)
        lengths = [len(item.get("text", "")) for item in lines]
        avg_length = sum(lengths) / line_count if line_count else 0
        speakers = [item.get("speaker", "") for item in lines]
        alternations = sum(
            1
            for left, right in zip(speakers, speakers[1:])
            if left and right and left != right
        )
        note_terms = {
            str(term.get("term", "")).lower()
            for note in notes
            for term in _dict_list(note.get("terms"))
            if term.get("term")
        }
        script_text = "\n".join(item.get("text", "") for item in lines).lower()
        covered_terms = sum(1 for term in note_terms if term in script_text)
        warnings: list[str] = []
        if line_count < 8:
            warnings.append("台词数量偏少，可能不像完整讲解。")
        if avg_length > 130:
            warnings.append("平均单句偏长，TTS 可能显得吃力。")
        if speakers and alternations / max(1, line_count - 1) < 0.35:
            warnings.append("双人对话交替不足，可能更像独白。")
        risky_markers = ["显然", "必然", "证明了", "完全解决"]
        if any(marker in script_text for marker in risky_markers):
            warnings.append("出现强断言词，建议人工核查事实表达。")
        score = 100
        score -= max(0, 8 - line_count) * 4
        score -= max(0, avg_length - 120) * 0.25
        score -= max(0, 0.45 - alternations / max(1, line_count - 1)) * 30
        if note_terms:
            score += min(10, covered_terms / max(1, len(note_terms)) * 10)
        score -= len(warnings) * 6
        return {
            "score": round(max(0, min(100, score)), 1),
            "line_count": line_count,
            "avg_line_characters": round(avg_length, 1),
            "speaker_alternation_ratio": round(
                alternations / max(1, line_count - 1),
                3,
            ),
            "covered_terms": covered_terms,
            "known_terms": len(note_terms),
            "warnings": warnings,
            "fact_check": (
                "启发式检查完成：仍建议对论文结论、实验数字和专名做人工复核。"
            ),
        }

    def synthesize(
        self,
        manifest: PaperPodcastManifest,
        *,
        voice_a: str,
        voice_b: str,
        silence_ms: int,
        make_video: bool = False,
    ) -> PaperPodcastManifest:
        if not manifest.script_json_path:
            raise PipelineError("尚未生成论文播客脚本。")
        script_path = Path(manifest.script_json_path)
        if not script_path.is_file():
            raise PipelineError("论文播客脚本不存在。")
        manifest.status = PaperPodcastStatus.synthesizing
        self.store.save(manifest)
        try:
            with record_step_metric(manifest, self.store, "synthesize"):
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
                expanded_lines = _expand_lines(lines)
                for index, line in enumerate(expanded_lines, start=1):
                    raise_if_canceled(manifest, self.store)
                    voice = _voice_for(
                        line.speaker,
                        voice_a=voice_a,
                        voice_b=voice_b,
                    )
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
                        len(expanded_lines),
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
                video_path = None
                if make_video:
                    video_path = self.store.outputs_dir / (
                        f"{manifest.title}-paper-podcast-"
                        f"{manifest.style}-{manifest.id[:8]}.mp4"
                    )
                    _make_explainer_video(
                        Path(manifest.source_path),
                        output,
                        video_path,
                        job_dir=self.store.job_dir(manifest.id),
                        media=media,
                    )
                    manifest.video_path = str(video_path)
                manifest.metadata.update(
                    {
                        "voice_a": voice_a,
                        "voice_b": voice_b,
                        "silence_ms": silence_ms,
                        "audio_clip_count": len(normalized_clips),
                        "video_enabled": make_video,
                    }
                )
                manifest.status = PaperPodcastStatus.completed
                _mark_done(manifest, "synthesize")
                manifest.error = None
                return self.store.save(manifest)
        except PipelineCanceled:
            mark_canceled(manifest, self.store)
            raise
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
        make_video: bool = False,
        script_backend: PodcastScriptBackend = "builtin",
        script_compare_models: list[str] | None = None,
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
            script_backend=script_backend,
            script_compare_models=script_compare_models,
            duration_minutes=duration_minutes,
            glossary=glossary,
        )
        current = self.store.get(manifest.id)
        return self.synthesize(
            current,
            voice_a=voice_a,
            voice_b=voice_b,
            silence_ms=silence_ms,
            make_video=make_video,
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


def _unique_models(models: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        cleaned = str(model or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result[:5]


def _current_script_model(settings: Settings) -> str:
    if settings.translator_provider == "codex_cli":
        return (
            settings.translator_codex_model
            or f"strategy:{settings.translator_codex_strategy}"
        )
    return settings.translator_model or "default"


def _comparison_models(settings: Settings, models: list[str]) -> list[str]:
    return _unique_models([_current_script_model(settings), *models])


def _settings_for_script_model(settings: Settings, model: str) -> Settings:
    if model.startswith("strategy:") or model == "default":
        return settings
    if settings.translator_provider == "codex_cli":
        return settings.model_copy(update={"translator_codex_model": model})
    return settings.model_copy(update={"translator_model": model})


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return (safe or "model")[:80]


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
    raw_lines = _candidate_script_lines(value)
    if not raw_lines:
        raw_lines = _outline_to_lines(value)
    if not raw_lines:
        raise PipelineError("论文播客脚本缺少 lines。")
    lines = []
    for item in raw_lines:
        speaker, text = _line_speaker_and_text(item)
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


def _candidate_script_lines(value: dict[str, Any]) -> list[Any]:
    """Accept common model variants, not only the exact `lines` key."""

    keys = (
        "lines",
        "dialogue",
        "dialogues",
        "conversation",
        "script",
        "podcast_script",
        "segments",
        "utterances",
        "transcript",
    )
    for key in keys:
        candidate = value.get(key)
        extracted = _extract_line_list(candidate)
        if extracted:
            return extracted
    for candidate in value.values():
        extracted = _extract_line_list(candidate)
        if extracted:
            return extracted
    return []


def _extract_line_list(candidate) -> list[Any]:
    if isinstance(candidate, list):
        if any(_line_speaker_and_text(item)[1] for item in candidate):
            return candidate
        for item in candidate:
            nested = _extract_line_list(item)
            if nested:
                return nested
    if isinstance(candidate, dict):
        for key in (
            "lines",
            "dialogue",
            "dialogues",
            "conversation",
            "script",
            "segments",
            "utterances",
            "items",
            "content",
        ):
            nested = _extract_line_list(candidate.get(key))
            if nested:
                return nested
    return []


def _line_speaker_and_text(item) -> tuple[str, str]:
    if isinstance(item, str):
        text = _clean_text(item)
        for delimiter in ("：", ":"):
            if delimiter in text:
                speaker, line = text.split(delimiter, 1)
                speaker = speaker.strip()
                line = _clean_text(line)
                if 0 < len(speaker) <= 20 and line:
                    return speaker, line
        return "旁白", text
    if not isinstance(item, dict):
        return "旁白", ""
    speaker = str(
        item.get("speaker")
        or item.get("role")
        or item.get("name")
        or item.get("host")
        or item.get("character")
        or "旁白"
    ).strip() or "旁白"
    text_value = (
        item.get("text")
        or item.get("line")
        or item.get("content")
        or item.get("utterance")
        or item.get("speech")
        or item.get("dialogue")
        or item.get("sentence")
    )
    if text_value is None and len(item) == 1:
        text_value = next(iter(item.values()))
    return speaker, _clean_text(str(text_value or ""))


def _outline_to_lines(value: dict[str, Any]) -> list[dict[str, str]]:
    summary = _clean_text(str(value.get("summary") or value.get("abstract") or ""))
    chapters = _string_list(
        value.get("chapters")
        or value.get("sections")
        or value.get("outline")
    )
    takeaways = _string_list(
        value.get("takeaways")
        or value.get("key_points")
        or value.get("highlights")
    )
    lines: list[dict[str, str]] = []
    if summary:
        lines.append({"speaker": "主持人A", "text": summary})
    for index, chapter in enumerate(chapters[:10]):
        lines.append(
            {
                "speaker": "嘉宾B" if index % 2 else "主持人A",
                "text": chapter,
            }
        )
    for index, takeaway in enumerate(takeaways[:12]):
        lines.append(
            {
                "speaker": "嘉宾B" if index % 2 else "主持人A",
                "text": takeaway,
            }
        )
    return lines


def _script_from_notes(
    title: str,
    notes: list[dict[str, Any]],
    *,
    target_language: str,
    style: PodcastStyle,
    duration_minutes: int,
) -> dict[str, Any]:
    collected: list[str] = []
    for note in notes:
        collected.extend(_string_list(note.get("notes")))
        collected.extend(
            f"{item.get('term', '术语')}：{item.get('meaning', '')}"
            for item in _dict_list(note.get("terms"))
        )
        collected.extend(_string_list(note.get("questions")))
    collected = [item for item in collected if item][: max(10, duration_minutes * 4)]
    if not collected:
        collected = ["这里原文信息不足，无法生成完整讲解。"]
    if style == "deep_dive":
        lines = [
            {
                "speaker": "主持人A",
                "text": f"欢迎来到论文讲解播客。今天我们聊这篇论文：{title}。",
            }
        ]
        for index, item in enumerate(collected):
            lines.append(
                {
                    "speaker": "嘉宾B" if index % 2 else "主持人A",
                    "text": item,
                }
            )
        lines.append(
            {
                "speaker": "主持人A",
                "text": "以上就是这篇论文的主要脉络。后续如果需要，我们可以继续围绕方法细节和实验设计做精读。",
            }
        )
    else:
        lines = [
            {
                "speaker": "旁白",
                "text": f"下面是一份关于 {title} 的论文讲解。",
            },
            *({"speaker": "旁白", "text": item} for item in collected),
        ]
    return {
        "title": f"{title}｜论文讲解",
        "summary": f"使用 {target_language} 根据阅读笔记生成的论文讲解稿。",
        "chapters": ["论文背景", "方法与实验", "局限和启发"],
        "takeaways": collected[:6],
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


def _render_pdf_slides(
    source: Path,
    slide_dir: Path,
    *,
    max_slides: int = 10,
) -> list[Path]:
    try:
        import pymupdf
    except ImportError as exc:
        raise PipelineError("论文讲解视频需要 PyMuPDF。") from exc

    slide_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(source)
    if doc.page_count == 0:
        raise PipelineError("PDF 没有可渲染页面。")
    scored: list[tuple[int, int]] = []
    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        image_score = len(page.get_images(full=True)) * 3
        drawing_score = len(page.get_drawings())
        text_score = min(5, len(page.get_text("text").strip()) // 600)
        scored.append((image_score + drawing_score + text_score, page_index))
    selected = {0}
    selected.update(
        page_index
        for _, page_index in sorted(scored, reverse=True)[: max_slides - 1]
    )
    slides: list[Path] = []
    matrix = pymupdf.Matrix(1.6, 1.6)
    for page_index in sorted(selected)[:max_slides]:
        page = doc.load_page(page_index)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        path = slide_dir / f"slide-{page_index + 1:03d}.png"
        pixmap.save(path)
        slides.append(path)
    doc.close()
    return slides


def _make_explainer_video(
    source_pdf: Path,
    audio: Path,
    output: Path,
    *,
    job_dir: Path,
    media,
) -> None:
    slides = _render_pdf_slides(source_pdf, job_dir / "video-slides")
    if not slides:
        raise PipelineError("没有生成论文讲解视频幻灯片。")
    duration = max(1.0, probe_duration(audio, media))
    slide_duration = max(2.0, duration / len(slides))
    concat = job_dir / "video-slides.txt"
    lines: list[str] = []
    for slide in slides:
        lines.append(f"file '{slide}'")
        lines.append(f"duration {slide_duration:.3f}")
    lines.append(f"file '{slides[-1]}'")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat,
            "-i",
            audio,
            "-shortest",
            "-vf",
            "scale=1280:-2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            output,
        ]
    )


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
