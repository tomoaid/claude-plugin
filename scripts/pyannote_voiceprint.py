#!/usr/bin/env python3
"""Batch-create pyannote.ai voiceprints from local audio files.

Usage:
    PYANNOTEAI_API_KEY=sk-... python3 scripts/pyannote_voiceprint.py \
        FILE [FILE ...] [--out voiceprints.json]

For each FILE: uploads to pyannote temp storage → creates voiceprint job →
polls until done → collects the voiceprint blob. The label is the file's stem
(e.g. /a/Eric.m4a → "Eric"). Writes a JSON map {label: voiceprint_b64} to --out.
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
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.pyannote.ai/v1"
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 300


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "audio"


def _request(method: str, url: str, *, api_key: str | None = None, json_body=None, raw_body: bytes | None = None, content_type: str | None = None) -> dict | bytes:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            if not body:
                return {}
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(body.decode("utf-8"))
            return body
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {detail}") from None


def upload(api_key: str, file_path: Path, object_key: str) -> str:
    media_uri = f"media://{object_key}"
    resp = _request("POST", f"{API_BASE}/media/input", api_key=api_key, json_body={"url": media_uri})
    presigned = resp.get("url") or resp.get("presigned_url")
    if not presigned:
        raise RuntimeError(f"no presigned URL: {resp}")
    _request("PUT", presigned, raw_body=file_path.read_bytes(), content_type="application/octet-stream")
    return media_uri


def create_voiceprint_job(api_key: str, media_uri: str) -> str:
    resp = _request("POST", f"{API_BASE}/voiceprint", api_key=api_key, json_body={"url": media_uri})
    job_id = resp.get("jobId")
    if not job_id:
        raise RuntimeError(f"no jobId in response: {resp}")
    return job_id


def poll_job(api_key: str, job_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while True:
        resp = _request("GET", f"{API_BASE}/jobs/{job_id}", api_key=api_key)
        status = resp.get("status")
        if status in ("succeeded", "failed", "canceled"):
            return resp
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish within {POLL_TIMEOUT_SEC}s (last status: {status})")
        time.sleep(POLL_INTERVAL_SEC)


def process_file(api_key: str, file_path: Path) -> tuple[str, str]:
    label = file_path.stem
    object_key = slugify(f"voiceprint-{label}-{int(time.time())}")
    print(f"[{label}] uploading {file_path.name}…", file=sys.stderr)
    media_uri = upload(api_key, file_path, object_key)
    print(f"[{label}] creating voiceprint job…", file=sys.stderr)
    job_id = create_voiceprint_job(api_key, media_uri)
    print(f"[{label}] polling job {job_id}…", file=sys.stderr)
    result = poll_job(api_key, job_id)
    if result["status"] != "succeeded":
        err = (result.get("output") or {}).get("error") or result
        raise RuntimeError(f"[{label}] job failed: {err}")
    voiceprint = (result.get("output") or {}).get("voiceprint")
    if not voiceprint:
        raise RuntimeError(f"[{label}] no voiceprint in succeeded job: {result}")
    print(f"[{label}] ✓ voiceprint ready ({len(voiceprint)} chars)", file=sys.stderr)
    return label, voiceprint


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-create pyannote.ai voiceprints")
    parser.add_argument("files", nargs="+", type=Path, help="Audio files (label = filename stem)")
    parser.add_argument("--out", type=Path, default=Path("voiceprints.json"), help="Output JSON path")
    parser.add_argument("--merge", action="store_true", help="Merge into existing --out JSON (new labels override) instead of overwriting")
    args = parser.parse_args()

    api_key = os.environ.get("PYANNOTEAI_API_KEY")
    if not api_key:
        print("error: PYANNOTEAI_API_KEY is not set", file=sys.stderr)
        return 2

    voiceprints: dict[str, str] = {}
    failures: list[tuple[str, str]] = []
    for file_path in args.files:
        if not file_path.is_file():
            failures.append((file_path.name, "file not found"))
            continue
        try:
            label, vp = process_file(api_key, file_path)
            voiceprints[label] = vp
        except Exception as e:
            failures.append((file_path.stem, str(e)))
            print(f"[{file_path.stem}] ✗ {e}", file=sys.stderr)

    if args.merge and args.out.is_file():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
        existing.update(voiceprints)
        voiceprints = existing
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
