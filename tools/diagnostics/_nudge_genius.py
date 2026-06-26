"""Remove the stuck Genius S01E05/E06 torrents so a healthier release is fetched.

Read-only listing first, then deletes only torrents whose name matches
"Genius" + (S01E05 | S01E06). Prints exactly what it removes.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
from qbit_client import QBitClient

with open("config.json", encoding="utf-8") as fh:
    config = json.load(fh)

qbit_cfg = config.get("qbittorrent", {})
qbit = QBitClient(
    qbit_cfg.get("url", "http://localhost:8080"),
    username=qbit_cfg.get("username", ""),
    password=qbit_cfg.get("password", ""),
    bypass_auth=qbit_cfg.get("bypass_auth", False),
)
torrents = qbit.get_torrents()

pat = re.compile(r"genius", re.IGNORECASE)
ep_pat = re.compile(r"s0?1e0?5|s0?1e0?6", re.IGNORECASE)

matches = []
for t in torrents:
    name = t.get("name", "")
    if pat.search(name) and ep_pat.search(name):
        matches.append(t)

print(f"Found {len(matches)} matching Genius S01E05/E06 torrent(s):")
for t in matches:
    print(f"  - {t.get('progress', 0)*100:5.1f}%  seeds={t.get('num_seeds',0)}  {t.get('name')}")

if not matches:
    print("Nothing to remove.")
    sys.exit(0)

for t in matches:
    try:
        qbit.delete_torrent(t["hash"], delete_files=True)
        print(f"REMOVED: {t.get('name')}")
    except Exception as exc:
        print(f"FAILED to remove {t.get('name')}: {exc}")
