"""
Image -> 128-pin raster lines.

The label is laid out with its length along the tape and its height across
it, scaled to 180 dpi, thresholded to 1 bit and centred on the head. Each
column of that picture becomes one raster line (pin 0 = top of the label =
MSB of the first byte), which is the transpose of the image.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .protocol import DPI, HEAD_PINS, LINE_BYTES, TAPES, mm_to_dots, printable_pins

# Hard ceiling on the label length: ~1 m of tape at 180 dpi.
MAX_LENGTH_DOTS = 7200


class RenderError(ValueError):
    pass


@dataclass
class RenderOptions:
    # Physical size of the image as supplied (before rotation). Either may be None.
    width_mm: float | None = None
    height_mm: float | None = None
    # exact: true size, scaled down only if it does not fit the tape.
    # fill:  scaled to the full printable height of the tape.
    fit: str = "exact"
    # auto: lay a portrait image on its side so the long edge runs along the tape.
    rotate: str = "auto"
    threshold: int = 128
    dither: bool = False
    invert: bool = False
    high_res: bool = False


@dataclass
class Rendered:
    lines: list[bytes]
    width_dots: int
    height_dots: int
    tape_mm: int
    scale_pct: int
    rotated: int
    warnings: list[str] = field(default_factory=list)
    high_res: bool = False

    @property
    def length_mm(self) -> float:
        dpi = DPI * 2 if self.high_res else DPI
        return round(self.width_dots * 25.4 / dpi, 1)


def load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise RenderError(f"not a readable image: {exc}") from exc
    if img.width < 1 or img.height < 1 or img.width * img.height > 40_000_000:
        raise RenderError("image dimensions out of range")
    return img


def _flatten(img: Image.Image) -> Image.Image:
    """Any mode -> L, transparency composited on white."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert("L")
    return img.convert("L")


def _rotation(opts: RenderOptions, img: Image.Image) -> int:
    value = str(opts.rotate).strip().lower()
    if value == "auto":
        # Long side along the tape: the largest the label gets.
        return 90 if img.height > img.width else 0
    if value == "across":
        # Long side across the tape: smaller, and shorter on the strip.
        return 90 if img.width > img.height else 0
    try:
        angle = int(value) % 360
    except ValueError as exc:
        raise RenderError("rotate must be auto, across, 0, 90, 180 or 270") from exc
    if angle not in (0, 90, 180, 270):
        raise RenderError("rotate must be auto, across, 0, 90, 180 or 270")
    return angle


def render(img: Image.Image, tape_mm: int, opts: RenderOptions) -> Rendered:
    warnings: list[str] = []
    if tape_mm not in TAPES:
        warnings.append(f"Unknown tape width {tape_mm} mm – using the full head.")
    pins = printable_pins(tape_mm)

    gray = _flatten(img)
    angle = _rotation(opts, gray)
    width_mm, height_mm = opts.width_mm, opts.height_mm
    if angle:
        # PIL rotates counter-clockwise: 90 puts the label's top on the left.
        gray = gray.rotate(angle, expand=True)
        if angle in (90, 270):
            width_mm, height_mm = height_mm, width_mm

    # Fall back on the file's own DPI when no size was given.
    if width_mm is None and height_mm is None:
        dpi = img.info.get("dpi")
        if dpi and 50 <= float(dpi[1]) <= 3000:
            height_mm = gray.height * 25.4 / float(dpi[1])
            width_mm = gray.width * 25.4 / float(dpi[0])

    if opts.fit == "fill" or (height_mm is None and width_mm is None):
        h = pins
        w = round(gray.width * h / gray.height)
    elif height_mm is not None:
        h = mm_to_dots(height_mm)
        w = mm_to_dots(width_mm) if width_mm is not None else round(gray.width * h / gray.height)
    else:
        w = mm_to_dots(width_mm)
        h = round(gray.height * w / gray.width)

    ideal_h = max(1, h)
    if h > pins:
        k = pins / h
        h = pins
        w = round(w * k)
        warnings.append(
            f"Label is {round(ideal_h * 25.4 / DPI, 1)} mm high, the {tape_mm} mm tape prints "
            f"{round(pins * 25.4 / DPI, 1)} mm – scaled to {round(k * 100)} %."
        )
    w = max(1, w)
    h = max(1, h)
    if opts.high_res:
        w *= 2
    if w > MAX_LENGTH_DOTS * (2 if opts.high_res else 1):
        raise RenderError("label is longer than 1 m")

    method = Image.Resampling.BOX if (w <= gray.width and h <= gray.height) else Image.Resampling.LANCZOS
    scaled = gray.resize((w, h), method)

    ink = scaled if opts.invert else ImageOps.invert(scaled)
    if opts.dither:
        mono = ink.convert("1")  # Floyd-Steinberg
    else:
        cut = 255 - max(1, min(254, int(opts.threshold)))
        mono = ink.point(lambda p: 255 if p > cut else 0).convert("1", dither=Image.Dither.NONE)

    canvas = Image.new("1", (w, HEAD_PINS), 0)
    canvas.paste(mono, (0, (HEAD_PINS - h) // 2))
    lines = canvas_to_lines(canvas)

    return Rendered(
        lines=lines,
        width_dots=w,
        height_dots=h,
        tape_mm=tape_mm,
        scale_pct=round(h / ideal_h * 100),
        rotated=angle,
        warnings=warnings,
        high_res=opts.high_res,
    )


def canvas_to_lines(canvas: Image.Image) -> list[bytes]:
    """A '1' image HEAD_PINS high, ink = 1 -> one 16-byte line per column."""
    if canvas.height != HEAD_PINS:
        raise ValueError("canvas must be exactly one head high")
    raw = canvas.transpose(Image.Transpose.TRANSPOSE).tobytes()
    return [raw[i:i + LINE_BYTES] for i in range(0, len(raw), LINE_BYTES)]


def lines_to_canvas(lines: list[bytes]) -> Image.Image:
    data = b"".join(lines)
    img = Image.frombytes("1", (HEAD_PINS, len(lines)), data)
    return img.transpose(Image.Transpose.TRANSPOSE)


def preview_png(lines: list[bytes], tape_mm: int | None, high_res: bool = False) -> bytes:
    """
    What the head will burn, as a PNG: ink black on white, the parts of the
    head outside the tape shaded, one pixel per dot (doubled across for a
    360 dpi job so the aspect ratio stays true).
    """
    ink = lines_to_canvas(lines) if lines else Image.new("1", (1, HEAD_PINS), 0)
    img = Image.new("RGB", ink.size, (255, 255, 255))
    if tape_mm in TAPES:
        _, margin = TAPES[tape_mm]
        if margin:
            shade = ImageDraw.Draw(img)
            shade.rectangle([0, 0, img.width, margin - 1], fill=(225, 228, 232))
            shade.rectangle([0, HEAD_PINS - margin, img.width, HEAD_PINS], fill=(225, 228, 232))
    img.paste((17, 24, 39), mask=ink)
    if high_res:
        img = img.resize((img.width, img.height * 2), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def preview_strip(pages: list[list[bytes]], tape_mm: int | None, gap_dots: int, high_res: bool = False) -> bytes:
    """
    A whole batch as the strip it becomes: the labels in order, the feed
    margin between them, a dashed red line where the printer cuts.
    """
    if len(pages) == 1:
        return preview_png(pages[0], tape_mm, high_res)
    gap = max(4, gap_dots)
    width = sum(len(lines) for lines in pages) + gap * (len(pages) - 1)
    strip = Image.new("RGB", (width, HEAD_PINS), (255, 255, 255))
    x = 0
    cuts = []
    for index, lines in enumerate(pages):
        tile = Image.open(io.BytesIO(preview_png(lines, tape_mm, False)))
        strip.paste(tile, (x, 0))
        x += tile.width
        if index < len(pages) - 1:
            if tape_mm in TAPES and TAPES[tape_mm][1]:
                margin = TAPES[tape_mm][1]
                band = ImageDraw.Draw(strip)
                band.rectangle([x, 0, x + gap - 1, margin - 1], fill=(225, 228, 232))
                band.rectangle([x, HEAD_PINS - margin, x + gap - 1, HEAD_PINS], fill=(225, 228, 232))
            cuts.append(x + gap // 2)
            x += gap
    draw = ImageDraw.Draw(strip)
    for cx in cuts:
        for y in range(0, HEAD_PINS, 6):
            draw.line([(cx, y), (cx, min(HEAD_PINS - 1, y + 3))], fill=(220, 38, 38), width=1)
    if high_res:
        strip = strip.resize((strip.width, strip.height * 2), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    strip.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --- Text labels ----------------------------------------------------------

FONT_CANDIDATES = [
    os.environ.get("PTB_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if path and os.path.isfile(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def text_image(text: str, tape_mm: int, *, align: str = "center") -> Image.Image:
    """
    One or more lines of text, as large as the tape allows, rendered at
    180 dpi exactly so render() does not scale it again.
    """
    rows = [row.strip() for row in str(text).replace("\r", "").split("\n")]
    rows = [row for row in rows if row] or [" "]
    if len(rows) > 6:
        raise RenderError("at most 6 lines")
    pins = printable_pins(tape_mm)
    usable = max(8, pins - 4)
    gap = 0.15
    size = max(6, int(usable / (len(rows) + gap * (len(rows) - 1)) * 0.92))

    # Shrink until the cap-to-descender box of every row fits.
    while True:
        font = _font(size)
        boxes = [font.getbbox(row) for row in rows]
        heights = [b[3] - b[1] for b in boxes]
        line_h = max(max(heights), 1)
        total = line_h * len(rows) + int(line_h * gap) * (len(rows) - 1)
        if total <= usable or size <= 6:
            break
        size -= 1

    pad = mm_to_dots(1.5)
    width = max(b[2] - b[0] for b in boxes) + pad * 2
    img = Image.new("L", (width, pins), 255)
    draw = ImageDraw.Draw(img)
    y = (pins - total) // 2
    for row, box in zip(rows, boxes):
        row_w = box[2] - box[0]
        if align == "left":
            x = pad
        elif align == "right":
            x = width - pad - row_w
        else:
            x = (width - row_w) // 2
        draw.text((x - box[0], y - box[1] + (line_h - (box[3] - box[1])) // 2), row, font=font, fill=0)
        y += line_h + int(line_h * gap)
    return img
