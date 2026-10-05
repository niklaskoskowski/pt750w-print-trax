"""
python -m ptbridge selftest – find the command set this printer accepts.

The printer only answers a job it dislikes with "error" (orange/red lamp),
never with a reason. So: print one short label per profile, ask whether it
came out, and on an error wait until the printer has been switched off and
on again. Stops at the first profile that prints and says what to put in
.env. Interactive: run it with `docker compose exec bridge …` (has a TTY).
"""

from __future__ import annotations

import json
import time

import io

from . import protocol as p
from .raster import text_image
from .service import ApiError, Bridge, JobParams

ORDER = ("compat", "plain", "ptouch", "minimal", "standard")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


def _wait_ready(bridge: Bridge) -> dict | None:
    """Until SNMP shows no error (or does not answer), with the operator power-cycling."""
    while True:
        st = bridge.printer._snmp_status()
        if st is None:
            print("  (no SNMP status – carrying on without the check)")
            return None
        if not st["errors"] and st["statusType"] != 0x02:
            return st
        why = ", ".join(st["errors"]) or "error state"
        _ask(f"  Printer reports: {why}. Switch it OFF and ON again, wait until the lamp is green, press Enter … ")
        time.sleep(1.0)


def run(bridge: Bridge, start: str | None = None) -> int:
    order = list(ORDER)
    if start in order:
        order = order[order.index(start):]
    results = []
    print("Self-test: one short label per command set. Watch the printer.\n")
    for letter, profile in zip("ABCDE", order):
        print(f"[{letter}] {profile}: {p.PROFILE_LABELS[profile]}")
        st = _wait_ready(bridge)
        if st:
            print(f"  ready – {st['tapeLabel']} {st['mediaLabel']}")
        params = JobParams({"profile": profile, "jobName": f"selftest {letter} {profile}", "source": "selftest"},
                           bridge.cfg)
        entry = {"profile": profile}
        try:
            res = bridge.print_text(f"Test {letter}", params)
            job = res["job"]
            entry.update(state=job["state"], after=job.get("statusAfter"), warnings=job["warnings"])
            print(f"  sent ({job['tapeLabel']}, {job['lengthMm']} mm) – status after: {job.get('statusAfter')}")
        except ApiError as exc:
            entry.update(state="error", error=exc.message)
            print(f"  bridge: {exc.message}")
        answer = _ask("  Did a label come out, readable? [y/n] ")
        entry["printed"] = answer.startswith(("y", "j"))
        results.append(entry)
        if entry["printed"]:
            print(f"\n=> Works with profile '{profile}'. Put this in .env and restart:\n"
                  f"   PTB_PROFILE={profile}\n   docker compose up -d")
            break
        print()
    else:
        print("=> No profile printed. Check that the printer prints over Wi-Fi from the Brother app,"
              " and send the summary below.")

    summary = json.dumps(results, indent=1, ensure_ascii=False)
    try:
        (bridge.cfg.data_dir / "selftest.json").write_text(summary, encoding="utf-8")
    except OSError:
        pass
    print("\nSummary (also in data/selftest.json):")
    print(summary)
    return 0 if results and results[-1]["printed"] else 1


BATCH_ORDER = ("once", "chain", "perpage", "noautocut")


def run_batch(bridge: Bridge, start: str | None = None) -> int:
    """
    python -m ptbridge batchtest – find how this printer wants a multi-page
    job, so a batch comes out as ONE half-cut strip instead of every label
    ejected and cut with its own leader.
    """
    order = list(BATCH_ORDER)
    if start in order:
        order = order[order.index(start):]
    results = []
    print("Batch test: three short labels per variant, as one job with half cuts. Watch the printer.\n")
    for letter, mode in zip("ABCD", order):
        print(f"[{letter}] {mode}: {p.BATCH_MODE_LABELS[mode]}")
        st = _wait_ready(bridge)
        tape = (st or {}).get("tapeMm") or bridge.cfg.default_tape_mm
        batch_id = bridge.batches.create()
        for i in range(3):
            buf = io.BytesIO()
            text_image(f"{letter}{i + 1}", tape).save(buf, "PNG")
            bridge.batches.add(batch_id, i, buf.getvalue(), None, None, f"{letter}{i + 1}")
        params = JobParams({"cut": "half", "batchMode": mode, "jobName": f"batchtest {letter} {mode}",
                            "source": "batchtest"}, bridge.cfg)
        entry = {"batchMode": mode}
        try:
            job = bridge.print_batch(batch_id, params, "along")["job"]
            entry.update(state=job["state"], after=job.get("statusAfter"))
            print(f"  sent 3 labels as one job ({job['tapeLabel']}, strip ~{job['stripMm']} mm)")
        except ApiError as exc:
            bridge.batches.delete(batch_id)
            entry.update(state="error", error=exc.message)
            print(f"  bridge: {exc.message}")
        answer = _ask(f"  Did {letter}1 {letter}2 {letter}3 come out as ONE strip – half cuts between them, "
                      "cut once at the end? [y/n] ")
        entry["strip"] = answer.startswith(("y", "j"))
        results.append(entry)
        if entry["strip"]:
            print(f"\n=> Batches work with '{mode}'. Put this in .env and restart:\n"
                  f"   PTB_BATCH_MODE={mode}\n   docker compose up -d")
            break
        print()
    else:
        print("=> No variant gave one strip. Send the summary below, and a photo of what came out.")

    summary = json.dumps(results, indent=1, ensure_ascii=False)
    try:
        (bridge.cfg.data_dir / "batchtest.json").write_text(summary, encoding="utf-8")
    except OSError:
        pass
    print("\nSummary (also in data/batchtest.json):")
    print(summary)
    return 0 if results and results[-1]["strip"] else 1
