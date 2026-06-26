# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for ShowTVDownloader.

Produces a one-folder build: dist/ShowTVDownloader/ShowTVDownloader.exe (+ _internal/).
Run via build.ps1, or directly:  pyinstaller --noconfirm ShowTVDownloader.spec
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# All first-party modules (some are imported dynamically inside functions, so
# we list them explicitly to be safe) plus the sources.* package.
local_modules = [
    "web", "downloader", "scanner", "tmdb_client", "torrent_finder",
    "tvmaze_client", "notifier", "qbit_client", "validator", "discovery",
    "duplicates", "renamer", "extractor", "campaign_review", "verify_replaced",
    "swarm_health", "runtime_paths", "version",
]

hidden = (
    local_modules
    + collect_submodules("sources")
    # pywin32 service plumbing
    + ["win32timezone", "win32serviceutil", "win32service", "win32event",
       "servicemanager", "win32api", "win32con", "pywintypes", "pythoncom"]
    # third-party libs that use lazy/dynamic imports
    + collect_submodules("feedparser")
    + ["bs4", "lxml", "lxml.etree", "rarfile", "charset_normalizer", "certifi",
       "idna", "schedule"]
)

a = Analysis(
    ["service.py"],
    pathex=[],
    binaries=[],
    datas=[("templates", "templates")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShowTVDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # so install/start/stop/remove print output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ShowTVDownloader",
)
