"""
Video file validator.

Checks downloaded episode/movie files for corruption using:
  1. Minimum file size per episode (catches truncated downloads)
  2. ffprobe stream check — verifies the file contains BOTH a playable video
     stream AND a playable audio stream of sufficient duration.

SAFETY: Only auto-deletes files that our downloader queued (tracked in
state["downloaded_torrents"] or state["queued"]).  Files that were not
downloaded by this system are reported but never touched.

When a corrupt file IS deleted:
  - The originating torrent name is added to state["torrent_blacklist"] for
    that episode so the next search skips that release and tries a different one.
  - The episode's retry_queue entry is cleared so it gets re-searched promptly.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts"}

# Minimum file size PER EPISODE in MB — anything below is definitely truncated.
_MIN_MB_PER_EP: float = 20.0

# Minimum playable duration in seconds (1 minute).
_MIN_DURATION_SEC: float = 60.0

# ffprobe timeout per attempt (seconds). NAS/SMB reads can be slow under load,
# so this is generous. A timeout is treated as INCONCLUSIVE, never as "broken".
_FFPROBE_TIMEOUT_SEC: int = 90

# How many times to retry ffprobe after a timeout before giving up (inconclusive).
_FFPROBE_TIMEOUT_RETRIES: int = 1

# ── Deep decode check (catches stuttering / desync the header check can't) ──
# How many seconds of actual video to decode when doing a deep check.
_DECODE_SAMPLE_SEC: int = 60

# Max time to allow a deep decode to run before giving up (inconclusive, never
# flagged broken). Decoding 60s is usually a few seconds, but slow NAS reads
# can stretch this — be generous.
_DECODE_TIMEOUT_SEC: int = 240

# Number of decode-error lines tolerated in the sample before a file is
# considered to have playback corruption. A couple of benign warnings are
# normal; sustained errors mean dropped/garbled frames → visible stutter.
_DECODE_ERROR_THRESHOLD: int = 3

# ffmpeg "-v warning" prints MANY benign lines for perfectly good files — e.g.
# "timescale not set", muxer "non monotonically increasing dts" (an artifact of
# the -f null output, not the source), channel-layout guesses, deprecation
# notices. Counting those flags healthy files as broken. We only count lines
# that signal REAL stream corruption: decode errors, error concealment, damaged
# packets, missing/invalid NAL units, broken containers, etc.
_CORRUPTION_RE = re.compile(
    r"error while decoding"
    r"|concealing \d+ "
    r"|decode_slice_header error"
    r"|\bno frame!"
    r"|non.?existing (?:pps|sps)"
    r"|missing picture in access unit"
    r"|invalid (?:nal unit|data found)"
    r"|corrupt(?:ed)? (?:frame|input|packet|macroblock)"
    r"|\bcorrupt decoded"
    r"|ebml header parsing failed"
    r"|header (?:damaged|missing)"
    r"|\bmmco:"
    r"|reference count .*overflow"
    r"|illegal (?:short term|memory management)"
    r"|cabac decode of qscale"
    r"|deblocking filter parameters"
    r"|bytestream.*overread"
    r"|truncat",
    re.IGNORECASE,
)


def _count_decode_issues(stderr: str) -> tuple:
    """Return (issue_count, last_detail) counting ONLY genuine-corruption lines
    from an ffmpeg decode pass, ignoring benign warnings."""
    count = 0
    last = ""
    for ln in (stderr or "").splitlines():
        ln = ln.strip()
        if ln and _CORRUPTION_RE.search(ln):
            count += 1
            last = ln[-160:]
    return count, last

# Seconds decoded at EACH sample point during a deep check.
_DECODE_POINT_SEC: int = 15

# Deep sampling spreads windows across the WHOLE file so corruption that only
# shows up in part of the episode is still caught. A fixed 3-point sample used
# to miss files whose damage happened to fall between the windows. We now start
# a new window roughly every _DECODE_WINDOW_INTERVAL_SEC, clamped to a sane
# count so a long file doesn't take forever.
_DECODE_WINDOW_INTERVAL_SEC: int = 90
_DECODE_MIN_POINTS: int = 4
_DECODE_MAX_POINTS: int = 14

# FULL decode (entire file) — used for the installed-only post-download review
# where accuracy matters most and there are only a handful of files. Reading a
# whole episode over a slow NAS can take several minutes, so the timeout is
# very generous. A timeout no longer silently passes the file: it FALLS BACK to
# the sampled-window decode (see _ffmpeg_decode_sample) so a slow read can never
# let an unverified episode through as "ok".
_FULL_DECODE_TIMEOUT_SEC: int = 2400

# ── Playback-skip detection (timestamp gaps) ──────────────────────────────
# Frame rate is informational ONLY — we intake whatever the release was encoded
# at (24/25/30/50/60/VFR, etc.) and never reject or re-download based on fps.
# The thing that actually matters for "skipping" is missing CONTENT: a chunk of
# the episode whose packets are absent, so the picture freezes/jumps even though
# every frame that IS present decodes cleanly (so the decode-error pass sees
# nothing). We detect that as a large gap between consecutive video presentation
# timestamps. The threshold is set well above any legitimate frame interval — at
# even 23.976fps a normal gap is ~0.042s — so variable-frame-rate or high-fps
# content never trips it; only genuinely-missing content does.
_PTS_GAP_SEC: float = 3.0

# ffprobe packet scan reads only the container index (not the media), so it is
# usually fast, but a slow NAS read still gets a generous timeout. A timeout is
# INCONCLUSIVE (never flagged as a skip).
_PTS_PROBE_TIMEOUT_SEC: int = 300

# How many files a full-library scan validates CONCURRENTLY. Each file's check
# spends almost all its time blocked on NAS reads + ffmpeg/ffprobe subprocesses
# (which run outside the GIL), so validating several at once turns a multi-day
# serial deep scan into hours without changing WHAT is detected. The per-file
# logic is identical; we just stop waiting for one file before starting the
# next. Deep scans are heavier (real decode), so they use fewer workers than the
# lightweight header-only scan. Tune down if the NAS or CPU is saturated.
_SCAN_WORKERS_DEEP: int = 8
_SCAN_WORKERS_SHALLOW: int = 16

# A full-library scan usually spans several NAS servers. Files are enumerated
# library-by-library, so without re-ordering, the first N items in the work
# queue all live on the SAME server — pinning N workers to one box while every
# other server sits idle. We round-robin the queue across servers (see
# _interleave_by_server) and widen the pool so several servers are read at once,
# multiplying effective read bandwidth. Per-server concurrency stays modest so
# no single box is saturated; the total is capped for sanity. Override the
# auto-sizing with the STVD_SCAN_WORKERS environment variable if needed.
_SCAN_WORKERS_PER_SERVER: int = 2
_SCAN_WORKERS_MAX: int = 24

# ── Incremental validation cache ──────────────────────────────────────────
# A full-library deep scan is bottlenecked by NAS bandwidth (each multi-GB file
# must be largely read over the network), so a first pass can take a long time.
# To make that pay off, we remember every file's result keyed by its identity
# (path + size + mtime) and the depth it was validated at. On the next scan a
# file whose size/mtime are unchanged is skipped instantly. We also flush the
# cache to disk every few hundred files, so if the server crashes or is
# restarted mid-scan, the next run RESUMES instead of starting over.
from runtime_paths import DATA_DIR as _DATA_DIR
_VALIDATION_CACHE_FILE = _DATA_DIR / "validation_cache.json"
# Persist progress often so a crash/restart mid-scan loses almost nothing.
# Deep scans are slow (a few files/min), so we flush on a TIME interval rather
# than purely on a file count — whichever comes first.
_CACHE_FLUSH_EVERY: int = 300
_CACHE_FLUSH_SECS: float = 30.0

# Reasons we must NOT cache as a settled result — they're "inconclusive", so the
# file should be retried on the next scan rather than permanently trusted/ignored.
_INCONCLUSIVE_MARKERS = (
    "inconclusive", "timed out", "not installed", "validator error",
)


def _load_validation_cache() -> dict:
    """Load the persisted validation cache (path -> entry). Never raises."""
    try:
        with open(_VALIDATION_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_validation_cache(cache: dict) -> None:
    """Atomically persist the validation cache. Never raises."""
    try:
        tmp = _VALIDATION_CACHE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, _VALIDATION_CACHE_FILE)
    except OSError as exc:
        logger.warning("[Validator] Could not persist validation cache: %s", exc)


def _is_conclusive(valid: bool, reason: str) -> bool:
    """A result is cacheable only if it's a definite verdict (broken results
    always are; an 'ok' that came from a timeout / missing tool is not)."""
    if not valid:
        return True
    low = reason.lower()
    return not any(m in low for m in _INCONCLUSIVE_MARKERS)


# ---------------------------------------------------------------------------
# ffprobe location — searches PATH first, then known WinGet/Chocolatey paths
# ---------------------------------------------------------------------------

_FFPROBE_CACHE: Optional[str] = None


def _find_ffprobe() -> Optional[str]:
    """Locate ffprobe.exe. Returns absolute path or None."""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE is not None:
        return _FFPROBE_CACHE or None

    on_path = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if on_path:
        _FFPROBE_CACHE = on_path
        return on_path

    # Common Windows install locations
    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        winget_root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            try:
                candidates.extend(winget_root.rglob("ffprobe.exe"))
            except OSError:
                pass

    extra_dirs = [
        Path("C:/ProgramData/chocolatey/bin"),
        Path("C:/Program Files/ffmpeg/bin"),
        Path("C:/ffmpeg/bin"),
    ]
    for d in extra_dirs:
        candidate = d / "ffprobe.exe"
        if candidate.exists():
            candidates.append(candidate)

    for c in candidates:
        if c.is_file():
            logger.info("[Validator] Using ffprobe at %s", c)
            _FFPROBE_CACHE = str(c)
            return _FFPROBE_CACHE

    _FFPROBE_CACHE = ""  # cache the "not found" result
    return None


_FFMPEG_CACHE: Optional[str] = None


def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg.exe. Returns absolute path or None."""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE or None

    # ffmpeg almost always lives next to ffprobe — check there first.
    ffprobe = _find_ffprobe()
    if ffprobe:
        sibling = Path(ffprobe).with_name(
            "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        )
        if sibling.exists():
            _FFMPEG_CACHE = str(sibling)
            logger.info("[Validator] Using ffmpeg at %s", sibling)
            return _FFMPEG_CACHE

    on_path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if on_path:
        _FFMPEG_CACHE = on_path
        return on_path

    _FFMPEG_CACHE = ""
    return None


def _decode_offsets(duration: Optional[float]) -> list:
    """
    Compute sample-window start offsets spread evenly across the whole file.

    A fixed 3-point sample (start/middle/end) used to miss files whose damage
    fell between the windows — exactly what happened with KotH S14E05/E10, which
    had 100+ corrupt-frame errors scattered across the file yet decoded clean in
    the 3 narrow windows. We now place one window roughly every
    _DECODE_WINDOW_INTERVAL_SEC (clamped between _DECODE_MIN_POINTS and
    _DECODE_MAX_POINTS) so coverage scales with episode length.
    """
    point = _DECODE_POINT_SEC
    if not duration or duration <= point * 2:
        return [0.0]

    usable = max(0.0, duration - point - 5)  # leave a little tail runway
    n = int(usable // _DECODE_WINDOW_INTERVAL_SEC) + 1
    n = max(_DECODE_MIN_POINTS, min(_DECODE_MAX_POINTS, n))
    if n <= 1:
        return [0.0]

    step = usable / (n - 1)
    return [round(i * step, 2) for i in range(n)]


def _ffmpeg_decode_sample(
    path: Path, duration: Optional[float] = None, full: bool = False
) -> tuple:
    """
    Actually DECODE the file and count decode errors AND timestamp warnings.
    This catches the corruption class the header check misses — damaged/dropped
    frames and non-monotonic / jittery timestamps that cause stuttering and
    audio desync during playback. Uses ffmpeg "-v warning" so timestamp problems
    (not just hard decode errors) are surfaced.

    Two modes:
      full=False  → sample several short windows spread across the whole file
                    (fast; used for full-library scans). Window placement comes
                    from _decode_offsets() so coverage scales with duration.
      full=True   → decode the ENTIRE file end to end (slow but exhaustive;
                    used for the installed-only post-download review where there
                    are only a handful of files and accuracy must be flawless).

    Returns (ran: bool, issue_count: int, detail: str):
      ran=False            → ffmpeg unavailable or timed out (INCONCLUSIVE)
      ran=True, count=0     → decoded cleanly
      ran=True, count>0     → that many error/warning lines were emitted
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return (False, 0, "ffmpeg not installed")

    if full:
        # Decode the whole file in one pass.
        cmd = [ffmpeg, "-v", "warning", "-i", str(path), "-f", "null", "-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_FULL_DECODE_TIMEOUT_SEC
            )
            # Count only genuine-corruption lines (benign warnings are ignored).
            count, detail = _count_decode_issues(result.stderr)
            return (True, count, detail)
        except subprocess.TimeoutExpired:
            # A timeout must NOT silently pass the file. A 50/60fps episode has
            # ~2x the frames to decode and is exactly the kind of file that
            # blows the full-decode budget over a slow NAS — historically that
            # let unverified (sometimes skippy) episodes through as "ok" and so
            # they were never re-downloaded. Fall back to the sampled-window
            # decode below, which is far faster and still catches damaged
            # frames, instead of returning an automatic pass.
            logger.info(
                "[Validator] full decode timed out on %s — falling back to "
                "sampled-window decode (NOT auto-passing).", path.name,
            )
            # fall through to the sampled path
        except OSError as exc:
            return (False, 0, f"ffmpeg exec error: {exc}")

    point = _DECODE_POINT_SEC
    offsets = _decode_offsets(duration)

    total_issues = 0
    last_detail = ""
    for off in offsets:
        cmd = [ffmpeg, "-v", "warning"]
        if off > 0:
            cmd += ["-ss", str(round(off, 2))]
        cmd += ["-i", str(path), "-t", str(point), "-f", "null", "-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_DECODE_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            # Inconclusive — slow read, NOT proof of corruption.
            logger.info("[Validator] decode sample timed out on %s — inconclusive", path.name)
            return (False, 0, "decode timed out")
        except OSError as exc:
            return (False, 0, f"ffmpeg exec error: {exc}")

        count, detail = _count_decode_issues(result.stderr)
        total_issues += count
        if detail:
            last_detail = detail

    return (True, total_issues, last_detail)


def _detect_pts_gaps(
    path: Path, fps: float = 0.0, duration: Optional[float] = None
) -> tuple:
    """
    Scan the primary video stream's packet presentation timestamps (PTS) for
    large gaps. A gap >= _PTS_GAP_SEC means a chunk of the episode's content is
    simply MISSING — on playback the picture freezes/jumps ("skipping"), yet the
    decode pass sees nothing wrong because every frame that IS present decodes
    cleanly. This is the gap that header + decode checks cannot catch.

    Reads only the container index via ffprobe (-show_entries packet=pts_time),
    not the media itself, so it is normally fast. B-frame reordering is handled
    by sorting the timestamps before measuring deltas, so out-of-order decode
    PTS never produce false gaps.

    Returns (ran: bool, gap_count: int, detail: str):
      ran=False            → ffprobe unavailable / timed out / too few packets
                             (INCONCLUSIVE — must never fail the file)
      ran=True, count=0     → timestamps are contiguous (no skipping)
      ran=True, count>0     → that many gaps >= _PTS_GAP_SEC were found
    """
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return (False, 0, "ffprobe not installed")

    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PTS_PROBE_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        logger.info("[Validator] pts scan timed out on %s — inconclusive", path.name)
        return (False, 0, "pts probe timed out")
    except OSError as exc:
        return (False, 0, f"ffprobe exec error: {exc}")

    times: list[float] = []
    for ln in (result.stdout or "").splitlines():
        ln = ln.strip().rstrip(",")
        if not ln or ln.upper() == "N/A":
            continue
        try:
            times.append(float(ln))
        except ValueError:
            continue

    # Too few packets to judge (e.g. ffprobe gave no PTS) — inconclusive, never
    # fail. A real episode has thousands of video packets.
    if len(times) < 30:
        return (False, 0, "too few packet timestamps to assess")

    times.sort()
    gap_count = 0
    worst = 0.0
    worst_at = 0.0
    for a, b in zip(times, times[1:]):
        delta = b - a
        if delta >= _PTS_GAP_SEC:
            gap_count += 1
            if delta > worst:
                worst, worst_at = delta, a

    detail = f"{worst:.1f}s gap starting ~{worst_at:.0f}s in" if gap_count else ""
    return (True, gap_count, detail)


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

def _count_episodes(filename: str) -> int:
    eps = re.findall(r"E(\d{2})", filename, re.IGNORECASE)
    return max(len(eps), 1)


def _parse_fps(rate: Optional[str]) -> float:
    """Parse an ffprobe frame-rate string like '24000/1001' into a float fps."""
    if not rate or rate in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


class _FFProbeResult:
    """
    Multi-state result for ffprobe runs.

      available : ffprobe binary was found and ran
      timed_out : ffprobe could not finish in time (INCONCLUSIVE — not corruption)
      error     : ffprobe ran but the file is genuinely unreadable/broken
      info      : parsed stream info on success
    """
    __slots__ = ("info", "error", "available", "timed_out")

    def __init__(self, info: Optional[dict], error: Optional[str],
                 available: bool, timed_out: bool = False):
        self.info      = info
        self.error     = error
        self.available = available
        self.timed_out = timed_out


def _ffprobe_streams(path: Path) -> _FFProbeResult:
    """
    Run ffprobe. Possible outcomes:

      1. ffprobe NOT installed:
         _FFProbeResult(info=None, error=None, available=False)
      2. ffprobe TIMED OUT (inconclusive — slow NAS, not corruption):
         _FFProbeResult(info=None, error=None, available=True, timed_out=True)
      3. ffprobe ran but FILE IS BROKEN (invalid header, no streams, etc.):
         _FFProbeResult(info=None, error="<stderr snippet>", available=True)
      4. ffprobe succeeded:
         _FFProbeResult(info={...}, error=None, available=True)

    Timeouts are retried _FFPROBE_TIMEOUT_RETRIES times before being reported
    as inconclusive, because a timeout is NEVER proof that a file is corrupt —
    it usually just means the NAS was busy.
    """
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return _FFProbeResult(None, None, False)

    result = None
    attempts = _FFPROBE_TIMEOUT_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,codec_name,avg_frame_rate,r_frame_rate",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT_SEC,
            )
            break  # ran to completion (success or genuine error)
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                logger.info(
                    "[Validator] ffprobe timed out on %s (attempt %d/%d) — retrying",
                    path.name, attempt, attempts,
                )
                continue
            # Out of retries — INCONCLUSIVE, do NOT mark broken
            logger.warning(
                "[Validator] ffprobe timed out on %s after %d attempt(s) — "
                "treating as inconclusive (file NOT flagged broken)",
                path.name, attempts,
            )
            return _FFProbeResult(None, None, True, timed_out=True)
        except OSError as exc:
            return _FFProbeResult(None, f"ffprobe exec error: {exc}", True)

    if result is None:
        # Shouldn't happen, but be safe — inconclusive, never broken.
        return _FFProbeResult(None, None, True, timed_out=True)

    stderr_msg = (result.stderr or "").strip()

    # Try to parse stdout regardless of returncode — sometimes ffprobe writes
    # partial JSON on damaged files but still returns non-zero.
    try:
        data = json.loads(result.stdout or "{}")
    except (ValueError, json.JSONDecodeError):
        data = {}

    streams = data.get("streams") or []
    format_section = data.get("format") or {}

    if result.returncode != 0:
        # ffprobe failed → the file is unreadable
        snippet = stderr_msg[-200:] if stderr_msg else "ffprobe non-zero exit"
        return _FFProbeResult(None, snippet, True)

    if not streams and not format_section.get("duration"):
        # ffprobe succeeded but found nothing — container is empty
        return _FFProbeResult(None, "ffprobe found no streams in container", True)

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        duration = float(format_section.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    # Frame rate of the primary video stream — prefer avg (actual playback rate)
    # and fall back to the nominal rate. Ignore tiny embedded cover-art/mjpeg
    # streams (those report absurd rates like 90000/1).
    fps = 0.0
    if video_streams:
        vs = video_streams[0]
        # Skip cover-art style streams (mjpeg/png with no real frame rate)
        if vs.get("codec_name") not in ("mjpeg", "png", "bmp"):
            fps = _parse_fps(vs.get("avg_frame_rate")) or _parse_fps(vs.get("r_frame_rate"))
        else:
            # Look for a real video stream beyond the cover art
            for vs2 in video_streams[1:]:
                if vs2.get("codec_name") not in ("mjpeg", "png", "bmp"):
                    fps = _parse_fps(vs2.get("avg_frame_rate")) or _parse_fps(vs2.get("r_frame_rate"))
                    break

    info = {
        "duration":    duration,
        "video_count": len(video_streams),
        "audio_count": len(audio_streams),
        "video_codec": video_streams[0].get("codec_name") if video_streams else None,
        "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
        "fps":         fps,
    }
    return _FFProbeResult(info, None, True)


def validate_video_file(path: Path, deep: bool = False, full: bool = False) -> tuple[bool, str]:
    """
    Returns (True, "ok") or (False, "<reason>").

    Checks (in order):
      1. File exists
      2. Size per episode (catches truncated downloads)
      3. ffprobe — file readable, contains video + audio streams of correct length
      4. (deep only) ffmpeg decode — counts decode/timestamp errors. Catches
         stuttering / dropped-frame / desync corruption the header check can't
         see. Slower, so opt-in.
            full=False → samples windows spread across the file (fast)
            full=True  → decodes the ENTIRE file (exhaustive; used for the
                         installed-only post-download review)
      5. (deep only) PTS timestamp-gap pass — catches "skipping" (missing
         content that freezes/jumps on playback even though every present frame
         decodes cleanly). Reads only the container index, so it's cheap and runs
         in BOTH deep sampled and full scans.
    """
    if not path.exists():
        return False, "file does not exist"

    size_mb   = path.stat().st_size / (1024 * 1024)
    ep_count  = _count_episodes(path.name)
    mb_per_ep = size_mb / ep_count

    if mb_per_ep < _MIN_MB_PER_EP:
        return False, (
            f"too small — {size_mb:.1f} MB / {ep_count} ep(s) = {mb_per_ep:.1f} MB/ep "
            f"(min {_MIN_MB_PER_EP} MB)"
        )

    probe = _ffprobe_streams(path)

    if not probe.available:
        # ffprobe binary not installed — only size check possible
        return True, "ok (size only — ffprobe not installed)"

    if probe.timed_out:
        # INCONCLUSIVE — the NAS was too slow to read this file in time.
        # A timeout is NOT corruption, so we must NOT flag it for removal.
        # The size check already passed, so treat as OK.
        return True, "ok (ffprobe timed out — inconclusive, not flagged)"

    if probe.error or probe.info is None:
        # File is unreadable — container is broken / truncated / not a real video
        return False, f"ffprobe could not decode file — {probe.error or 'unknown error'}"

    info = probe.info
    if info["video_count"] == 0:
        return False, "no video stream found (container has no decodable video)"
    if info["audio_count"] == 0:
        return False, "no audio stream found (container has no decodable audio)"

    expected_min = _MIN_DURATION_SEC * ep_count
    if info["duration"] < expected_min:
        return False, (
            f"duration too short — {info['duration']:.0f}s / {ep_count} ep(s) "
            f"(min {expected_min:.0f}s)"
        )

    # ── Deep checks (opt-in)
    if deep:
        # 1) Frame rate is INTAKE-AS-IS. Whatever the release was encoded at
        #    (24/25/30/50/60fps or variable) is accepted — fps is never a
        #    rejection signal, never auto-deletes, and never triggers a
        #    re-download. (Doing so previously removed good files, including the
        #    user's own originals, and re-downloaded other high-fps copies of the
        #    same episode in a loop.) We only record it for the log.
        fps = info.get("fps", 0.0)
        if fps:
            logger.info(
                "[Validator] %s — native frame rate %.3ffps (accepted as-is).",
                Path(path).name, fps,
            )

        # 2) Decode the file (sampled windows, or full pass) to catch damaged
        #    frames the header check can't see.
        ran, issue_count, detail = _ffmpeg_decode_sample(
            path, duration=info.get("duration"), full=full
        )
        if ran and issue_count > _DECODE_ERROR_THRESHOLD:
            scope = "full file" if full else "sampled segments"
            return False, (
                f"playback corruption — {issue_count} decode/timestamp issue(s) "
                f"across {scope} (causes stutter/desync). e.g. {detail}"
            )

        # 3) Timestamp-gap pass — runs in ANY deep scan (not just full mode).
        #    This catches the "skipping" class the decode pass cannot: missing
        #    content where the surrounding frames decode perfectly but a chunk of
        #    the episode is simply absent, so playback freezes/jumps. It reads
        #    only the container packet index (not the media), so it's cheap enough
        #    to run on every file in a deep library scan — which is exactly the
        #    case that previously let skippy episodes (e.g. Bridgerton S04E02/E07)
        #    pass as "ok". A timeout or unreadable index is INCONCLUSIVE and never
        #    fails the file.
        ran_pts, gap_count, gap_detail = _detect_pts_gaps(
            path, fps=fps, duration=info.get("duration")
        )
        if ran_pts and gap_count > 0:
            return False, (
                f"playback skipping — {gap_count} large timestamp gap(s) in the "
                f"video stream (missing content freezes/jumps on playback). "
                f"e.g. {gap_detail}"
            )

    return True, (
        f"ok (video={info['video_codec']}, audio={info['audio_codec']}, "
        f"{info['duration']:.0f}s)"
    )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state_key_for_file(filename: str) -> Optional[str]:
    """
    Derive the state key ("tv::Show Name::S01E02") from a clean filename
    like "Show Name - S01E02.mkv".
    """
    m = re.match(r"^(.+?)\s*[-–]\s*S(\d{2})E(\d{2})", filename, re.IGNORECASE)
    if not m:
        return None
    show = m.group(1).strip()
    s, e = int(m.group(2)), int(m.group(3))
    return f"tv::{show}::S{s:02d}E{e:02d}"


def _is_our_file(state_key: str, state: dict) -> bool:
    """
    Return True if this episode was queued/downloaded by our system.
    Checks both state["queued"] (actively queued list) and
    state["downloaded_torrents"] (per-episode torrent tracking).
    """
    if state_key in state.get("downloaded_torrents", {}):
        return True
    if state_key in state.get("queued", []):
        return True
    return False


def _get_torrent_name(state_key: str, state: dict) -> Optional[str]:
    """Return the torrent release name we downloaded for this episode, if known."""
    dt = state.get("downloaded_torrents", {})
    if state_key in dt:
        return dt[state_key].get("release")
    # Fall back to history scan
    for entry in state.get("history", []):
        if entry.get("type") != "tv":
            continue
        show = entry.get("show", "")
        s    = entry.get("season", 0)
        e    = entry.get("episode", 0)
        key  = f"tv::{show}::S{s:02d}E{e:02d}"
        if key == state_key:
            return entry.get("release")
    return None


def _extract_release_group(torrent_name: str) -> Optional[str]:
    """
    Extract the release group from a torrent/file name.
    'The.Show.S01E01.1080p.WEB.h264-EDITH'  -> 'EDITH'
    'Show.Name.S01E01.1080p.WEB.h264.EDITH' -> 'EDITH' (rare)
    Returns None if no recognisable group suffix is present.
    """
    if not torrent_name:
        return None
    # Strip .mkv/.mp4/etc.
    stem = re.sub(r"\.(mkv|mp4|avi|mov|wmv|m4v|ts)$", "", torrent_name, flags=re.IGNORECASE)
    # Drop trailing tracker tags like [rartv] / [rarbg] / {tag}
    stem = re.sub(r"[\[\{][^\]\}]*[\]\}]\s*$", "", stem).strip()
    # 1) Standard scene tag: "...-GROUP"
    m = re.search(r"-([A-Za-z0-9]{2,16})$", stem)
    if m:
        return m.group(1).upper()
    # 2) Indexers (e.g. Jackett) often normalise "-GROUP" to a trailing
    #    " GROUP" or ".GROUP". Only trust the trailing token as a group when the
    #    name clearly looks like a scene release (resolution AND source/codec),
    #    so an ordinary episode-title word isn't misread as a group. The token
    #    must start with a letter (so codec/audio fragments like "264"/"0" don't
    #    match).
    looks_scene = (
        re.search(r"\b(480p|576p|720p|1080p|2160p|4k)\b", stem, re.I)
        and re.search(
            r"\b(x ?26[45]|h ?\.?26[45]|hevc|avc|xvid|divx|web[ .-]?dl|webrip|"
            r"bluray|hdtv|amzn|nf|dsnp|hmax|atvp|nick|max)\b",
            stem, re.I,
        )
    )
    if looks_scene:
        m2 = re.search(r"[ .]([A-Za-z][A-Za-z0-9]{1,15})$", stem)
        if m2:
            return m2.group(1).upper()
    return None


def _record_group_failure(torrent_name: str, state: dict) -> Optional[str]:
    """
    Record a validation failure attributed to the release group from torrent_name.
    Returns the group name (uppercased) or None if it couldn't be parsed.
    """
    grp = _extract_release_group(torrent_name)
    if not grp:
        return None
    counter = state.setdefault("group_failure_count", {})
    counter[grp] = counter.get(grp, 0) + 1
    logger.info(
        "[Validator] Release group %r now has %d failure(s) attributed to it",
        grp, counter[grp],
    )
    return grp


def _blacklist_torrent(state_key: str, torrent_name: str, state: dict) -> None:
    """
    Add torrent_name to the blacklist for state_key so it won't be re-downloaded.
    Also bumps the per-release-group failure counter, which the torrent finder
    uses to auto-deprioritize/block consistently bad uploaders.
    """
    bl = state.setdefault("torrent_blacklist", {})
    bl.setdefault(state_key, [])
    if torrent_name not in bl[state_key]:
        bl[state_key].append(torrent_name)
        logger.info("[Validator] Blacklisted release %r for %s", torrent_name, state_key)

    _record_group_failure(torrent_name, state)


def _clear_cooldown(state_key: str, state: dict) -> int:
    """Remove state_key from retry_queue and queued so it gets re-searched."""
    removed = 0
    rq = state.get("retry_queue", {})
    if state_key in rq:
        del rq[state_key]
        removed += 1
    queued = state.get("queued", [])
    if state_key in queued:
        queued.remove(state_key)
        removed += 1
    # Also remove from downloaded_torrents so the new download gets re-tracked
    dt = state.get("downloaded_torrents", {})
    if state_key in dt:
        del dt[state_key]
    return removed


def _path_for_state_key(state_key: str, library_paths: list[str]) -> Optional[Path]:
    """
    Given a state_key like "tv::Show Name::S01E02", locate the actual file
    on disk by scanning the matching show/season folder under each library.
    Returns the first match or None.
    """
    parts = state_key.split("::")
    if len(parts) < 3 or parts[0] != "tv":
        return None
    show_name = parts[1]
    m = re.match(r"S(\d{2})E(\d{2})", parts[2])
    if not m:
        return None
    season = int(m.group(1))
    episode = int(m.group(2))

    pattern = re.compile(rf"[Ss]{season:02d}[Ee]{episode:02d}", re.IGNORECASE)
    norm_show = re.sub(r"[^\w]", "", show_name).lower()

    for lib in library_paths:
        root = Path(lib)
        if not root.exists():
            continue
        try:
            for show_dir in root.iterdir():
                if not show_dir.is_dir():
                    continue
                if re.sub(r"[^\w]", "", show_dir.name).lower() != norm_show:
                    continue
                # Walk show dir for the file (any season folder)
                for f in show_dir.rglob("*"):
                    if (f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
                            and pattern.search(f.name)):
                        return f
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Incremental validation — only files OUR downloader installed
# ---------------------------------------------------------------------------

def validate_downloaded_only(
    library_paths: list[str],
    state: dict,
    delete_corrupt: bool = True,
) -> dict:
    """
    Iterate only over state["downloaded_torrents"] entries (episodes WE queued)
    and validate each file on disk.  If broken: delete, blacklist torrent,
    clear cooldown so the next scan re-searches with a different release.

    This is the lightweight check that runs after every download cycle —
    it does NOT walk the entire NAS, only files this system installed.
    """
    summary: dict = {"checked": 0, "corrupt": [], "cleared": 0}
    downloaded = state.get("downloaded_torrents", {})
    if not downloaded:
        return summary

    for state_key, meta in list(downloaded.items()):
        file_path = _path_for_state_key(state_key, library_paths)
        if not file_path:
            # File not on disk yet (still downloading, or already removed) — skip
            continue
        summary["checked"] += 1
        valid, reason = validate_video_file(file_path)
        if valid:
            continue

        torrent_name = meta.get("release") if isinstance(meta, dict) else None
        entry = {
            "path":         str(file_path),
            "reason":       reason,
            "our_file":     True,
            "deleted":      False,
            "torrent_name": torrent_name or "",
            "state_key":    state_key,
        }
        logger.warning(
            "[Validator] CORRUPT (tracked download): %s — %s  |  release: %s",
            file_path.name, reason, torrent_name or "unknown",
        )

        if delete_corrupt:
            try:
                file_path.unlink()
                entry["deleted"] = True
                logger.info("[Validator] Deleted: %s", file_path)
                if torrent_name:
                    _blacklist_torrent(state_key, torrent_name, state)
                summary["cleared"] += _clear_cooldown(state_key, state)
            except OSError as exc:
                logger.warning("[Validator] Could not delete %s: %s", file_path, exc)

        summary["corrupt"].append(entry)

    return summary


def _show_dir_for_file(file_path: Path, library_paths: list[str]) -> Optional[Path]:
    """
    Given a video file path, return the show directory (the folder directly
    under whichever library root contains it). Falls back to the file's
    grandparent if the library can't be matched.
    """
    fp = file_path.resolve()
    for lib in library_paths:
        root = Path(lib).resolve()
        try:
            rel = fp.relative_to(root)
        except (ValueError, OSError):
            continue
        if rel.parts:
            return root / rel.parts[0]
    # Fallback: assume <show>/<season>/<file>
    return file_path.parent.parent if file_path.parent.parent != file_path else file_path.parent


def validate_downloaded_grouped(
    library_paths: list[str],
    state: dict,
    progress_cb=None,
    deep: bool = True,
    full: bool = True,
    only_keys: Optional[set] = None,
) -> dict:
    """
    DRY-RUN validation of ONLY the episodes our downloader installed
    (state["downloaded_torrents"]).  Returns the SAME grouped-by-show/season
    structure as find_broken_in_library so the frontend can reuse the exact
    same confirmation modal — but it is far faster because it only checks the
    handful of files we just installed, not the whole NAS.

    deep + full default to True here: since this only checks the few files we
    just installed, we can afford a FULL end-to-end decode of each one, which
    catches stuttering / desync corruption no matter where in the file it
    occurs (a sampled scan can miss damage that falls between windows — exactly
    what happened with KotH S14E05/E10). This is the flawless pre-flight check
    we run before declaring a download successful.

    Nothing is deleted or modified here.
    """
    summary: dict = {"checked": 0, "broken": 0, "shows": []}
    downloaded = state.get("downloaded_torrents", {})
    if not downloaded:
        return summary

    def _emit(**kw):
        if progress_cb is None:
            return
        try:
            progress_cb(kw)
        except Exception:
            pass

    items = list(downloaded.items())
    # When only_keys is given, restrict validation to just those episodes
    # (e.g. the ones a single "Download Missing" run just installed) so the
    # check stays fast and targeted instead of re-validating everything.
    if only_keys is not None:
        items = [(k, v) for (k, v) in items if k in only_keys]
    total = len(items)
    _emit(phase="scan", total=total, checked=0, broken=0,
          current_show="", current_file="", lib_total=len(library_paths))

    # show_path -> {show, library, show_path, seasons:{season_path:{...}}}
    grouped: dict = {}

    for idx, (state_key, meta) in enumerate(items, start=1):
        file_path = _path_for_state_key(state_key, library_paths)
        if not file_path:
            # Not on disk yet (still downloading) or already removed — skip
            _emit(phase="scan", total=total, checked=summary["checked"],
                  broken=summary["broken"], current_show="",
                  current_file=state_key, lib_total=len(library_paths))
            continue

        summary["checked"] += 1
        show_dir    = _show_dir_for_file(file_path, library_paths) or file_path.parent.parent
        season_path = file_path.parent

        _emit(phase="scan", total=total, checked=summary["checked"] - 1,
              broken=summary["broken"], current_show=show_dir.name,
              current_file=file_path.name, lib_total=len(library_paths))

        valid, reason = validate_video_file(file_path, deep=deep, full=full)
        if valid:
            continue

        summary["broken"] += 1
        torrent_name = meta.get("release") if isinstance(meta, dict) else None

        grp_show = grouped.setdefault(str(show_dir), {
            "show":      show_dir.name,
            "library":   "",
            "show_path": str(show_dir),
            "seasons":   {},
        })
        grp_season = grp_show["seasons"].setdefault(str(season_path), {
            "season_path":  str(season_path),
            "season_name":  season_path.name,
            "total_files":  0,
            "broken_files": [],
        })
        grp_season["broken_files"].append({
            "file":         file_path.name,
            "path":         str(file_path),
            "reason":       reason,
            "our_file":     True,
            "state_key":    state_key,
            "torrent_name": torrent_name or "",
        })

    # Count total video files per affected season folder (for whole_season_bad)
    for show_data in grouped.values():
        seasons_out: list = []
        from scanner import _parse_season_folder
        for season_data in show_data["seasons"].values():
            if not season_data["broken_files"]:
                continue
            season_dir = Path(season_data["season_path"])
            try:
                total_videos = sum(
                    1 for f in season_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
                )
            except OSError:
                total_videos = len(season_data["broken_files"])
            season_data["total_files"] = total_videos
            season_num = _parse_season_folder(season_data["season_name"])
            season_data["season"] = season_num if season_num is not None else 0
            season_data["whole_season_bad"] = (
                len(season_data["broken_files"]) == total_videos and total_videos > 0
            )
            seasons_out.append(season_data)
        if seasons_out:
            seasons_out.sort(key=lambda s: s["season"])
            show_data["seasons"] = seasons_out
            summary["shows"].append(show_data)

    summary["shows"].sort(key=lambda s: s["show"].lower())
    _emit(phase="done", total=total, checked=summary["checked"],
          broken=summary["broken"], current_show="", current_file="",
          lib_total=len(library_paths))
    return summary


# ---------------------------------------------------------------------------
# Full-library DRY-RUN scan — reports broken files for user confirmation
# ---------------------------------------------------------------------------

def _server_root(path_str: str) -> str:
    """Best-effort 'which physical server' key for a path so the scanner can
    spread concurrent reads across different NAS boxes. UNC ``\\\\host\\share``
    collapses to ``\\\\host``; a local path collapses to its drive letter."""
    s = str(path_str).replace("/", "\\")
    if s.startswith("\\\\"):
        parts = [p for p in s.split("\\") if p]
        if parts:
            return "\\\\" + parts[0].lower()
    drive = os.path.splitdrive(s)[0]
    return (drive or s).lower()


def _interleave_by_server(items: list) -> tuple:
    """Round-robin a list of ``(video, show, lib_path, size, mtime)`` tuples
    across their server roots so concurrent workers hit many servers at once
    instead of hammering one while the rest idle.

    Returns ``(reordered_items, distinct_server_count)``. This is an ORDER-ONLY
    transform — every file is still present exactly once and is validated by the
    identical check, so scan results are unchanged; only the dispatch order (and
    therefore how well the NAS bandwidth is used) differs.
    """
    buckets: dict = {}
    order: list = []
    for it in items:
        key = _server_root(it[2])  # lib_path
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(it)
    if len(order) <= 1:
        return items, len(order)
    result: list = []
    i = 0
    while True:
        added = False
        for key in order:
            b = buckets[key]
            if i < len(b):
                result.append(b[i])
                added = True
        if not added:
            break
        i += 1
    return result, len(order)


def find_broken_in_library(
    library_paths: list[str],
    state: dict,
    progress_cb=None,
    deep: bool = False,
    workers: int = 0,
    use_cache: bool = True,
) -> dict:
    """
    Walk every library, validate every video file, and return a report
    grouped by show + season. Does NOT delete or modify anything.

    deep=True (the UI "Deep decode" default) also catches stuttering / desync
    corruption and playback skipping that the header check misses. It's heavier
    per file, but files are validated CONCURRENTLY (see `workers`) so a
    full-library deep scan that used to take days now runs in hours. WHAT gets
    detected is identical to the old serial scan — only the waiting overlaps.

    workers controls how many files validate at once. 0 = auto (fewer for the
    heavy deep scan, more for the lightweight header scan). Lower it if the NAS
    or CPU is saturated.

    progress_cb, if supplied, is called with a dict containing keys:
      phase            : "enumerate" | "scan" | "finalize" | "done"
      total            : total files discovered so far (or final total)
      checked          : files validated so far
      broken           : broken files found so far
      current_lib      : library being scanned (during "enumerate")
      current_show     : show being scanned (during "scan")
      current_file     : file just inspected (during "scan")
      current_lib_idx  : library number (1-based) currently being scanned
      lib_total        : total libraries to scan
    """
    def _emit(**kw):
        if progress_cb is None:
            return
        try:
            progress_cb(kw)
        except Exception:
            pass

    summary: dict = {"checked": 0, "broken": 0, "shows": []}

    # show_path -> {"library": .., "seasons": {season_path: {"files":[], "broken":[]}}}
    grouped: dict = {}

    downloaded = state.get("downloaded_torrents", {})
    queued     = set(state.get("queued", []))

    # ── Pass 1: enumerate every video file so we have an accurate total
    # for the progress bar before we start the (slow) ffprobe step. We capture
    # each file's size + mtime here (one stat) so Pass 2 can consult the
    # incremental cache without a second stat round-trip to the NAS.
    # (video, show_dir, lib_path, size, mtime)
    files_to_check: list[tuple[Path, Path, str, int, int]] = []
    for lib_idx, lib_path in enumerate(library_paths, start=1):
        root = Path(lib_path)
        _emit(
            phase="enumerate",
            total=len(files_to_check),
            checked=0, broken=0,
            current_lib=lib_path,
            current_lib_idx=lib_idx,
            lib_total=len(library_paths),
        )
        if not root.exists():
            continue
        try:
            show_dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for show_dir in show_dirs:
            try:
                for video_file in show_dir.rglob("*"):
                    if not video_file.is_file():
                        continue
                    if video_file.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    if video_file.name.endswith((".part", ".!qb")):
                        continue
                    try:
                        st = video_file.stat()
                        size, mtime = int(st.st_size), int(st.st_mtime)
                    except OSError:
                        size, mtime = 0, 0
                    files_to_check.append(
                        (video_file, show_dir, lib_path, size, mtime)
                    )
            except OSError:
                continue

    total = len(files_to_check)
    _emit(phase="scan", total=total, checked=0, broken=0,
          current_lib="", current_show="", current_file="",
          lib_total=len(library_paths))

    # ── Pass 2: validate each file. The validation itself (ffprobe + ffmpeg
    # decode + packet-index read) is the slow part, and it is spent almost
    # entirely waiting on NAS reads and external subprocesses (which run outside
    # the GIL) — so we validate several files CONCURRENTLY. WHAT gets detected is
    # unchanged: every file still goes through the exact same validate_video_file
    # call. Only the *waiting* is overlapped. All grouping / counting below runs
    # on THIS single thread as each result returns, so the final report is
    # identical regardless of the order workers happen to finish in.
    # Auto worker-sizing is deferred until after the cache pass + server
    # interleave below, since it scales with how many distinct servers the
    # remaining (uncached) work actually spans. A caller-supplied workers>0 is
    # honoured as-is.
    requested_workers = workers

    def _bookkeep(video_file: Path, show_dir: Path, lib_path: str,
                  valid: bool, reason: str) -> None:
        """Record one file's result into the grouped report (main thread only)."""
        season_path = video_file.parent
        show_key    = str(show_dir)
        grp_show = grouped.setdefault(show_key, {
            "show":      show_dir.name,
            "library":   lib_path,
            "show_path": str(show_dir),
            "seasons":   {},
        })
        grp_season = grp_show["seasons"].setdefault(str(season_path), {
            "season_path":  str(season_path),
            "season_name":  season_path.name,
            "total_files":  0,
            "broken_files": [],
        })
        grp_season["total_files"] += 1
        if valid:
            return
        summary["broken"] += 1
        state_key = _state_key_for_file(video_file.name)
        our_file  = bool(
            state_key and (state_key in downloaded or state_key in queued)
        )
        grp_season["broken_files"].append({
            "file":      video_file.name,
            "path":      str(video_file),
            "reason":    reason,
            "our_file":  our_file,
            "state_key": state_key or "",
        })

    # Incremental cache: a file whose size+mtime are unchanged since it was last
    # validated at THIS depth is reused instantly (no NAS read). Depth must match
    # exactly so a deep scan never trusts a shallow result (and vice-versa),
    # which keeps results identical to a fresh scan at the requested depth.
    cache: dict = _load_validation_cache() if use_cache else {}
    pending_cache_writes = {"n": 0, "last_flush": time.monotonic()}

    def _cache_lookup(path_str: str, size: int, mtime: int):
        e = cache.get(path_str)
        if not e:
            return None
        if (e.get("size") != size or e.get("mtime") != mtime
                or bool(e.get("deep")) != deep):
            return None
        return bool(e.get("ok", True)), e.get("reason", "ok")

    def _cache_store(path_str: str, size: int, mtime: int,
                     valid: bool, reason: str) -> None:
        if not use_cache or not _is_conclusive(valid, reason):
            return
        cache[path_str] = {
            "size": size, "mtime": mtime, "deep": deep,
            "ok": valid, "reason": reason,
        }
        pending_cache_writes["n"] += 1
        now = time.monotonic()
        if (pending_cache_writes["n"] >= _CACHE_FLUSH_EVERY
                or now - pending_cache_writes["last_flush"] >= _CACHE_FLUSH_SECS):
            _save_validation_cache(cache)
            pending_cache_writes["n"] = 0
            pending_cache_writes["last_flush"] = now

    def _record(video_file, show_dir, lib_path, valid, reason):
        summary["checked"] += 1
        _bookkeep(video_file, show_dir, lib_path, valid, reason)
        _emit(
            phase="scan",
            total=total,
            checked=summary["checked"],
            broken=summary["broken"],
            current_lib=lib_path,
            current_show=show_dir.name,
            current_file=video_file.name,
            lib_total=len(library_paths),
        )

    def _validate_one(item):
        """Run in a worker thread — pure validation, no shared-state writes."""
        video_file, show_dir, lib_path, size, mtime = item
        try:
            valid, reason = validate_video_file(video_file, deep=deep)
        except Exception as exc:  # one bad file must never abort the whole scan
            # Treat an unexpected validator error as INCONCLUSIVE (not broken),
            # matching the rest of the validator's "never flag on uncertainty"
            # policy, but log it so it isn't silently lost.
            logger.warning("[Validator] scan error on %s — skipping: %s",
                           video_file, exc)
            valid, reason = True, "ok (validator error — inconclusive, skipped)"
        return item, valid, reason

    # First, settle cache hits instantly (no worker needed); queue the rest.
    to_validate: list = []
    for item in files_to_check:
        video_file, show_dir, lib_path, size, mtime = item
        hit = _cache_lookup(str(video_file), size, mtime)
        if hit is not None:
            valid, reason = hit
            _record(video_file, show_dir, lib_path, valid, reason)
        else:
            to_validate.append(item)

    cached_hits = summary["checked"]
    if use_cache and cached_hits:
        logger.info("[Validator] Incremental cache: %d/%d file(s) unchanged — "
                    "re-checking %d.", cached_hits, total, len(to_validate))

    # Spread concurrent reads across DIFFERENT servers (order-only change).
    to_validate, distinct_servers = _interleave_by_server(to_validate)

    # Size the worker pool now that we know how many servers the work spans.
    if requested_workers and requested_workers > 0:
        workers = requested_workers
    else:
        env = os.environ.get("STVD_SCAN_WORKERS", "")
        if env.isdigit() and int(env) > 0:
            workers = int(env)
        elif deep:
            workers = max(_SCAN_WORKERS_DEEP,
                          min(_SCAN_WORKERS_MAX,
                              max(1, distinct_servers) * _SCAN_WORKERS_PER_SERVER))
        else:
            workers = _SCAN_WORKERS_SHALLOW
    workers = max(1, min(workers, len(to_validate) or 1))
    if to_validate:
        logger.info(
            "[Validator] %s scan: %d file(s) across %d server(s) using %d workers.",
            "Deep" if deep else "Header", len(to_validate), distinct_servers, workers,
        )

    # Validate the remaining (new/changed) files concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_validate_one, item) for item in to_validate]
        for fut in concurrent.futures.as_completed(futures):
            item, valid, reason = fut.result()
            video_file, show_dir, lib_path, size, mtime = item
            _cache_store(str(video_file), size, mtime, valid, reason)
            _record(video_file, show_dir, lib_path, valid, reason)

    if use_cache:
        _save_validation_cache(cache)

    # Flatten to list, attach season number / whole-season flag
    for show_data in grouped.values():
        seasons_out: list = []
        from scanner import _parse_season_folder
        for season_data in show_data["seasons"].values():
            if not season_data["broken_files"]:
                continue
            # Stable, worker-order-independent ordering of the broken list.
            season_data["broken_files"].sort(key=lambda b: b["file"].lower())
            season_num = _parse_season_folder(season_data["season_name"])
            season_data["season"] = season_num if season_num is not None else 0
            season_data["whole_season_bad"] = (
                len(season_data["broken_files"]) == season_data["total_files"]
                and season_data["total_files"] > 0
            )
            seasons_out.append(season_data)

        if seasons_out:
            seasons_out.sort(key=lambda s: s["season"])
            show_data["seasons"] = seasons_out
            summary["shows"].append(show_data)

    summary["shows"].sort(key=lambda s: s["show"].lower())
    _emit(phase="done", total=total, checked=summary["checked"],
          broken=summary["broken"], current_lib="", current_show="", current_file="",
          lib_total=len(library_paths))
    return summary


# ---------------------------------------------------------------------------
# Confirmed removal of broken files — smart season-folder cleanup
# ---------------------------------------------------------------------------

def remove_broken_files(
    confirmed_paths: list[str],
    state: dict,
    library_paths: list[str],
) -> dict:
    """
    Remove a user-confirmed list of broken video files.

    Smart cleanup rules:
      - If ALL video files in a season folder are in the confirmation list,
        remove the whole season folder (including any NFOs, subs, etc).
      - Otherwise remove only the individual files.
      - For every removed episode, blacklist its torrent (if known) and clear
        the cooldown so the next scan re-searches a different release.

    Returns: {removed_files, removed_folders, blacklisted, requeued, errors}
    """
    result = {
        "removed_files":   0,
        "removed_folders": 0,
        "blacklisted":     0,
        "requeued":        0,
        "errors":          [],
    }

    if not confirmed_paths:
        return result

    confirmed = {str(Path(p).resolve()): Path(p) for p in confirmed_paths}

    # Group by season folder
    by_season: dict[Path, list[Path]] = {}
    for p in confirmed.values():
        if not p.exists():
            continue
        by_season.setdefault(p.parent, []).append(p)

    for season_dir, broken_paths in by_season.items():
        # Count current video files in folder (live re-check, in case the
        # frontend report is stale)
        try:
            all_videos = [
                f for f in season_dir.iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
            ]
        except OSError as exc:
            result["errors"].append(f"{season_dir}: {exc}")
            continue

        broken_set = {str(p.resolve()) for p in broken_paths}
        live_set   = {str(f.resolve()) for f in all_videos}
        whole_folder = broken_set >= live_set and len(live_set) > 0

        # Bookkeeping per file BEFORE deletion (so we can record blacklist)
        for p in broken_paths:
            state_key    = _state_key_for_file(p.name)
            torrent_name = _get_torrent_name(state_key, state) if state_key else None
            if torrent_name and state_key:
                _blacklist_torrent(state_key, torrent_name, state)
                result["blacklisted"] += 1
            if state_key:
                cleared = _clear_cooldown(state_key, state)
                if cleared:
                    result["requeued"] += 1

        if whole_folder:
            try:
                # Remove the entire season folder (videos, subs, nfos, everything)
                shutil.rmtree(str(season_dir))
                result["removed_folders"] += 1
                result["removed_files"]   += len(broken_paths)
                logger.info("[Validator] Removed whole season folder: %s", season_dir)
            except OSError as exc:
                result["errors"].append(f"{season_dir}: {exc}")
        else:
            for p in broken_paths:
                try:
                    p.unlink()
                    result["removed_files"] += 1
                    logger.info("[Validator] Removed file: %s", p)
                except OSError as exc:
                    result["errors"].append(f"{p}: {exc}")

    return result


# ---------------------------------------------------------------------------
# Quarantine helpers — "replace, then delete" so we NEVER lose an episode
# ---------------------------------------------------------------------------
# Instead of deleting a broken file the moment it's detected, we rename it to a
# ".corrupt" sidecar. That (a) frees the canonical name so a replacement can
# download into place, and (b) guarantees no data loss: if no working
# replacement is ever found, the original is renamed straight back. The
# ".corrupt" extension is deliberately NOT a video extension, so library
# scanners / the renamer ignore it and it isn't counted as a present episode.

_QUARANTINE_SUFFIX = ".corrupt"


def quarantine_broken_file(path) -> Optional[str]:
    """
    Rename a broken video file to a non-video ".corrupt" sidecar so its
    canonical name is freed (a replacement can land there) while the original
    bytes are preserved. Returns the quarantine path as a str, or None on
    failure (caller should then fall back to leaving the file as-is).
    """
    path = Path(path)
    if not path.exists():
        return None
    target = path.with_name(path.name + _QUARANTINE_SUFFIX)
    i = 1
    while target.exists():
        target = path.with_name(f"{path.name}{_QUARANTINE_SUFFIX}{i}")
        i += 1
    try:
        path.rename(target)
        logger.info("[Validator] Quarantined broken file: %s → %s", path.name, target.name)
        return str(target)
    except OSError as exc:
        logger.warning("[Validator] Could not quarantine %s: %s", path.name, exc)
        return None


def restore_quarantined_file(quarantine_path) -> bool:
    """
    Rename a ".corrupt" sidecar back to its original episode name. Used when no
    working replacement could be found, so the user keeps the (broken) episode
    rather than nothing. Refuses to clobber a file that already exists at the
    canonical name (i.e. a good replacement already landed). Returns True if the
    original was restored.
    """
    q = Path(quarantine_path)
    if not q.exists():
        return False
    orig_name = re.sub(re.escape(_QUARANTINE_SUFFIX) + r"\d*$", "", q.name)
    if orig_name == q.name:
        return False  # not a recognised quarantine name
    orig = q.with_name(orig_name)
    if orig.exists():
        # A good replacement already occupies the canonical name — the broken
        # original is now redundant; drop the sidecar.
        try:
            q.unlink()
        except OSError:
            pass
        return False
    try:
        q.rename(orig)
        logger.info("[Validator] Restored quarantined file (no replacement found): %s", orig.name)
        return True
    except OSError as exc:
        logger.warning("[Validator] Could not restore %s: %s", q.name, exc)
        return False


def purge_quarantined_file(quarantine_path) -> bool:
    """Delete a ".corrupt" sidecar — called once a replacement has downloaded
    and PASSED validation, so the broken original is no longer needed."""
    q = Path(quarantine_path)
    try:
        if q.exists():
            q.unlink()
            logger.info("[Validator] Removed quarantined original (replacement verified): %s", q.name)
        return True
    except OSError as exc:
        logger.warning("[Validator] Could not remove quarantine %s: %s", q.name, exc)
        return False


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def validate_library_paths(
    library_paths: list[str],
    state: dict,
    delete_corrupt: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Walk all library paths, validate every video file.

    SAFETY RULES:
    - Only deletes files that this system downloaded (tracked in state).
    - Untracked files are flagged in the report but never deleted.
    - Corrupt tracked files are deleted, their torrent blacklisted, and
      cooldowns cleared so the next scan re-downloads a different version.

    Returns:
      {
        "checked":   int,
        "corrupt":   [ {path, reason, our_file, deleted, torrent_name} ],
        "cleared":   int,
      }
    """
    summary: dict = {"checked": 0, "corrupt": [], "cleared": 0}

    for lib_path in library_paths:
        root = Path(lib_path)
        if not root.exists():
            continue
        for video_file in root.rglob("*"):
            if not video_file.is_file():
                continue
            if video_file.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if video_file.name.endswith((".part", ".!qb")):
                continue

            summary["checked"] += 1
            valid, reason = validate_video_file(video_file)
            if valid:
                continue

            state_key    = _state_key_for_file(video_file.name)
            our_file     = bool(state_key and _is_our_file(state_key, state))
            torrent_name = _get_torrent_name(state_key, state) if state_key else None

            entry = {
                "path":         str(video_file),
                "reason":       reason,
                "our_file":     our_file,
                "deleted":      False,
                "torrent_name": torrent_name or "",
            }

            if not our_file:
                logger.warning(
                    "[Validator] CORRUPT (not our download — skipping): %s — %s",
                    video_file.name, reason,
                )
                summary["corrupt"].append(entry)
                continue

            logger.warning(
                "[Validator] CORRUPT (our download): %s — %s  |  release: %s",
                video_file.name, reason, torrent_name or "unknown",
            )

            if not dry_run and delete_corrupt:
                try:
                    video_file.unlink()
                    entry["deleted"] = True
                    logger.info("[Validator] Deleted: %s", video_file)

                    # Blacklist the corrupt torrent so a different version is chosen
                    if torrent_name and state_key:
                        _blacklist_torrent(state_key, torrent_name, state)

                    # Clear cooldown so it gets re-searched next scan
                    if state_key:
                        cleared = _clear_cooldown(state_key, state)
                        summary["cleared"] += cleared

                except OSError as exc:
                    logger.warning("[Validator] Could not delete %s: %s", video_file, exc)

            summary["corrupt"].append(entry)

    return summary
