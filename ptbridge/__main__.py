"""
python -m ptbridge [serve]                 HTTP bridge (default)
python -m ptbridge status                  ask the printer
python -m ptbridge print FILE [options]    print an image
python -m ptbridge text "TEXT" [options]   print a text label
python -m ptbridge dump FILE -o job.bin    raw printer bytes, e.g. for: nc <printer> 9100 < job.bin
python -m ptbridge token                   show the API token
python -m ptbridge mock [--tape 18]        fake printer on :9100 for testing
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
from pathlib import Path

from . import protocol as p
from .config import Config
from .raster import RenderError, RenderOptions, load_image, preview_png, render
from .service import ApiError, Bridge, JobParams


def _job_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--cut", choices=("each", "half", "none"))
    parser.add_argument("--chain", action="store_true", help="do not feed/cut after the last label")
    parser.add_argument("--margin-mm", type=float)
    parser.add_argument("--tape", type=float, help="expected tape width in mm")
    parser.add_argument("--high-res", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="render only, do not print")
    parser.add_argument("--preview", help="write the rendered raster to this PNG")


def _params(args, extra: dict) -> dict:
    src = dict(extra)
    src.update({
        "copies": args.copies,
        "cut": args.cut,
        "chain": "1" if args.chain else None,
        "marginMm": args.margin_mm,
        "tapeMm": args.tape,
        "highRes": "1" if args.high_res else None,
        "dryRun": "1" if args.dry_run else None,
        "source": "cli",
    })
    return {k: v for k, v in src.items() if v is not None}


def _report(result: dict, preview: str | None) -> None:
    job = result["job"]
    print(json.dumps(job, indent=2, ensure_ascii=False))
    if preview:
        Path(preview).write_bytes(base64.b64decode(result["preview"].split(",", 1)[1]))
        print(f"preview -> {preview}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ptbridge", description="Brother PT-P750W network print bridge")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the HTTP bridge (default)")
    sub.add_parser("status", help="query the printer")
    sub.add_parser("token", help="print the API token")

    pr = sub.add_parser("print", help="print an image file")
    pr.add_argument("file")
    pr.add_argument("--width-mm", type=float)
    pr.add_argument("--height-mm", type=float)
    pr.add_argument("--fit", choices=("exact", "fill"), default="exact")
    pr.add_argument("--rotate", default="auto")
    pr.add_argument("--threshold", type=int, default=128)
    pr.add_argument("--dither", action="store_true")
    _job_args(pr)

    tx = sub.add_parser("text", help="print a text label")
    tx.add_argument("text", help=r"use \n for a second line")
    tx.add_argument("--align", choices=("left", "center", "right"), default="center")
    _job_args(tx)

    dp = sub.add_parser("dump", help="write the raw job to a file instead of the printer")
    dp.add_argument("file")
    dp.add_argument("-o", "--out", required=True)
    dp.add_argument("--tape", type=int, default=24)
    dp.add_argument("--width-mm", type=float)
    dp.add_argument("--height-mm", type=float)
    dp.add_argument("--copies", type=int, default=1)

    mk = sub.add_parser("mock", help="fake printer for testing")
    mk.add_argument("--host", default="127.0.0.1")
    mk.add_argument("--port", type=int, default=9100)
    mk.add_argument("--tape", type=int, default=18)
    mk.add_argument("--out", default="./mock-out")
    mk.add_argument("--silent", action="store_true", help="never answer (like a printer without read-back)")
    mk.add_argument("--cover-open", action="store_true", help="report 'cover open'")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("PTB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cmd = args.cmd or "serve"

    if cmd == "mock":
        from .mock import serve as mock_serve
        mock_serve(args.host, args.port, args.tape, args.out, args.silent, (0, 0x10) if args.cover_open else (0, 0))
        return 0

    if cmd == "dump":
        try:
            img = load_image(Path(args.file).read_bytes())
            r = render(img, args.tape, RenderOptions(width_mm=args.width_mm, height_mm=args.height_mm))
        except RenderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        job = p.build_job(r.lines, p.JobOptions(tape_mm=args.tape, copies=args.copies))
        Path(args.out).write_bytes(job)
        Path(args.out).with_suffix(".png").write_bytes(preview_png(r.lines, args.tape))
        print(f"{len(job)} bytes, {len(r.lines)} lines ({r.length_mm} mm) -> {args.out}")
        for warning in r.warnings:
            print(f"warning: {warning}")
        return 0

    cfg = Config.from_env()

    if cmd == "token":
        print(cfg.ensure_token())
        return 0

    bridge = Bridge(cfg)
    if cmd == "serve":
        from .server import serve
        serve(bridge)
        return 0

    try:
        if cmd == "status":
            print(json.dumps(bridge.status(), indent=2, ensure_ascii=False))
        elif cmd == "print":
            params = JobParams(_params(args, {
                "widthMm": args.width_mm, "heightMm": args.height_mm, "fit": args.fit,
                "rotate": args.rotate, "threshold": args.threshold,
                "dither": "1" if args.dither else None, "jobName": Path(args.file).name,
            }), cfg)
            _report(bridge.print_image(Path(args.file).read_bytes(), params), args.preview)
        elif cmd == "text":
            params = JobParams(_params(args, {}), cfg)
            _report(bridge.print_text(args.text.replace("\\n", "\n"), params, args.align), args.preview)
    except ApiError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
