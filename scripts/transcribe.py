#!/usr/bin/env python3
"""
將音檔送到 OpenAI gpt-4o-transcribe 取得逐字稿。

策略：
- 同時受兩個上限約束：檔案大小 25 MB、音檔時長 1400 秒（≈23.3 分鐘）
- 任一條件超標就用 ffmpeg 切成 ~10 分鐘 chunk，重新編碼為 64 kbps mono MP3
  以縮小體積，再逐段轉錄後合併
- 都沒超標就直接單檔送出
- --prompt / --prompt-file 提供詞彙 priming（人名、產品名、術語；用繁體寫可同時
  把輸出偏向繁體）。切段模式下，每段的 prompt 會自動附上前一段轉錄結尾，
  維持跨段上下文連續性
- 每段都會印一行進度到 stderr，方便上層觀察

用法：
    python transcribe.py <audio_path> [output_path] [--language zh] \
        [--prompt-file .tomoaid/asr-glossary.md]

退出碼：
    0 成功；非 0 表示失敗，stderr 會有訊息
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

API_URL = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "gpt-4o-transcribe"
MAX_CHUNK_BYTES = 24 * 1024 * 1024
# OpenAI gpt-4o-transcribe 硬上限是 1400 秒，留 20 秒緩衝
MAX_CHUNK_SECONDS = 1380
CHUNK_SECONDS = 600  # 10 分鐘
CONTEXT_TAIL_CHARS = 300  # 切段模式下，附給下一段當上下文的前段結尾長度
RETRY_SLEEP_SEC = 5  # curl 網路層錯誤的單次重試間隔


def log(msg: str) -> None:
    print(f"[transcribe] {msg}", file=sys.stderr, flush=True)


def probe_duration(src: str) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            src,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def split_audio(src: str, out_dir: str) -> list[str]:
    """用 ffmpeg 切段並重新編碼成 64 kbps mono MP3。"""
    pattern = os.path.join(out_dir, "chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-f", "segment",
            "-segment_time", str(CHUNK_SECONDS),
            "-c:a", "libmp3lame",
            "-b:a", "64k",
            "-ac", "1",
            "-ar", "16000",
            pattern,
        ],
        check=True,
    )
    chunks = sorted(Path(out_dir).glob("chunk_*.mp3"))
    return [str(c) for c in chunks]


def compose_prompt(base: str, prev_tail: str) -> str:
    """詞彙表 + 前一段結尾。兩者都可為空。"""
    parts = [p for p in (base.strip(), prev_tail.strip()) if p]
    return "\n".join(parts)


def transcribe_one(path: str, api_key: str, language: str | None, prompt: str = "") -> str:
    cmd = [
        "curl", "-sS", "--fail-with-body",
        "--config", "-",  # Authorization 走 stdin，key 不進 ps 可見的 argv
        "-X", "POST", API_URL,
        "-F", f"file=@{path}",
        "-F", f"model={MODEL}",
    ]
    if language:
        cmd += ["-F", f"language={language}"]
    if prompt:
        # --form-string：值不做 @ / < / ;type 解析，中文與標點安全
        cmd += ["--form-string", f"prompt={prompt}"]

    curl_cfg = f'header = "Authorization: Bearer {api_key}"\n'
    proc = subprocess.run(cmd, capture_output=True, text=True, input=curl_cfg)
    if proc.returncode not in (0, 22):  # 22 = HTTP error（原樣回報不重試）；其餘是網路層，重試一次
        log(f"curl 網路層錯誤（exit {proc.returncode}），{RETRY_SLEEP_SEC}s 後重試一次…")
        time.sleep(RETRY_SLEEP_SEC)
        proc = subprocess.run(cmd, capture_output=True, text=True, input=curl_cfg)
    if proc.returncode != 0:
        raise RuntimeError(
            f"OpenAI API failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bad JSON from API: {proc.stdout[:500]}") from e
    if "text" not in data:
        raise RuntimeError(f"No 'text' field in response: {data}")
    return data["text"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("output", nargs="?", help="若省略則寫到 stdout")
    parser.add_argument("--language", help="ISO-639-1，如 zh / en；不傳由模型自動偵測")
    parser.add_argument("--prompt", default="", help="詞彙/風格 priming 文字")
    parser.add_argument("--prompt-file", type=Path, help="從檔案讀 priming 文字（接在 --prompt 後）")
    args = parser.parse_args()

    base_prompt = args.prompt
    if args.prompt_file:
        if not args.prompt_file.is_file():
            print(f"ERROR: 找不到 prompt 檔 {args.prompt_file}", file=sys.stderr)
            return 2
        base_prompt = compose_prompt(base_prompt, args.prompt_file.read_text(encoding="utf-8"))

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY 未設定", file=sys.stderr)
        return 2

    audio = args.audio
    if not os.path.isfile(audio):
        print(f"ERROR: 找不到音檔 {audio}", file=sys.stderr)
        return 2

    size = os.path.getsize(audio)
    duration = probe_duration(audio)
    log(f"input: {audio} ({size / 1024 / 1024:.1f} MB, {duration:.0f}s)")

    needs_split = size > MAX_CHUNK_BYTES or duration > MAX_CHUNK_SECONDS

    parts: list[str] = []
    if not needs_split:
        log("size + duration OK，單檔送出")
        parts.append(transcribe_one(audio, api_key, args.language, base_prompt))
    else:
        reason = []
        if size > MAX_CHUNK_BYTES:
            reason.append(f"size > {MAX_CHUNK_BYTES // 1024 // 1024} MB")
        if duration > MAX_CHUNK_SECONDS:
            reason.append(f"duration > {MAX_CHUNK_SECONDS}s")
        with tempfile.TemporaryDirectory() as tmp:
            log(f"切段中（{', '.join(reason)}）…")
            chunks = split_audio(audio, tmp)
            log(f"切成 {len(chunks)} 段")
            prev_tail = ""
            for i, chunk in enumerate(chunks, 1):
                cs = os.path.getsize(chunk)
                log(f"  段 {i}/{len(chunks)} ({cs / 1024 / 1024:.1f} MB)")
                text = transcribe_one(chunk, api_key, args.language,
                                      compose_prompt(base_prompt, prev_tail))
                parts.append(text)
                prev_tail = text.strip()[-CONTEXT_TAIL_CHARS:]

    full = "\n\n".join(p.strip() for p in parts if p.strip())

    if args.output:
        Path(args.output).write_text(full, encoding="utf-8")
        log(f"已寫出 {args.output}（{len(full)} 字）")
    else:
        sys.stdout.write(full)

    return 0


if __name__ == "__main__":
    sys.exit(main())
