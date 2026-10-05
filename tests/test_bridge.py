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
from ptbridge.service import ApiError, Bridge, JobParams  # noqa: E402


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
        job = p.build_job(lines, p.JobOptions(tape_mm=18, copies=2, half_cut=True))
        self.assertTrue(job.startswith(b"\x00" * 100 + b"\x1b\x40\x1b\x69\x61\x01"))
        self.assertTrue(job.endswith(b"\x1a"))
        self.assertEqual(job.count(b"\x1b\x69\x7a"), 2)
        self.assertIn(b"\x1b\x69\x4b\x0c", job)  # half cut + no chain


class Status(unittest.TestCase):
    def test_parse(self):
        st = p.parse_status(p.fake_status(12, errors=(0x01, 0x10)))
        self.assertEqual(st["tapeMm"], 12)
        self.assertEqual(st["model"], "PT-P750W")
        self.assertIn("No media", st["errors"])
        self.assertIn("Cover open", st["errors"])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEnd(unittest.TestCase):
    """Bridge -> mock printer over a real socket."""

    def bridge(self, tape=18, silent=False):
        port = free_port()
        tmp = tempfile.mkdtemp()
        serve_in_thread(host="127.0.0.1", port=port, tape_mm=tape, out=os.path.join(tmp, "out"), silent=silent)
        time.sleep(0.3)
        os.environ.update(PTB_PRINTER_HOST="127.0.0.1", PTB_PRINTER_PORT=str(port), PTB_DATA_DIR=tmp,
                          PTB_STATUS_TIMEOUT="0.5", PTB_WAIT_TIMEOUT="3")
        cfg = Config.from_env()
        return Bridge(cfg), Path(tmp, "out")

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

    def test_silent_printer_uses_default_tape(self):
        bridge, out = self.bridge(18, silent=True)
        res = bridge.print_text("hello", JobParams({}, bridge.cfg))
        self.assertEqual(res["job"]["state"], "sent")
        self.assertEqual(res["job"]["tapeSource"], "default")
        time.sleep(0.5)
        self.assertEqual(len(list(out.glob("*.png"))), 1)


if __name__ == "__main__":
    unittest.main()
