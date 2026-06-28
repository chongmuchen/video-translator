#!/usr/bin/env python3
"""Step-by-step entry point for Video Translator.

Implemented flow:
    download -> extract -> transcribe -> translate -> synthesize -> align -> mux

Run ``.venv/bin/python main.py plan`` to see implemented stages and TODO items.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if sys.version_info < (3, 10):
    raise SystemExit(
        "需要 Python 3.10+。请使用：.venv/bin/python main.py <command>"
    )

try:
    from video_translator.cli import doctor
    from video_translator.errors import VideoTranslatorError
    from video_translator.models import JobManifest, PipelineOptions
    from video_translator.pipeline.stepwise import (
        STEP_DESCRIPTIONS,
        STEP_ORDER,
        TODO_ITEMS,
        PipelineStep,
        StepwiseVideoTranslationPipeline,
    )
    from video_translator.settings import Settings, get_settings
    from video_translator.store import JobStore
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"缺少依赖 {exc.name!r}。请先运行 ./scripts/bootstrap.sh，"
        "然后使用 .venv/bin/python main.py。"
    ) from exc


def load_glossary(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("术语表必须是 string:string 的 JSON 对象。")
    return value


def pipeline_options(args: argparse.Namespace) -> PipelineOptions:
    return PipelineOptions(
        target_language=getattr(args, "target_language", "简体中文"),
        source_language=getattr(args, "source_language", None),
        keep_original_audio=not getattr(args, "no_original_audio", False),
        burn_subtitles=getattr(args, "burn_subtitles", False),
        glossary=load_glossary(getattr(args, "glossary", None)),
    )


def configured_settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    updates = {}
    cookies_from_browser = getattr(args, "cookies_from_browser", None)
    if cookies_from_browser:
        updates["cookies_from_browser"] = cookies_from_browser
    cookie_file = getattr(args, "cookie_file", None)
    if cookie_file:
        updates["cookie_file"] = Path(cookie_file).expanduser().resolve()
    download_backend = getattr(args, "download_backend", None)
    if download_backend:
        updates["download_backend"] = download_backend
    download_proxy = getattr(args, "proxy", None)
    if download_proxy is not None:
        updates["download_proxy"] = "" if download_proxy == "direct" else download_proxy
    impersonate = getattr(args, "impersonate", None)
    if impersonate:
        updates["download_impersonate"] = impersonate
    if updates:
        settings = settings.model_copy(update=updates)
        settings.ensure_directories()
    return settings


def load_sources(args: argparse.Namespace) -> list[str]:
    sources = list(getattr(args, "sources", []) or [])
    source_file = getattr(args, "file", None)
    if source_file:
        for raw_line in Path(source_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                sources.append(line)
    if not sources:
        raise ValueError("请至少提供一个 URL/本地视频，或使用 --file。")
    return sources


def print_job(manifest: JobManifest, pipeline: StepwiseVideoTranslationPipeline) -> None:
    next_step = pipeline.next_step(manifest)
    completed = ", ".join(manifest.completed_steps) or "无"
    print(f"\n任务 ID:    {manifest.id}")
    print(f"标题:       {manifest.title or '-'}")
    print(f"状态:       {manifest.status.value}")
    print(f"进度:       {manifest.progress}%")
    print(f"已完成步骤: {completed}")
    print(f"下一步:     {next_step.value if next_step else '无，任务已完成'}")
    if manifest.source_path:
        print(f"视频:       {manifest.source_path}")
    if manifest.audio_path:
        print(f"识别音频:   {manifest.audio_path}")
    if manifest.segments_path:
        print(f"片段数据:   {manifest.segments_path}")
    if manifest.subtitle_path:
        print(f"字幕:       {manifest.subtitle_path}")
    if manifest.dub_audio_path:
        print(f"配音时间轴: {manifest.dub_audio_path}")
    if manifest.output_path:
        print(f"最终视频:   {manifest.output_path}")
    if manifest.error:
        print(f"错误:       {manifest.error}")
    if next_step:
        print(
            "继续命令:   "
            f".venv/bin/python main.py next {manifest.id}"
        )


def command_plan() -> int:
    print("已实现的端到端流程：\n")
    for number, step in enumerate(STEP_ORDER, start=1):
        print(f"  {number}. {step.value:<12} {STEP_DESCRIPTIONS[step]}")
    print("\n尚未完成的 TODO：\n")
    for priority, item in TODO_ITEMS:
        print(f"  [{priority}] {item}")
    return 0


def command_download(args: argparse.Namespace) -> int:
    settings = configured_settings(args)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    options = pipeline_options(args)
    failures = 0

    for source in load_sources(args):
        manifest = store.create(source, options)
        print(f"\n开始下载：{source}")
        print(f"任务 ID：{manifest.id}")
        try:
            pipeline.run_step(manifest, PipelineStep.download)
        except Exception as exc:
            failures += 1
            print(f"下载失败：{exc}", file=sys.stderr)
            print(f"查看日志：{store.job_dir(manifest.id) / 'pipeline.log'}")
            continue
        print_job(manifest, pipeline)
    return 1 if failures else 0


def command_run(args: argparse.Namespace) -> int:
    settings = configured_settings(args)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.create(args.source, pipeline_options(args))
    print(f"任务 ID：{manifest.id}")
    try:
        pipeline.run_all(manifest)
    finally:
        print_job(store.get(manifest.id), pipeline)
    return 0


def command_resume(args: argparse.Namespace) -> int:
    settings = configured_settings(args)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.get(args.job_id)
    try:
        pipeline.run_all(manifest)
    finally:
        print_job(store.get(args.job_id), pipeline)
    return 0


def command_step(args: argparse.Namespace, *, next_only: bool = False) -> int:
    settings = configured_settings(args)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.get(args.job_id)
    try:
        if next_only:
            pipeline.run_next(manifest)
        else:
            pipeline.run_step(
                manifest,
                PipelineStep(args.step),
                force=args.force,
            )
    finally:
        print_job(store.get(args.job_id), pipeline)
    return 0


def command_status(args: argparse.Namespace) -> int:
    settings = configured_settings(args)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    job_ids = list(args.job_ids)
    if not job_ids:
        manifests = sorted(
            settings.jobs_dir.glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        job_ids = [path.parent.name for path in manifests[:20]]
    if not job_ids:
        print("还没有任务。")
        return 0
    for job_id in job_ids:
        print_job(store.get(job_id), pipeline)
    return 0


def add_download_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cookies-from-browser",
        help="读取浏览器 Cookies，如 chrome、firefox、safari",
    )
    parser.add_argument(
        "--cookie-file",
        help="Netscape 格式 cookies.txt 文件",
    )
    parser.add_argument(
        "--download-backend",
        choices=["auto", "native", "curl"],
        help="媒体下载后端；auto 会为 B站选择 curl",
    )
    parser.add_argument(
        "--proxy",
        help="覆盖系统代理 URL；传 direct 表示直连",
    )
    parser.add_argument(
        "--impersonate",
        help="模拟浏览器 TLS，如 chrome",
    )


def add_pipeline_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-language", help="源语言，如 en、ja")
    parser.add_argument("--target-language", default="简体中文")
    parser.add_argument("--glossary", help="JSON 术语表")
    parser.add_argument("--no-original-audio", action="store_true")
    parser.add_argument("--burn-subtitles", action="store_true")
    add_download_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="分步执行视频下载、翻译、配音和封装",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  .venv/bin/python main.py plan\n"
            "  .venv/bin/python main.py download URL1 URL2\n"
            "  .venv/bin/python main.py next JOB_ID\n"
            "  .venv/bin/python main.py step JOB_ID transcribe\n"
            "  .venv/bin/python main.py resume JOB_ID\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("plan", help="显示已实现流程和 TODO")
    subparsers.add_parser("doctor", help="检查运行环境")

    download = subparsers.add_parser(
        "download",
        help="只下载视频；支持一次提交多个 URL",
    )
    download.add_argument("sources", nargs="*", help="URL 或本地视频路径")
    download.add_argument(
        "--file",
        help="每行一个 URL 的 UTF-8 文本文件，# 开头为注释",
    )
    add_pipeline_options(download)

    run = subparsers.add_parser("run", help="新建任务并执行完整流程")
    run.add_argument("source", help="URL 或本地视频路径")
    add_pipeline_options(run)

    resume = subparsers.add_parser("resume", help="从断点继续完整流程")
    resume.add_argument("job_id")
    add_download_options(resume)

    next_parser = subparsers.add_parser("next", help="只执行下一个未完成步骤")
    next_parser.add_argument("job_id")
    add_download_options(next_parser)

    step = subparsers.add_parser("step", help="执行指定步骤")
    step.add_argument("job_id")
    step.add_argument("step", choices=[item.value for item in STEP_ORDER])
    step.add_argument("--force", action="store_true", help="强制重跑并使后续步骤失效")
    add_download_options(step)

    status = subparsers.add_parser("status", help="显示一个或最近的任务状态")
    status.add_argument("job_ids", nargs="*")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "plan":
            code = command_plan()
        elif args.command == "doctor":
            code = doctor(configured_settings(args))
        elif args.command == "download":
            code = command_download(args)
        elif args.command == "run":
            code = command_run(args)
        elif args.command == "resume":
            code = command_resume(args)
        elif args.command == "next":
            code = command_step(args, next_only=True)
        elif args.command == "step":
            code = command_step(args)
        elif args.command == "status":
            code = command_status(args)
        else:
            raise AssertionError(args.command)
    except (VideoTranslatorError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
