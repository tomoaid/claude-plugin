#!/usr/bin/env python3
"""Identify speakers via pyannote.ai voiceprints + Whisper ASR → labeled transcript.

Pipeline:
  1. Upload local audio to pyannote temp storage
  2. POST /v1/identify with team voiceprints (exclusive=true) → jobId
  3. Send audio to OpenAI whisper-1 (verbose_json, segment timestamps)
     while the identify job runs, then poll /v1/jobs/{jobId} until succeeded
  4. Merge: segment-level max-overlap speaker assignment (official pyannote
     tutorial logic); segments with zero overlap against any pyannote speech
     region are flagged as suspected hallucination (pyannote 未偵測到語音),
     not silently kept or dropped

--prompt / --prompt-file primes whisper with names/jargon (寫繁體可同時把輸出
偏向繁體). whisper-1 只取 prompt 的最後 224 tokens；切段模式下每段 prompt 會
附上前段轉錄結尾，越近的內容越優先保留。

Usage:
  PYANNOTEAI_API_KEY=... OPENAI_API_KEY=... \\
  python3 diarize_merge.py <audio> \\
    --voiceprints .tomoaid/voiceprints.json \\
    --out transcript.md \\
    [--prompt-file .tomoaid/asr-glossary.md] [--raw-out raw.json]

Refs:
  https://docs.pyannote.ai/api-reference/identify
  https://docs.pyannote.ai/tutorials/diarization-asr-merge
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PYANNOTE_BASE = "https://api.pyannote.ai/v1"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 600
WHISPER_MAX_BYTES = 25 * 1024 * 1024
CHUNK_SECONDS = 600
CONTEXT_TAIL_CHARS = 300  # 切段模式下，附給下一段當上下文的前段結尾長度
RETRY_SLEEP_SEC = 5  # curl 網路層錯誤的單次重試間隔


def log(msg: str) -> None:
    print(f"[diarize_merge] {msg}", file=sys.stderr, flush=True)


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "audio"


def http_json(method: str, url: str, *, api_key: str | None = None, body: dict | bytes | None = None, content_type: str | None = None) -> dict:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif isinstance(body, (bytes, bytearray)):
        data = body
        if content_type:
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {detail}") from None


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        text=True,
    )
    return float(out.strip())


def upload_to_pyannote(api_key: str, audio: Path) -> str:
    object_key = slugify(f"diarize-{audio.stem}-{int(time.time())}")
    media_uri = f"media://{object_key}"
    resp = http_json("POST", f"{PYANNOTE_BASE}/media/input", api_key=api_key, body={"url": media_uri})
    presigned = resp.get("url") or resp.get("presigned_url")
    if not presigned:
        raise RuntimeError(f"no presigned URL: {resp}")
    log(f"uploading {audio.name} ({audio.stat().st_size} bytes) → {media_uri}")
    http_json("PUT", presigned, body=audio.read_bytes(), content_type="application/octet-stream")
    return media_uri


def create_identify_job(api_key: str, media_uri: str, voiceprints: dict[str, str]) -> str:
    payload = {
        "url": media_uri,
        "voiceprints": [{"label": name, "voiceprint": vp} for name, vp in voiceprints.items()],
        "exclusive": True,
    }
    log(f"creating identify job (exclusive=true, {len(voiceprints)} voiceprints)…")
    resp = http_json("POST", f"{PYANNOTE_BASE}/identify", api_key=api_key, body=payload)
    job_id = resp.get("jobId")
    if not job_id:
        raise RuntimeError(f"no jobId: {resp}")
    return job_id


def poll_identify(api_key: str, job_id: str) -> list[dict]:
    log(f"polling identify job {job_id}…")
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while True:
        job = http_json("GET", f"{PYANNOTE_BASE}/jobs/{job_id}", api_key=api_key)
        status = job.get("status")
        if status == "succeeded":
            output = job.get("output") or {}
            segments = output.get("identification") or output.get("exclusiveDiarization") or output.get("diarization")
            if not segments:
                raise RuntimeError(f"succeeded job has no segments: {job}")
            log(f"identify done: {len(segments)} segments")
            return segments
        if status in ("failed", "canceled"):
            raise RuntimeError(f"identify job ended with status={status}: {job}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"identify job {job_id} timed out (last status: {status})")
        time.sleep(POLL_INTERVAL_SEC)


def split_for_whisper(audio: Path, tmp_dir: Path) -> list[tuple[Path, float]]:
    """Split into <25MB MP3 chunks; return [(chunk_path, start_offset_sec)]."""
    pattern = str(tmp_dir / "chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(audio),
            "-f", "segment",
            "-segment_time", str(CHUNK_SECONDS),
            "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", "-ar", "16000",
            pattern,
        ],
        check=True,
    )
    chunks = sorted(tmp_dir.glob("chunk_*.mp3"))
    # offset 用實際長度累加 — segment muxer 在 frame 邊界切，每段不會剛好 CHUNK_SECONDS
    result: list[tuple[Path, float]] = []
    offset = 0.0
    for c in chunks:
        result.append((c, offset))
        offset += probe_duration(c)
    return result


def compose_prompt(base: str, prev_tail: str) -> str:
    """詞彙表 + 前一段結尾。whisper-1 只取最後 224 tokens，所以近期上下文放後面。"""
    parts = [p for p in (base.strip(), prev_tail.strip()) if p]
    return "\n".join(parts)


def whisper_transcribe(audio: Path, api_key: str, language: str | None, offset: float = 0.0, prompt: str = "") -> list[dict]:
    cmd = [
        "curl", "-sS", "--fail-with-body",
        "--config", "-",  # Authorization 走 stdin，key 不進 ps 可見的 argv
        "-X", "POST", OPENAI_TRANSCRIBE_URL,
        "-F", f"file=@{audio}",
        "-F", "model=whisper-1",
        "-F", "response_format=verbose_json",
        "-F", "timestamp_granularities[]=segment",
    ]
    if language:
        cmd += ["-F", f"language={language}"]
    if prompt:
        # --form-string：值不做 @ / < / ;type 解析，中文與標點安全
        cmd += ["--form-string", f"prompt={prompt}"]
    curl_cfg = f'header = "Authorization: Bearer {api_key}"\n'
    proc = subprocess.run(cmd, capture_output=True, text=True, input=curl_cfg)
    if proc.returncode not in (0, 22):  # 22 = HTTP error（原樣回報不重試）；其餘是網路層，重試一次
        log(f"curl network error (exit {proc.returncode}), retrying in {RETRY_SLEEP_SEC}s…")
        time.sleep(RETRY_SLEEP_SEC)
        proc = subprocess.run(cmd, capture_output=True, text=True, input=curl_cfg)
    if proc.returncode != 0:
        raise RuntimeError(f"whisper failed: {proc.stdout}\n{proc.stderr}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bad JSON from API: {proc.stdout[:500]}") from e
    return [
        {"start": s["start"] + offset, "end": s["end"] + offset, "text": s["text"].strip()}
        for s in data.get("segments") or []
    ]


def asr_with_timestamps(audio: Path, api_key: str, language: str | None, prompt: str = "") -> list[dict]:
    size = audio.stat().st_size
    if size <= WHISPER_MAX_BYTES:
        log(f"whisper: single shot ({size / 1024 / 1024:.1f} MB)")
        return whisper_transcribe(audio, api_key, language, prompt=prompt)
    log(f"whisper: file too large ({size / 1024 / 1024:.1f} MB), splitting…")
    import tempfile
    segments: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        chunks = split_for_whisper(audio, Path(tmp))
        log(f"split into {len(chunks)} chunks")
        prev_tail = ""
        for i, (chunk, offset) in enumerate(chunks, 1):
            cs = chunk.stat().st_size
            log(f"  chunk {i}/{len(chunks)} ({cs / 1024 / 1024:.1f} MB, +{offset:.0f}s)")
            segs = whisper_transcribe(chunk, api_key, language, offset,
                                      prompt=compose_prompt(prompt, prev_tail))
            segments.extend(segs)
            prev_tail = " ".join(s["text"] for s in segs).strip()[-CONTEXT_TAIL_CHARS:]
    return segments


def overlap_speaker(start: float, end: float, dia_sorted: list[dict]) -> str | None:
    """Max-overlap speaker for [start, end]; None = pyannote 在這段沒偵測到任何語音。"""
    overlap: dict[str, float] = {}
    for d in dia_sorted:
        iv = min(d["end"], end) - max(d["start"], start)
        if iv > 0:
            overlap[d["speaker"]] = overlap.get(d["speaker"], 0.0) + iv
    return max(overlap.items(), key=lambda x: x[1])[0] if overlap else None


def merge(asr_segments: list[dict], dia_segments: list[dict]) -> list[dict]:
    """Segment-level max-overlap speaker assignment（官方 tutorial 邏輯）.

    每個 ASR segment 配給重疊最多的 speaker；與所有語音區段零重疊 →
    speaker=UNKNOWN + no_speech=True（疑似幻覺，交給下游清理階段決定去留）。
    """
    dia_sorted = sorted(dia_segments, key=lambda x: x["start"])
    merged: list[dict] = []
    for seg in sorted(asr_segments, key=lambda s: s["start"]):
        sp = overlap_speaker(seg["start"], seg["end"], dia_sorted)
        merged.append({**seg, "speaker": sp or "UNKNOWN", "no_speech": sp is None})
    return merged


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


NO_SPEECH_MARK = "⚠ 無語音區段（疑似 ASR 幻覺，清理時預設刪除）"


def render_transcript(merged: list[dict]) -> str:
    """Group consecutive same-speaker segments into one paragraph."""
    lines = []
    current_key = None
    buffer: list[str] = []
    block_start = 0.0
    block_end = 0.0

    def flush() -> None:
        speaker, no_speech = current_key
        mark = f" {NO_SPEECH_MARK}" if no_speech else ""
        lines.append(f"**{speaker}** [{fmt_time(block_start)}–{fmt_time(block_end)}]{mark}")
        lines.append(" ".join(buffer).strip())
        lines.append("")

    for seg in merged:
        key = (seg["speaker"], seg.get("no_speech", False))
        if key != current_key:
            if buffer:
                flush()
            current_key = key
            buffer = [seg["text"]]
            block_start = seg["start"]
            block_end = seg["end"]
        else:
            buffer.append(seg["text"])
            block_end = seg["end"]
    if buffer:
        flush()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--voiceprints", type=Path, required=True, help="聲紋庫 JSON（{label: voiceprint_b64}）")
    parser.add_argument("--out", type=Path, help="Output transcript path (default: stdout)")
    parser.add_argument("--raw-out", type=Path, help="Optional: dump raw identify + ASR JSON")
    parser.add_argument("--language", help="ISO-639-1，如 zh / en；不傳由模型自動偵測")
    parser.add_argument("--prompt", default="", help="ASR 詞彙/風格 priming 文字")
    parser.add_argument("--prompt-file", type=Path, help="從檔案讀 priming 文字（接在 --prompt 後）")
    args = parser.parse_args()

    pyannote_key = os.environ.get("PYANNOTEAI_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not pyannote_key:
        log("error: PYANNOTEAI_API_KEY not set")
        return 2
    if not openai_key:
        log("error: OPENAI_API_KEY not set")
        return 2
    if not args.audio.is_file():
        log(f"error: audio not found: {args.audio}")
        return 2
    if not args.voiceprints.is_file():
        log(f"error: voiceprints not found: {args.voiceprints}")
        return 2
    base_prompt = args.prompt
    if args.prompt_file:
        if not args.prompt_file.is_file():
            log(f"error: prompt file not found: {args.prompt_file}")
            return 2
        base_prompt = compose_prompt(base_prompt, args.prompt_file.read_text(encoding="utf-8"))

    voiceprints = json.loads(args.voiceprints.read_text(encoding="utf-8"))
    if not voiceprints:
        log(f"error: voiceprints file is empty: {args.voiceprints}（先跑 voiceprint-setup 建立聲紋庫）")
        return 2
    log(f"loaded {len(voiceprints)} voiceprints: {list(voiceprints)}")
    duration = probe_duration(args.audio)
    log(f"audio: {args.audio.name}, {duration:.0f}s, {args.audio.stat().st_size / 1024 / 1024:.1f} MB")

    media_uri = upload_to_pyannote(pyannote_key, args.audio)
    job_id = create_identify_job(pyannote_key, media_uri, voiceprints)
    log("running whisper ASR while identify job runs…")
    asr_segments = asr_with_timestamps(args.audio, openai_key, args.language, base_prompt)
    log(f"whisper done: {len(asr_segments)} segments")
    dia_segments = poll_identify(pyannote_key, job_id)

    merged = merge(asr_segments, dia_segments)
    transcript = render_transcript(merged)

    if args.raw_out:
        args.raw_out.write_text(
            json.dumps({"diarization": dia_segments, "asr": asr_segments, "merged": merged}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"raw → {args.raw_out}")
    if args.out:
        args.out.write_text(transcript, encoding="utf-8")
        log(f"transcript → {args.out}")
    else:
        sys.stdout.write(transcript)

    speaker_counts: dict[str, int] = {}
    for seg in merged:
        speaker_counts[seg["speaker"]] = speaker_counts.get(seg["speaker"], 0) + 1
    log(f"speaker segment counts: {speaker_counts}")
    no_speech = sum(1 for seg in merged if seg.get("no_speech"))
    if no_speech:
        log(f"flagged {no_speech} segment(s) as no-speech (suspected hallucination)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
