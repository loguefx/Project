import sys, json
from pathlib import Path
sys.path.insert(0, '.')
from qbit_client import QBitClient

cfg = json.loads(Path('config.json').read_text())
qb_cfg = cfg.get('qbittorrent', {})
qb = QBitClient(
    url=qb_cfg.get('url', 'http://localhost:8083'),
    username=qb_cfg.get('username', ''),
    password=qb_cfg.get('password', ''),
)

torrents = qb.get_torrents()
korra = [t for t in torrents if 'korra' in t.get('name', '').lower()]
print(f"Found {len(korra)} Korra torrent(s):")
for t in korra:
    state = t.get('state', '?')
    name  = t.get('name', '?')
    print(f"  [{state}] {name}")
    qb.delete_torrent(t['hash'], delete_files=False)
    print("  -> Removed from queue (files kept on disk)")

if not korra:
    print("No Korra torrents in queue — already clear.")
