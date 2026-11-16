"""Mix an aligned effects bed under Mandarin and preserve the original picture and audio."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

from video_translator.pipeline.media import probe_media
from video_translator.runtime import MediaBinaries, resolve_media_binaries
from video_translator.settings import Settings


RATE = 48000
FRAME = 2400  # 50 ms
MODEL = "UVR-MDX-NET-Inst_HQ_3.onnx"


def run(program, *args):
    result = subprocess.run([program, *map(str, args)], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-3000:])
    return result.stdout


def decode(path, stream, media, rate=RATE):
    pcm = run(
        media.ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", stream,
        "-af", f"aresample={rate}:async=1000:first_pts=0", "-ac", "2", "-f", "f32le", "-",
    )
    return np.frombuffer(pcm, dtype=np.float32).reshape(-1, 2)


def timeline(audio, size):
    return np.pad(audio, ((0, max(0, size - len(audio))), (0, 0)))[:size]


def ranges(values, duration):
    """Accept draft rows or (start, end) pairs and clip them to the video timeline."""
    intervals = []
    for value in values:
        start, end = (value["start"], value["end"]) if isinstance(value, dict) else value
        start, end = max(0.0, float(start)), min(duration, float(end))
        if start < end:
            intervals.append((start, end))
    return intervals


def interval_mask(intervals, size):
    active = np.zeros(size, dtype=bool)
    for start, end in intervals:
        active[int(start * RATE / FRAME):int(np.ceil(end * RATE / FRAME))] = True
    return active


def vad_mask(audio, size, threshold):
    # The source sample clock is divisible by the Silero model's 16 kHz clock.
    mono = audio[:len(audio) // 3 * 3].reshape(-1, 3, 2).mean(axis=(1, 2))
    speech = get_speech_timestamps(
        mono, VadOptions(threshold=threshold, min_speech_duration_ms=250,
                         min_silence_duration_ms=250, speech_pad_ms=130),
    )
    return interval_mask(((item["start"] / 16000, item["end"] / 16000) for item in speech), size)


def frame_rms(audio):
    frames = audio.reshape(-1, FRAME * 2)
    return np.sqrt(np.einsum("ij,ij->i", frames, frames) / frames.shape[1])


def smooth(levels):
    gains = np.empty_like(levels)
    previous = float(levels[0])
    for i, target in enumerate(levels):
        weight = 0.64 if target < previous else 0.25
        previous += weight * (target - previous)
        gains[i] = previous
    return gains


def crossfade_windows(windows, size):
    times = (np.arange(size) + .5) * FRAME / RATE
    levels = np.zeros(size, dtype=np.float32)
    for start, end in windows:
        fade = np.clip(np.minimum(times - start, end - times) / .30, 0, 1)
        np.maximum(levels, fade, out=levels)
    return levels


def apply_gain(audio, levels):
    """Interpolate adjacent gain values on the sample clock."""
    centers = np.arange(len(levels)) * FRAME + FRAME / 2
    result = np.empty_like(audio)
    for start in range(0, len(audio), RATE * 5):
        end = min(len(audio), start + RATE * 5)
        gain = np.interp(np.arange(start, end), centers, levels).astype(np.float32)
        np.multiply(audio[start:end], gain[:, None], out=result[start:end])
    return result


def separate_soundtrack(base, target_bed, media, *, separator, model_dir, model=MODEL, batch_size=1):
    """Run the optional local MLX separator and save its synchronized instrumental estimate."""
    target_bed = Path(target_bed)
    if target_bed.suffix.lower() not in {".wav", ".flac"}:
        raise ValueError("Use a .wav or .flac destination for the separated soundtrack.")
    duration = float(probe_media(base, media)["format"]["duration"])
    target_bed.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".separate-", dir=target_bed.parent) as temporary:
        folder = Path(temporary)
        source = folder / "original.wav"
        run(media.ffmpeg, "-nostdin", "-v", "error", "-i", base, "-map", "0:a:1",
            "-af", f"aresample=44100:async=1000:first_pts=0,apad,atrim=duration={duration:.6f}",
            "-ac", "2", "-c:a", "pcm_s16le", source)
        run(separator, source, "-m", model, "--model_file_dir", model_dir, "--output_dir", folder,
            "--output_format", target_bed.suffix[1:].upper(), "--single_stem", "Instrumental",
            "--mdx_batch_size", batch_size)
        candidates = [path for path in folder.glob(f"*{target_bed.suffix.lower()}") if path != source]
        if len(candidates) != 1:
            raise RuntimeError("Expected one instrumental file from the separator.")
        actual = float(probe_media(candidates[0], media)["format"]["duration"])
        if abs(duration - actual) > .05:
            raise ValueError(f"Instrumental duration is {actual:.3f}s, expected {duration:.3f}s.")
        candidates[0].replace(target_bed)
    return target_bed


def preserved_hashes(path, media):
    return run(media.ffmpeg, "-nostdin", "-v", "error", "-i", path,
               "-map", "0:v:0", "-map", "0:a:1", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")


def mix_soundtrack(
    base: Path, dry: Path | None, target: Path, media: MediaBinaries, *,
    original_windows=(), separated_bed: Path | None = None, narration_intervals=(), report: Path | None = None,
):
    """Use dry Mandarin when supplied, or add effects to the current delivered default track."""
    if target.resolve() == base.resolve() or target.exists():
        raise ValueError("The preview needs a new output file.")
    base_info = probe_media(base, media)
    base_tracks = [stream for stream in base_info["streams"] if stream["codec_type"] == "audio"]
    if len(base_tracks) != 2:
        raise ValueError("The master needs a Mandarin track and a separate original track.")
    languages = [track.get("tags", {}).get("language", "und") for track in base_tracks]
    track_names = [track.get("tags", {}).get("handler_name", fallback)
                   for track, fallback in zip(base_tracks, ("中文配音", "原声"), strict=True)]
    duration = float(base_info["format"]["duration"])
    n_samples = round(duration * RATE)
    n_frames = (n_samples + FRAME - 1) // FRAME
    analysis_size = n_frames * FRAME
    original = timeline(decode(base, "0:a:1", media), analysis_size)
    voice_audio = decode(dry or base, "0:a:0", media)
    if dry is not None and abs(len(voice_audio) - n_samples) > RATE * .30:
        raise ValueError("Mandarin narration duration differs from the delivered timeline.")
    voice = timeline(voice_audio, analysis_size)
    del voice_audio
    windows = ranges(original_windows, duration)
    if separated_bed is None:
        # Recenter the side signal so it remains audible on mono playback.
        side = (original[:, 0] - original[:, 1]) * .5
        bed = np.repeat(side[:, None], 2, axis=1)
        method = "stereo-side"
    else:
        separated = decode(separated_bed, "0:a:0", media)
        if abs(len(separated) - n_samples) > RATE * .30:
            raise ValueError("Separated soundtrack duration differs from the delivered timeline.")
        bed = timeline(separated, analysis_size)
        method = "separated"

    voice_rms, bed_rms = frame_rms(voice), frame_rms(bed)
    raw_mix = crossfade_windows(windows, n_frames)
    narration = interval_mask(ranges(narration_intervals, duration), n_frames)
    # The previous Mandarin mix already contains the full original in these windows.
    narration |= (voice_rms > .014) & (raw_mix < .01 if dry is None else True)
    source_speech = vad_mask(original, n_frames, .50)
    bed_speech = vad_mask(bed, n_frames, .42)
    # Silero can flag music or shouts, so retain a little of the affected bed.
    suspected_dialogue = source_speech & bed_speech
    gain = np.where(narration, .42, .70).astype(np.float32)
    gain[suspected_dialogue] *= .22
    ceiling = np.where(narration, np.clip(voice_rms * .38, .019, .042), .073)
    gain = smooth(np.minimum(gain, ceiling / np.maximum(bed_rms, 1e-5)))
    background = apply_gain(bed, gain * (1 - raw_mix))
    if dry is not None and windows:
        raw_gain = np.where(narration, .10, .85) * raw_mix
        background += apply_gain(original, smooth(raw_gain))
    added_rms = frame_rms(background)
    voice += background

    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".mix-", dir=target.parent) as temporary:
        folder = Path(temporary)
        raw, staged = folder / "mix.f32", folder / "preview.mp4"
        voice[:n_samples].tofile(raw)
        run(media.ffmpeg, "-nostdin", "-v", "error", "-copyts", "-i", base,
            "-f", "f32le", "-ar", RATE, "-ac", "2", "-i", raw,
            "-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:1", "-map_metadata", "0",
            "-filter:a:0", "alimiter=limit=0.95:attack=5:release=45:level=0:latency=1",
            "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "224k", "-c:a:1", "copy",
            "-metadata:s:a:0", f"language={languages[0]}", "-metadata:s:a:0", f"handler_name={track_names[0]}",
            "-metadata:s:a:1", f"language={languages[1]}", "-metadata:s:a:1", f"handler_name={track_names[1]}",
            "-disposition:a:0", "default", "-disposition:a:1", "0", "-avoid_negative_ts", "disabled",
            "-movflags", "+faststart", staged)
        metadata = probe_media(staged, media)
        tracks = [stream for stream in metadata["streams"] if stream["codec_type"] == "audio"]
        if (
            abs(float(metadata["format"]["duration"]) - duration) > .05
            or [track.get("tags", {}).get("language", "und") for track in tracks] != languages
            or [track["disposition"]["default"] for track in tracks] != [1, 0]
            or preserved_hashes(staged, media) != preserved_hashes(base, media)
        ):
            raise ValueError("Output picture, original soundtrack, duration or track order changed.")
        staged.replace(target)

    active_bed = added_rms[narration & (raw_mix < .01)]
    measurements = {
        "method": method, "duration": duration, "output": str(target),
        "source_speech_seconds": round(float(source_speech.sum()) * FRAME / RATE, 2),
        "bed_speech_overlap_seconds": round(float(suspected_dialogue.sum()) * FRAME / RATE, 2),
        "narration_seconds": round(float(narration.sum()) * FRAME / RATE, 2),
        "median_narration_bed_rms": float(np.median(active_bed)) if active_bed.size else 0.0,
        "p95_narration_bed_rms": float(np.percentile(active_bed, 95)) if active_bed.size else 0.0,
        "original_windows": windows,
    }
    if report is not None:
        report.write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--narration", type=Path)
    parser.add_argument("--windows", type=Path, help="Draft JSON with original_audio_windows and segments")
    bed = parser.add_mutually_exclusive_group()
    bed.add_argument("--separated-bed", type=Path)
    bed.add_argument("--separator", help="Local mlx-audio-separator executable")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.separator and (not shutil.which(args.separator) or args.model_dir is None):
        parser.error("--separator needs an installed executable and --model-dir")
    draft = json.loads(args.windows.read_text(encoding="utf-8")) if args.windows else {}
    media = resolve_media_binaries(Settings())
    with TemporaryDirectory(prefix="bayonetta-bed-") as temporary:
        separated = args.separated_bed
        if args.separator:
            separated = separate_soundtrack(args.base, Path(temporary) / "bed.wav", media,
                                            separator=args.separator, model_dir=args.model_dir)
        measurements = mix_soundtrack(
            args.base, args.narration, args.output, media, separated_bed=separated,
            original_windows=draft.get("original_audio_windows", ()),
            narration_intervals=draft.get("segments", ()), report=args.report,
        )
    print(json.dumps(measurements, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
