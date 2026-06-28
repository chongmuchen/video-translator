from pathlib import Path

import pytest
import yt_dlp

from video_translator.collection import (
    bilibili_bvid,
    discover_collection,
    parse_part_spec,
    without_bilibili_part,
)
from video_translator.settings import Settings


def test_removes_only_bilibili_part_query() -> None:
    url = (
        "https://www.bilibili.com/video/BV123/?p=68"
        "&spm_id_from=333.1"
    )
    result = without_bilibili_part(url)
    assert "p=68" not in result
    assert "spm_id_from=333.1" in result


def test_extracts_bilibili_bvid() -> None:
    assert (
        bilibili_bvid(
            "https://www.bilibili.com/video/BV1pG6xBrEct/?p=68"
        )
        == "BV1pG6xBrEct"
    )


def test_part_spec_supports_ranges_and_deduplicates() -> None:
    assert parse_part_spec("1-3,2,5,68", 100) == [1, 2, 3, 5, 68]


@pytest.mark.parametrize("spec", ["0", "2-1", "101", "hello"])
def test_part_spec_rejects_invalid_values(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_part_spec(spec, 100)


def test_discovers_flat_collection_without_resolving_media(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = {}

    class FakeYDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            captured["url"] = url
            captured["download"] = download
            return {
                "_type": "playlist",
                "title": "测试合集",
                "entries": [
                    {
                        "url": "https://www.bilibili.com/video/BV123?p=1",
                        "playlist_index": 1,
                    },
                    {
                        "url": "https://www.bilibili.com/video/BV123?p=2",
                        "playlist_index": 2,
                    },
                ],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        "video_translator.collection._discover_bilibili_collection",
        lambda url, settings: None,
    )
    settings = Settings(data_dir=tmp_path)
    result = discover_collection(
        "https://www.bilibili.com/video/BV123/?p=68",
        settings,
    )

    assert result.title == "测试合集"
    assert result.total == 2
    assert [entry.index for entry in result.entries] == [1, 2]
    assert "p=68" not in captured["url"]
    assert captured["download"] is False
    assert captured["options"]["extract_flat"] == "in_playlist"
