import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from qbit_client import QBitClient  # noqa: E402

c = json.load(open("config.json", encoding="utf-8"))["qbittorrent"]
q = QBitClient(c["url"], c.get("username", ""), c.get("password", ""),
               bypass_auth=c.get("bypass_auth", False))

for label in ("downloading", "stalledDL", "active"):
    ts = q.get_torrents(label)
    print(f"\n== filter={label}: {len(ts)} torrent(s) ==")
    for t in ts:
        pct = t.get("progress", 0) * 100
        print(f"   {pct:5.1f}%  {t.get('state',''):14} {t.get('name','')[:78]}")
