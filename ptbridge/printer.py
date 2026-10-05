"""
Talking to the printer: raw TCP on port 9100.

One connection per job. The printer handles one client at a time, so every
access goes through `Printer.lock` – concurrent HTTP requests queue up here
instead of fighting over the socket.

Status read-back over the network is best effort: the bridge asks for it
first, and when the printer answers it uses the loaded tape width, refuses to
print into an error (cover open, no tape) and waits for "printing completed".
When it does not answer, the job is still sent, sized for the default tape,
and reported as `sent` instead of `printed`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p

log = logging.getLogger("ptbridge.printer")


class PrinterError(Exception):
    def __init__(self, code: str, message: str, http: int = 503, status: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http
        self.status = status


@dataclass
class JobResult:
    state: str  # printed | sent
    status_before: dict | None
    status_after: dict | None
    pages_confirmed: int = 0
    bytes_sent: int = 0
    notes: list[str] = field(default_factory=list)


class Printer:
    def __init__(self, host: str, port: int = 9100, *, connect_timeout: float = 5.0,
                 status_timeout: float = 2.0, wait_timeout: float = 25.0, busy_timeout: float = 120.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.status_timeout = status_timeout
        self.wait_timeout = wait_timeout
        self.busy_timeout = busy_timeout
        self.lock = threading.Lock()
        self.last_status: dict | None = None
        self.last_status_at: float | None = None
        self.last_error: str | None = None
        self.status_supported: bool | None = None

    # -- connection -------------------------------------------------------

    def _connect(self) -> socket.socket:
        if not self.host:
            raise PrinterError("NOT_CONFIGURED", "PTB_PRINTER_HOST is not set.", 500)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except socket.timeout as exc:
            raise PrinterError("OFFLINE", f"Printer {self.host}:{self.port} did not answer "
                               f"within {self.connect_timeout:g}s – switched off or asleep?") from exc
        except OSError as exc:
            raise PrinterError("OFFLINE", f"Cannot reach printer {self.host}:{self.port}: {exc.strerror or exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    @staticmethod
    def _read_packet(sock: socket.socket, timeout: float) -> bytes | None:
        """Exactly 32 bytes, or None when nothing (complete) arrives in time."""
        deadline = time.monotonic() + timeout
        buf = b""
        while len(buf) < 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(32 - len(buf))
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _close(sock: socket.socket, linger: float = 1.0) -> None:
        """Half-close, drain whatever the printer still says, then close – a
        close with unread data would send RST and could cut the job short."""
        try:
            sock.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + linger
            while time.monotonic() < deadline:
                sock.settimeout(max(0.05, deadline - time.monotonic()))
                if not sock.recv(1024):
                    break
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _remember(self, status: dict | None) -> None:
        if status is not None:
            self.last_status = status
            self.last_status_at = time.time()

    def _acquire(self) -> None:
        if not self.lock.acquire(timeout=self.busy_timeout):
            raise PrinterError("BUSY", "Printer is busy with another job.", 503)

    def _query(self, sock: socket.socket) -> dict | None:
        sock.sendall(p.INVALIDATE + p.INITIALIZE + p.STATUS_REQUEST)
        raw = self._read_packet(sock, self.status_timeout)
        if raw is None:
            self.status_supported = False
            return None
        try:
            status = p.parse_status(raw)
        except ValueError:
            log.warning("unexpected status reply: %s", raw.hex())
            return None
        self.status_supported = True
        self._remember(status)
        return status

    # -- public -----------------------------------------------------------

    def status(self) -> dict:
        """{reachable, status|None, statusSupported}; raises PrinterError when offline."""
        self._acquire()
        try:
            sock = self._connect()
            try:
                status = self._query(sock)
            finally:
                self._close(sock, 0.3)
            self.last_error = None
            return {"reachable": True, "status": status, "statusSupported": status is not None}
        except PrinterError as exc:
            self.last_error = exc.message
            raise
        finally:
            self.lock.release()

    def run(self, build: Callable[[dict | None], tuple[bytes, int]]) -> JobResult:
        """
        build(status) -> (job bytes without preamble, page count). Called with
        the connection open and the status (or None) already read, so the
        caller can size the label for the tape that is actually loaded.
        """
        self._acquire()
        started = time.monotonic()
        try:
            sock = self._connect()
            try:
                before = self._query(sock)
                if before and before["errors"]:
                    raise PrinterError("PRINTER_ERROR", "Printer reports: " + ", ".join(before["errors"]),
                                       409, before)
                if before and before["mediaType"] == 0x00:
                    raise PrinterError("NO_MEDIA", "No tape cassette loaded.", 409, before)
                if before and before["mediaType"] == 0xFF:
                    raise PrinterError("PRINTER_ERROR", "Incompatible tape cassette.", 409, before)

                job, pages = build(before)
                try:
                    sock.settimeout(max(10.0, self.wait_timeout))
                    sock.sendall(job)
                except OSError as exc:
                    raise PrinterError("SEND_FAILED", f"Connection dropped while sending: {exc}") from exc

                result = JobResult(state="sent", status_before=before, status_after=None, bytes_sent=len(job))
                if before is not None:
                    self._await(sock, pages, result)
                else:
                    result.notes.append("Printer does not report status over the network – job sent, not confirmed.")
            finally:
                self._close(sock, 1.0)
            self.last_error = None
            log.info("job %s: %d bytes, %d page(s), %.1fs", result.state, result.bytes_sent, pages,
                     time.monotonic() - started)
            return result
        except PrinterError as exc:
            self.last_error = exc.message
            raise
        finally:
            self.lock.release()

    def _await(self, sock: socket.socket, pages: int, result: JobResult) -> None:
        """Read status packets until every page is confirmed, an error, or a timeout."""
        deadline = time.monotonic() + self.wait_timeout + 2.0 * pages
        idle_after_done = 4.0
        while time.monotonic() < deadline:
            wait = deadline - time.monotonic()
            if result.pages_confirmed:
                wait = min(wait, idle_after_done)
            raw = self._read_packet(sock, wait)
            if raw is None:
                break
            try:
                st = p.parse_status(raw)
            except ValueError:
                continue
            result.status_after = st
            self._remember(st)
            if st["statusType"] == 0x02 or st["errors"]:
                raise PrinterError("PRINTER_ERROR", "Printer reports: " + (", ".join(st["errors"]) or "error"),
                                   409, st)
            if st["statusType"] == 0x01:
                result.pages_confirmed += 1
                result.state = "printed"
                if result.pages_confirmed >= pages:
                    break
        if result.state != "printed":
            result.notes.append("No completion message within the timeout – check the printer.")
