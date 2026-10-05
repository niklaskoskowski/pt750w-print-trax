"""Settings from the environment (see .env.example)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ptbridge")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)).replace(",", "."))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = _env(name, "1" if default else "0").lower()
    return value in ("1", "true", "yes", "on")


@dataclass
class Config:
    printer_host: str
    printer_port: int
    listen: str
    port: int
    data_dir: Path
    token: str
    default_tape_mm: int
    margin_mm: float
    cut: str
    chain: bool
    status_timeout: float
    wait_timeout: float
    connect_timeout: float
    max_upload_mb: float
    history: int
    max_copies: int
    snmp_community: str
    snmp_timeout: float
    snmp_port: int
    profile: str
    close_wait: float
    high_res: bool

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_env("PTB_DATA_DIR", "./data")).resolve()
        cut = _env("PTB_CUT", "each").lower()
        return cls(
            printer_host=_env("PTB_PRINTER_HOST"),
            printer_port=_int("PTB_PRINTER_PORT", 9100),
            listen=_env("PTB_LISTEN", "0.0.0.0"),
            port=_int("PTB_PORT", 8750),
            data_dir=data_dir,
            token=_env("PTB_TOKEN"),
            default_tape_mm=_int("PTB_DEFAULT_TAPE_MM", 24),
            margin_mm=max(0.0, _float("PTB_MARGIN_MM", 2.0)),
            cut=cut if cut in ("each", "half", "none") else "each",
            chain=_bool("PTB_CHAIN", False),
            status_timeout=max(0.2, _float("PTB_STATUS_TIMEOUT", 2.0)),
            wait_timeout=max(1.0, _float("PTB_WAIT_TIMEOUT", 25.0)),
            connect_timeout=max(1.0, _float("PTB_CONNECT_TIMEOUT", 5.0)),
            max_upload_mb=max(1.0, _float("PTB_MAX_UPLOAD_MB", 15.0)),
            history=max(0, _int("PTB_HISTORY", 50)),
            max_copies=max(1, _int("PTB_MAX_COPIES", 50)),
            # Empty switches the SNMP status fallback off.
            snmp_community=os.environ.get("PTB_SNMP_COMMUNITY", "public").strip(),
            snmp_timeout=max(0.2, _float("PTB_SNMP_TIMEOUT", 1.5)),
            snmp_port=_int("PTB_SNMP_PORT", 161),
            # compat is what a PT-P750W over Wi-Fi accepts (found with `selftest`).
            profile=_env("PTB_PROFILE", "compat").lower(),
            # How long to wait for the printer to close the connection after a job.
            close_wait=max(1.0, _float("PTB_CLOSE_WAIT", 15.0)),
            # 180 x 360 dpi: twice the resolution along the tape. Default on.
            high_res=_bool("PTB_HIGH_RES", True),
        )

    def ensure_token(self) -> str:
        """PTB_TOKEN, else <data>/token, else a new random one written there."""
        if self.token:
            return self.token
        path = self.data_dir / "token"
        if path.is_file():
            self.token = path.read_text(encoding="utf-8").strip()
            if self.token:
                log.info("API token loaded from %s", path)
                return self.token
        self.token = secrets.token_urlsafe(32)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(self.token + "\n", encoding="utf-8")
        except OSError as exc:
            raise SystemExit(
                f"Cannot store a generated token in {path} ({exc.strerror or exc}). "
                "Set PTB_TOKEN in .env, or make the data directory writable."
            ) from exc
        try:
            path.chmod(0o600)
        except OSError:
            pass
        log.warning("Generated a new API token (stored in %s): %s", path, self.token)
        return self.token
