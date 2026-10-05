"""
Talking to the printer: raw TCP on port 9100.

One connection per job. The printer handles one client at a time, so every
access goes through `Printer.lock` – concurrent HTTP requests queue up here
instead of fighting over the socket.

Status read-back is best effort, two ways: "ESC i S" on the print
connection, and – because the PT-P750W often ignores that over Wi-Fi – the
same 32-byte packet via SNMP (Brother private OID). With a status the bridge
uses the loaded tape width and refuses to print into an error (cover open,
no tape); over TCP it also waits for "printing completed". Without one the
job is still sent, sized for the requested or default tape, and reported as
`sent` instead of `printed`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p
from .snmp import BROTHER_STATUS_OID, SYS_DESCR_OID, SnmpError, snmp_get

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
                 status_timeout: float = 2.0, wait_timeout: float = 25.0, busy_timeout: float = 120.0,
                 snmp_community: str = "public", snmp_timeout: float = 1.5, snmp_port: int = 161):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.status_timeout = status_timeout
        self.wait_timeout = wait_timeout
        self.busy_timeout = busy_timeout
        self.snmp_community = snmp_community
        self.snmp_timeout = snmp_timeout
        self.snmp_port = snmp_port
        self.lock = threading.Lock()
        self.last_status: dict | None = None
        self.last_status_at: float | None = None
        self.last_error: str | None = None
        self.status_supported: bool | None = None
        # How the last status came in: "tcp", "snmp" or None.
        self.status_via: str | None = None
        # Whether port 9100 answers "ESC i S". Once it has not, jobs stop
        # waiting for it; an explicit status check asks again.
        self.tcp_status: bool | None = None

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

    def _snmp_status(self) -> dict | None:
        if not self.snmp_community:
            return None
        try:
            raw = snmp_get(self.host, BROTHER_STATUS_OID, self.snmp_community, self.snmp_timeout,
                           port=self.snmp_port)
        except SnmpError as exc:
            log.debug("SNMP status: %s", exc)
            return None
        if not raw:
            return None
        try:
            return p.parse_status(raw)
        except ValueError:
            log.warning("unexpected SNMP status value: %s", raw.hex())
            return None

    def _tcp_status(self, sock: socket.socket) -> dict | None:
        """ESC i S on an open connection whose preamble has been sent."""
        sock.sendall(p.STATUS_REQUEST)
        raw = self._read_packet(sock, self.status_timeout)
        self.tcp_status = raw is not None
        if raw is None:
            return None
        try:
            return p.parse_status(raw)
        except ValueError:
            log.warning("unexpected status reply: %s", raw.hex())
            return None

    def _query(self, sock: socket.socket, snmp_status: dict | None, probe_tcp: bool = False) -> dict | None:
        """
        SNMP first: it costs the print connection nothing. Only without it is
        ESC i S sent on 9100 – a request the printer leaves unanswered would
        otherwise sit in the middle of every job.
        """
        status, via = snmp_status, ("snmp" if snmp_status is not None else None)
        sock.sendall(p.INVALIDATE + p.INITIALIZE)
        if status is None and (self.tcp_status is not False or probe_tcp):
            status = self._tcp_status(sock)
            via = "tcp" if status is not None else None
        self.status_supported = status is not None
        self.status_via = via
        self._remember(status)
        return status

    # -- public -----------------------------------------------------------

    def status(self) -> dict:
        """{reachable, status|None, statusSupported}; raises PrinterError when offline."""
        self._acquire()
        try:
            snmp_status = self._snmp_status()
            sock = self._connect()
            try:
                status = self._query(sock, snmp_status, probe_tcp=True)
            finally:
                self._close(sock, 0.3)
            self.last_error = None
            return {"reachable": True, "status": status, "statusSupported": status is not None,
                    "statusVia": self.status_via}
        except PrinterError as exc:
            self.last_error = exc.message
            raise
        finally:
            self.lock.release()

    def run(self, build: Callable[[dict | None], tuple[bytes, int]]) -> JobResult:
        """
        build(status) -> (job bytes without preamble, page count). Called with
        the status (or None) already read, so the caller can size the label
        for the tape that is actually loaded – before the connection opens
        when SNMP answered, on the open connection otherwise.
        """
        self._acquire()
        started = time.monotonic()
        try:
            # SNMP first: then the whole job is ready before the connection
            # opens and goes out in one write – no request the printer leaves
            # unanswered, no gap between preamble and raster.
            before = self._snmp_status()
            via = "snmp" if before is not None else None
            job = b""
            pages = 1
            if before is not None:
                self._precheck(before)
                job, pages = build(before)
            sock = self._connect()
            try:
                if before is not None:
                    payload = p.INVALIDATE + p.INITIALIZE + job
                else:
                    sock.sendall(p.INVALIDATE + p.INITIALIZE)
                    if self.tcp_status is not False:
                        before = self._tcp_status(sock)
                        via = "tcp" if before is not None else None
                    if before is not None:
                        self._precheck(before)
                    job, pages = build(before)
                    payload = job
                self.status_supported = before is not None
                self.status_via = via
                self._remember(before)
                try:
                    sock.settimeout(max(10.0, self.wait_timeout))
                    sock.sendall(payload)
                except OSError as exc:
                    raise PrinterError("SEND_FAILED", f"Connection dropped while sending: {exc}") from exc

                result = JobResult(state="sent", status_before=before, status_after=None, bytes_sent=len(payload))
                tcp_confirmed = via == "tcp"
                if tcp_confirmed:
                    self._await(sock, pages, result)
            finally:
                self._close(sock, 1.0)
            if not tcp_confirmed:
                watched = self._watch_snmp(pages, result)
                if before is None:
                    result.notes.append("No status before the job (TCP 9100 or SNMP) – "
                                        "sized for the requested/default tape.")
                if result.state != "printed":
                    result.notes.append("Sent; no error reported afterwards (SNMP)." if watched
                                        else "Sent, not confirmed – the printer reports nothing back.")
            self.last_error = None
            log.info("job %s: %d bytes, %d page(s), %.1fs", result.state, result.bytes_sent, pages,
                     time.monotonic() - started)
            return result
        except PrinterError as exc:
            self.last_error = exc.message
            raise
        finally:
            self.lock.release()

    @staticmethod
    def _precheck(status: dict) -> None:
        if status["errors"]:
            raise PrinterError("PRINTER_ERROR", "Printer reports: " + ", ".join(status["errors"]), 409, status)
        if status["statusType"] == 0x02:
            raise PrinterError("PRINTER_ERROR", "Printer is still in an error state from the last job – "
                               "switch it off and on again.", 409, status)
        if status["mediaType"] == 0x00:
            raise PrinterError("NO_MEDIA", "No tape cassette loaded.", 409, status)
        if status["mediaType"] == 0xFF:
            raise PrinterError("PRINTER_ERROR", "Incompatible tape cassette.", 409, status)

    def _watch_snmp(self, pages: int, result: JobResult) -> bool:
        """
        After a job the print connection did not confirm: poll SNMP for an
        error, and for the printing phase coming and going. False when SNMP
        does not answer at all.
        """
        if not self.snmp_community:
            return False
        sent = time.monotonic()
        deadline = sent + min(self.wait_timeout, 6.0 + 3.0 * pages)
        seen_printing = False
        polled = False
        while time.monotonic() < deadline:
            time.sleep(0.5)
            st = self._snmp_status()
            if st is None:
                return polled
            polled = True
            result.status_after = st
            self._remember(st)
            if st["errors"]:
                raise PrinterError("PRINTER_ERROR", "Printer went into error after the job: "
                                   + ", ".join(st["errors"]) + f". Status {st['raw']}", 409, st)
            if st["statusType"] == 0x02 and not result.notes:
                # Status type "error" without a single error bit: report it with
                # the raw packet rather than guess what it means.
                result.notes.append(f"Printer status says 'error' without an error bit: {st['raw']}")
            if st["phase"] == "printing":
                seen_printing = True
            elif seen_printing:
                result.state = "printed"
                result.pages_confirmed = pages
                return True
            elif time.monotonic() - sent > 3.0:
                return True  # idle and no error: nothing more to learn
        return polled

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

    # -- diagnostics ------------------------------------------------------

    def probe(self, wait: float = 6.0) -> dict:
        """Every way of reading the status, raw – for `python -m ptbridge probe`."""
        out: dict = {"host": self.host, "port": self.port}
        self._acquire()
        try:
            started = time.monotonic()
            try:
                sock = self._connect()
            except PrinterError as exc:
                out["tcp"] = {"connect": False, "error": exc.message}
            else:
                out["tcp"] = {"connect": True, "connectMs": int((time.monotonic() - started) * 1000)}
                try:
                    sock.sendall(p.INVALIDATE + p.INITIALIZE + p.STATUS_REQUEST)
                    t0 = time.monotonic()
                    raw = self._read_packet(sock, wait)
                    out["tcp"]["statusReply"] = raw.hex() if raw else None
                    if raw:
                        out["tcp"]["replyMs"] = int((time.monotonic() - t0) * 1000)
                        try:
                            out["tcp"]["parsed"] = p.parse_status(raw)
                        except ValueError as exc:
                            out["tcp"]["parseError"] = str(exc)
                finally:
                    self._close(sock, 0.3)
        finally:
            self.lock.release()

        snmp: dict = {"community": self.snmp_community}
        for key, oid in (("sysDescr", SYS_DESCR_OID), ("brotherStatus", BROTHER_STATUS_OID)):
            try:
                value = snmp_get(self.host, oid, self.snmp_community or "public", 2.0, port=self.snmp_port)
                if value is None:
                    snmp[key] = "no such object"
                elif key == "sysDescr":
                    snmp[key] = value.decode("utf-8", "replace")
                else:
                    snmp[key] = value.hex()
                    try:
                        snmp["parsed"] = p.parse_status(value)
                    except ValueError as exc:
                        snmp["parseError"] = str(exc)
            except SnmpError as exc:
                snmp[key] = f"error: {exc}"
        out["snmp"] = snmp
        return out

    def watch(self, seconds: float, interval: float = 0.3):
        """Yields (elapsed, status) for every change of the SNMP status packet."""
        started = time.monotonic()
        last = None
        while time.monotonic() - started < seconds:
            st = self._snmp_status()
            raw = st["raw"] if st else None
            if raw != last:
                last = raw
                yield time.monotonic() - started, st
            time.sleep(interval)
