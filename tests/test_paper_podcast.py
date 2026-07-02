from pathlib import Path

import pymupdf

from video_translator.paper_podcast.models import PaperPodcastStatus
from video_translator.paper_podcast.pipeline import (
    PaperPodcastPipeline,
    _validate_script,
)
from video_translator.paper_podcast.store import PaperPodcastStore
from video_translator.settings import Settings


def make_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=420, height=560)
    page.insert_text((50, 80), "Attention Is All You Need", fontsize=18)
    page.insert_text(
        (50, 130),
        "The Transformer is a model architecture based entirely on attention.",
        fontsize=11,
    )
    page.insert_text(
        (50, 170),
        "Experiments on machine translation show strong quality and speed.",
        fontsize=11,
    )
    document.save(path)
    document.close()


def test_paper_podcast_pipeline_keeps_script_and_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf(source)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    store = PaperPodcastStore(settings)
    pipeline = PaperPodcastPipeline(settings, store)

    def fake_normalize(input_path, output_path, ffmpeg):
        output_path.write_bytes(b"wav")

    def fake_concat(clips, output, *, ffmpeg, silence_ms):
        output.write_bytes(b"mp3")

    class FakeSynthesizer:
        def __init__(self, settings, logger):
            self.settings = settings

        def synthesize(self, text, output):
            output.write_bytes(text.encode("utf-8"))

    monkeypatch.setattr(
        "video_translator.paper_podcast.pipeline.SpeechSynthesizer",
        FakeSynthesizer,
    )
    monkeypatch.setattr(
        "video_translator.paper_podcast.pipeline.resolve_media_binaries",
        lambda settings, allow_download=False: type(
            "Media",
            (),
            {"ffmpeg": "ffmpeg"},
        )(),
    )
    monkeypatch.setattr(
        "video_translator.paper_podcast.pipeline._normalize_clip",
        fake_normalize,
    )
    monkeypatch.setattr(
        "video_translator.paper_podcast.pipeline._concat_wav_to_mp3",
        fake_concat,
    )

    manifest = pipeline.import_paper(
        source,
        title="Attention Is All You Need",
        style="deep_dive",
        duration_minutes=4,
    )
    manifest = pipeline.extract(manifest)
    manifest = pipeline.script(
        manifest,
        target_language="简体中文",
        style="deep_dive",
        duration_minutes=4,
        glossary={},
    )
    manifest = pipeline.synthesize(
        manifest,
        voice_a="zh-CN-XiaoxiaoNeural",
        voice_b="zh-CN-YunxiNeural",
        silence_ms=100,
    )

    assert manifest.status == PaperPodcastStatus.completed
    assert Path(manifest.text_path).is_file()
    assert Path(manifest.script_json_path).is_file()
    assert Path(manifest.script_markdown_path).is_file()
    assert Path(manifest.audio_path).read_bytes() == b"mp3"
    assert manifest.completed_steps == ["extract", "script", "synthesize"]


def test_paper_podcast_accepts_common_model_script_variants() -> None:
    dialogue = _validate_script(
        {
            "title": "论文讲解",
            "summary": "这是一篇论文的讲解。",
            "dialogue": [
                {"role": "主持人A", "content": "这篇论文解决什么问题？"},
                {"role": "嘉宾B", "content": "它研究语言理解中的迁移学习。"},
            ],
        }
    )
    outline = _validate_script(
        {
            "title": "论文讲解",
            "summary": "这是一篇关于预训练语言模型的论文。",
            "takeaways": ["核心是先预训练，再针对任务微调。"],
        }
    )

    assert len(dialogue["lines"]) == 2
    assert dialogue["lines"][0]["speaker"] == "主持人A"
    assert "迁移学习" in dialogue["lines"][1]["text"]
    assert outline["lines"]
