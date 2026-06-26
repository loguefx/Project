"""Read-only: report free space per library share and any qBittorrent torrents
stuck in an error / missing-files / stalled state (e.g. disk full)."""
import os, sys, shutil
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from downloader import load_config
from qbit_client import QBitClient

cfg = load_config()

print("== Free space per configured library path ==")
seen = set()
for lib in cfg.get("libraries", []):
    p = lib.get("path", "")
    if not p or p in seen:
        continue
    seen.add(p)
    try:
        u = shutil.disk_usage(p)
        gb = 1024 ** 3
        print(f"  {u.free/gb:8.1f} GB free / {u.total/gb:8.1f} GB  {p}")
    except Exception as exc:
        print(f"  (unreadable: {exc})  {p}")

print("\n== qBittorrent torrents in error/stalled state ==")
qc = cfg["qbittorrent"]
q = QBitClient(qc.get("url", ""), qc.get("username", ""), qc.get("password", ""),
               qc.get("bypass_auth", False))
ts = q.get_torrents()
bad = [t for t in ts if t.get("state", "") in
       ("error", "missingFiles", "stalledDL", "pausedDL")]
if not bad:
    print("  (none)")
for t in bad:
    print(f"  {t.get('state',''):<12} {t.get('progress',0)*100:5.1f}%  {t.get('name','')}")
    print(f"               save_path={t.get('save_path','')}")
