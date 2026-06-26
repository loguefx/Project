"""
One-off: re-queue a single episode whose only viable replacement was wrongly
abandoned by the now-fixed stall-state carry-over bug. Un-blacklists the 720p
fallback release so the finder can pick it again, re-queues it, and spawns the
validate-as-you-go watcher (which now resets per-attempt stall/recheck state).

Usage:
    python _retry_episode.py "tv::SpongeBob SquarePants::S01E10"
"""
import sys
import time

from downloader import Downloader, load_config, load_state, save_state, ensure_jackett_up


def main(key: str) -> None:
    cfg = load_config()
    dl = Downloader(cfg)
    dl.state = load_state()

    if not ensure_jackett_up(cfg):
        print("Jackett offline — aborting (would delete with no replacement).")
        return

    # Un-blacklist the 720p fallback release that the bug killed prematurely so
    # it becomes a candidate again. Leave genuinely-bad releases blacklisted.
    bl = dl.state.setdefault("torrent_blacklist", {}).get(key, [])
    kept = [b for b in bl if not ("720p" in b.lower() and "pizza" in b.lower())]
    removed = [b for b in bl if b not in kept]
    dl.state.setdefault("torrent_blacklist", {})[key] = kept
    print(f"Un-blacklisted {len(removed)} release(s): {removed}")
    save_state(dl.state)

    dl._concurrent_watch = True
    dl._ignore_cooldown = True
    dl._watcher_spawned = False
    lib_paths = dl._lib_paths()

    new_rel = dl._requeue_one_episode(key, lib_paths)
    print("Re-queued release:", new_rel)
    if new_rel:
        dl._publish_pending(key, new_rel)
        pv = dl.state.setdefault("pending_validation", {})
        pv["scan_active"] = False  # no more episodes coming — watcher can drain+exit
        save_state(dl.state)
        print("Watcher armed — it will download + deep-validate the replacement.")
    else:
        print("No untried release found.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('usage: python _retry_episode.py "tv::Show::S01E02"')
        sys.exit(2)
    main(sys.argv[1])
