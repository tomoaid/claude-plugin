#!/usr/bin/env python3
"""Local web UI to label diarized speaker clips with team member names.

Usage:
    python3 label_server.py --dir <out-dir-from-voiceprint_extract> \
        [--port 8765] [--team "Alice,Bob,Carol"] [--timeout 900]

Serves http://127.0.0.1:<port>/ with one card per speaker clip (audio player +
name input). On 儲存, writes <dir>/labels.json ({speaker_id: name|null}), prints
it to stdout, and exits 0. Exits 3 if nothing is saved within --timeout seconds.

The page is the sibling label_page.html template; __MANIFEST__ / __TEAM__
placeholders are filled at startup.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "label_page.html"


def main() -> int:
    parser = argparse.ArgumentParser(description="Label diarized speaker clips in a browser")
    parser.add_argument("--dir", type=Path, required=True, help="out-dir from voiceprint_extract.py")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--team", default="", help="Comma-separated member names for autocomplete")
    parser.add_argument("--timeout", type=int, default=900, help="Exit 3 if not saved within N seconds")
    args = parser.parse_args()

    manifest_path = args.dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: {manifest_path} not found (run voiceprint_extract.py first)", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    team = [n.strip() for n in args.team.split(",") if n.strip()]
    page = TEMPLATE_PATH.read_text(encoding="utf-8").replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False)).replace(
        "__TEAM__", json.dumps(team, ensure_ascii=False))

    saved = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/clips/"):
                clip = (args.dir / self.path.lstrip("/")).resolve()
                if clip.is_file() and clip.parent == (args.dir / "clips").resolve():
                    body = clip.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/save":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                labels = data["labels"]
                assert isinstance(labels, dict)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
                return
            (args.dir / "labels.json").write_text(
                json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            saved.set()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"error: cannot bind port {args.port} ({e.strerror}) — try another, e.g. --port {args.port + 1}", file=sys.stderr)
        return 2
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"labeling UI → http://127.0.0.1:{args.port}/", file=sys.stderr, flush=True)

    if not saved.wait(timeout=args.timeout):
        print("error: timed out waiting for labels", file=sys.stderr)
        server.shutdown()
        return 3
    server.shutdown()
    labels = json.loads((args.dir / "labels.json").read_text(encoding="utf-8"))
    print(json.dumps(labels, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
