from pathlib import Path

import pytest

from video_translator.errors import InvalidSourceError
from video_translator.pipeline.downloader import (
    explain_download_error,
    validate_remote_url,
)
from video_translator.settings import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=Vcks8p1NpKw",
        "https://youtu.be/abc",
        "https://www.bilibili.com/video/BV1st7G6WEYR",
        "https://b23.tv/example",
        (
            "https://podcasts.apple.com/us/podcast/example/"
            "id1531349107?i=1000748574256"
        ),
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
        "https://podcasts.apple.com/us/podcast/example/id1531349107",
    ],
)
def test_rejects_untrusted_urls(url: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError):
        validate_remote_url(url, settings(tmp_path))


def test_explains_bilibili_412() -> None:
    message = explain_download_error(
        RuntimeError("HTTP Error 412: Precondition Failed"),
        is_bilibili=True,
    )
    assert "--cookies-from-browser chrome" in message
    assert "--proxy direct" in message


def test_explains_tls_eof() -> None:
    message = explain_download_error(
        RuntimeError("SSL: UNEXPECTED_EOF_WHILE_READING"),
        is_bilibili=True,
    )
    assert "TLS" in message
    assert "--proxy direct" in message


def test_explains_apple_podcast_extractor_failure() -> None:
    message = explain_download_error(
        RuntimeError("No video formats found"),
        is_bilibili=False,
        is_apple_podcasts=True,
    )
    assert "make podcast" in message
    assert "--latest 1" in message
