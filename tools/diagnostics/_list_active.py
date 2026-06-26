"""Read-only: list active qBittorrent downloads and whether each maps to a
persisted pack_whitelist entry (so we know a fresh watcher can import it)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from downloader import load_config, load_state
from qbit_client import QBitClient

cfg = load_config()
state = load_state()
wl = state.get("pack_whitelist", {})
qc = cfg["qbittorrent"]
q = QBitClient(qc.get("url", ""), qc.get("username", ""), qc.get("password", ""),
               qc.get("bypass_auth", False))

for label in ("downloading", "stalledDL", "all"):
    ts = q.get_torrents(label)
    if label != "all":
        print(f"\n== {label}: {len(ts)} ==")
        for t in ts:
            name = t.get("name", "")
            in_wl = "PACK->whitelist OK" if name in wl else "(single/other)"
            print(f"  {t.get('progress',0)*100:5.1f}%  {t.get('state','?'):<12} {in_wl}  {name}")
