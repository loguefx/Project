"""
READ-ONLY deep scan of a single show folder.

Runs the SAME detection logic the downloader uses to decide an episode is
broken — but DELETES NOTHING. It surfaces three problem classes:

  1. structural  — no video / no audio / truncated / too short (ffprobe header)
  2. corruption  — damaged/garbled frames (sampled multi-window ffmpeg decode)
  3. skipping    — large video timestamp gaps = missing content that freezes /
                   jumps on playback (the new _detect_pts_gaps pass)

Writes an incremental JSON report (rewritten as files finish) so progress and
the broken list can be inspected while the scan is still running.

Files are checked CONCURRENTLY (thread pool) because each file is dominated by
SMB I/O wait + ffmpeg/ffprobe subprocesses, so running several at once is a big
speedup over a slow NAS. Each check is otherwise identical to the serial path.

Usage:
    python _scan_show.py "<show folder>" "<report.json>" [workers]
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import validator as v


def _check_one(f: Path) -> dict:
    """Run the full detection on a single file. Returns a result dict with
    reason=None when healthy."""
    reason = None
    kind = None
    try:
        # Pass 1: header (no video/audio, truncated, too short) + sampled
        # multi-window decode (scattered frame corruption).
        ok, detail = v.validate_video_file(f, deep=True, full=False)
        if not ok:
            reason = detail
            kind = "corruption/structural"
        else:
            # Pass 2: skip detection — full video PTS scan for large gaps.
            pr = v._ffprobe_streams(f)
            fps = pr.info.get("fps", 0.0) if pr.info else 0.0
            dur = pr.info.get("duration") if pr.info else None
            ran, gaps, gdetail = v._detect_pts_gaps(f, fps=fps, duration=dur)
            if ran and gaps > 0:
                reason = (
                    f"playback skipping — {gaps} large timestamp gap(s) "
                    f"(missing content). e.g. {gdetail}"
                )
                kind = "skipping"
    except Exception as exc:  # noqa: BLE001 — report, never crash the scan
        reason = f"scan error: {exc}"
        kind = "error"
    return {"path": str(f), "file": f.name, "kind": kind, "reason": reason}


def scan(show_path: str, report_path: Path, workers: int = 6) -> None:
    root = Path(show_path)
    files = sorted(
        x for x in root.rglob("*")
        if x.is_file()
        and x.suffix.lower() in v.VIDEO_EXTENSIONS
        and not x.name.endswith((".part", ".!qb"))
    )
    total = len(files)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"=== Scanning {root.name} — {total} video file(s), {workers} workers ===",
        flush=True,
    )

    results: dict = {
        "show": str(root),
        "total": total,
        "checked": 0,
        "broken": [],
        "started": started,
    }
    broken: list[dict] = []
    lock = threading.Lock()
    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_one, f): f for f in files}
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                done += 1
                elapsed = time.time() - t0
                eta_min = (total - done) * (elapsed / done) / 60
                status = (
                    "OK" if not res["reason"]
                    else f"BROKEN [{res['kind']}] — {res['reason']}"
                )
                print(
                    f"[{done}/{total}] ({eta_min:5.0f}m left) {res['file']}: {status}",
                    flush=True,
                )
                results["checked"] = done
                if res["reason"]:
                    broken.append(res)
                    results["broken"] = sorted(broken, key=lambda r: r["file"])
                try:
                    report_path.write_text(
                        json.dumps(results, indent=2), encoding="utf-8"
                    )
                except OSError:
                    pass

    results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    results["elapsed_min"] = round((time.time() - t0) / 60, 1)
    try:
        report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    except OSError:
        pass
    print(
        f"=== DONE — {total} checked, {len(broken)} broken "
        f"({results['elapsed_min']}m) ===",
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python _scan_show.py \"<show folder>\" \"<report.json>\" [workers]")
        sys.exit(2)
    nworkers = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    scan(sys.argv[1], Path(sys.argv[2]), workers=nworkers)
