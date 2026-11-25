import json
import logging
import struct
import sys
import wave
from pathlib import Path

import pytest

from video_translator import cli
from video_translator import settings as settings_module
from video_translator.commands import run_command
from video_translator.errors import PipelineError
from video_translator.models import JobManifest, JobStatus, PipelineOptions, Segment
from video_translator.pipeline import runner
from video_translator.pipeline.media import DubbingOverflowError, probe_media
from video_translator.pipeline.runner import VideoTranslationPipeline
from video_translator.runtime import MediaBinaries, resolve_media_binaries
from video_translator.settings import Settings
from video_translator.store import JobStore


class FakeTranscriber:
    def __init__(self, settings: Settings, logger: logging.Logger):
        pass

    def transcribe(self, path: str, *, language: str | None = None):
        source_text = "こんにちは。" if language == "ja" else "Hello world."
        return (
            [
                Segment(
                    index=0,
                    start=0.25,
                    end=1.5,
                    source_text=source_text,
                )
            ],
            {"detected_language": language or "en"},
        )


class FakeTranslator:
    def __init__(self, settings: Settings, logger: logging.Logger):
        pass

    def translate(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[Segment]:
        segments[0].translated_text = "你好，世界。"
        return segments


class FakeSynthesizer:
    seconds = 1
    sample = b"\x00\x00"

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.rate = settings.dub_sample_rate

    def synthesize_segments(
        self,
        segments: list[Segment],
        output_dir: Path,
    ) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for segment in segments:
            output = output_dir / f"{segment.index:05d}.wav"
            with wave.open(str(output), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(self.rate)
                audio.writeframes(self.sample * self.rate * self.seconds)
            segment.tts_file = str(output)
        return segments


@pytest.fixture
def local_clip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Settings, MediaBinaries, Path]:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_tempo_factor=1.8,
        keep_original_audio=True,
        burn_subtitles=False,
        enable_demucs=False,
    )
    media = resolve_media_binaries(settings)
    source = tmp_path / "source.mp4"
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            source,
        ]
    )
    monkeypatch.setattr(runner, "FasterWhisperTranscriber", FakeTranscriber)
    monkeypatch.setattr(runner, "SpeechSynthesizer", FakeSynthesizer)
    return settings, media, source


def assert_outputs(
    completed: JobManifest,
    media: MediaBinaries,
    *,
    chinese: str,
    english: str = "Hello world.",
) -> None:
    assert completed.status == JobStatus.completed
    assert completed.output_path and completed.subtitle_path
    assert completed.english_subtitle_path
    output = Path(completed.output_path)
    assert output.is_file()
    chinese_srt = output.with_suffix(".zh-CN.srt")
    english_srt = output.with_suffix(".en.srt")
    assert Path(completed.subtitle_path) == chinese_srt
    assert Path(completed.english_subtitle_path) == english_srt
    timestamp = "00:00:00,250 --> 00:00:01,500"
    assert chinese_srt.read_text(encoding="utf-8").strip().splitlines() == [
        "1",
        timestamp,
        chinese,
    ]
    assert english_srt.read_text(encoding="utf-8").strip().splitlines() == [
        "1",
        timestamp,
        english,
    ]
    streams = probe_media(output, media)["streams"]
    assert [stream["codec_type"] for stream in streams] == ["video", "audio", "audio"]
    assert streams[1]["tags"]["language"] == "chi"
    assert streams[1]["disposition"]["default"] == 1
    assert streams[2]["disposition"]["default"] == 0


def test_end_to_end_runner_with_fake_model_adapters(
    local_clip: tuple[Settings, MediaBinaries, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, media, source = local_clip
    settings = settings.model_copy(update={"translator_provider": "passthrough"})
    monkeypatch.setattr(runner, "SegmentTranslator", FakeTranslator)
    store = JobStore(settings)
    manifest = store.create(str(source), PipelineOptions())
    completed = VideoTranslationPipeline(settings, store).run(manifest)

    assert_outputs(completed, media, chinese="你好，世界。")


@pytest.mark.parametrize("scope", ["project", "standalone", "legacy"])
def test_cli_runs_job_in_selected_directory(
    local_clip: tuple[Settings, MediaBinaries, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scope: str,
) -> None:
    _, media, source = local_clip
    root = source.parent
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", root)
    monkeypatch.delenv("VT_DATA_DIR", raising=False)
    settings = Settings(_env_file=None, ffmpeg_bin=media.ffmpeg, ffprobe_bin=media.ffprobe)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def run_cli(*args: str) -> dict:
        monkeypatch.setattr(sys, "argv", ["video-translator", *args, "--json"])
        with pytest.raises(SystemExit) as exit_status:
            cli.main()
        result = json.loads(capsys.readouterr().out)
        assert exit_status.value.code == 0, result
        return result

    project = "test-project"
    project_args = [] if scope == "standalone" else ["--project", project]
    job_root = settings.data_dir if not project_args else root / "data/projects" / project / "job"
    prepared = run_cli("prepare", str(source), *project_args)
    job_id = prepared["job_id"]
    translations = job_root / "jobs" / job_id / "translations.json"
    assert Path(prepared["translations_path"]) == translations
    translations.write_text(
        json.dumps({"segments": [{"id": 0, "text": "你好，世界。"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    if scope == "legacy":
        legacy_jobs = root / "data/jobs"
        legacy_jobs.mkdir()
        (legacy_jobs / job_id).symlink_to(translations.parent, target_is_directory=True)
        rendered = run_cli("render", job_id)
        output_dir = root / "data/outputs"
    else:
        rendered = run_cli("render", job_id, *project_args)
        output_dir = job_root / "outputs"
        assert not (root / "data/jobs").exists()
        assert not (root / "data/outputs").exists()

    assert Path(rendered["output_path"]).is_relative_to(output_dir / job_id)
    manifest = JobManifest.model_validate_json(
        translations.with_name("manifest.json").read_text(encoding="utf-8")
    )
    assert_outputs(manifest, media, chinese="你好，世界。")


@pytest.mark.parametrize("source_language", ["en", "ja"])
def test_prepare_and_rerender_with_revised_translation(
    local_clip: tuple[Settings, MediaBinaries, Path],
    monkeypatch: pytest.MonkeyPatch,
    source_language: str,
) -> None:
    settings, media, source = local_clip
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    options = PipelineOptions(source_language=source_language)
    prepared = pipeline.prepare(store.create(str(source), options))
    assert prepared.status == JobStatus.prepared
    assert prepared.output_path is None
    assert prepared.segments_path and prepared.translations_path
    transcript = Path(prepared.segments_path)
    original_transcript = transcript.read_bytes()
    translations = Path(prepared.translations_path)
    source.unlink()

    def unexpected_model(*args, **kwargs):
        pytest.fail("Render should use the saved transcript and imported translation")

    monkeypatch.setattr(runner, "FasterWhisperTranscriber", unexpected_model)
    monkeypatch.setattr(runner, "SegmentTranslator", unexpected_model)

    english = "Hello." if source_language == "ja" else "Hello world."
    for chinese in ("你好，世界。", "大家好。"):
        row = {"id": 0, "text": chinese}
        if source_language == "ja":
            row["en"] = english
        translations.write_text(
            json.dumps({"segments": [row]}, ensure_ascii=False),
            encoding="utf-8",
        )
        completed = pipeline.render(store.get(prepared.id))
        assert completed.id == prepared.id
        assert_outputs(completed, media, chinese=chinese, english=english)
        assert transcript.read_bytes() == original_transcript


def test_bad_drafts_and_overflow_can_be_retried(
    local_clip: tuple[Settings, MediaBinaries, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, media, source = local_clip
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    prepared = pipeline.prepare(store.create(str(source), PipelineOptions()))
    assert prepared.translations_path
    translations = Path(prepared.translations_path)
    row = {"id": 0, "text": "你好，世界。"}

    for rows, message in (([], "缺少片段"), ([row, row], "重复片段")):
        translations.write_text(json.dumps({"segments": rows}), encoding="utf-8")
        with pytest.raises(PipelineError, match=message):
            pipeline.render(store.get(prepared.id))
        failed = store.get(prepared.id)
        assert failed.status == JobStatus.failed
        assert failed.failed_stage == JobStatus.translating
        assert not list(settings.outputs_dir.iterdir())

    translations.write_text(json.dumps({"segments": [row]}), encoding="utf-8")
    monkeypatch.setattr(FakeSynthesizer, "seconds", 3)
    with pytest.raises(DubbingOverflowError):
        pipeline.render(store.get(prepared.id))
    failed = store.get(prepared.id)
    assert failed.status == JobStatus.failed
    assert failed.failed_stage == JobStatus.aligning
    assert failed.overflow_segments == [
        {"id": 0, "audio_seconds": 3.0, "slot_seconds": 1.25, "required_tempo": 2.4}
    ]
    assert not list(settings.outputs_dir.iterdir())

    row["text"] = "大家好。"
    translations.write_text(json.dumps({"segments": [row]}), encoding="utf-8")
    monkeypatch.setattr(FakeSynthesizer, "seconds", 1)
    completed = pipeline.render(store.get(prepared.id))
    assert completed.failed_stage is None
    assert not completed.overflow_segments
    assert_outputs(completed, media, chinese=row["text"])


def test_sentence_crossing_subtitle_cues_is_dubbed_without_padding(
    local_clip: tuple[Settings, MediaBinaries, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _, source = local_clip
    spoken = []

    class SplitSentence(FakeTranscriber):
        def transcribe(self, path: str, *, language: str | None = None):
            return (
                [
                    Segment(index=0, start=0.20, end=0.55, source_text="If you're ready,"),
                    Segment(index=1, start=1.05, end=1.70, source_text="we'll leave."),
                ],
                {"detected_language": "en"},
            )

    class ContinuousVoice(FakeSynthesizer):
        sample = b"\x00\x20"

        def synthesize_segments(
            self, segments: list[Segment], output_dir: Path
        ) -> list[Segment]:
            spoken.extend(segment.translated_text for segment in segments)
            synthesized = super().synthesize_segments(segments, output_dir)
            for segment in synthesized:
                assert segment.tts_file
                words = [
                    {"start": 0.00, "end": 0.08, "text": "如果"},
                    {"start": 0.08, "end": 0.18, "text": "准备"},
                    {"start": 0.18, "end": 0.25, "text": "好了"},
                    {"start": 0.31, "end": 0.43, "text": "我们"},
                    {"start": 0.48, "end": 0.52, "text": "就"},
                    {"start": 0.53, "end": 0.99, "text": "出发"},
                ]
                Path(segment.tts_file).with_suffix(".words.json").write_text(
                    json.dumps(words, ensure_ascii=False), encoding="utf-8"
                )
            return synthesized

    monkeypatch.setattr(runner, "FasterWhisperTranscriber", SplitSentence)
    monkeypatch.setattr(runner, "SpeechSynthesizer", ContinuousVoice)
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    prepared = pipeline.prepare(store.create(str(source), PipelineOptions()))
    assert prepared.translations_path
    rows = [{"id": 0, "text": "如果准备好了，"}, {"id": 1, "text": "我们就出发。"}]
    Path(prepared.translations_path).write_text(
        json.dumps({"segments": rows}, ensure_ascii=False), encoding="utf-8"
    )
    completed = pipeline.render(store.get(prepared.id))

    assert spoken == ["如果准备好了，我们就出发。"]
    assert completed.subtitle_path and completed.english_subtitle_path
    english = Path(completed.english_subtitle_path).read_text(encoding="utf-8").strip()
    assert english == (
        "1\n00:00:00,200 --> 00:00:00,550\nIf you're ready,\n\n"
        "2\n00:00:01,050 --> 00:00:01,700\nwe'll leave."
    )
    chinese = Path(completed.subtitle_path).read_text(encoding="utf-8").strip()
    first, second = [cue.splitlines() for cue in chinese.split("\n\n")]
    assert [first[2], second[2]] == [row["text"] for row in rows]
    assert second[1].startswith("00:00:00,510 --> ")

    assert completed.dub_audio_path
    with wave.open(completed.dub_audio_path, "rb") as dub:
        # The original cues leave 0.55–1.05 s empty. The joined utterance fills it.
        dub.setpos(round(0.60 * dub.getframerate()))
        frames = round(0.40 * dub.getframerate())
        samples = list(struct.iter_unpack("<h", dub.readframes(frames)))
        assert len(samples) == frames
        assert all(sample > 4096 for (sample,) in samples)



def test_render_preserves_pronunciations_and_subtitle_text_on_rerender(local_clip, monkeypatch):
    settings, media, source = local_clip
    received = []

    class AnnotatedVoice(FakeSynthesizer):
        def synthesize_segments(self, segments, output_dir):
            received.extend([
                (segment.translated_text, [reading.model_dump() for reading in segment.pronunciations])
                for segment in segments
            ])
            return super().synthesize_segments(segments, output_dir)

    monkeypatch.setattr(runner, "SpeechSynthesizer", AnnotatedVoice)
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    prepared = pipeline.prepare(store.create(str(source), PipelineOptions()))
    translations = Path(prepared.translations_path)
    translations.write_text(json.dumps({"segments": [{
        "id": 0,
        "text": "  你得记住。  ",
        "pronunciations": [{"start": 3, "char": "得", "pinyin": "dei3"}],
    }]}), encoding="utf-8")
    expected_annotations = [{"start": 1, "char": "得", "pinyin": "dei3"}]

    for _ in range(2):
        completed = pipeline.render(store.get(prepared.id))
        assert_outputs(completed, media, chinese="你得记住。")
        saved = json.loads(translations.read_text())["segments"][0]
        assert saved == {"id": 0, "text": "你得记住。", "pronunciations": expected_annotations}
    assert received == [("你得记住。", expected_annotations)] * 2


def test_api_translation_run_is_rejected_before_download(tmp_path, monkeypatch):
    from video_translator.errors import ConfigurationError

    settings = Settings(data_dir=tmp_path / "data")
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    manifest = store.create("https://www.youtube.com/watch?v=example", PipelineOptions())

    def unexpected_download(*args, **kwargs):
        pytest.fail("Rejected API translation should not acquire source media")

    monkeypatch.setattr(runner, "acquire_source", unexpected_download)
    with pytest.raises(ConfigurationError, match="Codex Pro"):
        pipeline.run(manifest)
    assert store.get(manifest.id).status == JobStatus.failed
