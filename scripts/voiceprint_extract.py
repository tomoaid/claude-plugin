#!/usr/bin/env python3
"""Diarize a meeting recording and cut one concatenated sample clip per speaker.

Usage:
    PYANNOTEAI_API_KEY=sk-... python3 voiceprint_extract.py <audio> \
        --out-dir /tmp/voiceprint-setup/<basename> [--max-clip-seconds 29]

Pipeline:
  1. Upload audio to pyannote.ai temp storage
  2. POST /v1/diarize (no voiceprints needed) → poll → segments per speaker
  3. For each speaker: pick the longest segments, concat with ffmpeg up to
     --max-clip-seconds (29s default — voiceprint API caps at 30s)
  4. Write <out-dir>/clips/SPEAKER_XX.wav + <out-dir>/manifest.json

The clips are meant to be labeled by a human (see label_server.py), then fed
to pyannote_voiceprint.py to build the team voiceprint library.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _common import PYANNOTE_BASE, fmt_time, http_json, poll_job, probe_duration, slugify, upload_to_pyannote

EDGE_TRIM_SEC = 0.1  # shave segment edges to avoid bleed from adjacent speakers
MIN_SEGMENT_SEC = 1.0


def log(msg: str) -> None:
    print(f"[voiceprint_extract] {msg}", file=sys.stderr, flush=True)


def upload_audio(api_key: str, audio: Path) -> str:
    object_key = slugify(f"vpsetup-{audio.stem}-{int(time.time())}")
    log(f"uploading {audio.name} ({audio.stat().st_size / 1024 / 1024:.1f} MB) → media://{object_key}")
    return upload_to_pyannote(api_key, audio, object_key)


def diarize(api_key: str, media_uri: str) -> list[dict]:
    resp = http_json("POST", f"{PYANNOTE_BASE}/diarize", api_key=api_key, body={"url": media_uri})
    job_id = resp.get("jobId")
    if not job_id:
        raise RuntimeError(f"no jobId: {resp}")
    log(f"polling diarize job {job_id}…")
    job = poll_job(api_key, job_id)
    if job["status"] != "succeeded":
        raise RuntimeError(f"diarize job ended with status={job['status']}: {job}")
    segments = (job.get("output") or {}).get("diarization")
    if not segments:
        raise RuntimeError(f"succeeded job has no diarization: {job}")
    log(f"diarize done: {len(segments)} segments")
    return segments


def select_segments(segments: list[dict], max_total: float) -> list[dict]:
    """Longest segments first until budget is spent; returned in chronological order."""
    usable = []
    for seg in segments:
        start = seg["start"] + EDGE_TRIM_SEC
        end = seg["end"] - EDGE_TRIM_SEC
        if end - start >= MIN_SEGMENT_SEC:
            usable.append({"start": start, "end": end})
    if not usable:  # speaker only has very short segments — keep the longest as-is
        best = max(segments, key=lambda s: s["end"] - s["start"])
        usable = [{"start": best["start"], "end": best["end"]}]
    picked: list[dict] = []
    total = 0.0
    for seg in sorted(usable, key=lambda s: s["end"] - s["start"], reverse=True):
        remain = max_total - total
        if remain < MIN_SEGMENT_SEC:
            break
        dur = seg["end"] - seg["start"]
        if dur > remain:
            picked.append({"start": seg["start"], "end": seg["start"] + remain})
            total += remain
            break
        picked.append(seg)
        total += dur
    return sorted(picked, key=lambda s: s["start"])


def cut_concat(audio: Path, picked: list[dict], out_wav: Path, tmp_dir: Path) -> float:
    parts = []
    for i, seg in enumerate(picked):
        part = tmp_dir / f"{out_wav.stem}_part{i:02d}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{seg['start']:.2f}", "-to", f"{seg['end']:.2f}",
                "-i", str(audio),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                str(part),
            ],
            check=True,
        )
        parts.append(part)
    concat_list = tmp_dir / f"{out_wav.stem}_concat.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-c", "copy", str(out_wav)],
        check=True,
    )
    return probe_duration(out_wav)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diarize and cut per-speaker sample clips for voiceprint setup")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-clip-seconds", type=float, default=29.0)
    args = parser.parse_args()

    api_key = os.environ.get("PYANNOTEAI_API_KEY")
    if not api_key:
        log("error: PYANNOTEAI_API_KEY not set")
        return 2
    if not args.audio.is_file():
        log(f"error: audio not found: {args.audio}")
        return 2

    clips_dir = args.out_dir / "clips"
    tmp_dir = args.out_dir / "tmp"
    clips_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    media_uri = upload_audio(api_key, args.audio)
    segments = diarize(api_key, media_uri)

    by_speaker: dict[str, list[dict]] = {}
    for seg in segments:
        by_speaker.setdefault(seg["speaker"], []).append(seg)

    manifest = {"audio": str(args.audio), "speakers": []}
    for speaker in sorted(by_speaker):
        segs = by_speaker[speaker]
        total_speech = sum(s["end"] - s["start"] for s in segs)
        picked = select_segments(segs, args.max_clip_seconds)
        out_wav = clips_dir / f"{speaker}.wav"
        clip_sec = cut_concat(args.audio, picked, out_wav, tmp_dir)
        manifest["speakers"].append({
            "id": speaker,
            "clip": f"clips/{out_wav.name}",
            "clip_seconds": round(clip_sec, 1),
            "total_speech_seconds": round(total_speech, 1),
            "n_segments": len(segs),
            "sample_times": [f"{fmt_time(s['start'])}–{fmt_time(s['end'])}" for s in picked],
        })
        log(f"{speaker}: 總發言 {total_speech:.0f}s，樣本 {clip_sec:.1f}s（{len(picked)} 段拼接）→ {out_wav}")

    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(manifest_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
