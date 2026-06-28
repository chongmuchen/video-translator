"""End-to-end pipeline orchestration."""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import InvalidSourceError, VideoTranslatorError
from ..logging_utils import create_job_logger
from ..models import JobManifest, JobStatus, Segment
from ..runtime import resolve_media_binaries
from ..settings import Settings
from ..store import JobStore
from .downloader import acquire_source
from .media import (
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
from .synthesizer import SpeechSynthesizer
from .transcriber import FasterWhisperTranscriber
from .translator import SegmentTranslator


def _safe_output_name(title: str, job_id: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", title, flags=re.UNICODE)
    cleaned = cleaned.strip("-._")[:80] or "translated-video"
    return f"{cleaned}-{job_id[:8]}.mp4"


class VideoTranslationPipeline:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store

    def _write_segments(
        self,
        manifest: JobManifest,
        segments: list[Segment],
    ) -> None:
        self.store.write_segments(
            manifest,
            [segment.model_dump(mode="json") for segment in segments],
        )

    def run(self, manifest: JobManifest) -> JobManifest:
        job_dir = self.store.job_dir(manifest.id)
        logger = create_job_logger(manifest.id, job_dir)
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

        try:
            media = resolve_media_binaries(self.settings)

            self.store.set_stage(
                manifest,
                JobStatus.downloading,
                5,
                "获取视频",
            )
            acquired = acquire_source(
                manifest.source,
                job_dir,
                self.settings,
                media,
                logger,
            )
            manifest.title = acquired.title
            manifest.source_path = str(acquired.path)
            manifest.metadata.update(acquired.metadata)
            self.store.save(manifest)

            if not has_audio_stream(acquired.path, media):
                raise InvalidSourceError("视频中没有可识别的音轨。")
            total_duration = probe_duration(acquired.path, media)
            if total_duration > self.settings.max_video_seconds:
                raise InvalidSourceError(
                    f"视频时长 {total_duration:.0f} 秒，超过限制 "
                    f"{self.settings.max_video_seconds} 秒。"
                )
            manifest.metadata["media_duration"] = total_duration

            self.store.set_stage(
                manifest,
                JobStatus.extracting,
                12,
                "提取识别音频",
            )
            speech_audio = extract_speech_audio(
                acquired.path,
                job_dir / "speech-16k.wav",
                media,
                logger,
            )
            manifest.audio_path = str(speech_audio)
            self.store.save(manifest)

            self.store.set_stage(
                manifest,
                JobStatus.transcribing,
                22,
                "语音识别",
            )
            transcriber = FasterWhisperTranscriber(self.settings, logger)
            segments, asr_metadata = transcriber.transcribe(
                str(speech_audio),
                language=options.source_language,
            )
            if not segments:
                raise VideoTranslatorError("没有识别到可翻译的语音。")
            manifest.metadata.update(asr_metadata)
            self._write_segments(manifest, segments)

            self.store.set_stage(
                manifest,
                JobStatus.translating,
                42,
                "翻译为中文",
            )
            translator = SegmentTranslator(self.settings, logger)
            translator.translate(
                segments,
                target_language=options.target_language,
                glossary=options.glossary,
            )
            self._write_segments(manifest, segments)

            subtitles = write_srt(segments, job_dir / "zh-CN.srt")
            manifest.subtitle_path = str(subtitles)
            self.store.save(manifest)

            self.store.set_stage(
                manifest,
                JobStatus.synthesizing,
                58,
                "生成中文配音",
            )
            synthesizer = SpeechSynthesizer(self.settings, logger)
            synthesizer.synthesize_segments(
                segments,
                job_dir / "tts",
            )
            self._write_segments(manifest, segments)

            self.store.set_stage(
                manifest,
                JobStatus.aligning,
                76,
                "对齐配音时间轴",
            )
            aligned_files = normalize_tts_segments(
                segments,
                job_dir / "aligned",
                self.settings,
                media,
                logger,
            )
            dub_audio = compose_dub_timeline(
                segments,
                aligned_files,
                job_dir / "dub-timeline.wav",
                total_duration=total_duration,
                sample_rate=self.settings.dub_sample_rate,
                logger=logger,
            )
            manifest.dub_audio_path = str(dub_audio)
            self.store.save(manifest)

            background_audio: Path | None = None
            if self.settings.enable_demucs:
                self.store.set_stage(
                    manifest,
                    JobStatus.aligning,
                    84,
                    "分离原声与背景音",
                )
                original_audio = extract_original_audio(
                    acquired.path,
                    job_dir / "original.wav",
                    media,
                    logger,
                )
                background_audio = separate_background(
                    original_audio,
                    job_dir / "separated",
                    logger,
                )

            self.store.set_stage(
                manifest,
                JobStatus.muxing,
                90,
                "混音并封装视频",
            )
            output = (
                self.settings.outputs_dir
                / _safe_output_name(manifest.title or "video", manifest.id)
            )
            mux_video(
                acquired.path,
                dub_audio,
                subtitles,
                output,
                settings=self.settings,
                media=media,
                keep_original_audio=keep_original,
                duck_original_audio=duck_original,
                burn_subtitles=burn_subtitles,
                background_audio=background_audio,
                logger=logger,
            )
            manifest.output_path = str(output)
            manifest.status = JobStatus.completed
            manifest.progress = 100
            manifest.stage_message = "处理完成"
            self.store.save(manifest)
            logger.info("任务完成: %s", output)
            return manifest
        except Exception as exc:
            logger.exception("任务失败")
            self.store.fail(manifest, str(exc))
            raise

