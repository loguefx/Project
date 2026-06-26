#!/usr/bin/env python3
"""Drive the per-series catch-up gate from outside the campaign process.

The campaign (`downloader.py --catch-up-series`) BLOCKS at every series waiting
for the user's manual-validation verdict, recorded in state["review_decision"].
This tool writes that verdict so the campaign can either advance or stay & fix:

  python campaign_review.py status
      Show which series the campaign is currently waiting on (and any auto-flags).

  python campaign_review.py confirm
      "This series is good" → the campaign moves on to the next series.

  python campaign_review.py redownload "<file path>" ["<file path>" ...]
  python campaign_review.py redownload --key "Steven Universe::S05E09" [...]
      "These episode(s) are bad" → remove + blacklist them (so the next search
      picks a DIFFERENT release), then tell the campaign to STAY on this series,
      re-download, and re-validate.

Nothing here bypasses the safety model: redownload only removes the exact files
you name, and it blacklists their release so we never re-pull the same bad copy.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from downloader import load_state, save_state, load_config
from validator import remove_broken_files


def _lib_paths(config: dict) -> list[str]:
    return [
        lib.get("path", "")
        for lib in config.get("libraries", [])
        if lib.get("enabled", True) and lib.get("path")
    ]


def _resolve_key_to_path(state: dict, key: str) -> str | None:
    """Map a downloaded_torrents key (full or partial) to its on-disk path."""
    downloaded = state.get("downloaded_torrents", {})
    if key in downloaded:
        return downloaded[key].get("path") or downloaded[key].get("file")
    # Partial match (e.g. "Steven Universe::S05E09" → "tv::Steven Universe::S05E09")
    matches = [k for k in downloaded if key.lower() in k.lower()]
    if len(matches) == 1:
        rec = downloaded[matches[0]]
        return rec.get("path") or rec.get("file")
    if len(matches) > 1:
        print(f"[campaign_review] '{key}' is ambiguous — matched {len(matches)}:")
        for m in matches:
            print(f"    {m}")
    return None


def cmd_status() -> int:
    state = load_state()
    awaiting = state.get("awaiting_series_review")
    if not awaiting:
        active = state.get("campaign_active")
        print(f"[campaign_review] Not waiting on a series review. "
              f"(campaign_active={bool(active)})")
        return 0
    print(f"[campaign_review] Campaign is WAITING on: {awaiting.get('show')}")
    print(f"    auto_flagged = {awaiting.get('auto_flagged')}")
    print(f"    since        = {awaiting.get('since')}")
    review = state.get("pending_download_review")
    if isinstance(review, dict) and review.get("broken"):
        print(f"    validator flagged {review.get('broken')} file(s):")
        for show in review.get("shows", []):
            for season in show.get("seasons", []):
                for bf in season.get("broken_files", []):
                    print(f"      - {bf.get('file')}  ({bf.get('reason')})")
    return 0


def cmd_confirm() -> int:
    state = load_state()
    if not state.get("campaign_active"):
        print("[campaign_review] WARNING: no campaign is active; setting confirm anyway.")
    state.pop("pending_download_review", None)
    state["review_decision"] = {"action": "confirm", "source": "manual"}
    save_state(state)
    show = (state.get("awaiting_series_review") or {}).get("show", "?")
    print(f"[campaign_review] CONFIRMED '{show}' — campaign will advance to the next series.")
    return 0


def _collect_show_paths(state: dict, show_needle: str) -> list[str]:
    """Pull every flagged broken-file path for shows matching `show_needle`."""
    review = state.get("pending_download_review") or {}
    needle = show_needle.lower()
    paths: list[str] = []
    for show in review.get("shows", []):
        if needle not in str(show.get("show", "")).lower():
            continue
        for season in show.get("seasons", []):
            for bf in season.get("broken_files", []):
                p = bf.get("path")
                if p:
                    paths.append(p)
    return paths


def cmd_redownload_show(show_needle: str) -> int:
    """Re-download ONLY the flagged corrupt episodes for one show (leaves any
    other show's flagged files in place)."""
    state = load_state()
    paths = _collect_show_paths(state, show_needle)
    if not paths:
        print(f"[campaign_review] No flagged files matched show ~ {show_needle!r}.")
        return 2
    print(f"[campaign_review] {len(paths)} flagged file(s) for ~ {show_needle!r}:")
    for p in paths:
        print(f"    {p}")
    return cmd_redownload(paths, as_keys=False)


def cmd_redownload(items: list[str], as_keys: bool) -> int:
    state = load_state()
    config = load_config()
    lib_paths = _lib_paths(config)

    paths: list[str] = []
    for item in items:
        if as_keys:
            p = _resolve_key_to_path(state, item)
            if not p:
                print(f"[campaign_review] Could not resolve key to a path: {item}")
                return 2
            paths.append(p)
        else:
            paths.append(item)

    # Sanity: make sure the files exist before we try to remove them.
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print("[campaign_review] These paths do not exist:")
        for m in missing:
            print(f"    {m}")
        return 2

    print(f"[campaign_review] Removing + blacklisting {len(paths)} episode(s):")
    for p in paths:
        print(f"    {p}")

    result = remove_broken_files(paths, state=state, library_paths=lib_paths)
    state.pop("pending_download_review", None)
    state["review_decision"] = {"action": "redownload", "source": "manual"}
    save_state(state)

    print(f"[campaign_review] Removed {result.get('removed_files', 0)} file(s), "
          f"blacklisted {result.get('blacklisted', 0)}, re-queued {result.get('requeued', 0)}.")
    if result.get("errors"):
        print("[campaign_review] Errors:")
        for e in result["errors"]:
            print(f"    {e}")
    show = (state.get("awaiting_series_review") or {}).get("show", "?")
    print(f"[campaign_review] Campaign will STAY on '{show}', re-download a different "
          f"release, and re-validate.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-series catch-up review control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show which series the campaign is waiting on")
    sub.add_parser("confirm", help="Confirm the current series is good; advance")

    rd = sub.add_parser("redownload", help="Mark episode(s) bad; remove + re-download")
    rd.add_argument("items", nargs="+", help="File paths (or keys with --key)")
    rd.add_argument("--key", action="store_true",
                    help="Treat items as downloaded_torrents keys, not file paths")

    rds = sub.add_parser("redownload-show",
                         help="Re-download ALL flagged corrupt files for one show")
    rds.add_argument("show", help="Show name (case-insensitive substring match)")

    args = parser.parse_args()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "confirm":
        return cmd_confirm()
    if args.cmd == "redownload":
        return cmd_redownload(args.items, args.key)
    if args.cmd == "redownload-show":
        return cmd_redownload_show(args.show)
    return 1


if __name__ == "__main__":
    sys.exit(main())
