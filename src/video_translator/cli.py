"""Command-line interface."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx

from .errors import VideoTranslatorError
from .manager import JobManager
from .models import PipelineOptions
from .runtime import resolve_media_binaries
from .settings import Settings, get_settings


def _print_check(label: str, ok: bool, detail: str) -> None:
    icon = "✓" if ok else "✗"
    print(f"{icon} {label:<16} {detail}")


def doctor(settings: Settings) -> int:
    failed = False
    print("Video Translator 环境检查\n")

    try:
        media = resolve_media_binaries(settings)
        _print_check("ffmpeg", True, media.ffmpeg)
        _print_check("ffprobe", True, media.ffprobe)
    except VideoTranslatorError as exc:
        failed = True
        _print_check("FFmpeg", False, str(exc))

    for package, label in (
        ("yt_dlp", "yt-dlp"),
        ("faster_whisper", "faster-whisper"),
        ("edge_tts", "edge-tts"),
    ):
        available = importlib.util.find_spec(package) is not None
        failed = failed or not available
        _print_check(
            label,
            available,
            "已安装" if available else "未安装",
        )

    deno = shutil.which("deno")
    venv_deno = Path(sys.prefix) / "bin" / "deno"
    deno = deno or (str(venv_deno) if venv_deno.is_file() else None)
    failed = failed or not deno
    _print_check("Deno / YouTube JS", bool(deno), deno or "未安装")

    if settings.translator_provider == "passthrough":
        _print_check("翻译服务", False, "passthrough 仅用于测试，不会翻译")
        failed = True
    else:
        models_url = (
            settings.translator_base_url.rstrip("/") + "/models"
        )
        try:
            headers = {}
            if settings.translator_api_key:
                headers["Authorization"] = (
                    f"Bearer {settings.translator_api_key}"
                )
            response = httpx.get(models_url, headers=headers, timeout=5)
            reachable = response.status_code < 500
            detail = (
                f"{settings.translator_model} @ "
                f"{settings.translator_base_url}"
            )
        except httpx.HTTPError as exc:
            reachable = False
            detail = str(exc)
        failed = failed or not reachable
        _print_check("翻译服务", reachable, detail)

    settings.ensure_directories()
    _print_check("数据目录", True, str(settings.data_dir))
    print()
    return 1 if failed else 0


def _load_glossary(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("术语表必须是 string:string 的 JSON 对象。")
    return value


def translate_command(args: argparse.Namespace, settings: Settings) -> int:
    options = PipelineOptions(
        target_language=args.target_language,
        source_language=args.source_language,
        keep_original_audio=not args.no_original_audio,
        burn_subtitles=args.burn_subtitles,
        glossary=_load_glossary(args.glossary),
    )
    manager = JobManager(settings)
    try:
        manifest = manager.run_sync(args.source, options)
    finally:
        manager.shutdown()
    print(f"\n处理完成：{manifest.output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-translator",
        description="视频翻译与中文配音工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="检查运行环境和服务配置")
    subparsers.add_parser(
        "bootstrap-media",
        help="下载项目本地的 ffmpeg/ffprobe",
    )

    translate = subparsers.add_parser(
        "translate",
        help="同步处理一个 URL 或本地视频",
    )
    translate.add_argument("source", help="YouTube/B站 URL 或本地视频路径")
    translate.add_argument(
        "--target-language",
        default="简体中文",
    )
    translate.add_argument("--source-language")
    translate.add_argument("--glossary", help="JSON 术语表路径")
    translate.add_argument("--no-original-audio", action="store_true")
    translate.add_argument("--burn-subtitles", action="store_true")

    serve = subparsers.add_parser("serve", help="启动 Web/API 服务")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    try:
        if args.command == "doctor":
            raise SystemExit(doctor(settings))
        if args.command == "bootstrap-media":
            media = resolve_media_binaries(settings, allow_download=True)
            print(f"ffmpeg:  {media.ffmpeg}")
            print(f"ffprobe: {media.ffprobe}")
            return
        if args.command == "translate":
            raise SystemExit(translate_command(args, settings))
        if args.command == "serve":
            import uvicorn
            from .api import app

            uvicorn.run(
                app,
                host=args.host or settings.api_host,
                port=args.port or settings.api_port,
            )
            return
    except (VideoTranslatorError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
