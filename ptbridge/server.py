"""
HTTP API + web UI. Standard library only (ThreadingHTTPServer); meant to sit
behind cloudflared or a reverse proxy that terminates TLS.

  GET  /                      web UI (asks for the token, keeps it in localStorage)
  GET  /health                liveness, no auth
  GET  /api/status            printer status (?cached=1: last known, no connection)
  GET  /api/jobs              recent jobs
  GET  /api/jobs/<id>/preview.png
  POST /api/print             JSON {image: base64, ...options}  or  raw image body + ?options
  POST /api/print/text        JSON {text, align, ...options}
  POST /api/preview           same as /api/print, renders only

Auth: "Authorization: Bearer <token>" or "X-Api-Key: <token>".
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import __version__
from .service import ApiError, Bridge, JobParams, decode_image_field

log = logging.getLogger("ptbridge.http")

WEB_DIR = Path(__file__).parent / "web"
PREVIEW_PATH = re.compile(r"^/api/jobs/([0-9a-f]{8,32})/preview\.png$")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp", "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    server_version = f"pt750w-print-trax/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    bridge: Bridge  # set by serve()

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - BaseHTTPRequestHandler API
        log.info("%s %s", self.client_ip(), fmt % args)

    def client_ip(self) -> str:
        return (self.headers.get("CF-Connecting-IP")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def send_bytes(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def fail(self, err: ApiError) -> None:
        body = {"ok": False, "error": {"code": err.code, "message": err.message}}
        if err.details:
            body["error"]["details"] = err.details
        self.send_json(err.http, body)

    def authorized(self) -> bool:
        token = self.bridge.cfg.token
        given = self.headers.get("X-Api-Key", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if given and token and hmac.compare_digest(given.encode(), token.encode()):
            return True
        time.sleep(0.4)  # blunt brute force a little
        return False

    def read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raise ApiError("LENGTH_REQUIRED", "Chunked uploads are not supported – send Content-Length.", 411)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", "Invalid Content-Length") from exc
        limit = int(self.bridge.cfg.max_upload_mb * 1024 * 1024)
        if length > limit:
            raise ApiError("TOO_LARGE", f"Upload is larger than {self.bridge.cfg.max_upload_mb:g} MB.", 413)
        return self.rfile.read(length) if length else b""

    def json_body(self, raw: bytes) -> dict:
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", "Body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ApiError("BAD_REQUEST", "Body must be a JSON object")
        return data

    # -- routing ----------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = dict(parse_qsl(url.query))
        try:
            if path in ("/", "/index.html"):
                body = (WEB_DIR / "index.html").read_bytes()
                self.send_bytes(200, body, "text/html; charset=utf-8", {
                    "Content-Security-Policy": (
                        "default-src 'self'; img-src 'self' data: blob:; "
                        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "font-src https://cdn.jsdelivr.net; "
                        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "connect-src 'self'; frame-ancestors 'none'"
                    ),
                })
                return
            if path == "/health":
                self.send_json(200, {"ok": True, "name": "pt750w-print-trax", "version": __version__})
                return
            if not path.startswith("/api/"):
                raise ApiError("NOT_FOUND", "Not found", 404)
            if not self.authorized():
                raise ApiError("UNAUTHORIZED", "Missing or wrong API token.", 401)

            if path == "/api/status":
                cached = query.get("cached") in ("1", "true")
                self.send_json(200, {"ok": True, **self.bridge.status(cached=cached)})
                return
            if path == "/api/jobs":
                self.send_json(200, {"ok": True, "jobs": self.bridge.jobs.list()})
                return
            match = PREVIEW_PATH.match(path)
            if match:
                png = self.bridge.jobs.preview(match.group(1))
                if png is None:
                    raise ApiError("NOT_FOUND", "No preview for this job", 404)
                self.send_bytes(200, png, "image/png")
                return
            raise ApiError("NOT_FOUND", "Not found", 404)
        except ApiError as err:
            self.fail(err)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            self.fail(ApiError("SERVER", "Internal error – see the bridge log.", 500))

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/")
        query = dict(parse_qsl(url.query))
        try:
            if path not in ("/api/print", "/api/print/text", "/api/preview"):
                raise ApiError("NOT_FOUND", "Not found", 404)
            if not self.authorized():
                # Read and drop the body so the connection stays usable.
                self.read_body()
                raise ApiError("UNAUTHORIZED", "Missing or wrong API token.", 401)

            raw = self.read_body()
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cfg = self.bridge.cfg

            if path == "/api/print/text":
                body = self.json_body(raw)
                params = JobParams(body, cfg)
                align = str(body.get("align") or "center").lower()
                if align not in ("left", "center", "right"):
                    raise ApiError("BAD_REQUEST", "align must be left, center or right")
                result = self.bridge.print_text(str(body.get("text") or ""), params, align)
            else:
                if ctype == "application/json":
                    body = self.json_body(raw)
                    data = decode_image_field(body.get("image"))
                    params = JobParams(body, cfg)
                elif ctype in IMAGE_TYPES:
                    data = raw
                    params = JobParams(query, cfg)
                else:
                    raise ApiError("UNSUPPORTED_MEDIA", "Send application/json or an image body.", 415)
                if not data:
                    raise ApiError("BAD_REQUEST", "Empty image")
                if path == "/api/preview":
                    params.dry_run = True
                result = self.bridge.print_image(data, params)

            self.send_json(200, {"ok": True, **result})
        except ApiError as err:
            self.fail(err)
        except Exception:  # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self.fail(ApiError("SERVER", "Internal error – see the bridge log.", 500))


def serve(bridge: Bridge) -> None:
    cfg = bridge.cfg
    cfg.ensure_token()
    Handler.bridge = bridge
    httpd = ThreadingHTTPServer((cfg.listen, cfg.port), Handler)
    httpd.daemon_threads = True
    log.info("pt750w-print-trax %s on http://%s:%d → printer %s:%d", __version__, cfg.listen, cfg.port,
             cfg.printer_host or "(PTB_PRINTER_HOST not set!)", cfg.printer_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
