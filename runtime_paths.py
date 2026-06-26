"""Runtime path + process-spawning helpers that work both from source and when
frozen into a single Windows-service .exe by PyInstaller.

Two distinct roots:

* ``DATA_DIR``     — where *writable* runtime files live (config.json, state.json,
                     logs, caches). When frozen this is a stable, update-proof
                     location (``%ProgramData%\\ShowTVDownloader``) so that
                     swapping the app folder during an update never wipes the
                     user's configuration or downloaded-state bookkeeping. In a
                     normal source checkout it's just the repo directory, so dev
                     behaviour is unchanged.
* ``RESOURCE_DIR`` — where *read-only* bundled assets live (the Jinja templates).
                     When frozen, PyInstaller unpacks these under ``sys._MEIPASS``.

``child_argv(*flags)`` returns the correct argv to launch a downloader
subcommand as its own process: ``[python, downloader.py, *flags]`` from source,
or ``[exe, *flags]`` when frozen (the exe re-dispatches to ``downloader.main``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_FROZEN: bool = bool(getattr(sys, "frozen", False))

APP_NAME = "ShowTVDownloader"


def _resolve_data_dir() -> Path:
    # Explicit override always wins (useful for tests / custom installs).
    override = os.environ.get("STVD_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if IS_FROZEN:
        base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / APP_NAME

    # Source checkout: keep everything next to the code, as before.
    return Path(__file__).resolve().parent


def _resolve_resource_dir() -> Path:
    if IS_FROZEN:
        # PyInstaller sets _MEIPASS to the unpacked bundle root.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


DATA_DIR: Path = _resolve_data_dir()
RESOURCE_DIR: Path = _resolve_resource_dir()

# The directory the running .exe lives in (the swappable "app" folder). Used by
# the updater to know what to replace. Meaningless from source.
EXE_DIR: Path = Path(sys.executable).resolve().parent if IS_FROZEN else DATA_DIR

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def data_path(name: str) -> Path:
    """Path to a writable runtime file under DATA_DIR."""
    return DATA_DIR / name


def resource_path(name: str) -> Path:
    """Path to a bundled read-only resource under RESOURCE_DIR."""
    return RESOURCE_DIR / name


def child_argv(*flags: str) -> list[str]:
    """argv to spawn a downloader subcommand as its own process."""
    if IS_FROZEN:
        return [sys.executable, *flags]
    return [sys.executable, str(Path(__file__).resolve().parent / "downloader.py"), *flags]
