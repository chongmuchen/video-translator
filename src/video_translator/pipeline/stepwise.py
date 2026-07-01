"""Resumable, one-stage-at-a-time video translation pipeline."""

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from pathlib import Path

from ..errors import InvalidSourceError, PipelineError, VideoTranslatorError
from ..logging_utils import create_job_logger
from ..models import JobManifest, JobStatus, Segment
from ..runtime import MediaBinaries, resolve_media_binaries
from ..settings import Settings
from ..store import JobStore
from .downloader import acquire_source
from .media import (
    compose_dub_timeline,
    extract_original_audio,
    extract_speech_audio,
    has_audio_stream,
    has_video_stream,
    mux_audio,
    mux_video,
    normalize_tts_segments,
    probe_duration,
    separate_background,
    write_duck_control,
    write_srt,
)
from .synthesizer import SpeechSynthesizer
from .transcriber import FasterWhisperTranscriber, MlxWhisperTranscriber
from .translator import SegmentTranslator


class PipelineStep(str, Enum):
    download = "download"
    extract = "extract"
    transcribe = "transcribe"
    translate = "translate"
    synthesize = "synthesize"
    align = "align"
    mux = "mux"


STEP_ORDER = [
    PipelineStep.download,
    PipelineStep.extract,
    PipelineStep.transcribe,
    PipelineStep.translate,
    PipelineStep.synthesize,
    PipelineStep.align,
    PipelineStep.mux,
]

STEP_DESCRIPTIONS = {
    PipelineStep.download: "下载或复制视频，并检查音轨和时长",
    PipelineStep.extract: "提取 16 kHz 单声道识别音频",
    PipelineStep.transcribe: "使用 faster-whisper 生成原文和时间戳",
    PipelineStep.translate: "调用 LLM 翻译，并生成中文字幕",
    PipelineStep.synthesize: "逐句生成中文语音",
    PipelineStep.align: "将中文语音适配到原视频时间轴",
    PipelineStep.mux: "压低原声、混音并封装双音轨 MP4/M4A",
}

# TODO: These are intentionally visible through ``python main.py plan``.
TODO_ITEMS = [
    ("P0", "接通 Ollama/兼容 LLM，验证真实翻译和术语表"),
    ("P1", "翻译过长时自动缩写并重新生成 TTS，而不是最终裁尾"),
    ("P1", "为下载、翻译和 TTS 增加统一重试、退避和断点恢复"),
    ("P1", "增加说话人分离和多角色音色映射"),
    ("P1", "为 Web/API 增加任务取消"),
    ("P1", "为扫描版 PDF 增加 OCR，并支持复杂彩色版面的背景修复"),
    ("P2", "增加可选口型同步"),
    ("P2", "把本地 JSON/线程池替换为数据库和分布式任务队列"),
    ("P2", "增加中间文件过期清理、指标和质量评估报告"),
]


ACTIVE_STATUS = {
    PipelineStep.download: JobStatus.downloading,
    PipelineStep.extract: JobStatus.extracting,
    PipelineStep.transcribe: JobStatus.transcribing,
    PipelineStep.translate: JobStatus.translating,
    PipelineStep.synthesize: JobStatus.synthesizing,
    PipelineStep.align: JobStatus.aligning,
    PipelineStep.mux: JobStatus.muxing,
}

COMPLETED_STATUS = {
    PipelineStep.download: JobStatus.downloaded,
    PipelineStep.extract: JobStatus.extracted,
    PipelineStep.transcribe: JobStatus.transcribed,
    PipelineStep.translate: JobStatus.translated,
    PipelineStep.synthesize: JobStatus.synthesized,
    PipelineStep.align: JobStatus.aligned,
    PipelineStep.mux: JobStatus.completed,
}

ACTIVE_PROGRESS = {
    PipelineStep.download: 5,
    PipelineStep.extract: 12,
    PipelineStep.transcribe: 22,
    PipelineStep.translate: 42,
    PipelineStep.synthesize: 58,
    PipelineStep.align: 76,
    PipelineStep.mux: 90,
}

COMPLETED_PROGRESS = {
    PipelineStep.download: 10,
    PipelineStep.extract: 20,
    PipelineStep.transcribe: 40,
    PipelineStep.translate: 55,
    PipelineStep.synthesize: 70,
    PipelineStep.align: 85,
    PipelineStep.mux: 100,
}

STEP_START_MESSAGES = {
    PipelineStep.download: "获取视频",
    PipelineStep.extract: "提取识别音频",
    PipelineStep.transcribe: "语音识别",
    PipelineStep.translate: "翻译为中文",
    PipelineStep.synthesize: "生成中文配音",
    PipelineStep.align: "对齐配音时间轴",
    PipelineStep.mux: "混音并封装视频",
}

STEP_DONE_MESSAGES = {
    PipelineStep.download: "视频已下载，等待提取音频",
    PipelineStep.extract: "音频已提取，等待语音识别",
    PipelineStep.transcribe: "语音已识别，等待翻译",
    PipelineStep.translate: "字幕已翻译，等待生成配音",
    PipelineStep.synthesize: "中文配音已生成，等待时间轴对齐",
    PipelineStep.align: "配音已对齐，等待混音封装",
    PipelineStep.mux: "处理完成",
}


def safe_output_name(title: str, job_id: str, suffix: str = ".mp4") -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", title, flags=re.UNICODE)
    cleaned = cleaned.strip("-._")[:80] or "translated-video"
    return f"{cleaned}-{job_id[:8]}{suffix}"


class StepwiseVideoTranslationPipeline:
    """Run, stop, inspect, and resume each pipeline stage independently."""

    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store

    def _make_transcriber(
        self,
        logger: logging.Logger,
    ) -> FasterWhisperTranscriber | MlxWhisperTranscriber:
        if self.settings.asr_backend == "mlx_whisper":
            return MlxWhisperTranscriber(self.settings, logger)
        return FasterWhisperTranscriber(self.settings, logger)

    def _make_translator(self, logger: logging.Logger) -> SegmentTranslator:
        return SegmentTranslator(self.settings, logger)

    def _make_synthesizer(
        self,
        logger: logging.Logger,
    ) -> SpeechSynthesizer:
        return SpeechSynthesizer(self.settings, logger)

    def _write_segments(
        self,
        manifest: JobManifest,
        segments: list[Segment],
    ) -> None:
        self.store.write_segments(
            manifest,
            [segment.model_dump(mode="json") for segment in segments],
        )

    def _load_segments(self, manifest: JobManifest) -> list[Segment]:
        if not manifest.segments_path:
            raise PipelineError("任务没有 segments.json，请先执行 transcribe。")
        path = Path(manifest.segments_path)
        if not path.is_file():
            raise PipelineError(f"片段文件不存在：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [Segment.model_validate(item) for item in payload]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PipelineError(f"片段文件无效：{exc}") from exc

    def _source_path(self, manifest: JobManifest) -> Path:
        if not manifest.source_path:
            raise PipelineError("任务没有下载结果，请先执行 download。")
        path = Path(manifest.source_path)
        if not path.is_file():
            raise PipelineError(f"视频文件不存在：{path}")
        return path

    def _audio_path(self, manifest: JobManifest) -> Path:
        if not manifest.audio_path:
            raise PipelineError("任务没有识别音频，请先执行 extract。")
        path = Path(manifest.audio_path)
        if not path.is_file():
            raise PipelineError(f"识别音频不存在：{path}")
        return path

    def _total_duration(
        self,
        manifest: JobManifest,
        media: MediaBinaries,
    ) -> float:
        value = manifest.metadata.get("media_duration")
        if value is not None:
            return float(value)
        duration = probe_duration(self._source_path(manifest), media)
        manifest.metadata["media_duration"] = duration
        self.store.save(manifest)
        return duration

    def _require_previous(
        self,
        manifest: JobManifest,
        step: PipelineStep,
    ) -> None:
        index = STEP_ORDER.index(step)
        if index == 0:
            return
        required = STEP_ORDER[index - 1].value
        if required not in manifest.completed_steps:
            raise PipelineError(
                f"执行 {step.value} 前必须先完成 {required}。"
            )

    def _invalidate_from(
        self,
        manifest: JobManifest,
        step: PipelineStep,
    ) -> None:
        """Drop step markers and path references at/after a forced stage."""

        index = STEP_ORDER.index(step)
        allowed = {item.value for item in STEP_ORDER[:index]}
        manifest.completed_steps = [
            item for item in manifest.completed_steps if item in allowed
        ]
        if index <= STEP_ORDER.index(PipelineStep.download):
            manifest.source_path = None
            manifest.title = None
        if index <= STEP_ORDER.index(PipelineStep.extract):
            manifest.audio_path = None
        if index <= STEP_ORDER.index(PipelineStep.transcribe):
            manifest.segments_path = None
        if index <= STEP_ORDER.index(PipelineStep.translate):
            manifest.subtitle_path = None
        if (
            step == PipelineStep.translate
            and manifest.segments_path
            and Path(manifest.segments_path).is_file()
        ):
            segments = self._load_segments(manifest)
            for segment in segments:
                segment.translated_text = None
                segment.tts_file = None
            self._write_segments(manifest, segments)
        if index <= STEP_ORDER.index(PipelineStep.align):
            manifest.dub_audio_path = None
        if index <= STEP_ORDER.index(PipelineStep.mux):
            manifest.output_path = None
        manifest.error = None
        self.store.save(manifest)

    def _mark_completed(
        self,
        manifest: JobManifest,
        step: PipelineStep,
    ) -> None:
        if step.value not in manifest.completed_steps:
            manifest.completed_steps.append(step.value)
        manifest.completed_steps.sort(
            key=lambda value: [item.value for item in STEP_ORDER].index(value)
        )
        manifest.status = COMPLETED_STATUS[step]
        manifest.progress = COMPLETED_PROGRESS[step]
        manifest.stage_message = STEP_DONE_MESSAGES[step]
        manifest.error = None
        self.store.save(manifest)

    def next_step(self, manifest: JobManifest) -> PipelineStep | None:
        for step in STEP_ORDER:
            if step.value not in manifest.completed_steps:
                return step
        return None

    def run_step(
        self,
        manifest: JobManifest,
        step: PipelineStep | str,
        *,
        force: bool = False,
    ) -> JobManifest:
        selected = PipelineStep(step)
        logger = create_job_logger(
            manifest.id,
            self.store.job_dir(manifest.id),
        )
        if selected.value in manifest.completed_steps and not force:
            logger.info("步骤 %s 已完成，跳过。使用 --force 可重新执行。", selected.value)
            return manifest
        if force:
            self._invalidate_from(manifest, selected)
        self._require_previous(manifest, selected)

        try:
            self.store.set_stage(
                manifest,
                ACTIVE_STATUS[selected],
                ACTIVE_PROGRESS[selected],
                STEP_START_MESSAGES[selected],
            )
            method = getattr(self, f"_step_{selected.value}")
            method(manifest, logger)
            self._mark_completed(manifest, selected)
            return manifest
        except Exception as exc:
            logger.exception("步骤 %s 失败", selected.value)
            self.store.fail(manifest, str(exc))
            raise

    def run_next(self, manifest: JobManifest) -> JobManifest:
        step = self.next_step(manifest)
        if step is None:
            return manifest
        return self.run_step(manifest, step)

    def run_all(self, manifest: JobManifest) -> JobManifest:
        while (step := self.next_step(manifest)) is not None:
            self.run_step(manifest, step)
        return manifest

    def _step_download(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        media = resolve_media_binaries(self.settings)
        acquired = acquire_source(
            manifest.source,
            self.store.job_dir(manifest.id),
            self.settings,
            media,
            logger,
        )
        if not has_audio_stream(acquired.path, media):
            raise InvalidSourceError("视频中没有可识别的音轨。")
        duration = probe_duration(acquired.path, media)
        if duration > self.settings.max_video_seconds:
            raise InvalidSourceError(
                f"视频时长 {duration:.0f} 秒，超过限制 "
                f"{self.settings.max_video_seconds} 秒。"
            )
        manifest.title = acquired.title
        manifest.source_path = str(acquired.path)
        manifest.metadata.update(acquired.metadata)
        manifest.metadata["media_duration"] = duration
        manifest.metadata["media_kind"] = (
            "video" if has_video_stream(acquired.path, media) else "audio"
        )
        self.store.add_title_to_job_dir(manifest, acquired.title)

    def _step_extract(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        media = resolve_media_binaries(self.settings)
        speech_audio = extract_speech_audio(
            self._source_path(manifest),
            self.store.job_dir(manifest.id) / "speech-16k.wav",
            media,
            logger,
        )
        manifest.audio_path = str(speech_audio)
        self.store.save(manifest)

    def _step_transcribe(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        transcriber = self._make_transcriber(logger)
        segments, asr_metadata = transcriber.transcribe(
            str(self._audio_path(manifest)),
            language=manifest.options.source_language,
        )
        if not segments:
            raise VideoTranslatorError("没有识别到可翻译的语音。")
        manifest.metadata.update(asr_metadata)
        self._write_segments(manifest, segments)

    def _step_translate(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        segments = self._load_segments(manifest)
        translator = self._make_translator(logger)
        translator.translate(
            segments,
            target_language=manifest.options.target_language,
            glossary=manifest.options.glossary,
            on_batch_completed=lambda current: self._write_segments(
                manifest,
                current,
            ),
        )
        self._write_segments(manifest, segments)
        subtitles = write_srt(
            segments,
            self.store.job_dir(manifest.id) / "zh-CN.srt",
        )
        manifest.subtitle_path = str(subtitles)
        self.store.save(manifest)

    def _step_synthesize(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        segments = self._load_segments(manifest)
        if any(not segment.translated_text for segment in segments):
            raise PipelineError("存在没有译文的片段，请先重新执行 translate。")
        synthesizer = self._make_synthesizer(logger)
        synthesizer.synthesize_segments(
            segments,
            self.store.job_dir(manifest.id) / "tts",
        )
        self._write_segments(manifest, segments)

    def _step_align(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        media = resolve_media_binaries(self.settings)
        segments = self._load_segments(manifest)
        aligned_files = normalize_tts_segments(
            segments,
            self.store.job_dir(manifest.id) / "aligned",
            self.settings,
            media,
            logger,
        )
        dub_audio = compose_dub_timeline(
            segments,
            aligned_files,
            self.store.job_dir(manifest.id) / "dub-timeline.wav",
            total_duration=self._total_duration(manifest, media),
            sample_rate=self.settings.dub_sample_rate,
            logger=logger,
        )
        manifest.dub_audio_path = str(dub_audio)
        self.store.save(manifest)

    def _step_mux(
        self,
        manifest: JobManifest,
        logger: logging.Logger,
    ) -> None:
        media = resolve_media_binaries(self.settings)
        source = self._source_path(manifest)
        if not manifest.dub_audio_path or not Path(manifest.dub_audio_path).is_file():
            raise PipelineError("缺少完整配音时间轴，请先执行 align。")

        is_audio_only = manifest.metadata.get("media_kind") == "audio"
        if (
            not is_audio_only
            and (
                not manifest.subtitle_path
                or not Path(manifest.subtitle_path).is_file()
            )
        ):
            raise PipelineError("缺少中文字幕，请先执行 translate。")

        background_audio: Path | None = None
        if self.settings.enable_demucs:
            original_audio = extract_original_audio(
                source,
                self.store.job_dir(manifest.id) / "original.wav",
                media,
                logger,
            )
            background_audio = separate_background(
                original_audio,
                self.store.job_dir(manifest.id) / "separated",
                logger,
            )

        options = manifest.options
        keep_original = (
            self.settings.keep_original_audio
            if options.keep_original_audio is None
            else options.keep_original_audio
        )
        duck_original = (
            self.settings.duck_original_audio
            if options.duck_original_audio is None
            else options.duck_original_audio
        )
        burn_subtitles = (
            self.settings.burn_subtitles
            if options.burn_subtitles is None
            else options.burn_subtitles
        )
        duck_control_audio: Path | None = None
        if duck_original:
            duck_control_audio = write_duck_control(
                self._load_segments(manifest),
                self.store.job_dir(manifest.id) / "duck-control.wav",
                total_duration=self._total_duration(manifest, media),
                sample_rate=self.settings.dub_sample_rate,
            )
        if is_audio_only:
            output = self.settings.outputs_dir / safe_output_name(
                manifest.title or "audio",
                manifest.id,
                suffix=".m4a",
            )
            mux_audio(
                source,
                Path(manifest.dub_audio_path),
                output,
                settings=self.settings,
                media=media,
                keep_original_audio=keep_original,
                duck_original_audio=duck_original,
                background_audio=background_audio,
                duck_control_audio=duck_control_audio,
                logger=logger,
            )
            manifest.output_path = str(output)
            self.store.save(manifest)
            return

        output = self.settings.outputs_dir / safe_output_name(
            manifest.title or "video",
            manifest.id,
        )
        mux_video(
            source,
            Path(manifest.dub_audio_path),
            Path(manifest.subtitle_path),
            output,
            settings=self.settings,
            media=media,
            keep_original_audio=keep_original,
            duck_original_audio=duck_original,
            burn_subtitles=burn_subtitles,
            background_audio=background_audio,
            duck_control_audio=duck_control_audio,
            logger=logger,
        )
        manifest.output_path = str(output)
        self.store.save(manifest)
