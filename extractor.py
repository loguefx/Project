"""
Auto-extraction of RAR archives that scene release groups pack video files into.
Uses 7-Zip (preferred) or WinRAR if available.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Common 7-Zip install paths on Windows
_SEVENZIP_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]
# Common WinRAR paths
_WINRAR_PATHS = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    r"C:\Program Files\WinRAR\WinRAR.exe",
]

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts"}


def find_extractor() -> Optional[tuple[str, str]]:
    """Return (tool_type, path) for the first available extraction tool."""
    for p in _SEVENZIP_PATHS:
        if os.path.exists(p):
            return ("7zip", p)
    for p in _WINRAR_PATHS:
        if os.path.exists(p):
            return ("unrar", p)
    # Check PATH
    for cmd in ["7z", "7za", "unrar"]:
        try:
            subprocess.run([cmd, "--help"], capture_output=True, timeout=3)
            return ("7zip" if "7z" in cmd else "unrar", cmd)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return None


def extract_rar(rar_path: Path, dest_dir: Path, tool: tuple[str, str]) -> bool:
    """Extract a RAR archive to dest_dir. Returns True on success."""
    tool_type, tool_exe = tool
    try:
        if tool_type == "7zip":
            cmd = [tool_exe, "e", str(rar_path), f"-o{dest_dir}", "-y", "-aoa"]
        else:
            cmd = [tool_exe, "e", "-y", str(rar_path), str(dest_dir)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return True
        logger.error("[Extractor] Extraction failed for %s: %s", rar_path.name, result.stderr[:200])
        return False
    except subprocess.TimeoutExpired:
        logger.error("[Extractor] Extraction timed out for %s", rar_path.name)
        return False
    except Exception as exc:
        logger.error("[Extractor] Error extracting %s: %s", rar_path.name, exc)
        return False


def has_video_file(folder: Path) -> bool:
    """Return True if the folder already contains a video file."""
    return any(f.suffix.lower() in VIDEO_EXTENSIONS for f in folder.rglob("*") if f.is_file())


def process_folder(folder: Path, tool: tuple[str, str], delete_rar: bool = True) -> int:
    """
    Look for .rar archives in folder (not already extracted).
    Extract them, then optionally delete the RAR parts.
    Returns the number of successfully extracted archives.
    """
    # Only the main .rar (not .r00, .r01 etc.) triggers extraction
    main_rars = [f for f in folder.iterdir() if f.suffix.lower() == ".rar"]
    if not main_rars:
        return 0

    if has_video_file(folder):
        logger.debug("[Extractor] %s already has video file — skipping extraction", folder.name)
        return 0

    extracted = 0
    for rar in main_rars:
        logger.info("[Extractor] Extracting %s → %s", rar.name, folder)
        if extract_rar(rar, folder, tool):
            extracted += 1
            if delete_rar:
                _delete_rar_parts(folder)
                logger.info("[Extractor] Deleted RAR parts from %s", folder.name)
        else:
            logger.warning("[Extractor] Failed to extract %s", rar.name)

    return extracted


def _delete_rar_parts(folder: Path) -> None:
    """Deletion disabled — NAS files are managed manually by the user."""
    logger.debug("[Extractor] RAR cleanup skipped (deletion disabled): %s", folder.name)


def scan_and_extract(library_paths: list[str], delete_rar: bool = True) -> dict:
    """
    Walk all library paths, find completed RAR downloads missing their video,
    and extract them.  Returns summary dict.
    """
    tool = find_extractor()
    if not tool:
        logger.error(
            "[Extractor] No extraction tool found. "
            "Please install 7-Zip from https://7-zip.org"
        )
        return {"ok": False, "reason": "7-Zip not installed", "extracted": 0, "failed": 0}

    logger.info("[Extractor] Using %s at %s", tool[0], tool[1])
    total_extracted = 0
    total_failed = 0

    for lib_path_str in library_paths:
        lib_path = Path(lib_path_str)
        if not lib_path.exists():
            logger.warning("[Extractor] Library path not found: %s", lib_path_str)
            continue

        # Walk up to 4 levels deep (lib / show / season / release_folder)
        for root, dirs, files in os.walk(lib_path):
            depth = len(Path(root).relative_to(lib_path).parts)
            if depth > 4:
                dirs.clear()
                continue
            folder = Path(root)
            rar_files = [f for f in files if f.lower().endswith(".rar")]
            if rar_files:
                n = process_folder(folder, tool, delete_rar=delete_rar)
                if n > 0:
                    total_extracted += n
                    logger.info("[Extractor] Extracted %d archive(s) in %s", n, folder)
                elif not has_video_file(folder):
                    total_failed += 1

    logger.info(
        "[Extractor] Done — %d extracted, %d failed/skipped",
        total_extracted, total_failed,
    )
    return {"ok": True, "extracted": total_extracted, "failed": total_failed}
