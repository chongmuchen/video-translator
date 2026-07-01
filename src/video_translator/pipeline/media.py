"""FFmpeg-based extraction, alignment, subtitles, and muxing."""

from __future__ import annotations

import json
import logging
import math
import sys
import wave
from pathlib import Path

from ..commands import run_command
from ..errors import PipelineError
from ..models import Segment
from ..runtime import MediaBinaries
from ..settings import Settings


def probe_media(path: Path, media: MediaBinaries) -> dict:
    result = run_command(
        [
            media.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ]
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe 返回了无效 JSON：{exc}") from exc


def probe_duration(path: Path, media: MediaBinaries) -> float:
    data = probe_media(path, media)
    duration = data.get("format", {}).get("duration")
    if duration is None:
        for stream in data.get("streams", []):
            if stream.get("duration") is not None:
                duration = stream["duration"]
                break
    try:
        value = float(duration)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"无法读取媒体时长：{path}") from exc
    if value <= 0:
        raise PipelineError(f"媒体时长无效：{path}")
    return value


def has_audio_stream(path: Path, media: MediaBinaries) -> bool:
    data = probe_media(path, media)
    return any(
        stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )


def has_video_stream(path: Path, media: MediaBinaries) -> bool:
    data = probe_media(path, media)
    return any(
        stream.get("codec_type") == "video"
        for stream in data.get("streams", [])
    )


def extract_speech_audio(
    source: Path,
    output: Path,
    media: MediaBinaries,
    logger: logging.Logger,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-i",
            source,
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            output,
        ],
        logger=logger,
    )
    return output


def extract_original_audio(
    source: Path,
    output: Path,
    media: MediaBinaries,
    logger: logging.Logger,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-i",
            source,
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            output,
        ],
        logger=logger,
    )
    return output


def atempo_chain(factor: float) -> str:
    """Create a quality-oriented atempo chain using factors in [0.5, 2]."""

    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    parts: list[float] = []
    remaining = factor
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    parts.append(remaining)
    return ",".join(f"atempo={part:.6f}" for part in parts)


def select_tempo_factor(
    source_duration: float,
    target_duration: float,
    maximum: float,
) -> float:
    """Return 1x unless a clip needs bounded acceleration to fit."""

    required = source_duration / max(target_duration, 0.05)
    return 1.0 if required <= 1.02 else min(required, maximum)


def normalize_tts_segments(
    segments: list[Segment],
    output_dir: Path,
    settings: Settings,
    media: MediaBinaries,
    logger: logging.Logger,
) -> list[Path]:
    """Resample every generated line and fit it to its subtitle slot."""

    output_dir.mkdir(parents=True, exist_ok=True)
    aligned: list[Path] = []
    for segment in segments:
        if not segment.tts_file:
            raise PipelineError(f"片段 {segment.index} 缺少 TTS 文件。")
        input_path = Path(segment.tts_file)
        source_duration = probe_duration(input_path, media)
        target_duration = segment.duration
        required_tempo = source_duration / target_duration
        tempo = select_tempo_factor(
            source_duration,
            target_duration,
            settings.max_tempo_factor,
        )
        if required_tempo > settings.max_tempo_factor:
            logger.warning(
                "片段 %s 的配音长 %.2fs、时间槽 %.2fs；达到最大加速后会裁尾。"
                "建议缩短译文。",
                segment.index,
                source_duration,
                target_duration,
            )

        filters = [
            f"aresample={settings.dub_sample_rate}",
            atempo_chain(tempo),
            f"apad=pad_dur={target_duration:.6f}",
            f"atrim=duration={target_duration:.6f}",
            "asetpts=PTS-STARTPTS",
        ]
        target = output_dir / f"{segment.index:05d}.wav"
        run_command(
            [
                media.ffmpeg,
                "-y",
                "-i",
                input_path,
                "-vn",
                "-af",
                ",".join(filters),
                "-ac",
                "1",
                "-ar",
                str(settings.dub_sample_rate),
                "-c:a",
                "pcm_s16le",
                target,
            ],
            logger=logger,
        )
        aligned.append(target)
    return aligned


def _write_silence(
    output: wave.Wave_write,
    frame_count: int,
    *,
    sample_width: int,
) -> None:
    remaining = max(0, frame_count)
    zero_chunk = b"\x00" * (8192 * sample_width)
    while remaining:
        count = min(remaining, 8192)
        output.writeframesraw(zero_chunk[: count * sample_width])
        remaining -= count


def _write_control_signal(
    output: wave.Wave_write,
    frame_count: int,
) -> None:
    """Write an inaudible-to-output sidechain signal in bounded chunks."""

    remaining = max(0, frame_count)
    positive = int(12000).to_bytes(2, "little", signed=True)
    negative = int(-12000).to_bytes(2, "little", signed=True)
    signal_chunk = (positive + negative) * 4096
    while remaining:
        count = min(remaining, 8192)
        output.writeframesraw(signal_chunk[: count * 2])
        remaining -= count


def write_duck_control(
    segments: list[Segment],
    output: Path,
    *,
    total_duration: float,
    sample_rate: int,
    lead_seconds: float = 0.08,
    trail_seconds: float = 0.35,
) -> Path:
    """Create a sidechain control track active for full speech slots.

    The translated speech may finish before the source-language subtitle slot.
    Driving the compressor with this control track, rather than the actual dub
    waveform, prevents the original voice from returning during that tail.
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    total_frames = max(0, math.ceil(total_duration * sample_rate))
    raw_intervals = sorted(
        (
            max(0.0, segment.start - lead_seconds),
            min(total_duration, segment.end + trail_seconds),
        )
        for segment in segments
        if segment.end > 0 and segment.start < total_duration
    )
    intervals: list[tuple[float, float]] = []
    for start, end in raw_intervals:
        if end <= start:
            continue
        if intervals and start <= intervals[-1][1]:
            previous_start, previous_end = intervals[-1]
            intervals[-1] = (previous_start, max(previous_end, end))
        else:
            intervals.append((start, end))

    cursor = 0
    with wave.open(str(output), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        for start, end in intervals:
            start_frame = min(total_frames, round(start * sample_rate))
            end_frame = min(total_frames, round(end * sample_rate))
            if start_frame > cursor:
                _write_silence(
                    writer,
                    start_frame - cursor,
                    sample_width=2,
                )
            _write_control_signal(writer, max(0, end_frame - start_frame))
            cursor = max(cursor, end_frame)
        if cursor < total_frames:
            _write_silence(
                writer,
                total_frames - cursor,
                sample_width=2,
            )
    return output


def compose_dub_timeline(
    segments: list[Segment],
    aligned_files: list[Path],
    output: Path,
    *,
    total_duration: float,
    sample_rate: int,
    logger: logging.Logger,
) -> Path:
    """Place normalized PCM segments on the original media timeline."""

    if len(segments) != len(aligned_files):
        raise PipelineError("配音片段与对齐文件数量不一致。")
    output.parent.mkdir(parents=True, exist_ok=True)
    total_frames = int(math.ceil(total_duration * sample_rate))
    cursor = 0

    with wave.open(str(output), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)

        for segment, clip_path in zip(segments, aligned_files):
            start_frame = max(0, round(segment.start * sample_rate))
            if start_frame > cursor:
                _write_silence(
                    writer,
                    start_frame - cursor,
                    sample_width=2,
                )
                cursor = start_frame

            with wave.open(str(clip_path), "rb") as reader:
                if (
                    reader.getnchannels() != 1
                    or reader.getsampwidth() != 2
                    or reader.getframerate() != sample_rate
                ):
                    raise PipelineError(f"对齐音频格式不一致：{clip_path}")
                overlap = max(0, cursor - start_frame)
                if overlap:
                    logger.warning(
                        "片段 %s 与前一片段重叠 %.3fs，重叠开头将被裁掉。",
                        segment.index,
                        overlap / sample_rate,
                    )
                    reader.setpos(min(overlap, reader.getnframes()))
                available = min(
                    reader.getnframes() - reader.tell(),
                    max(0, total_frames - cursor),
                )
                remaining = available
                while remaining:
                    frames = reader.readframes(min(remaining, 8192))
                    if not frames:
                        break
                    writer.writeframesraw(frames)
                    written = len(frames) // 2
                    cursor += written
                    remaining -= written

        if cursor < total_frames:
            _write_silence(
                writer,
                total_frames - cursor,
                sample_width=2,
            )
    return output


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: list[Segment], output: Path) -> Path:
    lines: list[str] = []
    for position, segment in enumerate(segments, start=1):
        text = (segment.translated_text or segment.source_text).strip()
        text = " ".join(text.splitlines())
        lines.extend(
            [
                str(position),
                (
                    f"{_format_srt_timestamp(segment.start)} --> "
                    f"{_format_srt_timestamp(segment.end)}"
                ),
                text,
                "",
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def separate_background(
    original_audio: Path,
    output_root: Path,
    logger: logging.Logger,
) -> Path:
    """Run optional Demucs two-stem separation and return no_vocals.wav."""

    output_root.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            sys.executable,
            "-m",
            "demucs",
            "--two-stems=vocals",
            "-n",
            "htdemucs",
            "-o",
            output_root,
            original_audio,
        ],
        logger=logger,
    )
    candidates = list(output_root.rglob("no_vocals.wav"))
    if not candidates:
        raise PipelineError("Demucs 完成后没有找到 no_vocals.wav。")
    return candidates[0]


def mux_video(
    source: Path,
    dub_audio: Path,
    subtitles: Path,
    output: Path,
    *,
    settings: Settings,
    media: MediaBinaries,
    keep_original_audio: bool,
    duck_original_audio: bool,
    burn_subtitles: bool,
    background_audio: Path | None,
    duck_control_audio: Path | None,
    logger: logging.Logger,
) -> Path:
    """Create Chinese dubbed audio, optional original track, and subtitles."""

    source_has_audio = has_audio_stream(source, media)
    args: list[str | Path] = [
        media.ffmpeg,
        "-y",
        "-i",
        source,
        "-i",
        dub_audio,
    ]
    next_input_index = 2
    background_index: int | None = None
    if background_audio:
        background_index = next_input_index
        next_input_index += 1
        args.extend(["-i", background_audio])
    duck_control_index: int | None = None
    if duck_control_audio:
        duck_control_index = next_input_index
        next_input_index += 1
        args.extend(["-i", duck_control_audio])
    subtitle_index = next_input_index
    args.extend(["-i", subtitles])

    mix_source = (
        f"{background_index}:a:0"
        if background_index is not None
        else "0:a:0"
    )
    can_mix = source_has_audio or background_index is not None
    if can_mix:
        if duck_original_audio:
            sidechain_source = (
                f"{duck_control_index}:a:0"
                if duck_control_index is not None
                else "1:a:0"
            )
            filter_complex = (
                f"[{mix_source}]volume=0.62[original];"
                f"[original][{sidechain_source}]"
                "sidechaincompress=threshold=0.010:ratio=20:"
                "attack=5:release=250:detection=peak:link=maximum[ducked];"
                "[ducked][1:a:0]"
                "amix=inputs=2:duration=longest:weights='1 1':normalize=0,"
                "loudnorm=I=-16:LRA=11:TP=-1.5[dubbed]"
            )
        else:
            filter_complex = (
                f"[{mix_source}]volume=0.28[original];"
                "[original][1:a:0]"
                "amix=inputs=2:duration=longest:weights='1 1':normalize=0,"
                "loudnorm=I=-16:LRA=11:TP=-1.5[dubbed]"
            )
        args.extend(["-filter_complex", filter_complex])

    args.extend(["-map", "0:v:0"])
    args.extend(["-map", "[dubbed]" if can_mix else "1:a:0"])
    if keep_original_audio and source_has_audio:
        args.extend(["-map", "0:a:0"])
    args.extend(["-map", f"{subtitle_index}:0"])

    if burn_subtitles:
        escaped = str(subtitles).replace("\\", "\\\\").replace(":", "\\:")
        args.extend(["-vf", f"subtitles=filename='{escaped}'", "-c:v", "libx264"])
    else:
        args.extend(["-c:v", "copy"])

    args.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-c:s",
            "mov_text",
            "-metadata:s:a:0",
            "language=chi",
            "-metadata:s:a:0",
            "title=中文配音",
            "-disposition:a:0",
            "default",
        ]
    )
    if keep_original_audio and source_has_audio:
        args.extend(
            [
                "-metadata:s:a:1",
                "language=und",
                "-metadata:s:a:1",
                "title=原声",
                "-disposition:a:1",
                "0",
            ]
        )
    args.extend(
        [
            "-metadata:s:s:0",
            "language=chi",
            "-metadata:s:s:0",
            "title=简体中文字幕",
            "-movflags",
            "+faststart",
            output,
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(args, logger=logger)
    return output


def mux_audio(
    source: Path,
    dub_audio: Path,
    output: Path,
    *,
    settings: Settings,
    media: MediaBinaries,
    keep_original_audio: bool,
    duck_original_audio: bool,
    background_audio: Path | None,
    duck_control_audio: Path | None,
    logger: logging.Logger,
) -> Path:
    """Create a Chinese-dubbed M4A for pure-audio sources.

    The first audio track is the listening track: Chinese narration mixed with
    either the ducked original audio or an optional separated background stem.
    The second audio track is the untouched original audio when requested.
    Subtitles remain as a sidecar SRT because player support for subtitle
    streams inside M4A is inconsistent.
    """

    source_has_audio = has_audio_stream(source, media)
    if not source_has_audio and background_audio is None:
        raise PipelineError("纯音频封装需要原声音轨或背景音轨。")

    args: list[str | Path] = [
        media.ffmpeg,
        "-y",
        "-i",
        source,
        "-i",
        dub_audio,
    ]
    next_input_index = 2
    background_index: int | None = None
    if background_audio:
        background_index = next_input_index
        next_input_index += 1
        args.extend(["-i", background_audio])
    duck_control_index: int | None = None
    if duck_control_audio:
        duck_control_index = next_input_index
        args.extend(["-i", duck_control_audio])

    mix_source = (
        f"{background_index}:a:0"
        if background_index is not None
        else "0:a:0"
    )
    if duck_original_audio:
        sidechain_source = (
            f"{duck_control_index}:a:0"
            if duck_control_index is not None
            else "1:a:0"
        )
        filter_complex = (
            f"[{mix_source}]volume=0.62[original];"
            f"[original][{sidechain_source}]"
            "sidechaincompress=threshold=0.010:ratio=20:"
            "attack=5:release=250:detection=peak:link=maximum[ducked];"
            "[ducked][1:a:0]"
            "amix=inputs=2:duration=longest:weights='1 1':normalize=0,"
            "loudnorm=I=-16:LRA=11:TP=-1.5[dubbed]"
        )
    else:
        filter_complex = (
            f"[{mix_source}]volume=0.28[original];"
            "[original][1:a:0]"
            "amix=inputs=2:duration=longest:weights='1 1':normalize=0,"
            "loudnorm=I=-16:LRA=11:TP=-1.5[dubbed]"
        )

    args.extend(["-filter_complex", filter_complex])
    args.extend(["-map", "[dubbed]"])
    if keep_original_audio and source_has_audio:
        args.extend(["-map", "0:a:0"])

    args.extend(
        [
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-metadata:s:a:0",
            "language=chi",
            "-metadata:s:a:0",
            "title=中文配音",
            "-disposition:a:0",
            "default",
        ]
    )
    if keep_original_audio and source_has_audio:
        args.extend(
            [
                "-metadata:s:a:1",
                "language=und",
                "-metadata:s:a:1",
                "title=原声",
                "-disposition:a:1",
                "0",
            ]
        )
    args.extend(["-movflags", "+faststart", output])
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(args, logger=logger)
    return output
