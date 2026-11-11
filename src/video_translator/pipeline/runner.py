"""Prepare a timed transcript, then render a translated video from it."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from ..errors import InvalidSourceError, PipelineError
from ..logging_utils import create_job_logger
from ..models import JobManifest, JobStatus, Segment
from ..runtime import MediaBinaries, resolve_media_binaries
from ..settings import Settings
from ..store import JobStore
from .downloader import acquire_source
from .media import (
    DubbingOverflowError,
    SpeechAlignment,
    compose_dub_timeline,
    extract_original_audio,
    extract_speech_audio,
    has_audio_stream,
    mux_video,
    normalize_tts_segments,
    probe_duration,
    separate_background,
    write_srt,
)
from .synthesizer import SpeechSynthesizer, WordTiming, read_word_timings
from .transcriber import FasterWhisperTranscriber
from .translator import SegmentTranslator, translation_map
from .utterances import build_utterances, retime_chinese_cues


_TRANSCRIPT = TypeAdapter(list[Segment])


def _safe_output_name(title: str, job_id: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", title, flags=re.UNICODE)
    cleaned = cleaned[:80].encode("utf-8")[:200].decode("utf-8", errors="ignore")
    cleaned = cleaned.strip("-._") or "translated-video"
    return f"{cleaned}-{job_id[:8]}.mp4"


def _is_english(manifest: JobManifest) -> bool:
    language = manifest.metadata.get("detected_language") or manifest.options.source_language
    if not isinstance(language, str):
        return False
    return language.lower().replace("_", "-").partition("-")[0] == "en"


class VideoTranslationPipeline:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store

    @contextmanager
    def _processing(self, job_id: str) -> Iterator[tuple[JobManifest, Path, logging.Logger]]:
        with self.store.lock_job(job_id):
            manifest = self.store.get(job_id)
            job_dir = self.store.job_dir(job_id)
            logger = create_job_logger(job_id, job_dir)
            manifest.overflow_segments = []
            try:
                yield manifest, job_dir, logger
            except Exception as exc:
                if isinstance(exc, DubbingOverflowError):
                    manifest.overflow_segments = exc.segments
                    logger.warning("%s", exc)
                else:
                    logger.exception("任务失败")
                self.store.fail(manifest, str(exc))
                raise

    def prepare(self, manifest: JobManifest) -> JobManifest:
        with self._processing(manifest.id) as (current, job_dir, logger):
            self._prepare(current, job_dir, logger)
            return current

    def render(self, manifest: JobManifest, translations: Path | None = None) -> JobManifest:
        with self._processing(manifest.id) as (current, job_dir, logger):
            self.store.set_stage(current, JobStatus.translating, 42, "校验译稿")
            segments = self._load_transcript(current)
            translation_path = translations or (job_dir / "translations.json")
            try:
                content = translation_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise PipelineError(f"无法读取译稿：{translation_path}") from exc

            chinese = translation_map(segments, content)
            english = {} if _is_english(current) else translation_map(segments, content, field="en")
            for segment in segments:
                segment.translated_text = chinese[segment.index]
            self._save_translations(current, segments, english)
            self._render(current, job_dir, logger, segments, english)
            return current

    def run(self, manifest: JobManifest) -> JobManifest:
        """Use the configured translation service for an unattended run."""
        with self._processing(manifest.id) as (current, job_dir, logger):
            segments = self._prepare(current, job_dir, logger)
            self.store.set_stage(current, JobStatus.translating, 42, "翻译为中文")
            translator = SegmentTranslator(self.settings, logger)
            translator.translate(
                segments,
                target_language=current.options.target_language,
                glossary=current.options.glossary,
            )
            english: dict[int, str] = {}
            if not _is_english(current):
                english_segments = [
                    segment.model_copy(update={"translated_text": None}) for segment in segments
                ]
                translator.translate(
                    english_segments, target_language="English", glossary={}
                )
                english = {
                    segment.index: segment.translated_text or "" for segment in english_segments
                }
            self._save_translations(current, segments, english)
            self._render(current, job_dir, logger, segments, english)
            return current

    def _prepare(
        self,
        manifest: JobManifest,
        job_dir: Path,
        logger: logging.Logger,
    ) -> list[Segment]:
        self.store.set_stage(manifest, JobStatus.downloading, 5, "获取视频")
        media = resolve_media_binaries(self.settings)
        acquired = acquire_source(manifest.source, job_dir, self.settings, media, logger)
        manifest.title = acquired.title
        manifest.source_path = str(acquired.path)
        manifest.metadata.update(acquired.metadata)

        if not has_audio_stream(acquired.path, media):
            raise InvalidSourceError("视频中没有可识别的音轨。")
        total_duration = probe_duration(acquired.path, media)
        if total_duration > self.settings.max_video_seconds:
            raise InvalidSourceError(
                f"视频时长 {total_duration:.0f} 秒，超过限制 "
                f"{self.settings.max_video_seconds} 秒。"
            )
        manifest.metadata["media_duration"] = total_duration

        self.store.set_stage(manifest, JobStatus.extracting, 12, "提取识别音频")
        speech_audio = extract_speech_audio(
            acquired.path, job_dir / "speech-16k.wav", media, logger
        )
        manifest.audio_path = str(speech_audio)

        self.store.set_stage(manifest, JobStatus.transcribing, 22, "语音识别")
        segments, asr_metadata = FasterWhisperTranscriber(self.settings, logger).transcribe(
            str(speech_audio), language=manifest.options.source_language
        )
        if not segments:
            raise PipelineError("没有识别到可翻译的语音。")
        manifest.metadata.update(asr_metadata)
        manifest.translations_path = str(job_dir / "translations.json")
        self.store.write_segments(
            manifest, [segment.model_dump(mode="json") for segment in segments]
        )
        self.store.set_stage(manifest, JobStatus.prepared, 35, "转写已完成，等待译稿")
        return segments

    @staticmethod
    def _load_transcript(manifest: JobManifest) -> list[Segment]:
        if not manifest.source_path or not Path(manifest.source_path).is_file():
            raise PipelineError("任务缺少已准备的源视频，请先运行 prepare。")
        if not manifest.segments_path:
            raise PipelineError("任务缺少转写稿，请先运行 prepare。")
        try:
            segments = _TRANSCRIPT.validate_json(Path(manifest.segments_path).read_bytes())
        except (OSError, ValidationError) as exc:
            raise PipelineError(f"无法读取任务转写稿：{manifest.segments_path}") from exc
        if not segments:
            raise PipelineError("任务转写稿没有可渲染的片段。")
        return segments

    def _save_translations(
        self,
        manifest: JobManifest,
        segments: list[Segment],
        english: dict[int, str],
    ) -> None:
        rows = []
        for segment in segments:
            row = {"id": segment.index, "text": segment.translated_text}
            if english:
                row["en"] = english[segment.index]
            rows.append(row)
        self.store.write_translations(manifest, rows)

    def _render(
        self,
        manifest: JobManifest,
        job_dir: Path,
        logger: logging.Logger,
        segments: list[Segment],
        english: dict[int, str],
    ) -> None:
        self.store.set_stage(manifest, JobStatus.synthesizing, 58, "按句生成中文配音")
        media = resolve_media_binaries(self.settings)
        source = Path(manifest.source_path or "")
        total_duration = float(
            manifest.metadata.get("media_duration") or probe_duration(source, media)
        )
        utterances = build_utterances(segments, total_duration=total_duration)
        speech = [utterance.segment for utterance in utterances]
        SpeechSynthesizer(self.settings, logger).synthesize_segments(speech, job_dir / "tts")
        word_timings = {segment.index: read_word_timings(segment.tts_file) for segment in speech}
        speech_tails: dict[int, float] = {}
        for segment in speech:
            words = word_timings[segment.index]
            if not words:
                continue
            last_word = re.sub(r"\W", "", words[-1]["text"]).casefold()
            text = re.sub(r"\W", "", segment.translated_text or "").casefold()
            if last_word and text.endswith(last_word):
                speech_tails[segment.index] = words[-1]["end"]

        self.store.set_stage(manifest, JobStatus.aligning, 76, "对齐配音时间轴")
        alignments: dict[int, SpeechAlignment] = {}
        try:
            aligned_files = normalize_tts_segments(
                speech,
                job_dir / "aligned",
                self.settings,
                media,
                logger,
                reject_overflow=True,
                speech_ends=speech_tails,
                alignments=alignments,
            )
        except DubbingOverflowError as exc:
            by_index = {utterance.segment.index: utterance for utterance in utterances}
            for overflow in exc.segments:
                fragments = by_index[overflow["id"]].fragments
                cue_ids = list(dict.fromkeys(fragment.cue_index for fragment in fragments))
                overflow["id"] = cue_ids[0]
                if len(cue_ids) > 1:
                    overflow["cue_ids"] = cue_ids
            raise
        dub_audio = compose_dub_timeline(
            speech,
            aligned_files,
            job_dir / "dub-timeline.wav",
            total_duration=total_duration,
            sample_rate=self.settings.dub_sample_rate,
            logger=logger,
        )
        manifest.dub_audio_path = str(dub_audio)
        placed_words: dict[int, list[WordTiming]] = {}
        speech_ends: dict[int, float] = {}
        for segment in speech:
            alignment = alignments[segment.index]
            end = min(segment.end, segment.start + alignment.duration)
            speech_ends[segment.index] = end
            words: list[WordTiming] = []
            for word in word_timings[segment.index]:
                start = segment.start + word["start"] / alignment.tempo
                if start < end:
                    words.append(
                        {
                            "text": word["text"],
                            "start": start,
                            "end": min(end, segment.start + word["end"] / alignment.tempo),
                        }
                    )
            placed_words[segment.index] = words
        chinese_cues = retime_chinese_cues(utterances, placed_words, speech_ends=speech_ends)

        background_audio: Path | None = None
        if self.settings.enable_demucs:
            self.store.set_stage(manifest, JobStatus.aligning, 84, "分离原声与背景音")
            original_audio = extract_original_audio(source, job_dir / "original.wav", media, logger)
            background_audio = separate_background(
                original_audio, job_dir / "separated", logger
            )

        self.store.set_stage(manifest, JobStatus.muxing, 90, "封装视频并导出双语字幕")
        self._export(
            manifest, segments, chinese_cues, english, source, dub_audio, background_audio, media, logger
        )
        self.store.set_stage(manifest, JobStatus.completed, 100, "处理完成")
        logger.info("任务完成: %s", manifest.output_path)

    def _export(
        self,
        manifest: JobManifest,
        segments: list[Segment],
        chinese_cues: list[Segment],
        english: dict[int, str],
        source: Path,
        dub_audio: Path,
        background_audio: Path | None,
        media: MediaBinaries,
        logger: logging.Logger,
    ) -> None:
        options = manifest.options
        keep_original = options.keep_original_audio
        if keep_original is None:
            keep_original = self.settings.keep_original_audio
        duck_original = options.duck_original_audio
        if duck_original is None:
            duck_original = self.settings.duck_original_audio
        burn_subtitles = options.burn_subtitles
        if burn_subtitles is None:
            burn_subtitles = self.settings.burn_subtitles

        name = _safe_output_name(manifest.title or "video", manifest.id)
        job_outputs = self.settings.outputs_dir / manifest.id
        job_outputs.mkdir(parents=True, exist_ok=True)
        bundle = job_outputs / uuid4().hex[:12]
        output = bundle / name
        chinese_srt = output.with_suffix(".zh-CN.srt")
        english_srt = output.with_suffix(".en.srt")
        with TemporaryDirectory(prefix=".render-", dir=job_outputs) as temp:
            staging = Path(temp) / "bundle"
            chinese_temp = write_srt(chinese_cues, staging / chinese_srt.name)
            if english:
                english_segments = [
                    segment.model_copy(update={"translated_text": english[segment.index]})
                    for segment in segments
                ]
                write_srt(english_segments, staging / english_srt.name)
            else:
                write_srt(segments, staging / english_srt.name, source_text=True)
            video_temp = staging / name
            mux_video(
                source,
                dub_audio,
                chinese_temp if burn_subtitles else None,
                video_temp,
                settings=self.settings,
                media=media,
                keep_original_audio=keep_original,
                duck_original_audio=duck_original,
                burn_subtitles=burn_subtitles,
                background_audio=background_audio,
                logger=logger,
            )
            staging.rename(bundle)
        manifest.output_path = str(output)
        manifest.subtitle_path = str(chinese_srt)
        manifest.english_subtitle_path = str(english_srt)
