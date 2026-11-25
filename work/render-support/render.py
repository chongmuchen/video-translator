"""Synthesize new sentence-level Mandarin dubs for timed Sakurai B–H drafts."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from tempfile import TemporaryDirectory

from video_translator.models import JobManifest, JobStatus, PipelineOptions, Segment
from video_translator.pipeline import runner
from video_translator.pipeline.media import probe_media, write_srt
from video_translator.runtime import MediaBinaries, resolve_media_binaries
from video_translator.settings import PROJECT_ROOT, Settings
from video_translator.store import JobStore


WORK = Path(__file__).resolve().parent
REPO = PROJECT_ROOT
DELIVERY_ROOT = WORK.parent / "delivery"
if not DELIVERY_ROOT.is_dir():
    DELIVERY_ROOT = REPO / "data" / "outputs" / "樱井政博B-H系列-新配音"
LOGS = WORK / "logs" / "fresh"
CLOCK = r"(\d+):(\d{2}):(\d{2})[,.](\d{3})"
TIMESTAMP = re.compile(rf"^{CLOCK}\s+-->\s+{CLOCK}(?:\s+.*)?$")
JP_CODES = {"ja", "jpn", "japanese"}
MAX_AUDIO_DRIFT = .5

# Reuse the tested A-series draft parser and bounded synthesis adapter.
a_renderer = REPO / "work" / "render-support" / "a_render.py"
if not a_renderer.is_file():
    a_renderer = REPO / "work" / "sakurai-a" / "render.py"
spec = importlib.util.spec_from_file_location("sakurai_a_render", a_renderer)
if spec is None or spec.loader is None:
    raise RuntimeError("找不到 A 系列渲染脚本")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def safe_name(value: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", value).strip(" .")
    if not name:
        raise ValueError("无效的目录名称")
    return name


def destination(row: dict) -> tuple[Path, str]:
    category = safe_name(f'{row["category"]} {row["category_zh"]}')
    stem = safe_name(f'{row["code"]} {row["title_zh"]}')
    return DELIVERY_ROOT / category / stem, stem


def input_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("渲染产物的输入路径为空")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO / path
    if not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError(path)
    return path


def media_duration(probe: dict) -> float:
    value = float(probe["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("媒体时长无效")
    return value


def timestamp_ms(fields: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = map(int, fields)
    if not (minutes < 60 and seconds < 60):
        raise ValueError("字幕时码分钟或秒超限")
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + millis


def validate_srt(path: Path, duration: float) -> int:
    cues = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    previous_end = 0
    for index, cue in enumerate(cues, 1):
        lines = cue.splitlines()
        match = TIMESTAMP.fullmatch(lines[1].strip()) if len(lines) >= 3 else None
        if not match or lines[0].strip() != str(index) or not any(line.strip() for line in lines[2:]):
            raise ValueError(f"{path.name} 第 {index} 条字幕格式无效")
        start = timestamp_ms(match.groups()[:4])
        end = timestamp_ms(match.groups()[4:])
        if not (previous_end <= start < end <= round(duration * 1000) + 2):
            raise ValueError(f"{path.name} 第 {index} 条字幕超时或重叠")
        previous_end = end
    return len(cues)


def source_streams(
    source: Path, media: MediaBinaries, original_audio_index: int = 0,
) -> tuple[dict, dict, float]:
    info = probe_media(source, media)
    video, audio = a.streams_of(info, "video"), a.streams_of(info, "audio")
    if len(video) != 1 or len(audio) <= original_audio_index:
        raise ValueError("原片缺少可用的画面或日语音轨")
    original = audio[original_audio_index]
    language = original.get("tags", {}).get("language", "").lower()
    if language not in JP_CODES:
        raise ValueError(f"输入的第 {original_audio_index + 1} 条音轨是 {language}，需要日语原声")
    return video[0], original, media_duration(info)


def source_for(row: dict) -> Path:
    source = WORK / "sources" / f'{row["id"]}.mp4'
    if not source.is_file() or not source.stat().st_size:
        raise FileNotFoundError(f'原片缺失 {row["id"]}')
    return source


def official_english_path(code: str) -> Path:
    folder = WORK / "en-ocr" / code
    mapped = folder / "mapped-cues.json"
    return mapped if mapped.is_file() else folder / "cues.json"


def official_english_segments(path: Path, duration: float, supplements: list[dict]) -> list[Segment]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path} 缺少官方英语字幕")
    if not isinstance(supplements, list):
        raise ValueError("补充英语字幕必须为数组")
    rows = sorted([*rows, *supplements], key=lambda row: float(row["start"]))
    segments = []
    prior_end = 0.0
    for index, row in enumerate(rows):
        start, end = round(float(row["start"]), 3), round(float(row["end"]), 3)
        text = row["en"].strip()
        if end > duration and end - duration <= 0.30:
            end = round(duration, 3)
        if not text or not prior_end <= start < end <= round(duration, 3):
            raise ValueError(f"{path} 第 {index + 1} 条字幕文本或时间无效")
        segments.append(Segment(index=index, start=start, end=end, source_text=text))
        prior_end = end
    return segments


def original_audio_windows(rows: list[dict], narration: list[Segment], duration: float) -> list[tuple[float, float]]:
    windows = []
    prior_end = 0.0
    for row in rows:
        start, end = round(float(row["start"]), 3), round(float(row["end"]), 3)
        if not (prior_end <= start < end <= duration) or any(
            start < segment.end and segment.start < end for segment in narration
        ):
            raise ValueError(f"原声保留区间无效或与解说重叠：{start}–{end}")
        windows.append((start, end))
        prior_end = end
    return windows


class FreshDubPipeline(runner.VideoTranslationPipeline):
    """Export timeline assets once; the delivery mux adds both verified audio tracks."""

    def __init__(self, settings: Settings, store: JobStore, official_english: list[Segment] | None):
        super().__init__(settings, store)
        self.official_english = official_english

    def _export(
        self, manifest, segments, chinese_cues, english, source, dub_audio, background_audio, media, logger,
    ) -> None:
        folder = self.store.job_dir(manifest.id)
        chinese_srt = write_srt(chinese_cues, folder / "dub.zh-CN.srt")
        english_cues = self.official_english
        if english_cues is None:
            english_cues = [
                segment.model_copy(update={"source_text": english[segment.index]})
                for segment in segments
            ]
        english_srt = write_srt(english_cues, folder / "dub.en.srt", source_text=True)
        manifest.subtitle_path = str(chinese_srt)
        manifest.english_subtitle_path = str(english_srt)


def install_bundle(bundle: Path, output: Path, replace: bool) -> None:
    if not replace or not output.exists():
        bundle.rename(output)
        return
    if not output.is_dir():
        raise ValueError(f"交付路径不是目录：{output}")
    # macOS exchanges two directories in one operation on the same volume.
    if sys.platform == "darwin":
        swap = ctypes.CDLL(None, use_errno=True).renamex_np
        swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        swap.restype = ctypes.c_int
        if swap(os.fsencode(bundle), os.fsencode(output), 0x00000002) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(output))
    else:
        previous = bundle.with_name("previous")
        output.rename(previous)
        try:
            bundle.rename(output)
        except BaseException:
            previous.rename(output)
            raise


def package(
    inputs: dict, output: Path, stem: str, media: MediaBinaries, *,
    replace: bool = False, original_audio_index: int = 0,
) -> dict:
    source, voice, zh, en = (input_path(inputs[field]) for field in ("source", "voice", "zh_srt", "en_srt"))
    video, japanese, duration = source_streams(source, media, original_audio_index)
    voice_info = probe_media(voice, media)
    voice_duration = media_duration(voice_info)
    if len(a.streams_of(voice_info, "audio")) != 1 or abs(voice_duration - duration) > MAX_AUDIO_DRIFT:
        raise ValueError(f"中文音轨未对齐：{voice_duration:.3f}s，原片 {duration:.3f}s")
    cue_counts = {label: validate_srt(path, duration) for label, path in (("zh", zh), ("en", en))}

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix=f".{stem[:3]}-") as temp:
        bundle = Path(temp) / "bundle"
        bundle.mkdir()
        mp4, zh_out, en_out = a.filenames(bundle, stem)
        codec = ["-c:v", "copy"] if video["codec_name"] == "h264" else [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
        ]
        duration_filter = f"apad,atrim=duration={duration:.6f}"
        windows = inputs.get("original_audio_windows", [])
        original_audio = f"0:a:{original_audio_index}"
        if windows:
            # Fade only the original soundtrack. These intervals have no narration.
            gain = "+".join(
                f"clip(min((t-{start:.3f})/0.35,({end:.3f}-t)/0.35),0,1)"
                for start, end in windows
            )
            filter_args = [
                "-filter_complex",
                f"[1:a:0]{duration_filter}[dub];"
                f"[{original_audio}]volume='{gain}':eval=frame[bed];"
                "[bed][dub]amix=inputs=2:duration=longest:normalize=0,"
                f"{duration_filter}[mandarin]",
            ]
            mandarin = "[mandarin]"
        else:
            filter_args = ["-filter:a:0", duration_filter]
            mandarin = "1:a:0"
        command = [
            media.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-i", str(voice),
            "-map", "0:v:0", "-map", mandarin, "-map", original_audio,
            *codec, "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "copy",
            *filter_args,
            "-metadata:s:a:0", "language=zho", "-metadata:s:a:0", "handler_name=中文配音",
            "-metadata:s:a:1", "language=jpn", "-metadata:s:a:1", "handler_name=日语原声",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-movflags", "+faststart", str(mp4),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        shutil.copyfile(zh, zh_out)
        shutil.copyfile(en, en_out)

        result = probe_media(mp4, media)
        video_out = a.streams_of(result, "video")
        audio = a.streams_of(result, "audio")
        if len(video_out) != 1 or video_out[0]["codec_name"] != "h264":
            raise ValueError("输出不是 H.264 视频")
        if [video_out[0].get(k) for k in ("width", "height")] != [video.get(k) for k in ("width", "height")]:
            raise ValueError("输出分辨率与原片不同")
        if (
            len(audio) != 2 or a.streams_of(result, "subtitle")
            or [track.get("tags", {}).get("language") for track in audio] != ["zho", "jpn"]
            or [track.get("disposition", {}).get("default") for track in audio] != [1, 0]
            or audio[1]["codec_name"] != japanese["codec_name"]
        ):
            raise ValueError("输出未保留默认中文和可选日语两条音轨")
        if abs(media_duration(result) - duration) > MAX_AUDIO_DRIFT:
            raise ValueError("封装时长与原片不同")
        install_bundle(bundle, output, replace)
    return {
        "duration": duration,
        "height": video["height"],
        "width": video["width"],
        "cues": cue_counts,
        "files": [str(path) for path in a.filenames(output, stem)],
    }


def rerender(
    row: dict, draft: Path, stem: str, settings: Settings, store: JobStore, media: MediaBinaries, *,
    source: Path | None = None, original_audio_index: int = 0,
) -> dict:
    video_id = row["id"]
    if source is None:
        source = source_for(row)
    _, _, duration = source_streams(source, media, original_audio_index)
    original, translations = a.parse_draft(draft, duration)
    official_english = None
    english_signature = None
    payload = json.loads(draft.read_text(encoding="utf-8"))
    windows = original_audio_windows(payload.get("original_audio_windows", []), original, duration)
    if payload.get("english_source") == "official-ocr":
        english_path = official_english_path(row["code"])
        official_english = official_english_segments(english_path, duration, payload.get("supplemental_english", []))
        english_signature = hashlib.sha256(english_path.read_bytes()).hexdigest()
    signature = [
        hashlib.sha256(draft.read_bytes()).hexdigest(), source.stat().st_size, source.stat().st_mtime_ns,
        settings.tts_provider, settings.tts_voice, settings.tts_rate, english_signature,
    ]
    job_id = fresh_job_id(video_id)
    try:
        manifest = store.get(job_id)
    except FileNotFoundError:
        manifest = JobManifest(
            id=job_id, source=f"https://www.youtube.com/watch?v={video_id}",
            options=PipelineOptions(source_language="ja", keep_original_audio=True, burn_subtitles=False),
        )
    if not (
        manifest.status == JobStatus.completed
        and manifest.metadata.get("input_signature") == signature
        and all(path and Path(path).is_file() for path in (
            manifest.dub_audio_path, manifest.subtitle_path, manifest.english_subtitle_path
        ))
    ):
        store.job_dir(job_id).mkdir(exist_ok=True)
        manifest.title = stem
        manifest.source_path = str(source)
        manifest.metadata.update(
            detected_language="ja", media_duration=duration, input_signature=signature,
        )
        store.write_segments(manifest, [segment.model_dump(mode="json") for segment in original])
        store.write_translations(manifest, translations)
        store.set_stage(manifest, JobStatus.prepared, 35, "字幕和译稿已准备")
        manifest = FreshDubPipeline(settings, store, official_english).render(manifest)
    return {
        "source": str(source),
        "voice": manifest.dub_audio_path,
        "zh_srt": manifest.subtitle_path,
        "en_srt": manifest.english_subtitle_path,
        "original_audio_windows": windows,
    }


def fresh_job_id(video_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://youtu.be/{video_id}#sakurai-b-h-fresh").hex


def preview_episode(
    row: dict, preview_root: Path, draft: Path | None, tts_workers: int | None,
) -> dict:
    final_folder, stem = destination(row)
    source = a.filenames(final_folder, stem)[0]
    output = preview_root.expanduser().resolve() / stem
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"预览目录已存在：{output}")
    if not source.is_file():
        raise FileNotFoundError(f"缺少作为修复源的已交付成片：{source}")
    if draft is None:
        draft = WORK / "drafts" / f'{row["code"]}.json'
    draft = draft.expanduser().resolve()
    if not draft.is_file():
        raise FileNotFoundError(f"缺少译稿：{draft}")

    job_id = fresh_job_id(row["id"])
    cache = WORK / "pipeline-fresh" / "jobs" / job_id / "tts"
    with TemporaryDirectory(prefix="sakurai-repair-") as directory:
        settings = Settings(data_dir=Path(directory), enable_demucs=False)
        if tts_workers is not None:
            settings.tts_threads = tts_workers
        store = JobStore(settings)
        media = resolve_media_binaries(settings)
        if cache.is_dir():
            shutil.copytree(cache, store.job_dir(job_id) / "tts")
        inputs = rerender(
            row, draft, stem, settings, store, media,
            source=source, original_audio_index=1,
        )
        result = package(inputs, output, stem, media, original_audio_index=1)
    return {"code": row["code"], "status": "preview", **result}


def episode(row: dict, settings: Settings, store: JobStore, media: MediaBinaries, replace: bool = False) -> dict:
    code = row["code"]
    output, stem = destination(row)
    log = LOGS / f"{code}-render.json"
    result = {"code": code}
    with (LOGS / f"{code}.render.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"code": code, "status": "busy"}
        try:
            paths = a.filenames(output, stem)
            if output.exists() and not replace:
                if not all(path.is_file() and path.stat().st_size for path in paths):
                    raise ValueError(f"交付目录已存在，但文件不完整：{output}")
                result.update(status="skipped", files=[str(path) for path in paths])
                return result
            draft = WORK / "drafts" / f"{code}.json"
            if not draft.is_file():
                return {"code": code, "status": "not-ready"}
            inputs = rerender(row, draft, stem, settings, store, media)
            previous = None
            if replace and log.is_file():
                receipt = json.loads(log.read_text(encoding="utf-8"))
                if receipt.get("status") == "complete":
                    previous = Path(receipt["files"][0]).parent
            result.update(status="complete", mode="sentence-dub", **package(inputs, output, stem, media, replace=replace))
            if (
                previous is not None and previous != output and previous.is_dir()
                and previous.parent == output.parent and previous.name.startswith(f"{code} ")
            ):
                archive = WORK / "superseded"
                archive.mkdir(exist_ok=True)
                previous.rename(archive / f"{previous.name} [{uuid.uuid4().hex[:8]}]")
        except subprocess.CalledProcessError as exc:
            result.update(status="failed", error=(exc.stderr or str(exc))[-2000:])
        except Exception as exc:
            result.update(status="failed", error=str(exc))
            job_id = fresh_job_id(row["id"])
            try:
                manifest = store.get(job_id)
                if manifest.status == JobStatus.failed:
                    result.update(job_id=job_id, overflow_segments=manifest.overflow_segments)
            except FileNotFoundError:
                pass
        a.atomic_json(log, result)
    return result


def watch_signature(row: dict) -> tuple[int, ...] | None:
    code = row["code"]
    source = WORK / "sources" / f'{row["id"]}.mp4'
    draft = WORK / "drafts" / f"{code}.json"
    if not source.with_suffix(".jpn").is_file():
        return None
    try:
        revision = [source.stat().st_mtime_ns, draft.stat().st_mtime_ns]
        payload = json.loads(draft.read_text(encoding="utf-8"))
        if payload.get("english_source") == "official-ocr":
            revision.append(official_english_path(code).stat().st_mtime_ns)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return tuple(revision)


def watch_drafts(rows: list[dict], pool: ThreadPoolExecutor, workers: int, run, idle_timeout: int) -> bool:
    remaining = {row["code"]: row for row in rows}
    active = {}
    attempted = {}
    deadline = time.monotonic() + idle_timeout
    while remaining or active:
        if active:
            done, _ = wait(active, timeout=0, return_when=FIRST_COMPLETED)
            for future in done:
                code = active.pop(future)
                result = future.result()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result["status"] in {"complete", "skipped"}:
                    remaining.pop(code)
                elif result["status"] in {"busy", "not-ready"}:
                    attempted.pop(code, None)
                deadline = time.monotonic() + idle_timeout
        running = set(active.values())
        for code, row in list(remaining.items()):
            if len(active) >= workers:
                break
            if code in running:
                continue
            output, stem = destination(row)
            if all(path.is_file() and path.stat().st_size for path in a.filenames(output, stem)):
                remaining.pop(code)
                continue
            signature = watch_signature(row)
            if signature is None or attempted.get(code) == signature:
                continue
            active[pool.submit(run, row)] = code
            attempted[code] = signature
            deadline = time.monotonic() + idle_timeout
        if not active and time.monotonic() >= deadline:
            break
        if remaining or active:
            time.sleep(2)
    if remaining:
        print(json.dumps({"status": "waiting", "remaining": list(remaining)}, ensure_ascii=False), flush=True)
    return bool(remaining)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", help="B01 C01 …; omit to render available drafts")
    parser.add_argument("--jobs", type=int, default=4, help="parallel packaging jobs")
    parser.add_argument("--tts-workers", type=int, help="Kokoro CPU threads")
    parser.add_argument("--watch", action="store_true", help="wait for drafts and verified Japanese media")
    parser.add_argument("--idle-timeout", type=int, default=1200, help="maximum idle seconds in watch mode")
    parser.add_argument("--replace", action="store_true", help="replace one delivered episode after verification")
    parser.add_argument("--preview", type=Path, help="render one episode from its final video into this directory")
    parser.add_argument("--draft", type=Path, help="use an edited translation file with --preview")
    args = parser.parse_args()
    if min(args.jobs, args.idle_timeout) < 1 or (args.tts_workers is not None and args.tts_workers < 1):
        parser.error("worker counts and idle timeout must be positive")
    if args.replace and (args.watch or len(args.codes) != 1):
        parser.error("--replace requires exactly one explicit episode code")
    if args.preview is not None and (args.replace or args.watch or len(args.codes) != 1):
        parser.error("--preview requires exactly one explicit episode code")
    if args.draft is not None and args.preview is None:
        parser.error("--draft requires --preview")
    catalog = json.loads((WORK / "catalog.json").read_text(encoding="utf-8"))
    lookup = {row["code"]: row for row in catalog}
    codes = list(dict.fromkeys(code.upper() for code in args.codes)) or [
        row["code"] for row in catalog
        if args.watch or (WORK / "drafts" / f'{row["code"]}.json').is_file()
    ]
    if not codes or any(code not in lookup for code in codes):
        parser.error("no ready episodes or invalid code")
    if args.preview is not None:
        try:
            result = preview_episode(lookup[codes[0]], args.preview, args.draft, args.tts_workers)
        except subprocess.CalledProcessError as error:
            parser.exit(1, f"{(error.stderr or str(error))[-2000:]}\n")
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"{error}\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    LOGS.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=WORK / "pipeline-fresh", enable_demucs=False)
    if args.tts_workers is not None:
        settings.tts_threads = args.tts_workers
    store = JobStore(settings)
    media = resolve_media_binaries(settings)
    failed = False
    with ThreadPoolExecutor(max_workers=args.jobs) as jobs:
        if args.watch:
            def run(row: dict) -> dict:
                return episode(row, settings, store, media)

            rows = [lookup[code] for code in codes]
            return int(watch_drafts(rows, jobs, args.jobs, run, args.idle_timeout))
        pending = [jobs.submit(episode, lookup[code], settings, store, media, args.replace) for code in codes]
        for future in as_completed(pending):
            result = future.result()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            failed |= result["status"] in {"failed", "not-ready"}
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
