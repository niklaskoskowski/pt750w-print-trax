"""Recent jobs: metadata in <data>/jobs.json, the rendered raster as a PNG next to it."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("ptbridge.jobs")

_ID = re.compile(r"^[0-9a-f]{8,32}$")


def new_id() -> str:
    return f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"


class JobLog:
    def __init__(self, data_dir: Path, keep: int = 50):
        self.keep = keep
        self.dir = data_dir / "previews"
        self.file = data_dir / "jobs.json"
        self.lock = threading.Lock()
        self.jobs: list[dict] = []
        if keep:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Printing must not depend on the history: run without it.
                log.error("job history off – cannot write %s (%s). Fix: chown the data directory "
                          "to the user the bridge runs as.", self.dir, exc.strerror or exc)
                self.keep = 0
                return
            try:
                loaded = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self.jobs = [j for j in loaded if isinstance(j, dict) and _ID.match(str(j.get("id", "")))]
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.warning("ignoring unreadable %s: %s", self.file, exc)

    def add(self, job: dict, preview: bytes | None) -> None:
        if not self.keep:
            return
        with self.lock:
            if preview:
                try:
                    (self.dir / f"{job['id']}.png").write_bytes(preview)
                    job["hasPreview"] = True
                except OSError as exc:
                    log.warning("cannot store preview: %s", exc)
            self.jobs.insert(0, job)
            for old in self.jobs[self.keep:]:
                try:
                    (self.dir / f"{old['id']}.png").unlink(missing_ok=True)
                except OSError:
                    pass
            self.jobs = self.jobs[: self.keep]
            self._save()

    def _save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.jobs, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError as exc:
            log.warning("cannot write %s: %s", self.file, exc)

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs]

    def preview(self, job_id: str) -> bytes | None:
        if not _ID.match(job_id):
            return None
        path = self.dir / f"{job_id}.png"
        try:
            return path.read_bytes()
        except OSError:
            return None
