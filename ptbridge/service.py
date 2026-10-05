"""
The bridge itself, independent of HTTP: parse a request's options, size the
image for the loaded tape, send it, remember it. Both the HTTP API and the
CLI go through `Bridge`.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from . import __version__
from . import protocol as p
from .config import Config
from .jobs import JobLog, new_id
from .printer import Printer, PrinterError
from .raster import (Rendered, RenderError, RenderOptions, load_image, preview_png, preview_strip, render,
                     text_image)

log = logging.getLogger("ptbridge")

# A cached status counts as "the tape that is loaded" for previews this long.
STATUS_FRESH_SECONDS = 15 * 60


class ApiError(Exception):
    def __init__(self, code: str, message: str, http: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http
        self.details = details


# --- option parsing -------------------------------------------------------

def _num(src: dict, key: str, lo: float, hi: float, default):
    raw = src.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", f"{key} must be a number") from exc
    if not lo <= value <= hi:
        raise ApiError("BAD_REQUEST", f"{key} must be between {lo:g} and {hi:g}")
    return value


def _flag(src: dict, key: str, default: bool) -> bool:
    raw = src.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _choice(src: dict, key: str, allowed: tuple[str, ...], default: str) -> str:
    raw = src.get(key)
    if raw is None or raw == "":
        return default
    value = str(raw).strip().lower()
    if value not in allowed:
        raise ApiError("BAD_REQUEST", f"{key} must be one of: {', '.join(allowed)}")
    return value


class JobParams:
    def __init__(self, src: dict, cfg: Config):
        self.render = RenderOptions(
            width_mm=_num(src, "widthMm", 1, 1000, None),
            height_mm=_num(src, "heightMm", 1, 1000, None),
            fit=_choice(src, "fit", ("exact", "fill"), "exact"),
            rotate=_choice(src, "rotate", ("auto", "across", "0", "90", "180", "270"), "auto"),
            threshold=int(_num(src, "threshold", 1, 254, 128)),
            dither=_flag(src, "dither", False),
            invert=_flag(src, "invert", False),
            high_res=_flag(src, "highRes", cfg.high_res),
        )
        self.copies = int(_num(src, "copies", 1, cfg.max_copies, 1))
        self.cut = _choice(src, "cut", ("each", "half", "none"), cfg.cut)
        self.chain = _flag(src, "chain", cfg.chain)
        self.margin_mm = _num(src, "marginMm", 0, 50, cfg.margin_mm)
        # Optional: the tape the job is meant for. 3.5 mm tape reports as 4.
        tape = _num(src, "tapeMm", 3, 24, None)
        self.tape_mm = None if tape is None else (4 if tape < 4 else int(round(tape)))
        if self.tape_mm is not None and self.tape_mm not in p.TAPES:
            raise ApiError("BAD_REQUEST", "tapeMm must be one of 3.5, 6, 9, 12, 18, 24")
        self.name = str(src.get("jobName") or "").strip()[:120]
        self.source = str(src.get("source") or "").strip()[:60]
        self.dry_run = _flag(src, "dryRun", False)
        default_profile = cfg.profile if cfg.profile in p.PROFILES else "compat"
        self.profile = _choice(src, "profile", p.PROFILES, default_profile)
        if self.profile not in p.HIGH_RES_PROFILES:
            self.render.high_res = False
        default_batch = cfg.batch_mode if cfg.batch_mode in p.BATCH_MODES else "once"
        self.batch_mode = _choice(src, "batchMode", p.BATCH_MODES, default_batch)

    def job_options(self, tape_mm: int, media_type: int) -> p.JobOptions:
        return p.JobOptions(
            tape_mm=tape_mm,
            media_type=media_type if media_type not in (0x00, 0xFF) else 0x01,
            copies=self.copies,
            auto_cut=self.cut != "none",
            half_cut=self.cut == "half",
            chain=self.chain,
            margin_dots=p.mm_to_dots(self.margin_mm),
            high_res=self.render.high_res,
            profile=self.profile,
            batch_mode=self.batch_mode,
        )


def decode_image_field(value) -> bytes:
    if not isinstance(value, str) or not value:
        raise ApiError("BAD_REQUEST", "image (base64) is required")
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ApiError("BAD_REQUEST", "image is not valid base64") from exc


# --- batches ----------------------------------------------------------------

BATCH_ID = re.compile(r"^[0-9a-f]{24}$")


class BatchStore:
    """
    Labels collected one request at a time, printed as one job. Kept in
    memory as the uploaded images, so they are rendered at print time for
    the tape loaded then. Abandoned batches expire.
    """

    TTL = 30 * 60
    MAX_LABELS = 500
    MAX_BYTES = 80 * 1024 * 1024

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}

    def _purge(self) -> None:
        cutoff = time.time() - self.TTL
        for key in [k for k, v in self.items.items() if v["touched"] < cutoff]:
            del self.items[key]

    def create(self) -> str:
        with self.lock:
            self._purge()
            if len(self.items) >= 20:
                raise ApiError("BUSY", "Too many open batches – print or cancel one first.", 429)
            batch_id = secrets.token_hex(12)
            self.items[batch_id] = {"touched": time.time(), "labels": {}, "bytes": 0}
            return batch_id

    def _get(self, batch_id: str) -> dict:
        batch = self.items.get(batch_id) if BATCH_ID.match(batch_id or "") else None
        if batch is None:
            raise ApiError("NOT_FOUND", "Unknown or expired batch – start it again.", 404)
        return batch

    def add(self, batch_id: str, index: int, data: bytes, width_mm: float | None, height_mm: float | None,
            name: str) -> int:
        load_image(data)  # refuse what cannot be decoded now, not at print time
        with self.lock:
            batch = self._get(batch_id)
            if index not in batch["labels"] and len(batch["labels"]) >= self.MAX_LABELS:
                raise ApiError("TOO_LARGE", f"A batch holds at most {self.MAX_LABELS} labels.", 413)
            old = batch["labels"].get(index)
            size = batch["bytes"] + len(data) - (len(old["data"]) if old else 0)
            if size > self.MAX_BYTES:
                raise ApiError("TOO_LARGE", "The batch is too large – print it in parts.", 413)
            batch["labels"][index] = {"data": data, "widthMm": width_mm, "heightMm": height_mm, "name": name}
            batch["bytes"] = size
            batch["touched"] = time.time()
            return len(batch["labels"])

    def labels(self, batch_id: str) -> list[dict]:
        with self.lock:
            batch = self._get(batch_id)
            batch["touched"] = time.time()
            if not batch["labels"]:
                raise ApiError("BAD_REQUEST", "The batch is empty.")
            return [batch["labels"][i] for i in sorted(batch["labels"])]

    def delete(self, batch_id: str) -> None:
        with self.lock:
            self.items.pop(batch_id, None)


# --- the bridge -----------------------------------------------------------

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.printer = Printer(
            cfg.printer_host,
            cfg.printer_port,
            connect_timeout=cfg.connect_timeout,
            status_timeout=cfg.status_timeout,
            wait_timeout=cfg.wait_timeout,
            snmp_community=cfg.snmp_community,
            snmp_timeout=cfg.snmp_timeout,
            snmp_port=cfg.snmp_port,
            close_wait=cfg.close_wait,
        )
        self.jobs = JobLog(cfg.data_dir, cfg.history)
        self.batches = BatchStore()
        self.started = time.time()

    # -- info ------------------------------------------------------------

    def info(self) -> dict:
        return {
            "name": "pt750w-print-trax",
            "version": __version__,
            "printer": f"{self.cfg.printer_host or '?'}:{self.cfg.printer_port}",
            "defaults": {
                "tapeMm": self.cfg.default_tape_mm,
                "marginMm": self.cfg.margin_mm,
                "cut": self.cfg.cut,
                "chain": self.cfg.chain,
                "maxCopies": self.cfg.max_copies,
            },
            "tapes": [{"mm": mm, "label": p.TAPE_LABELS[mm], "printableMm": p.dots_to_mm(pins)}
                      for mm, (pins, _) in p.TAPES.items()],
            "uptime": int(time.time() - self.started),
        }

    def status(self, cached: bool = False) -> dict:
        out = {"bridge": self.info()}
        if cached:
            out["printer"] = {
                "reachable": None,
                "status": self.printer.last_status,
                "statusAt": self.printer.last_status_at,
                "lastError": self.printer.last_error,
                "statusSupported": self.printer.status_supported,
                "statusVia": self.printer.status_via,
            }
            return out
        try:
            result = self.printer.status()
        except PrinterError as exc:
            raise ApiError(exc.code, exc.message, exc.http, {"bridge": out["bridge"]}) from exc
        result["statusAt"] = self.printer.last_status_at
        out["printer"] = result
        return out

    # -- printing --------------------------------------------------------

    def _tape_for(self, status: dict | None, params: JobParams) -> tuple[int, str]:
        if status and status.get("tapeMm") in p.TAPES:
            loaded = status["tapeMm"]
            if params.tape_mm and params.tape_mm != loaded:
                raise ApiError(
                    "TAPE_MISMATCH",
                    f"Loaded tape is {p.TAPE_LABELS[loaded]}, the job asks for {p.TAPE_LABELS[params.tape_mm]}.",
                    409,
                )
            return loaded, "printer"
        if params.tape_mm:
            return params.tape_mm, "request"
        return self.cfg.default_tape_mm, "default"

    def print_image(self, data: bytes, params: JobParams) -> dict:
        img = load_image(data)
        return self._run(lambda tape: [render(img, tape, params.render)], params, "image")

    def print_batch(self, batch_id: str, params: JobParams, orientation: str = "along") -> dict:
        """
        Every label of a batch as ONE job: a page per label, in upload order,
        rendered now for the tape that is loaded now. With cut=half that is
        one strip, half-cut between the labels and cut once at the end.
        """
        labels = self.batches.labels(batch_id)
        rotate = "across" if orientation == "across" else "auto"
        images = [(load_image(label["data"]), label) for label in labels]

        def make(tape: int) -> list[Rendered]:
            return [
                render(img, tape, replace(params.render, width_mm=label["widthMm"],
                                          height_mm=label["heightMm"], rotate=rotate))
                for img, label in images
            ]

        if not params.name:
            params.name = f"Batch · {len(labels)} label{'s' if len(labels) != 1 else ''}"
        result = self._run(make, params, "batch")
        result["job"]["orientation"] = orientation
        if not params.dry_run:
            self.batches.delete(batch_id)
        return result

    def print_text(self, text: str, params: JobParams, align: str = "center") -> dict:
        if not str(text).strip():
            raise ApiError("BAD_REQUEST", "text is required")
        if len(text) > 500:
            raise ApiError("BAD_REQUEST", "text is too long (500 characters)")
        params.render.fit = "fill"
        params.render.rotate = "0"
        params.render.width_mm = params.render.height_mm = None
        if not params.name:
            params.name = str(text).strip().splitlines()[0][:60]
        return self._run(lambda tape: [render(text_image(text, tape, align=align), tape, params.render)],
                         params, "text")

    def _keep_last_job(self, data: bytes) -> None:
        """The exact bytes of the last job, for `nc <printer> 9100 < data/last-job.bin` and analysis."""
        try:
            (self.cfg.data_dir / "last-job.bin").write_bytes(data)
        except OSError:
            pass

    def _run(self, make, params: JobParams, kind: str) -> dict:
        started = time.monotonic()
        holder: dict = {}

        def build(status: dict | None) -> tuple[bytes, int]:
            tape, source = self._tape_for(status, params)
            rendered: list[Rendered] = make(tape)
            holder.update(rendered=rendered, tape=tape, source=source)
            media = status["mediaType"] if status else 0x01
            pages = [r.lines for r in rendered] * params.copies
            job = p.build_pages(pages, params.job_options(tape, media), preamble=False)
            if not params.dry_run:
                self._keep_last_job(p.INVALIDATE + p.INITIALIZE + job)
            return job, len(pages)

        try:
            if params.dry_run:
                status = self.printer.last_status
                fresh = self.printer.last_status_at and time.time() - self.printer.last_status_at < STATUS_FRESH_SECONDS
                if params.tape_mm:
                    status = None  # an explicit tape wins for a preview
                build(status if fresh else None)
                result = None
            else:
                result = self.printer.run(build)
        except PrinterError as exc:
            raise ApiError(exc.code, exc.message, exc.http, {"printer": exc.status} if exc.status else None) from exc
        except RenderError as exc:
            raise ApiError("BAD_IMAGE", str(exc), 422) from exc

        rendered_all: list[Rendered] = holder["rendered"]
        rendered = rendered_all[0]
        margin_dots = p.mm_to_dots(params.margin_mm)
        gap = 2 * margin_dots * (2 if rendered.high_res else 1)
        preview = preview_strip([r.lines for r in rendered_all], holder["tape"], gap, rendered.high_res)
        warnings: list[str] = []
        for r in rendered_all:
            for warning in r.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        job = {
            "id": new_id(),
            "name": params.name or kind,
            "source": params.source,
            "kind": kind,
            "state": "preview" if params.dry_run else result.state,
            "copies": params.copies,
            "tapeMm": holder["tape"],
            "tapeLabel": p.TAPE_LABELS.get(holder["tape"], f"{holder['tape']} mm"),
            "tapeSource": holder["source"],
            "labels": len(rendered_all),
            # One label's size, or – for a batch – the first label's size and the whole strip.
            "lengthMm": rendered.length_mm,
            "heightMm": p.dots_to_mm(rendered.height_dots),
            "stripMm": round(sum(r.length_mm for r in rendered_all) * params.copies
                             + (len(rendered_all) * params.copies - 1) * 2 * params.margin_mm, 1),
            "scalePct": min(r.scale_pct for r in rendered_all),
            "rotated": rendered.rotated,
            "cut": params.cut,
            "highRes": rendered.high_res,
            "batchMode": params.batch_mode,
            "warnings": warnings + ([] if result is None else result.notes),
            "pagesConfirmed": 0 if result is None else result.pages_confirmed,
            "durationMs": int((time.monotonic() - started) * 1000),
            "profile": params.profile,
            "statusVia": self.printer.status_via,
            "statusBefore": (result.status_before or {}).get("raw") if result else None,
            "statusAfter": (result.status_after or {}).get("raw") if result else None,
            "connection": result.connection if result else None,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if not params.dry_run:
            self.jobs.add(dict(job), preview)
            log.info("%s '%s' %s, %s, %d copies", job["state"], job["name"], job["tapeLabel"],
                     f"{job['lengthMm']} mm", job["copies"])
        out = {"job": job, "preview": "data:image/png;base64," + base64.b64encode(preview).decode()}
        if result is not None and (result.status_after or result.status_before):
            out["printer"] = result.status_after or result.status_before
        return out
