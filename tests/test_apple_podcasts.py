import pytest

from video_translator.apple_podcasts import (
    PodcastEpisode,
    PodcastFeed,
    apple_episode_id,
    apple_show_id,
    match_apple_episode,
)
from video_translator.errors import InvalidSourceError


APPLE_EPISODE_URL = (
    "https://podcasts.apple.com/us/podcast/"
    "207-whitney-webb-returns/id1135137367?i=1000482637777"
)


def test_extracts_apple_show_and_episode_ids() -> None:
    assert apple_show_id(APPLE_EPISODE_URL) == "1135137367"
    assert apple_episode_id(APPLE_EPISODE_URL) == "1000482637777"


def test_show_id_is_required() -> None:
    with pytest.raises(InvalidSourceError):
        apple_show_id("https://podcasts.apple.com/us/podcast/example")


def test_matches_episode_slug_to_rss_title() -> None:
    expected = PodcastEpisode(
        index=2,
        title="207 - Whitney Webb Returns",
        audio_url="https://cdn.example/episode.mp3",
        guid="episode-207",
        published=None,
        duration="01:00:00",
        media_type="audio/mpeg",
    )
    feed = PodcastFeed(
        show_id="1135137367",
        title="Example",
        feed_url="https://example.com/feed.xml",
        episodes=[
            PodcastEpisode(
                index=1,
                title="Another Episode",
                audio_url="https://cdn.example/other.mp3",
                guid="other",
                published=None,
                duration=None,
                media_type="audio/mpeg",
            ),
            expected,
        ],
    )
    assert match_apple_episode(APPLE_EPISODE_URL, feed) == expected


def test_show_url_does_not_guess_an_episode() -> None:
    feed = PodcastFeed(
        show_id="1135137367",
        title="Example",
        feed_url="https://example.com/feed.xml",
        episodes=[],
    )
    assert (
        match_apple_episode(
            "https://podcasts.apple.com/us/podcast/example/id1135137367",
            feed,
        )
        is None
    )

