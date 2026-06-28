"""Bootstrap project-local runtime dependencies."""

from __future__ import annotations

from .runtime import resolve_media_binaries
from .settings import get_settings


def main() -> None:
    settings = get_settings()
    binaries = resolve_media_binaries(settings, allow_download=True)
    print(f"ffmpeg:  {binaries.ffmpeg}")
    print(f"ffprobe: {binaries.ffprobe}")


if __name__ == "__main__":
    main()

