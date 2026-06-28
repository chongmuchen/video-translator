from pathlib import Path

import pytest

from video_translator.errors import InvalidSourceError
from video_translator.pipeline.downloader import validate_remote_url
from video_translator.settings import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://www.bilibili.com/video/BV123",
        "https://b23.tv/example",
    ],
)
def test_allowed_video_urls(url: str, tmp_path: Path) -> None:
    assert validate_remote_url(url, settings(tmp_path)) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/video",
        "http://localhost/video",
        "https://example.com/video",
        "https://user:pass@youtube.com/video",
    ],
)
def test_rejects_untrusted_urls(url: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        validate_remote_url(url, settings(tmp_path))

