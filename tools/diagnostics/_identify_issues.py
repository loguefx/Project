"""Read-only deep issue identification.

Walks one or more show paths, runs the SAME full-file deep decode the
downloader uses (validate_video_file deep+full), and writes a human-readable
report of every episode with a problem. It NEVER deletes anything and NEVER
touches state.json — purely diagnostic, safe to run alongside other jobs.

Usage:
    python _identify_issues.py "<path1>" ["<path2>" ...]
"""
import logging
import sys
import time
from pathlib import Path

from validator import validate_video_file, VIDEO_EXTENSIONS

logging.basicConfig(level=logging.WARNING)

# full=True decodes the ENTIRE file (most accurate, slow); full=False samples
# many windows across the file (fast, used for library-wide scans). Toggled by
# passing "--full" on the command line.
FULL_DECODE = "--full" in sys.argv


def _season_of(p: Path) -> str:
    # …\Show\Season N\file  → "Season N"; fall back to parent folder name
    parent = p.parent.name
    return parent


def scan_path(root_str: str) -> dict:
    root = Path(root_str)
    show = root.name
    files = []
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                files.append(f)
    elif root.is_file():
        files = [root]

    total = len(files)
    print(f"\n=== {show} — {total} video file(s) ===", flush=True)
    broken: list[tuple[str, str, str]] = []   # (season, name, reason)
    inconclusive: list[str] = []
    checked = 0
    t0 = time.time()

    for idx, f in enumerate(files, 1):
        try:
            ok, detail = validate_video_file(f, deep=True, full=FULL_DECODE)
        except Exception as exc:
            inconclusive.append(f"{f.name} — error: {exc}")
            continue
        checked += 1
        if not ok:
            # Distinguish genuine corruption from inconclusive timeouts.
            if "inconclusive" in detail or "timed out" in detail:
                inconclusive.append(f"{f.name} — {detail}")
            else:
                broken.append((_season_of(f), f.name, detail))
                print(f"  [BROKEN] {f.name}\n           {detail}", flush=True)
        if idx % 10 == 0 or idx == total:
            rate = (time.time() - t0) / max(idx, 1)
            eta = rate * (total - idx)
            print(f"  ...{idx}/{total} checked ({len(broken)} broken so far, "
                  f"ETA {eta/60:.0f}m)", flush=True)

    return {
        "show": show,
        "path": root_str,
        "total": total,
        "checked": checked,
        "broken": broken,
        "inconclusive": inconclusive,
    }


def main() -> None:
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        print("No paths given.")
        return
    print(f"Decode mode: {'FULL (entire file)' if FULL_DECODE else 'SAMPLED (windows across file)'}",
          flush=True)

    results = [scan_path(p) for p in paths]

    # Write a combined report next to the script.
    report = Path(__file__).parent / "issues_report.txt"
    lines: list[str] = []
    lines.append("DEEP ISSUE REPORT (read-only — nothing was deleted)")
    lines.append("Generated: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")
    for r in results:
        lines.append("=" * 70)
        lines.append(f"{r['show']}")
        lines.append(f"  Path:    {r['path']}")
        lines.append(f"  Scanned: {r['checked']}/{r['total']} file(s)")
        lines.append(f"  BROKEN:  {len(r['broken'])}")
        lines.append("")
        if r["broken"]:
            by_season: dict[str, list] = {}
            for season, name, reason in r["broken"]:
                by_season.setdefault(season, []).append((name, reason))
            for season in sorted(by_season):
                lines.append(f"  [{season}]")
                for name, reason in sorted(by_season[season]):
                    lines.append(f"    - {name}")
                    lines.append(f"        {reason}")
                lines.append("")
        else:
            lines.append("  No corrupt episodes found.")
            lines.append("")
        if r["inconclusive"]:
            lines.append(f"  Inconclusive (NAS too slow to verify — NOT flagged): "
                         f"{len(r['inconclusive'])}")
            for s in r["inconclusive"][:30]:
                lines.append(f"    - {s}")
            lines.append("")

    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nReport written to: {report}")


if __name__ == "__main__":
    main()
