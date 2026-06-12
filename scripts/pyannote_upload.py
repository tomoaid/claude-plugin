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
import os
import sys
import time
from pathlib import Path

from _common import slugify, upload_to_pyannote


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
    print(f"→ uploading {args.file.name} ({args.file.stat().st_size} bytes) → media://{object_key}", file=sys.stderr)
    media_uri = upload_to_pyannote(api_key, args.file, object_key)
    print(media_uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
