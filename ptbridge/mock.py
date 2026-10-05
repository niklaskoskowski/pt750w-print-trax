"""
A fake PT-P750W on TCP 9100, for testing without the printer.

It answers status requests, decodes every page it receives exactly as the
printer would read it and writes it to <out>/<time>-<n>.png, then reports
"printing completed". --silent emulates a printer that never answers on 9100
(like the real one over Wi-Fi); --snmp-port adds the SNMP agent that does.

Strict on purpose: only the PT-P750W command set is accepted. Anything else
puts the mock into an error state, as an unknown command does on the
printer, and it stays there until restarted.
"""

from __future__ import annotations

import itertools
import logging
import socket
import threading
import time
from pathlib import Path

from . import protocol as p
from . import snmp
from .raster import preview_png

log = logging.getLogger("ptbridge.mock")


class Incomplete(Exception):
    pass


_page_counter = itertools.count(1)


class MockState:
    """What the printer's status packet says – shared by 9100 and SNMP."""

    def __init__(self, tape_mm: int, errors: tuple[int, int]):
        self.tape_mm = tape_mm
        self.errors = list(errors)
        self.phase = 0

    def packet(self, status_type: int = 0x00) -> bytes:
        return p.fake_status(self.tape_mm, status_type=status_type, errors=tuple(self.errors), phase=self.phase)


class Session:
    def __init__(self, conn: socket.socket, state: MockState, out: Path, silent: bool):
        self.conn = conn
        self.state = state
        self.tape_mm = state.tape_mm
        self.out = out
        self.silent = silent
        self.buf = bytearray()
        self.compress = False
        self.lines: list[bytes] = []
        self.info: dict = {}
        self.pages: list[dict] = []
        self.log: list[str] = []

    def reply(self, status_type: int = 0x00) -> None:
        if self.silent:
            return
        self.conn.sendall(self.state.packet(status_type))

    def reply_and_close(self) -> None:
        try:
            self.conn.settimeout(2)
            if self.conn.recv(4096):
                self.reply(0x02)
        except OSError:
            pass
        finally:
            self.conn.close()

    def need(self, n: int) -> bytes:
        if len(self.buf) < n:
            raise Incomplete
        return bytes(self.buf[:n])

    def step(self) -> bool:
        """Consume one command from the buffer. False when more data is needed."""
        b = self.buf
        if not b:
            return False
        try:
            c = b[0]
            if c == 0x00:
                n = 0
                while n < len(b) and b[n] == 0:
                    n += 1
                del b[:n]
                return True
            if c == 0x1B:
                head = self.need(2)
                if head[1] == 0x40:
                    del b[:2]
                    self.log.append("init")
                    return True
                if head[1] != 0x69:
                    raise ValueError(f"unknown ESC 0x{head[1]:02x}")
                sub = self.need(3)[2]
                if sub == 0x53:  # status request
                    del b[:3]
                    self.log.append("status?")
                    self.reply(0x00)
                    return True
                # a = command mode, M = various mode, K = advanced mode, ! = status notification
                if sub in (0x61, 0x4D, 0x4B, 0x21):
                    v = self.need(4)[3]
                    del b[:4]
                    self.log.append(f"ESC i {chr(sub)} {v:#04x}")
                    self.info[chr(sub)] = v
                    return True
                if sub == 0x64:
                    raw = self.need(5)
                    del b[:5]
                    self.info["margin"] = raw[3] | raw[4] << 8
                    return True
                if sub == 0x7A:
                    raw = self.need(13)
                    del b[:13]
                    self.info["z"] = {
                        "flags": raw[3], "media": raw[4], "width": raw[5],
                        "lines": int.from_bytes(raw[7:11], "little"), "page": raw[11],
                    }
                    return True
                raise ValueError(f"unknown ESC i 0x{sub:02x}")
            if c == 0x4D:
                v = self.need(2)[1]
                del b[:2]
                self.compress = v == 0x02
                return True
            if c == 0x5A:
                del b[:1]
                self.lines.append(bytes(p.LINE_BYTES))
                return True
            if c == 0x47:
                head = self.need(3)
                n = head[1] | head[2] << 8
                data = self.need(3 + n)[3:]
                del b[:3 + n]
                line = p.packbits_decode(data) if self.compress else data
                if len(line) != p.LINE_BYTES:
                    raise ValueError(f"raster line decodes to {len(line)} bytes")
                self.lines.append(line)
                return True
            if c in (0x0C, 0x1A):
                del b[:1]
                self.finish_page(last=c == 0x1A)
                return True
            raise ValueError(f"unknown byte 0x{c:02x}")
        except Incomplete:
            return False

    def finish_page(self, last: bool) -> None:
        z = self.info.get("z", {})
        if z.get("lines") is not None and z["lines"] != len(self.lines):
            log.error("page announces %d lines, received %d", z["lines"], len(self.lines))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.out / f"{stamp}-{next(_page_counter):04d}.png"
        high_res = bool(self.info.get("K", 0) & p.ADV_HIGH_RES)
        path.write_bytes(preview_png(self.lines, self.tape_mm, high_res))
        page = {
            "file": str(path), "lines": len(self.lines),
            "autoCut": bool(self.info.get("M", 0) & p.MODE_AUTO_CUT),
            "halfCut": bool(self.info.get("K", 0) & p.ADV_HALF_CUT),
            "chain": not (self.info.get("K", 0) & p.ADV_NO_CHAIN),
            "margin": self.info.get("margin"), "last": last, "info": z,
        }
        self.pages.append(page)
        log.info("page %d: %d lines (%.1f mm) -> %s", len(self.pages), len(self.lines),
                 len(self.lines) * 25.4 / (360 if high_res else 180), path)
        self.lines = []
        self.state.phase = 1
        self.reply(0x06)
        time.sleep(0.8)
        self.state.phase = 0
        self.reply(0x01)

    def run(self) -> None:
        self.conn.settimeout(30)
        try:
            while True:
                chunk = self.conn.recv(65536)
                if not chunk:
                    break
                self.buf += chunk
                while self.step():
                    pass
        except ValueError as exc:
            # Like the printer: an unknown command is an error state, not a skipped byte.
            self.state.errors[1] |= 0x04
            log.error("ERROR state – %s", exc)
            try:
                self.reply(0x02)
            except OSError:
                pass
        except OSError as exc:
            log.error("session ended: %s", exc)
        finally:
            if self.buf:
                log.error("%d unparsed bytes left: %s", len(self.buf), bytes(self.buf[:32]).hex())
            self.conn.close()


def serve_snmp(host: str, port: int, state: MockState) -> None:
    """Answers the Brother status OID and sysDescr, like the printer's SNMP agent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    log.info("mock SNMP agent on %s:%d/udp", host, port)
    while True:
        data, addr = sock.recvfrom(4096)
        try:
            version, community, request_id, oid = snmp.parse_request(data)
        except (snmp.SnmpError, IndexError, ValueError):
            continue
        if community != "public":
            continue
        value = {
            snmp.BROTHER_STATUS_OID: state.packet(),
            snmp.SYS_DESCR_OID: b"Brother NC-18002w, Firmware Ver.1.10 (mock)",
        }.get(oid)
        sock.sendto(snmp.get_response(request_id, oid, value, community, version), addr)


def serve(host: str = "127.0.0.1", port: int = 9100, tape_mm: int = 18, out: str = "./mock-out",
          silent: bool = False, errors: tuple[int, int] = (0, 0), snmp_port: int | None = None) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = MockState(tape_mm, errors)
    if snmp_port:
        threading.Thread(target=serve_snmp, args=(host, snmp_port, state), daemon=True).start()
    srv = socket.create_server((host, port), reuse_port=False)
    log.info("mock PT-P750W on %s:%d, %d mm tape, pages -> %s%s", host, port, tape_mm, out_dir,
             " (silent)" if silent else "")
    try:
        while True:
            conn, addr = srv.accept()
            log.info("connection from %s", addr[0])
            # One at a time, like the printer.
            if any(state.errors):
                # In error the printer takes nothing until it is reset.
                Session(conn, state, out_dir, silent).reply_and_close()
                continue
            Session(conn, state, out_dir, silent).run()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


def serve_in_thread(**kwargs) -> threading.Thread:
    thread = threading.Thread(target=serve, kwargs=kwargs, daemon=True)
    thread.start()
    return thread
