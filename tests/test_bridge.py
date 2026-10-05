"""python -m unittest discover -s tests"""

import io
import os
import random
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ptbridge import protocol as p  # noqa: E402
from ptbridge.config import Config  # noqa: E402
from ptbridge.mock import serve_in_thread  # noqa: E402
from ptbridge.raster import RenderOptions, canvas_to_lines, lines_to_canvas, render  # noqa: E402
from ptbridge.printer import PrinterError  # noqa: E402
from ptbridge.service import ApiError, Bridge, JobParams  # noqa: E402
from ptbridge import snmp  # noqa: E402


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class PackBits(unittest.TestCase):
    def test_roundtrip(self):
        rnd = random.Random(7)
        samples = [b"", b"\x00", b"\xff" * 16, bytes(range(16)), b"\x00\x00\x01\x01\x01\x02"]
        samples += [bytes(rnd.choice([0, 0, 0, 255, rnd.randrange(256)]) for _ in range(rnd.randrange(1, 300)))
                    for _ in range(500)]
        for data in samples:
            self.assertEqual(p.packbits_decode(p.packbits_encode(data)), data)

    def test_known_encoding(self):
        # Apple's reference example.
        raw = bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22AAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(p.packbits_decode(p.packbits_encode(raw)), raw)
        self.assertEqual(p.packbits_encode(b"\x00" * 16), b"\xf1\x00")


class Raster(unittest.TestCase):
    def test_pin_order_is_transpose(self):
        # One dot at the top-left of the canvas -> line 0, MSB of byte 0.
        canvas = Image.new("1", (3, p.HEAD_PINS), 0)
        canvas.putpixel((0, 0), 1)
        canvas.putpixel((2, 127), 1)
        lines = canvas_to_lines(canvas)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0][0], 0x80)
        self.assertEqual(lines[2][15], 0x01)
        self.assertEqual(lines_to_canvas(lines).tobytes(), canvas.tobytes())

    def test_exact_size_and_centering(self):
        # 30 x 14 mm label, black frame, 900 dpi like label-w.php renders it.
        img = Image.new("L", (1063, 496), 255)
        ImageDraw.Draw(img).rectangle([0, 0, 1062, 495], outline=0, width=20)
        r = render(img, 18, RenderOptions(width_mm=30, height_mm=14))
        self.assertEqual(r.width_dots, p.mm_to_dots(30))
        self.assertEqual(r.height_dots, p.mm_to_dots(14))
        self.assertEqual(r.scale_pct, 100)
        self.assertFalse(r.warnings)
        canvas = lines_to_canvas(r.lines)
        top = (p.HEAD_PINS - r.height_dots) // 2
        self.assertEqual(canvas.getpixel((r.width_dots // 2, top)), 255)
        self.assertEqual(canvas.getpixel((r.width_dots // 2, top - 1)), 0)

    def test_scaled_down_on_narrow_tape(self):
        img = Image.new("L", (300, 140), 0)
        r = render(img, 12, RenderOptions(width_mm=30, height_mm=14))
        self.assertEqual(r.height_dots, 70)
        self.assertLess(r.scale_pct, 100)
        self.assertTrue(r.warnings)

    def test_portrait_is_rotated(self):
        img = Image.new("L", (140, 300), 255)
        r = render(img, 24, RenderOptions(width_mm=14, height_mm=30))
        self.assertEqual(r.rotated, 90)
        self.assertEqual(r.height_dots, p.mm_to_dots(14))
        self.assertEqual(r.width_dots, p.mm_to_dots(30))

    def test_job_structure(self):
        lines = [bytes(16), b"\xff" * 16]
        job = p.build_job(lines, p.JobOptions(tape_mm=18, copies=2, half_cut=True, profile="standard"))
        self.assertTrue(job.startswith(b"\x00" * 100 + b"\x1b\x40\x1b\x69\x61\x01"))
        self.assertTrue(job.endswith(b"\x1a"))
        self.assertEqual(job.count(b"\x1b\x69\x7a"), 2)
        self.assertIn(b"\x1b\x69\x4b\x0c", job)  # half cut + no chain
        # Not in the P750W command set: ESC i A, and Z for an empty line.
        self.assertNotIn(b"\x1b\x69\x41", job)
        self.assertIn(b"\x47\x02\x00\xf1\x00", job)  # the empty line, as G

    def test_default_is_compat(self):
        job = p.build_job([b"\xff" * 16], p.JobOptions(tape_mm=12, half_cut=True))
        # Print information with media kind + width checked, half cut, uncompressed lines.
        self.assertIn(b"\x1b\x69\x7a\x86\x01\x0c", job)
        self.assertIn(b"\x1b\x69\x4b\x0c", job)
        self.assertIn(b"\x4d\x00\x47\x10\x00" + b"\xff" * 16, job)

    def test_minimal_profile(self):
        job = p.build_job([b"\xff" * 16], p.JobOptions(tape_mm=12, profile="minimal"))
        self.assertNotIn(b"\x1b\x69\x4b", job)
        self.assertNotIn(b"\x1b\x69\x64", job)
        self.assertIn(b"\x1b\x69\x4d\x40\x4d\x02", job)


class Status(unittest.TestCase):
    def test_parse(self):
        st = p.parse_status(p.fake_status(12, errors=(0x01, 0x10)))
        self.assertEqual(st["tapeMm"], 12)
        self.assertEqual(st["model"], "PT-P750W")
        self.assertIn("No media", st["errors"])
        self.assertIn("Cover open", st["errors"])


class Snmp(unittest.TestCase):
    def test_request_roundtrip(self):
        req = snmp.get_request(snmp.BROTHER_STATUS_OID, "public", 4242, 1)
        self.assertEqual(snmp.parse_request(req), (1, "public", 4242, snmp.BROTHER_STATUS_OID))

    def test_response_value_and_missing(self):
        status = p.fake_status(12)
        ok = snmp.get_response(7, snmp.BROTHER_STATUS_OID, status, "public", 1)
        self.assertEqual(snmp.parse_response(ok, 7), status)
        for version in (0, 1):
            missing = snmp.get_response(8, snmp.BROTHER_STATUS_OID, None, "public", version)
            self.assertIsNone(snmp.parse_response(missing, 8))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockBase(unittest.TestCase):
    """Bridge -> mock printer over a real socket."""

    def bridge(self, tape=18, silent=False, snmp_on=False):
        port = free_port()
        snmp_port = free_port() if snmp_on else 0
        tmp = tempfile.mkdtemp()
        serve_in_thread(host="127.0.0.1", port=port, tape_mm=tape, out=os.path.join(tmp, "out"), silent=silent,
                        snmp_port=snmp_port or None)
        time.sleep(0.3)
        os.environ.update(PTB_PRINTER_HOST="127.0.0.1", PTB_PRINTER_PORT=str(port), PTB_DATA_DIR=tmp,
                          PTB_STATUS_TIMEOUT="0.5", PTB_WAIT_TIMEOUT="3", PTB_SNMP_TIMEOUT="0.3",
                          PTB_SNMP_PORT=str(snmp_port or free_port()))
        cfg = Config.from_env()
        return Bridge(cfg), Path(tmp, "out")


class EndToEnd(MockBase):

    def test_print_and_confirm(self):
        bridge, out = self.bridge(18)
        img = Image.new("L", (1063, 496), 255)
        ImageDraw.Draw(img).text((50, 50), "TEST", fill=0)
        res = bridge.print_image(png(img), JobParams({"widthMm": 30, "heightMm": 14, "copies": 3}, bridge.cfg))
        self.assertEqual(res["job"]["state"], "printed")
        self.assertEqual(res["job"]["pagesConfirmed"], 3)
        self.assertEqual(res["job"]["tapeMm"], 18)
        self.assertEqual(len(list(out.glob("*.png"))), 3)
        self.assertEqual(len(bridge.jobs.list()), 1)

    def test_tape_mismatch(self):
        bridge, _ = self.bridge(12)
        with self.assertRaises(ApiError) as ctx:
            bridge.print_text("x", JobParams({"tapeMm": 18}, bridge.cfg))
        self.assertEqual(ctx.exception.code, "TAPE_MISMATCH")

    def test_every_profile_prints_the_same_raster(self):
        bridge, out = self.bridge(12, silent=True, snmp_on=True)
        for profile in p.PROFILES:
            bridge.print_text("Same", JobParams({"profile": profile}, bridge.cfg))
        time.sleep(0.3)
        pages = sorted(out.glob("*.png"), key=os.path.getmtime)
        self.assertEqual(len(pages), len(p.PROFILES))
        first = Image.open(pages[0]).tobytes()
        for page in pages[1:]:
            self.assertEqual(Image.open(page).tobytes(), first, page.name)

    def test_waits_for_the_printer_to_close(self):
        bridge, _ = self.bridge(12, silent=True, snmp_on=True)
        res = bridge.print_text("x", JobParams({}, bridge.cfg))
        self.assertTrue(res["job"]["connection"]["closedByPrinter"])
        # A status check with SNMP answering does not open 9100 at all.
        status = bridge.printer.status()
        self.assertEqual(status["statusVia"], "snmp")

    def test_tcp_status_used_without_snmp(self):
        bridge, _ = self.bridge(18)
        res = bridge.print_text("x", JobParams({}, bridge.cfg))
        self.assertEqual(res["job"]["statusVia"], "tcp")
        self.assertEqual(res["job"]["state"], "printed")

    def test_silent_printer_uses_default_tape(self):
        bridge, out = self.bridge(18, silent=True)
        res = bridge.print_text("hello", JobParams({}, bridge.cfg))
        self.assertEqual(res["job"]["state"], "sent")
        self.assertEqual(res["job"]["tapeSource"], "default")
        time.sleep(0.5)
        self.assertEqual(len(list(out.glob("*.png"))), 1)


if __name__ == "__main__":
    unittest.main()


class SnmpFallback(MockBase):
    def test_silent_tcp_uses_snmp_tape(self):
        bridge, out = self.bridge(12, silent=True, snmp_on=True)
        res = bridge.print_text("hello", JobParams({}, bridge.cfg))
        self.assertEqual(res["job"]["tapeMm"], 12)
        self.assertEqual(res["job"]["tapeSource"], "printer")
        # SNMP sees the printing phase come and go – or misses it on a fast job.
        self.assertIn(res["job"]["state"], ("printed", "sent"))
        self.assertEqual(bridge.printer.status_via, "snmp")
        # SNMP answered, so 9100 was never asked for a status it would not give.
        self.assertIsNone(bridge.printer.tcp_status)
        self.assertTrue(res["job"]["statusBefore"].startswith("80 20 42 30 68"))
        self.assertTrue((Path(bridge.cfg.data_dir) / "last-job.bin").read_bytes().startswith(b"\x00" * 100 + b"\x1b\x40\x1b\x69\x61\x01"))
        with self.assertRaises(ApiError) as ctx:
            bridge.print_text("x", JobParams({"tapeMm": 18}, bridge.cfg))
        self.assertEqual(ctx.exception.code, "TAPE_MISMATCH")

    def test_minimal_profile_prints(self):
        bridge, out = self.bridge(12, silent=True, snmp_on=True)
        res = bridge.print_text("hi", JobParams({"profile": "minimal"}, bridge.cfg))
        self.assertEqual(res["job"]["tapeMm"], 12)
        time.sleep(0.3)
        self.assertEqual(len(list(out.glob("*.png"))), 1)

    def test_error_after_job_is_reported(self):
        bridge, _ = self.bridge(12, silent=True, snmp_on=True)
        # What the old bridge sent: ESC i A, which the printer does not know.
        bad = p.build_job([b"\xff" * 16], p.JobOptions(tape_mm=12), preamble=False)
        bad = bad.replace(b"\x1b\x69\x4b", b"\x1b\x69\x41\x01\x1b\x69\x4b", 1)
        with self.assertRaises(PrinterError) as ctx:
            bridge.printer.run(lambda status: (bad, 1))
        self.assertIn("went into error", ctx.exception.message)
        # And the next job is refused up front instead of sent into the error.
        with self.assertRaises(ApiError) as ctx:
            bridge.print_text("x", JobParams({}, bridge.cfg))
        self.assertEqual(ctx.exception.code, "PRINTER_ERROR")

    def test_probe(self):
        bridge, _ = self.bridge(12, silent=True, snmp_on=True)
        result = bridge.printer.probe(wait=0.5)
        self.assertTrue(result["tcp"]["connect"])
        self.assertIsNone(result["tcp"]["statusReply"])
        self.assertEqual(result["snmp"]["parsed"]["tapeMm"], 12)
        self.assertIn("Brother", result["snmp"]["sysDescr"])
