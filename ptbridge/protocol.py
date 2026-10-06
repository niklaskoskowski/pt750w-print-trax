"""
Brother P-touch raster protocol (PT-P750W / PT-E550W / PT-P710BT family).

Source: Brother "Software Developer's Manual – Raster Command Reference
PT-E550W/P750W/P710BT". Everything the bridge sends goes through here, so
this is the one place to compare against the manual.

Head: 128 pins at 180 dpi. One raster line = 16 bytes = one column across
the tape; lines run along the tape (the label's length). The tape only
covers the middle of the head, symmetrically, so a label centred on pin 64
lands on the tape whatever its width.
"""

from __future__ import annotations

from dataclasses import dataclass

HEAD_PINS = 128
LINE_BYTES = HEAD_PINS // 8
DPI = 180

# Tape width (mm, as the status reports it) -> printable pins, margin pins.
# 3.5 mm tape reports as 4.
TAPES: dict[int, tuple[int, int]] = {
    4: (24, 52),
    6: (32, 48),
    9: (50, 39),
    12: (70, 29),
    18: (112, 8),
    24: (128, 0),
}

TAPE_LABELS = {4: "3.5 mm", 6: "6 mm", 9: "9 mm", 12: "12 mm", 18: "18 mm", 24: "24 mm"}

MEDIA_TYPES = {
    0x00: "no media",
    0x01: "laminated tape (TZe)",
    0x03: "non-laminated tape",
    0x04: "fabric tape",
    0x11: "heat-shrink tube 2:1 (HSe)",
    0x13: "fle tape",
    0x14: "flexible ID tape",
    0x15: "satin tape",
    0x17: "heat-shrink tube 3:1",
    0xFF: "incompatible tape",
}

MODELS = {
    0x64: "PT-H500",
    0x65: "PT-E550W",
    0x66: "PT-E500",
    0x67: "PT-P700",
    0x68: "PT-P750W",
    0x76: "PT-P710BT",
}

TAPE_COLORS = {
    0x01: "white", 0x02: "other", 0x03: "clear", 0x04: "red", 0x05: "blue",
    0x06: "yellow", 0x07: "green", 0x08: "black", 0x09: "clear (white text)",
    0x20: "matte white", 0x21: "matte clear", 0x22: "matte silver",
    0x23: "satin gold", 0x24: "satin silver", 0x30: "blue (D)", 0x31: "red (D)",
    0x40: "fluorescent orange", 0x41: "fluorescent yellow",
    0x50: "berry pink (S)", 0x51: "light gray (S)", 0x52: "lime green (S)",
    0x60: "yellow (F)", 0x61: "pink (F)", 0x62: "blue (F)",
    0x70: "white (heat-shrink)", 0x90: "white (flex ID)", 0x91: "yellow (flex ID)",
    0xF0: "cleaning", 0xF1: "stencil", 0xFF: "incompatible",
}

TEXT_COLORS = {
    0x01: "white", 0x02: "other", 0x04: "red", 0x05: "blue", 0x08: "black",
    0x0A: "gold", 0x62: "blue (F)", 0xF0: "cleaning", 0xF1: "stencil", 0xFF: "incompatible",
}

ERRORS_1 = {
    0x01: "No media",
    0x04: "Cutter jam",
    0x08: "Weak batteries",
    0x10: "Printer in use",
    0x40: "High-voltage adapter",
    0x80: "Fan error",
}

ERRORS_2 = {
    0x01: "Wrong media / replace media",
    0x02: "Expansion buffer full",
    0x04: "Communication error",
    0x08: "Buffer full",
    0x10: "Cover open",
    0x20: "Overheating",
    0x40: "Black marking not detected",
    0x80: "System error",
}

STATUS_TYPES = {
    0x00: "status reply",
    0x01: "printing completed",
    0x02: "error",
    0x03: "exit IF mode",
    0x04: "turned off",
    0x05: "notification",
    0x06: "phase change",
}

# --- Commands -------------------------------------------------------------

INVALIDATE = b"\x00" * 100
INITIALIZE = b"\x1b\x40"
STATUS_REQUEST = b"\x1b\x69\x53"
RASTER_MODE = b"\x1b\x69\x61\x01"
COMPRESSION_TIFF = b"\x4d\x02"
COMPRESSION_NONE = b"\x4d\x00"
ZERO_LINE = b"\x5a"
PRINT = b"\x0c"
PRINT_FEED = b"\x1a"

# Print information (ESC i z) validity flags
PI_KIND = 0x02
PI_WIDTH = 0x04
PI_LENGTH = 0x08
PI_QUALITY = 0x40
PI_RECOVER = 0x80

# Various mode settings (ESC i M)
MODE_AUTO_CUT = 0x40
MODE_MIRROR = 0x80

# Command-set profiles. The printer only says "error" when it dislikes a job,
# so these are the variants `python -m ptbridge selftest` walks through.
# compat is the default: a real PT-P750W over Wi-Fi printed it and rejected
# standard and minimal (TIFF compression and/or the PI_QUALITY flags).
#   standard: ESC i z, ESC i M (auto cut), ESC i K (half cut/chain/360 dpi), ESC i d (margin), TIFF
#   minimal:  ESC i z, ESC i M, TIFF
#   compat:   ESC i z with media check (kind + width valid), ESC i M/K/d, uncompressed
#   plain:    ESC i M, uncompressed – no print information at all
#   ptouch:   byte for byte what ptouch-print sends a P750W: M 02, ESC i a 01, lines as one literal run
# None sends ESC i A ("cut every n labels") or Z (empty line): both are in
# the QL/P900 references, not proven on the P750W.
PROFILES = ("standard", "minimal", "compat", "plain", "ptouch")
# Only these send ESC i K, which switches 180 x 360 dpi on. Anything else
# must stay at 180 dpi, or the doubled raster prints the label twice as long.
HIGH_RES_PROFILES = ("standard", "compat")

# How a job of several pages (batch, copies) carries the page settings. The
# printer only shows the difference: one half-cut strip, or every label
# ejected and cut with its own leader. `python -m ptbridge batchtest` tries them.
# On a real PT-P750W (12 mm TZe, Wi-Fi) "legacy" and "once" gave every label
# on its own, fed and cut; "noautocut" – auto cut off, half cut on, settings
# once – gave one half-cut strip with a single full cut at the end. Default.
#   once:      ESC i M/K/d + M before the first page only; then ESC i z + raster + FF
#   chain:     like once, with chain printing on – Ctrl-Z at the end feeds and cuts
#   perpage:   settings on every page, chain printing on for all but the last
#   noautocut: like once, auto cut off – half cuts only, the end via Ctrl-Z
#   legacy:    settings on every page, chain printing off on every page (≤ 1.0 behaviour)
BATCH_MODES = ("once", "chain", "perpage", "noautocut", "legacy")
BATCH_MODE_LABELS = {
    "once": "cut/half cut/margin once at the start, then only page data",
    "chain": "settings once, chain printing on – fed and cut by the final Ctrl-Z",
    "perpage": "settings on every page, chain printing on until the last page",
    "noautocut": "settings once, auto cut off – half cuts only",
    "legacy": "settings and 'no chain' on every page (old behaviour)",
}
PROFILE_LABELS = {
    "standard": "ESC i z + cut/half cut/margin, TIFF-compressed (rejected by a PT-P750W over Wi-Fi)",
    "minimal": "ESC i z + auto cut, TIFF-compressed",
    "compat": "ESC i z with media check + cut/margin, uncompressed",
    "plain": "auto cut only, no print information, uncompressed",
    "ptouch": "exactly like ptouch-print: M 02, ESC i a 01, literal PackBits",
}

# Advanced mode settings (ESC i K)
ADV_HALF_CUT = 0x04
ADV_NO_CHAIN = 0x08
ADV_SPECIAL_TAPE = 0x10
ADV_HIGH_RES = 0x40
ADV_NO_BUFFER_CLEAR = 0x80


def mm_to_dots(mm: float, dpi: int = DPI) -> int:
    return max(0, round(mm * dpi / 25.4))


def dots_to_mm(dots: int, dpi: int = DPI) -> float:
    return round(dots * 25.4 / dpi, 2)


def printable_pins(tape_mm: int) -> int:
    """Printable pins across a tape; unknown widths get the full head."""
    return TAPES.get(tape_mm, (HEAD_PINS, 0))[0]


# --- PackBits (TIFF) ------------------------------------------------------

def packbits_encode(data: bytes) -> bytes:
    """
    Apple PackBits as TIFF uses it: a header byte n, then
    0..127   -> n+1 literal bytes follow,
    -1..-127 -> the next byte repeated 1-n times (as an unsigned byte 255..129).
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        # A run of at least two equal bytes?
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run >= 2:
            out.append((257 - run) & 0xFF)
            out.append(data[i])
            i += run
            continue
        # Literal: until the next run of 2+ or 128 bytes.
        start = i
        i += 1
        while i < n and i - start < 128:
            if i + 1 < n and data[i] == data[i + 1]:
                break
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


def packbits_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        h = data[i]
        i += 1
        if h < 128:
            out += data[i:i + h + 1]
            i += h + 1
        elif h > 128:
            out += bytes([data[i]]) * (257 - h)
            i += 1
        # 128 is a no-op
    return bytes(out)


# --- Job ------------------------------------------------------------------

@dataclass
class JobOptions:
    tape_mm: int = 24
    media_type: int = 0x01
    copies: int = 1
    auto_cut: bool = True
    half_cut: bool = False
    chain: bool = False
    margin_dots: int = 14
    high_res: bool = False
    compress: bool = True
    validate_media: bool = False
    profile: str = "compat"
    batch_mode: str = "noautocut"


def _print_info(opts: JobOptions, lines: int, page: int, pages: int) -> bytes:
    flags = PI_RECOVER | PI_QUALITY
    if opts.validate_media or opts.profile == "compat":
        flags = PI_RECOVER | PI_KIND | PI_WIDTH
    if pages == 1 or page == 0:
        n9 = 0
    elif page == pages - 1:
        n9 = 2
    else:
        n9 = 1
    return bytes([
        0x1B, 0x69, 0x7A,
        flags,
        opts.media_type & 0xFF,
        opts.tape_mm & 0xFF,
        0x00,
        lines & 0xFF, (lines >> 8) & 0xFF, (lines >> 16) & 0xFF, (lines >> 24) & 0xFF,
        n9,
        0x00,
    ])


def _page_settings(opts: JobOptions, *, chain: bool | None = None, auto_cut: bool | None = None) -> bytes:
    chain = opts.chain if chain is None else chain
    auto_cut = opts.auto_cut if auto_cut is None else auto_cut
    mode = MODE_AUTO_CUT if auto_cut else 0
    adv = 0
    if opts.half_cut:
        adv |= ADV_HALF_CUT
    if not chain:
        adv |= ADV_NO_CHAIN
    margin = max(0, min(0xFFFF, int(opts.margin_dots)))
    if opts.high_res:
        adv |= ADV_HIGH_RES
        # Along the tape a dot is 1/360 inch now: same margin in mm.
        margin = min(0xFFFF, margin * 2)
    compression = COMPRESSION_TIFF if _line_mode(opts) == "tiff" else COMPRESSION_NONE
    if opts.profile == "ptouch":
        return b""  # compression went out once, before raster mode
    if opts.profile in ("minimal", "plain"):
        return b"\x1b\x69\x4d" + bytes([mode]) + compression
    return b"".join([
        b"\x1b\x69\x4d" + bytes([mode]),
        b"\x1b\x69\x4b" + bytes([adv]),
        b"\x1b\x69\x64" + bytes([margin & 0xFF, margin >> 8]),
        compression,
    ])


def _line_mode(opts: JobOptions) -> str:
    """tiff (real PackBits), literal (PackBits, one literal run – ptouch-print) or none."""
    if opts.profile == "ptouch":
        return "literal"
    if opts.profile in ("compat", "plain") or not opts.compress:
        return "none"
    return "tiff"


def encode_lines(lines: list[bytes], compress: bool | str = True) -> bytes:
    mode = compress if isinstance(compress, str) else ("tiff" if compress else "none")
    out = bytearray()
    for line in lines:
        if len(line) != LINE_BYTES:
            raise ValueError(f"raster line must be {LINE_BYTES} bytes, got {len(line)}")
        if mode == "tiff":
            # Empty lines too go out as G (16 zeros pack to 2 bytes), never Z.
            packed = packbits_encode(line)
        elif mode == "literal":
            packed = bytes([LINE_BYTES - 1]) + line
        else:
            packed = line
        out += b"\x47" + bytes([len(packed) & 0xFF, len(packed) >> 8]) + packed
    return bytes(out)


def build_job(lines: list[bytes], opts: JobOptions, *, preamble: bool = True) -> bytes:
    """
    The full byte stream for `copies` identical pages.

    preamble=False leaves out invalidate + initialize, for a connection that
    has already sent them ahead of a status request.
    """
    if not lines:
        raise ValueError("nothing to print")
    return build_pages([lines] * max(1, int(opts.copies)), opts, preamble=preamble)


def build_pages(pages: list[list[bytes]], opts: JobOptions, *, preamble: bool = True) -> bytes:
    """
    One job, one page per label – each page its own length. With half cut on
    the printer half-cuts between the pages and fully cuts after the last:
    one continuous strip. FF ends a page, Ctrl-Z the job.
    """
    if not pages or any(not lines for lines in pages):
        raise ValueError("nothing to print")
    mode = _line_mode(opts)
    out = bytearray()
    if preamble:
        out += INVALIDATE + INITIALIZE
    if opts.profile == "ptouch":
        out += COMPRESSION_TIFF
    out += RASTER_MODE
    encoded: dict[int, bytes] = {}
    batch = opts.batch_mode if opts.batch_mode in BATCH_MODES else "noautocut"
    if len(pages) == 1:
        # One label is no strip: the cut settings exactly as asked for.
        batch = "legacy"
    for index, lines in enumerate(pages):
        last = index == len(pages) - 1
        if opts.profile not in ("plain", "ptouch"):
            out += _print_info(opts, len(lines), index, len(pages))
        if batch == "legacy":
            out += _page_settings(opts)
        elif batch == "perpage":
            out += _page_settings(opts, chain=opts.chain if last else True)
        elif index == 0:
            if batch == "chain":
                out += _page_settings(opts, chain=True)
            elif batch == "noautocut":
                out += _page_settings(opts, auto_cut=False)
            else:
                out += _page_settings(opts)
        key = id(lines)
        if key not in encoded:  # copies share their encoding
            encoded[key] = encode_lines(lines, mode)
        out += encoded[key]
        out += PRINT_FEED if index == len(pages) - 1 else PRINT
    return bytes(out)


# --- Status ---------------------------------------------------------------

def _bits(value: int, table: dict[int, str]) -> list[str]:
    return [label for bit, label in table.items() if value & bit]


def parse_status(raw: bytes) -> dict:
    """The 32-byte status reply, or a ValueError for anything else."""
    if len(raw) < 32 or raw[0] != 0x80 or raw[1] != 0x20:
        raise ValueError("not a status packet")
    errors = _bits(raw[8], ERRORS_1) + _bits(raw[9], ERRORS_2)
    width = raw[10]
    media = raw[11]
    return {
        "model": MODELS.get(raw[4], f"unknown (0x{raw[4]:02x})"),
        "modelCode": raw[4],
        "errors": errors,
        "errorBits": [raw[8], raw[9]],
        "tapeMm": width or None,
        "tapeLabel": TAPE_LABELS.get(width, f"{width} mm" if width else "none"),
        "mediaType": media,
        "mediaLabel": MEDIA_TYPES.get(media, f"unknown (0x{media:02x})"),
        "printablePins": printable_pins(width) if width in TAPES else None,
        "statusType": raw[18],
        "statusLabel": STATUS_TYPES.get(raw[18], f"0x{raw[18]:02x}"),
        "phase": "printing" if raw[19] == 0x01 else "idle",
        "notification": raw[22],
        "tapeColor": TAPE_COLORS.get(raw[24], f"0x{raw[24]:02x}"),
        "textColor": TEXT_COLORS.get(raw[25], f"0x{raw[25]:02x}"),
        "raw": raw[:32].hex(" "),
    }


def fake_status(tape_mm: int = 24, media: int = 0x01, status_type: int = 0x00,
                errors: tuple[int, int] = (0, 0), phase: int = 0, model: int = 0x68) -> bytes:
    """A status packet as the printer sends it. Used by the mock printer and the tests."""
    raw = bytearray(32)
    raw[0], raw[1], raw[2], raw[3], raw[4], raw[5] = 0x80, 0x20, 0x42, 0x30, model, 0x30
    raw[8], raw[9] = errors
    raw[10] = tape_mm
    raw[11] = media
    raw[18] = status_type
    raw[19] = phase
    raw[24] = 0x01
    raw[25] = 0x08
    return bytes(raw)
