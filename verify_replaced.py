"""
Independent verification of the episodes we just replaced. For each file we run
TWO checks and compare:

  1) OUR validator   — validate_video_file(deep=True, full=True): full decode +
                       timestamp-gap pass (the same logic the watcher used).
  2) RAW cross-check — a completely separate ffmpeg full decode (counting error
                       lines) and a raw ffprobe PTS scan (max packet gap),
                       independent of our validator code.

If the raw decode is clean (0 errors) and the raw max-PTS-gap is small while our
validator also PASSES, that proves the replacement plays without
skipping/corruption AND that our validation isn't giving false passes.
"""
import concurrent.futures
import subprocess
from pathlib import Path

from validator import validate_video_file, _find_ffmpeg, _find_ffprobe, _PTS_GAP_SEC

BASE = r"\\192.168.0.181\Jellyfin3\Adult Animations & Cartoons\SpongeBob SquarePants"
EPS = {
    "S02E28": 2, "S04E33": 4, "S07E01": 7,
    "S10E28": 10, "S10E61": 10, "S10E64": 10, "S10E67": 10,
    "S15E02": 15, "S15E05": 15, "S15E06": 15, "S15E12": 15,
}
FF = _find_ffmpeg()
FP = _find_ffprobe()


def _path(ep: str) -> Path:
    p = Path(BASE) / f"Season {EPS[ep]}" / f"SpongeBob SquarePants - {ep}.mkv"
    if not p.exists():
        alt = p.with_suffix(".mp4")
        if alt.exists():
            return alt
    return p


def raw_decode_errors(path: Path):
    r = subprocess.run(
        [FF, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    errs = [l for l in (r.stderr or "").splitlines() if l.strip()]
    return len(errs), (errs[0][:90] if errs else "")


def raw_pts_max_gap(path: Path) -> float:
    r = subprocess.run(
        [FP, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    ts = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip().rstrip(",")
        if ln and ln.upper() != "N/A":
            try:
                ts.append(float(ln))
            except ValueError:
                pass
    if len(ts) < 30:
        return -1.0
    ts.sort()
    return max((b - a) for a, b in zip(ts, ts[1:]))


def check(ep: str):
    p = _path(ep)
    if not p.exists():
        return (ep, "MISSING", None, None, "")
    ours_ok, ours_detail = validate_video_file(p, deep=True, full=True)
    n_err, first = raw_decode_errors(p)
    gap = raw_pts_max_gap(p)
    return (ep, "PASS" if ours_ok else f"FAIL:{ours_detail[:40]}", n_err, gap, first)


def main():
    eps = sorted(EPS)
    print(f"Verifying {len(eps)} replaced episodes (full decode x2, this takes a while)…\n", flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        for res in ex.map(check, eps):
            results.append(res)
            ep, ours, nerr, gap, first = res
            print(f"  done {ep}: ours={ours}, raw_errors={nerr}, max_pts_gap={gap}", flush=True)

    print("\n" + "=" * 78)
    print(f"{'EP':8}{'OUR VALIDATOR':16}{'RAW DECODE ERRS':18}{'MAX PTS GAP(s)':16}note")
    print("-" * 78)
    all_good = True
    for ep, ours, nerr, gap, first in sorted(results):
        bad = (ours != "PASS") or (nerr is None or nerr > 0) or (gap is not None and gap >= _PTS_GAP_SEC)
        if bad:
            all_good = False
        gtxt = f"{gap:.1f}" if isinstance(gap, float) else str(gap)
        print(f"{ep:8}{ours:16}{str(nerr):18}{gtxt:16}{first}")
    print("=" * 78)
    print("\nRESULT:", "ALL CLEAN — every replacement decodes error-free, no skipping, and our validator agrees."
          if all_good else "DISCREPANCY FOUND — see rows above.")


if __name__ == "__main__":
    main()
