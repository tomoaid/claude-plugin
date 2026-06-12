#!/usr/bin/env python3
"""Upload an audio file to pyannote.ai temporary storage.

Usage:
    PYANNOTEAI_API_KEY=sk-... python3 scripts/pyannote_upload.py <audio_file> [object_key]

Prints the resulting media URI (media://<object_key>) on success.
Uploaded files auto-delete after 48 hours.
Docs: https://docs.pyannote.ai/tutorials/how-to-upload-files
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

API_BASE = "https://api.pyannote.ai/v1"


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "audio"


def create_presigned_url(api_key: str, object_key: str) -> str:
    req = urllib.request.Request(
        f"{API_BASE}/media/input",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps({"url": f"media://{object_key}"}).encode("utf-8"),
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    presigned = body.get("url") or body.get("presigned_url")
    if not presigned:
        raise RuntimeError(f"no presigned URL in response: {body}")
    return presigned


def upload_file(presigned_url: str, file_path: Path) -> None:
    data = file_path.read_bytes()
    req = urllib.request.Request(
        presigned_url,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
        data=data,
    )
    with urllib.request.urlopen(req):
        pass  # 非 2xx urlopen 會自己 raise HTTPError


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload an audio file to pyannote.ai")
    parser.add_argument("file", type=Path, help="Path to audio file")
    parser.add_argument(
        "object_key",
        nargs="?",
        help="Object key (default: slugified filename + timestamp). Must be unique per team.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("PYANNOTEAI_API_KEY")
    if not api_key:
        print("error: PYANNOTEAI_API_KEY is not set", file=sys.stderr)
        return 2
    if not args.file.is_file():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2

    object_key = args.object_key or slugify(f"{args.file.stem}-{int(time.time())}")
    media_uri = f"media://{object_key}"

    print(f"→ creating presigned URL for {media_uri}", file=sys.stderr)
    presigned = create_presigned_url(api_key, object_key)
    print(f"→ uploading {args.file.name} ({args.file.stat().st_size} bytes)", file=sys.stderr)
    upload_file(presigned, args.file)
    print(media_uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
