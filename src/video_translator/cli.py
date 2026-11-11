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
from .models import JobManifest, PipelineOptions
from .pipeline.runner import VideoTranslationPipeline
from .runtime import resolve_media_binaries
from .settings import Settings, get_settings
from .store import JobStore


def _print_check(label: str, ok: bool, detail: str) -> None:
    icon = "✓" if ok else "✗"
    print(f"{icon} {label:<16} {detail}")


def doctor(settings: Settings, *, check_translator: bool = False) -> int:
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

    if not check_translator:
        print("- 翻译服务         导入译稿时无需配置；用 --check-translator 检查自动翻译")
    elif settings.translator_provider == "passthrough":
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
            reachable = response.is_success
            detail = (
                f"{settings.translator_model} /models HTTP {response.status_code}"
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            reachable = False
            detail = type(exc).__name__
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


def _target_language(value: str) -> str:
    normalized = " ".join(value.casefold().replace("_", "-").split())
    if normalized not in {
        "简体中文", "中文", "普通话", "汉语", "简体",
        "zh", "zh-cn", "zh-hans", "zh-hans-cn",
        "chinese", "simplified chinese", "chinese (simplified)",
        "mandarin", "mandarin chinese",
    }:
        raise ValueError("--target-language 仅支持简体中文，例如 zh-CN 或 简体中文。")
    return "简体中文"


def _print_job_result(
    args: argparse.Namespace,
    store: JobStore,
    manifest: JobManifest | None,
    error: str | None = None,
) -> None:
    job_id = manifest.id if manifest else getattr(args, "job_id", None)
    result: dict[str, object] = {"job_id": job_id}
    if manifest:
        fields = {
            "status", "failed_stage", "error", "segments_path",
            "translations_path", "overflow_segments",
        }
        if not error:
            fields.update({"output_path", "subtitle_path", "english_subtitle_path"})
        result.update(manifest.model_dump(mode="json", include=fields))
        result["log_path"] = str(store.job_dir(manifest.id) / "pipeline.log")
        result["detected_language"] = manifest.metadata.get("detected_language")
    if error:
        result.update(status="failed", error=error)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif error:
        if job_id:
            print(f"任务 ID：{job_id}", file=sys.stderr)
        if result.get("failed_stage"):
            print(f"失败阶段：{result['failed_stage']}", file=sys.stderr)
        if result.get("log_path"):
            print(f"日志：{result['log_path']}", file=sys.stderr)
        print(f"错误：{error}", file=sys.stderr)
    elif manifest:
        print(f"任务 ID：{manifest.id}")
        if manifest.output_path:
            print(f"处理完成：{manifest.output_path}")
            print(f"中文字幕：{manifest.subtitle_path}")
            print(f"英文字幕：{manifest.english_subtitle_path}")
        else:
            print(f"转写稿：{manifest.segments_path}")
            print(f"译稿路径：{manifest.translations_path}")


def _job_command(args: argparse.Namespace, settings: Settings) -> int:
    store = JobStore(settings)
    pipeline = VideoTranslationPipeline(settings, store)
    manifest: JobManifest | None = None
    try:
        if args.command == "render":
            try:
                manifest = store.get(args.job_id)
            except FileNotFoundError as exc:
                raise VideoTranslatorError(f"找不到任务 {args.job_id}。") from exc
            translations = args.translations.expanduser() if args.translations else None
            manifest = pipeline.render(manifest, translations=translations)
        else:
            options = PipelineOptions(
                target_language=_target_language(getattr(args, "target_language", "简体中文")),
                source_language=args.source_language,
                keep_original_audio=False if args.no_original_audio else None,
                burn_subtitles=getattr(args, "burn_subtitles", False),
                glossary=_load_glossary(args.glossary),
            )
            manifest = store.create(args.source, options)
            if args.command == "prepare":
                manifest = pipeline.prepare(manifest)
            else:
                manifest = pipeline.run(manifest)
    except Exception as exc:
        if manifest:
            try:
                manifest = store.get(manifest.id)
            except (VideoTranslatorError, ValueError, OSError):
                pass
        _print_job_result(args, store, manifest, str(exc) or type(exc).__name__)
        return 1
    _print_job_result(args, store, manifest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-translator",
        description="视频翻译与中文配音工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查运行环境和服务配置")
    doctor_parser.add_argument(
        "--check-translator",
        action="store_true",
        help="同时检查自动翻译服务的 /models 接口",
    )
    subparsers.add_parser(
        "bootstrap-media",
        help="下载项目本地的 ffmpeg/ffprobe",
    )

    prepare = subparsers.add_parser(
        "prepare",
        help="下载或复制视频，生成带时间戳的转写稿",
    )
    render = subparsers.add_parser(
        "render",
        help="导入译稿，生成中文配音和中英文外置字幕",
    )
    render.add_argument("job_id", help="prepare 输出的任务 ID")
    render.add_argument(
        "--translations",
        type=Path,
        help="译稿 JSON 路径，默认使用任务目录内的 translations.json",
    )

    translate = subparsers.add_parser(
        "translate",
        help="通过配置的翻译服务处理一个 URL 或本地视频",
    )
    for source_command in (prepare, translate):
        source_command.add_argument("source", help="YouTube/B站 URL 或本地视频路径")
        source_command.add_argument("--source-language")
        source_command.add_argument("--glossary", help="JSON 术语表路径")
        source_command.add_argument("--no-original-audio", action="store_true")
    for job_command in (prepare, render, translate):
        job_command.add_argument(
            "--json", action="store_true", help="将任务结果以 JSON 输出到 stdout"
        )
    translate.add_argument(
        "--target-language",
        default="简体中文",
        help="配音语言，仅支持简体中文（例如 zh-CN）",
    )
    translate.add_argument(
        "--burn-subtitles", action="store_true", help="另将中文字幕烧录到画面"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.command == "doctor":
            raise SystemExit(doctor(settings, check_translator=args.check_translator))
        if args.command == "bootstrap-media":
            media = resolve_media_binaries(settings, allow_download=True)
            print(f"ffmpeg:  {media.ffmpeg}")
            print(f"ffprobe: {media.ffprobe}")
            return
        raise SystemExit(_job_command(args, settings))
    except (VideoTranslatorError, ValueError, OSError) as exc:
        if getattr(args, "json", False):
            result = {
                "job_id": getattr(args, "job_id", None),
                "status": "failed",
                "error": str(exc),
            }
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
