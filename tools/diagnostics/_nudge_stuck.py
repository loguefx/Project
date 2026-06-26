"""Nudge a specific stuck torrent: find Bridgerton S04E04 in qBittorrent and
delete it (with its partial files) so the running watcher blacklists that dead
release and re-searches a healthier one. Only deletes torrents whose name
matches the S04E04 episode pattern, to avoid touching packs/other episodes."""
import os, re, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from downloader import load_config
from qbit_client import QBitClient

cfg = load_config()
qc = cfg["qbittorrent"]
q = QBitClient(qc.get("url", ""), qc.get("username", ""), qc.get("password", ""),
               qc.get("bypass_auth", False))

ep_re = re.compile(r"s0?4[ ._-]*e0?4", re.IGNORECASE)
ts = q.get_torrents()
matches = [t for t in ts
           if "bridgerton" in t.get("name", "").lower() and ep_re.search(t.get("name", ""))]

print(f"Matched {len(matches)} torrent(s):")
for t in matches:
    print(f"  {t.get('progress',0)*100:5.1f}%  {t.get('state',''):<12} {t.get('name','')}")

if len(matches) == 1:
    t = matches[0]
    ok = q.delete_torrent(t["hash"], delete_files=True)
    print(f"\nDeleted (with files): {ok}  -> {t.get('name','')}")
    print("Watcher will detect it vanished, blacklist this release, and re-search.")
elif not matches:
    print("\nNo Bridgerton S04E04 torrent found — maybe it already advanced/was replaced.")
else:
    print("\nMultiple matches — NOT deleting automatically. Review the list above.")
