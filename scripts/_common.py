"""tomoaid plugin scripts 共用工具（僅標準庫）。

各 script 以 `python3 <plugin>/scripts/<name>.py` 執行時，sys.path[0]
就是 scripts 目錄，直接 `import _common` 即可，不需安裝任何套件。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PYANNOTE_BASE = "https://api.pyannote.ai/v1"
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 600
HTTP_TIMEOUT_SEC = 30  # 一般 JSON 呼叫
UPLOAD_TIMEOUT_SEC = 300  # presigned PUT 上傳（音檔可達數十 MB）
UPLOAD_RETRY_SLEEP_SEC = 5  # 上傳網路層錯誤的單次重試間隔
WHISPER_PROMPT_TOKENS = 224  # whisper-1 只保留 prompt 的最後 224 tokens


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "audio"


def http_json(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: dict | bytes | None = None,
    content_type: str | None = None,
    timeout: float = HTTP_TIMEOUT_SEC,
) -> dict:
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {detail}") from None


def upload_to_pyannote(api_key: str, file_path: Path, object_key: str) -> str:
    """上傳到 pyannote 暫存區（48 小時自動刪除），回傳 media:// URI。

    presigned PUT 遇網路層錯誤（含 timeout）重試一次；HTTP 錯誤
    （http_json 已轉成 RuntimeError）原樣上拋不重試。
    """
    media_uri = f"media://{object_key}"
    resp = http_json("POST", f"{PYANNOTE_BASE}/media/input", api_key=api_key, body={"url": media_uri})
    presigned = resp.get("url") or resp.get("presigned_url")
    if not presigned:
        raise RuntimeError(f"no presigned URL: {resp}")
    data = file_path.read_bytes()
    try:
        http_json("PUT", presigned, body=data, content_type="application/octet-stream", timeout=UPLOAD_TIMEOUT_SEC)
    except OSError:
        time.sleep(UPLOAD_RETRY_SLEEP_SEC)
        http_json("PUT", presigned, body=data, content_type="application/octet-stream", timeout=UPLOAD_TIMEOUT_SEC)
    return media_uri


def poll_job(api_key: str, job_id: str, *, timeout: float = POLL_TIMEOUT_SEC) -> dict:
    """Poll 到 job 進入終態（succeeded / failed / canceled），回傳 job dict。"""
    deadline = time.monotonic() + timeout
    while True:
        job = http_json("GET", f"{PYANNOTE_BASE}/jobs/{job_id}", api_key=api_key)
        status = job.get("status")
        if status in ("succeeded", "failed", "canceled"):
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish within {timeout:.0f}s (last status: {status})")
        time.sleep(POLL_INTERVAL_SEC)


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def probe_duration(path: Path | str) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        text=True,
    )
    return float(out.strip())


def est_tokens(text: str) -> int:
    """粗估 whisper tokenizer 用量（刻意高估）：CJK/全形每字 2 tokens，其餘 3 字元 1 token。"""
    cjk = sum(1 for ch in text if ch >= "⺀")  # CJK 部首區起點以上，含全形符號/假名/諺文
    other = len(text) - cjk
    return cjk * 2 + (other + 2) // 3


def compose_prompt(base: str, prev_tail: str) -> str:
    """詞彙表 + 前段轉錄結尾，總量控制在 whisper 的 prompt 視窗內。

    whisper-1 超過 224 tokens 時會從 prompt「開頭」裁掉。詞彙表（人名、
    術語）是使用者刻意維護的 priming，必須完整存活；前段結尾只是上下文
    連續性的 best effort——預算不夠就從 tail 開頭裁、甚至裁到空。
    """
    base = base.strip()
    prev_tail = prev_tail.strip()
    if prev_tail:
        budget = WHISPER_PROMPT_TOKENS - est_tokens(base)
        if budget <= 0:
            prev_tail = ""
        else:
            while prev_tail and est_tokens(prev_tail) > budget:
                prev_tail = prev_tail[len(prev_tail) // 5 + 1:].lstrip()
    parts = [p for p in (base, prev_tail) if p]
    return "\n".join(parts)
