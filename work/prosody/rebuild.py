"""Rebuild chosen speech spans as continuous utterances, independently of SRT cues."""
import argparse
import json
import logging
import math
import re
import shutil
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from video_translator.models import Segment
from video_translator.pipeline.synthesizer import SpeechSynthesizer
from video_translator.pipeline.utterances import CueFragment, Utterance, retime_chinese_cues
from video_translator.runtime import resolve_media_binaries
from video_translator.settings import get_settings

parser = argparse.ArgumentParser()
parser.add_argument("tag")
parser.add_argument("job")
parser.add_argument("plans", nargs="+", type=Path)
parser.add_argument("--workers", type=int, help="Kokoro CPU threads")
parser.add_argument("--master", type=Path, help="latest MP4 with matching .zh-CN.srt and .en.srt")
parser.add_argument("--dub", type=Path, help="pure Mandarin WAV that matches the latest master")
parser.add_argument("--logical-srt", type=Path, help="Mandarin cues keyed by the original segment IDs")
parser.add_argument("--soundtrack-bed", type=Path, help="aligned music and effects without the original narrator")
parser.add_argument("--output-dir", type=Path, help="new directory for the preview, subtitles and dry narration")
args = parser.parse_args()
if args.workers is not None and args.workers < 1:
    parser.error("--workers must be positive")
if bool(args.master) != bool(args.output_dir):
    parser.error("--master and --output-dir must be used together")
if args.dub and not args.master:
    parser.error("--dub requires --master")
if (args.logical_srt or args.soundtrack_bed) and not args.master:
    parser.error("--logical-srt and --soundtrack-bed require --master")
if args.output_dir and args.output_dir.exists():
    parser.error(f"Output directory already exists: {args.output_dir}")
settings = get_settings()
if args.workers is not None:
    settings = settings.model_copy(update={"tts_threads": args.workers})
media = resolve_media_binaries(settings)
work = Path("work") / args.tag
scratch = work / "prosody"
scratch.mkdir(parents=True, exist_ok=True)
job = Path("data/jobs") / args.job
manifest = json.loads((job / "manifest.json").read_text())
old_video = args.master or Path(manifest["output_path"])
old_chinese = args.logical_srt or (
    old_video.with_suffix(".zh-CN.srt") if args.master else Path(manifest["subtitle_path"])
)
old_english = old_video.with_suffix(".en.srt") if args.master else Path(manifest["english_subtitle_path"])
segments = json.loads((job / Path(manifest["segments_path"]).name).read_text())
old_cues = re.split(r"\n\s*\n", old_chinese.read_text(encoding="utf-8-sig").strip())
if len(old_cues) != len(segments):
    raise ValueError("Chinese subtitles do not match the original segment count")


def srt_seconds(stamp):
    hours, minutes, seconds, milliseconds = map(int, re.split(r"[:,]", stamp))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


if args.master:
    old_dub_path = args.dub or scratch / "dub-timeline.wav"
    source_path = job / "source.mp4"
    draft = {}
    for row, cue in zip(segments, old_cues):
        index = row["index"]
        number, timing, *lines = cue.splitlines()
        if int(number) != index + 1 or not lines:
            raise ValueError(f"Unexpected Chinese subtitle numbering at segment {index}")
        start, end = timing.split(" --> ")
        row.update(start=srt_seconds(start), end=srt_seconds(end))
        draft[index] = "".join(lines)
else:
    old_dub_path = Path(manifest["dub_audio_path"])
    source_path = Path(manifest["source_path"])
    draft = {row["id"]: row["text"] for row in json.loads((work / "complete-draft.json").read_text())["segments"]}

spans = []
revised = set()
used = set()
for plan_path in args.plans:
    plan = json.loads(plan_path.read_text())
    for item in plan["groups"]:
        ids = item["ids"]
        assert ids == list(range(ids[0], ids[-1] + 1))
        assert not used.intersection(ids)
        spans.append(ids)
        used.update(ids)
    for key, value in plan["revisions"].items():
        index = int(key)
        draft[index] = value
        revised.add(index)
spans.extend([index] for index in sorted(revised - used))
spans.sort()
assert list(draft.keys()) == [row["index"] for row in segments]
assert all(0 <= index < len(segments) for span in spans for index in span)


def run(arguments):
    return subprocess.run(arguments, capture_output=True, check=True).stdout


def probe(path):
    return json.loads(run([
        media.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)
    ]))


def active_samples(pcm, rate):
    step = rate // 100
    windows = pcm[:len(pcm) // step * step].astype(np.float32).reshape(-1, step)
    if not len(windows):
        return 0, len(pcm)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    voiced = np.flatnonzero(rms > max(35, np.max(rms) * .013))
    if not len(voiced):
        return 0, 0
    return int(voiced[0]) * step, (int(voiced[-1]) + 1) * step


def format_srt_time(seconds):
    hours, rest = divmod(round(seconds * 1000), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


with wave.open(str(old_dub_path), "rb") as old:
    assert old.getsampwidth() == 2 and old.getnchannels() == 1
    params = old.getparams()
    rate = old.getframerate()
    dub = np.frombuffer(old.readframes(old.getnframes()), dtype="<i2").copy()


def pcm_decode(path, filters=None):
    args = [media.ffmpeg, "-hide_banner", "-v", "error", "-i", str(path)]
    if filters:
        args.extend(["-af", filters])
    return np.frombuffer(run([*args, "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"]), dtype="<i2")


def speech_request(indices):
    cues = [Segment.model_validate(segments[index]) for index in indices]
    fragments = tuple(CueFragment(cue, draft[cue.index].strip(), cue.start, cue.end) for cue in cues)
    segment = cues[0].model_copy(update={
        "end": cues[-1].end,
        "translated_text": "".join(fragment.text for fragment in fragments),
    })
    return Utterance(segment, fragments)


def align_speech(utterance):
    indices = [fragment.cue_index for fragment in utterance.fragments]
    texts = [fragment.text for fragment in utterance.fragments]
    segment = utterance.segment
    audio = Path(segment.tts_file)
    pcm = pcm_decode(audio)
    onset, offset = active_samples(pcm, rate)
    assert offset > onset, indices
    start, end = segment.start, segment.end
    frame_start = round(start * rate)
    frame_end = min(len(dub), round(end * rate))
    room = (frame_end - frame_start) / rate
    trim_start = max(0, onset / rate - .025)
    trim_end = min(len(pcm) / rate, offset / rate + .09)
    tempo = max(.94, (trim_end - trim_start) / max(.05, room - .055))
    if tempo > 1.72:
        raise ValueError(f"{indices}: {tempo:.2f}x; shorten speech")
    filters = f"atrim=start={trim_start:.7f}:end={trim_end:.7f},asetpts=PTS-STARTPTS,atempo={tempo:.8f}"
    rendered = pcm_decode(audio, filters)
    assert len(rendered) <= frame_end - frame_start + round(.025 * rate), (indices, len(rendered), frame_end - frame_start)
    rendered = rendered[:frame_end - frame_start].copy()
    # Fade the extreme edges for a clean splice, without changing voiced timing.
    edge = min(len(rendered) // 2, round(.005 * rate))
    if edge:
        rendered[:edge] = rendered[:edge] * np.linspace(0., 1., edge)
        rendered[-edge:] = rendered[-edge:] * np.linspace(1., 0., edge)

    # Kokoro's Mandarin pipeline has no word timestamps. Estimate the interior
    # subtitle cuts over the actual voiced duration using the shared cue logic.
    _, voiced_end = active_samples(rendered, rate)
    assert voiced_end > 0, indices
    cues = retime_chinese_cues([utterance], speech_ends={segment.index: start + voiced_end / rate})
    cuts = [start]
    gaps = []
    for left, right, cue in zip(utterance.fragments, utterance.fragments[1:], cues[1:]):
        cut = cue.start
        assert cut >= cuts[-1] + .30 and cut <= end - .30, (indices, cut, start, end)
        cuts.append(cut)
        old_left = dub[round(left.start * rate):round(left.end * rate)]
        old_right = dub[round(right.start * rate):round(right.end * rate)]
        _, old_tail = active_samples(old_left, rate)
        old_head, _ = active_samples(old_right, rate)
        old_gap = max(0, right.start + old_head / rate - left.start - old_tail / rate)
        gaps.append({
            "ids": [left.cue_index, right.cue_index],
            "old_gap_seconds": round(old_gap, 3),
            "old_change_seconds": right.start,
            "new_change_seconds": round(cut, 3),
        })
    cuts.append(end)
    return indices, texts, cuts, frame_start, frame_end, rendered, tempo, gaps


# All reads from the old waveform finish before any splice is applied.
requests = [speech_request(indices) for indices in spans]
SpeechSynthesizer(settings, logging.getLogger(__name__)).synthesize_segments(
    [request.segment for request in requests], scratch / "tts-kokoro"
)
generated = []
for request in requests:
    part = align_speech(request)
    generated.append(part)
    print(f"Speech {len(generated)}/{len(spans)}: {part[0]} ({part[6]:.2f}x)", flush=True)

gaps = []
for indices, texts, cuts, first, last, rendered, tempo, measurements in generated:
    dub[first:last] = 0
    dub[first:first + len(rendered)] = rendered
    gaps.extend(measurements)
    for index, text, start, end in zip(indices, texts, cuts, cuts[1:]):
        if len(text) > 28:
            candidates = [match.end() for match in re.finditer(r"[，。；、！？]", text)
                          if 12 <= match.end() <= len(text) - 9]
            split = min(candidates, key=lambda i: abs(i - len(text) / 2)) if candidates else math.ceil(len(text) / 2)
            text = f"{text[:split]}\n{text[split:]}"
        old_cues[index] = f"{index + 1}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}"

outputs = args.output_dir or old_video.parent.parent / "continuous-dub"
assert not outputs.exists(), outputs
outputs.parent.mkdir(parents=True, exist_ok=True)
with TemporaryDirectory(dir=outputs.parent, prefix=".continuous-dub-") as directory:
    bundle = Path(directory) / "bundle"
    bundle.mkdir()
    video = bundle / (old_video.name if args.master else f"{old_video.stem}-continuous.mp4")
    chinese = video.with_suffix(".zh-CN.srt")
    english = video.with_suffix(".en.srt")
    chinese.write_text("\n\n".join(old_cues) + "\n")
    shutil.copyfile(old_english, english)
    new_dub = (bundle if args.master else scratch) / "dub-timeline.wav"
    with wave.open(str(new_dub), "wb") as audio:
        audio.setparams(params)
        audio.writeframes(dub.tobytes())
    mix = (
        "[2:a:0]volume=0.72[original];[original][1:a:0]"
        "sidechaincompress=threshold=0.025:ratio=8:attack=20:release=500[ducked];"
        "[ducked][1:a:0]amix=inputs=2:duration=longest:weights='1 1':normalize=0,"
        "loudnorm=I=-16:LRA=11:TP=-1.5[dubbed]"
    )
    if args.soundtrack_bed:
        from video_translator.pipeline.soundtrack import mix_soundtrack

        intervals = [
            tuple(map(srt_seconds, cue.splitlines()[1].split(" --> "))) for cue in old_cues
        ]
        mix_soundtrack(old_video, new_dub, video, media,
                       separated_bed=args.soundtrack_bed, narration_intervals=intervals)
    else:
        run([
            media.ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(old_video),
            "-i", str(new_dub), "-i", str(source_path), "-filter_complex", mix,
            "-map", "0:v:0", "-map", "[dubbed]", "-map", "0:a:1",
            "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "copy",
            "-metadata:s:a:0", "language=chi", "-metadata:s:a:0", "handler_name=中文配音",
            "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "handler_name=English",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-movflags", "+faststart", str(video),
        ])
    old_info, new_info = probe(old_video), probe(video)
    streams = new_info["streams"]
    assert [stream["codec_type"] for stream in streams] == ["video", "audio", "audio"]
    assert [(s["tags"]["language"], s["disposition"]["default"]) for s in streams[1:]] == [("chi", 1), ("eng", 0)]
    for key in ("codec_name", "width", "height", "nb_frames", "duration"):
        assert streams[0][key] == old_info["streams"][0][key], key
    duration = float(new_info["format"]["duration"])
    assert abs(duration - float(old_info["format"]["duration"])) <= .10
    last_end = ""
    for n, cue in enumerate(old_cues, start=1):
        number, times, *text = cue.splitlines()
        start, end = times.split(" --> ")
        assert int(number) == n and last_end <= start < end <= format_srt_time(duration) and text
        last_end = end
    for item in sorted(gaps, key=lambda x: x["old_gap_seconds"], reverse=True)[:3]:
        run([
            media.ffmpeg, "-hide_banner", "-v", "error", "-xerror",
            "-ss", str(max(0, item["new_change_seconds"] - .4)), "-i", str(video),
            "-t", "1.5", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
        ])
    report = {
        "video": str(outputs / video.name), "chinese": str(outputs / chinese.name),
        "english": str(outputs / english.name), "groups": sum(len(span) > 1 for span in spans),
        "single_revisions": sum(len(span) == 1 for span in spans), "gaps": gaps,
    }
    if args.master:
        report["dub"] = str(outputs / new_dub.name)
    report_dir = bundle if args.master else scratch
    (report_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    bundle.rename(outputs)
print(json.dumps({key: value for key, value in report.items() if key != "gaps"}, ensure_ascii=False))
