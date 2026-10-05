"""
Minimal SNMP GET (v2c/v1, UDP 161), standard library only.

Brother printers publish the same 32-byte status packet that "ESC i S"
returns under a private OID. Over Wi-Fi the PT-P750W does not always answer
the status request on port 9100, so this is the second way to learn which
tape is loaded and whether the printer reports an error.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("ptbridge.snmp")

# Brother: the raw status packet (as ESC i S returns it) as an OCTET STRING.
BROTHER_STATUS_OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"


class SnmpError(Exception):
    pass


# --- BER -----------------------------------------------------------------

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(out)]) + out


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    return _tlv(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _oid(oid: str) -> bytes:
    arcs = [int(a) for a in oid.strip(".").split(".")]
    if len(arcs) < 2:
        raise ValueError("OID needs at least two arcs")
    body = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, bytes(body))


def get_request(oid: str, community: str, request_id: int, version: int = 1) -> bytes:
    """version 1 = SNMPv2c, 0 = SNMPv1."""
    varbind = _tlv(0x30, _oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA0, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, varbind))
    return _tlv(0x30, _int(version) + _tlv(0x04, community.encode()) + pdu)


def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    if pos + 2 > len(data):
        raise SnmpError("truncated")
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    value = data[pos:pos + length]
    if len(value) != length:
        raise SnmpError("truncated")
    return tag, value, pos + length


def _children(value: bytes) -> list[tuple[int, bytes]]:
    out = []
    pos = 0
    while pos < len(value):
        tag, inner, pos = _read_tlv(value, pos)
        out.append((tag, inner))
    return out


def parse_response(data: bytes, request_id: int) -> bytes | None:
    """The value of the single varbind; None for noSuchObject/noSuchInstance/null."""
    tag, msg, _ = _read_tlv(data, 0)
    if tag != 0x30:
        raise SnmpError("not an SNMP message")
    parts = _children(msg)
    if len(parts) < 3 or parts[2][0] != 0xA2:
        raise SnmpError("not a GetResponse")
    pdu = _children(parts[2][1])
    if int.from_bytes(pdu[0][1], "big", signed=True) != request_id:
        raise SnmpError("response to another request")
    error_status = int.from_bytes(pdu[1][1], "big")
    if error_status:
        return None  # v1 noSuchName (2) and friends
    varbinds = _children(pdu[3][1])
    if not varbinds:
        return None
    vtag, value = _children(varbinds[0][1])[1]
    if vtag in (0x80, 0x81, 0x82, 0x05):  # noSuchObject, noSuchInstance, endOfMibView, NULL
        return None
    return value


def snmp_get(host: str, oid: str, community: str = "public", timeout: float = 1.5,
             retries: int = 1, port: int = 161) -> bytes | None:
    """
    One value, trying SNMPv2c and then v1. None when the agent says the OID
    does not exist; SnmpError when nothing answers at all.
    """
    for version in (1, 0):
        for _ in range(retries + 1):
            request_id = int.from_bytes(os.urandom(3), "big")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                try:
                    sock.sendto(get_request(oid, community, request_id, version), (host, port))
                    while True:
                        data, _ = sock.recvfrom(4096)
                        try:
                            return parse_response(data, request_id)
                        except SnmpError as exc:
                            if "another request" in str(exc):
                                continue
                            raise
                except socket.timeout:
                    continue
                except OSError as exc:
                    raise SnmpError(str(exc)) from exc
    raise SnmpError(f"no SNMP answer from {host}:{port}")


# --- agent side (mock printer, tests) ------------------------------------

def _decode_oid(body: bytes) -> str:
    arcs = [body[0] // 40, body[0] % 40]
    value = 0
    for byte in body[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(a) for a in arcs)


def parse_request(data: bytes) -> tuple[int, str, int, str]:
    """(version, community, request_id, oid) of a single-varbind GetRequest."""
    _, msg, _ = _read_tlv(data, 0)
    version, community, pdu = _children(msg)[:3]
    if pdu[0] != 0xA0:
        raise SnmpError("not a GetRequest")
    fields = _children(pdu[1])
    varbind = _children(_children(fields[3][1])[0][1])
    return (int.from_bytes(version[1], "big"), community[1].decode(),
            int.from_bytes(fields[0][1], "big", signed=True), _decode_oid(varbind[0][1]))


def get_response(request_id: int, oid: str, value: bytes | None, community: str, version: int) -> bytes:
    if value is None:
        # v2c: noSuchObject; v1: error-status noSuchName.
        vb = _tlv(0x30, _oid(oid) + (b"\x80\x00" if version else b"\x05\x00"))
        err = 0 if version else 2
        pdu = _tlv(0xA2, _int(request_id) + _int(err) + _int(1 if err else 0) + _tlv(0x30, vb))
    else:
        vb = _tlv(0x30, _oid(oid) + _tlv(0x04, value))
        pdu = _tlv(0xA2, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, vb))
    return _tlv(0x30, _int(version) + _tlv(0x04, community.encode()) + pdu)
