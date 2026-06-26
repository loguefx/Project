"""Re-attempt the episodes whose only 1080p source was corrupt, now that 720p
fallback is allowed and blacklist matching is tracker-tag-robust.

Scoped to the two shows under test (SpongeBob, Simpsons) via _retry_keys.json.
For each key we re-queue a release: find_tv first tries clean seeded 1080p, and
ONLY falls back to a verified 720p when every 1080p option is blacklisted/corrupt
or unavailable. The watcher (with the fixed validator) then verifies each one.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from downloader import Downloader, load_config, load_state, save_state, ensure_jackett_up


def main() -> None:
    keys = json.load(open(Path(__file__).parent / "_retry_keys.json"))
    cfg = load_config()
    if not ensure_jackett_up(cfg):
        print("Jackett down and could not start — aborting (won't queue).")
        return
    dl = Downloader(cfg)
    lib_paths = dl._lib_paths()
    queued, rels, exhausted = [], [], []
    for k in keys:
        try:
            rel = dl._requeue_one_episode(k, lib_paths)
        except Exception as exc:
            print(f"  error {k}: {exc}")
            continue
        if rel:
            queued.append(k)
            rels.append(rel)
            res = "720p" if "720" in rel else ("1080p" if "1080" in rel else "?")
            print(f"  queued [{res}] {k}: {rel}")
        else:
            exhausted.append(k)
            print(f"  still no source {k}")
    print(f"\nQueued {len(queued)}; no-source {len(exhausted)}.")
    if not queued:
        print("Nothing queued.")
        return
    st = load_state()
    st["pending_validation"] = {
        "torrents": sorted(set(rels)),
        "keys": sorted(set(queued)),
        "scan_active": False,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_state(st)
    dl._concurrent_watch = True
    dl._spawn_download_watcher()
    print("Watcher armed (fixed validator + 720p fallback). It verifies each, then stops.")


if __name__ == "__main__":
    main()
