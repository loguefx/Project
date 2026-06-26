"""Cancel all Legend of Korra torrents from qBittorrent (files NOT deleted from NAS)."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

cfg = json.load(open("config.json"))
qbit_cfg = cfg.get("qbittorrent", {})
from qbit_client import QBitClient
qbit = QBitClient(
    url=qbit_cfg.get("url", "http://127.0.0.1:8083"),
    username=qbit_cfg.get("username", "admin"),
    password=qbit_cfg.get("password", "adminadmin"),
)

torrents = qbit.get_torrents()
korra = [t for t in torrents if "korra" in t.get("name","").lower()]
if not korra:
    print("No Legend of Korra torrents found in qBittorrent.")
else:
    for t in korra:
        print(f"Removing: {t['name']}  [{t.get('state','')}]")
        qbit.delete_torrent(t["hash"], delete_files=False)
    print(f"\nDone — removed {len(korra)} Korra torrent(s). NAS files untouched.")
