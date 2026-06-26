"""Summarize qBittorrent torrents: state, progress, seeds, stall age."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
from collections import Counter
from qbit_client import QBitClient

with open("config.json", encoding="utf-8") as fh:
    config = json.load(fh)

qc = config.get("qbittorrent", {})
qbit = QBitClient(
    qc.get("url", "http://localhost:8080"),
    username=qc.get("username", ""),
    password=qc.get("password", ""),
    bypass_auth=qc.get("bypass_auth", False),
)
torrents = qbit.get_torrents()
now = time.time()

states = Counter(t.get("state", "?") for t in torrents)
print(f"Total torrents: {len(torrents)}")
print("By state:")
for s, n in states.most_common():
    print(f"  {n:3d}  {s}")

print("\nIncomplete torrents (progress < 100%):")
inc = [t for t in torrents if (t.get("progress", 0) or 0) < 1.0]
inc.sort(key=lambda t: t.get("progress", 0))
for t in inc:
    last = t.get("last_activity") or 0
    idle_min = (now - last) / 60 if last else -1
    print(f"  {t.get('progress',0)*100:5.1f}%  state={t.get('state','?'):12s} "
          f"seeds={t.get('num_seeds',0)}/{t.get('num_complete',0):<3} "
          f"idle={idle_min:6.0f}m  {t.get('name','')[:70]}")
print(f"\nIncomplete count: {len(inc)}")
