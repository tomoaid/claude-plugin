#!/usr/bin/env python3
"""Batch-create pyannote.ai voiceprints from local audio files.

Usage:
    PYANNOTEAI_API_KEY=sk-... python3 scripts/pyannote_voiceprint.py \
        FILE [FILE ...] [--out voiceprints.json] [--merge]
    PYANNOTEAI_API_KEY=sk-... python3 scripts/pyannote_voiceprint.py \
        --labels /tmp/voiceprint-setup/<name>/labels.json --out voiceprints.json --merge

FILE mode: label is the file's stem (e.g. /a/Eric.m4a → "Eric").
--labels mode: 讀 label_server 寫出的 labels.json（與 clips/ 同目錄），
為每個有標名字的 speaker 的 clip 建立 voiceprint；標 null（略過）的不處理。
名字須符合 ^[A-Za-z][A-Za-z0-9._-]*$ 且不重複（大小寫不敏感）。

For each clip: uploads to pyannote temp storage → creates voiceprint job →
polls until done → collects the voiceprint blob. Writes a JSON map
{label: voiceprint_b64} to --out.
Docs:
  https://docs.pyannote.ai/tutorials/how-to-upload-files
  https://docs.pyannote.ai/api-reference/voiceprint
  https://docs.pyannote.ai/api-reference/get-job
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from _common import PYANNOTE_BASE, http_json, poll_job, slugify, upload_to_pyannote

NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")


def create_voiceprint_job(api_key: str, media_uri: str) -> str:
    resp = http_json("POST", f"{PYANNOTE_BASE}/voiceprint", api_key=api_key, body={"url": media_uri})
    job_id = resp.get("jobId")
    if not job_id:
        raise RuntimeError(f"no jobId in response: {resp}")
    return job_id


def process_file(api_key: str, file_path: Path, label: str) -> str:
    object_key = slugify(f"voiceprint-{label}-{int(time.time())}")
    print(f"[{label}] uploading {file_path.name}…", file=sys.stderr)
    media_uri = upload_to_pyannote(api_key, file_path, object_key)
    print(f"[{label}] creating voiceprint job…", file=sys.stderr)
    job_id = create_voiceprint_job(api_key, media_uri)
    print(f"[{label}] polling job {job_id}…", file=sys.stderr)
    result = poll_job(api_key, job_id)
    if result["status"] != "succeeded":
        err = (result.get("output") or {}).get("error") or result
        raise RuntimeError(f"job failed: {err}")
    voiceprint = (result.get("output") or {}).get("voiceprint")
    if not voiceprint:
        raise RuntimeError(f"no voiceprint in succeeded job: {result}")
    print(f"[{label}] ✓ voiceprint ready ({len(voiceprint)} chars)", file=sys.stderr)
    return voiceprint


def labeled_clips(labels_path: Path) -> list[tuple[str, Path]]:
    """讀 label_server 寫出的 labels.json，回傳 [(label, clip_path)]。

    labels.json 與 clips/ 同在 voiceprint_extract.py 的 out-dir 下。
    標記介面理論上已擋掉壞名字，這裡是最終防線：名字必須檔名安全、
    不重複（大小寫不敏感）；null（略過）跳過。
    """
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    clips_dir = labels_path.parent / "clips"
    pairs: list[tuple[str, Path]] = []
    seen: dict[str, str] = {}
    for speaker_id, name in labels.items():
        if name is None:
            continue
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid label for {speaker_id}: {name!r}（須英文字母開頭，限 A-Za-z0-9 . _ -）")
        if name.lower() in seen:
            raise ValueError(f"duplicate label {name!r}（與 {seen[name.lower()]!r} 重複，同一人只能標一段）")
        seen[name.lower()] = name
        clip = clips_dir / f"{speaker_id}.wav"
        if not clip.is_file():
            raise ValueError(f"clip not found for {speaker_id}: {clip}")
        pairs.append((name, clip))
    if not pairs:
        raise ValueError(f"no labeled speakers in {labels_path}（至少要標記一位成員）")
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-create pyannote.ai voiceprints")
    parser.add_argument("files", nargs="*", type=Path, help="Audio files (label = filename stem)")
    parser.add_argument("--labels", type=Path, help="label_server 寫出的 labels.json（與 clips/ 同目錄）；與 FILE 擇一")
    parser.add_argument("--out", type=Path, default=Path("voiceprints.json"), help="Output JSON path")
    parser.add_argument("--merge", action="store_true", help="Merge into existing --out JSON (new labels override) instead of overwriting")
    args = parser.parse_args()

    if bool(args.files) == bool(args.labels):
        print("error: 提供音檔（label = 檔名）或 --labels labels.json，二擇一", file=sys.stderr)
        return 2

    if args.labels:
        if not args.labels.is_file():
            print(f"error: labels file not found: {args.labels}", file=sys.stderr)
            return 2
        try:
            jobs = labeled_clips(args.labels)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        jobs = [(p.stem, p) for p in args.files]

    api_key = os.environ.get("PYANNOTEAI_API_KEY")
    if not api_key:
        print("error: PYANNOTEAI_API_KEY is not set", file=sys.stderr)
        return 2

    voiceprints: dict[str, str] = {}
    failures: list[tuple[str, str]] = []
    for label, file_path in jobs:
        if not file_path.is_file():
            failures.append((label, "file not found"))
            continue
        try:
            voiceprints[label] = process_file(api_key, file_path, label)
        except Exception as e:
            failures.append((label, str(e)))
            print(f"[{label}] ✗ {e}", file=sys.stderr)

    if args.merge and args.out.is_file():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
        existing.update(voiceprints)
        voiceprints = existing
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(voiceprints, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(voiceprints)} voiceprint(s) → {args.out}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for name, err in failures:
            print(f"  - {name}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
