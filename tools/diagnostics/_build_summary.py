"""
Parse the last scan's log file and write a last_scan_summary into state.json
so the Resend Notif button can replay it.
"""
import re, json
from pathlib import Path
from collections import defaultdict

LOG = Path(r"C:\Users\Logan\.cursor\projects\c-Users-Logan-Documents-ShowTVDownloader\terminals\307033.txt")
STATE = Path(r"C:\Users\Logan\Documents\ShowTVDownloader\state.json")

tv_added     = defaultdict(list)   # show -> [SxxExx, ...]
movies_added = []
not_found    = defaultdict(list)

# ── parse queued items ──────────────────────────────────────────────────────
QUEUED_RE      = re.compile(r"\[Notify\] Queued \(batched\): (.+?) (S\d{2}E\d{2})")
SEASON_PACK_RE = re.compile(r"\[Notify\] Season pack queued \(batched\): (.+?) (S\d{2}) [—\-] (\d+) eps")
MOVIE_RE       = re.compile(r"\[Notify\] Movie queued \(batched\): (.+)")

# ── parse not-found items ───────────────────────────────────────────────────
NF_TV_RE    = re.compile(r"\[TV\] No torrent found for (.+?) (S\d{2}E\d{2})")
NF_MOVIE_RE = re.compile(r"\[Movies\] No torrent found for (.+)")

with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        m = QUEUED_RE.search(line)
        if m:
            tv_added[m.group(1)].append(m.group(2))
            continue

        m = SEASON_PACK_RE.search(line)
        if m:
            show, season, count = m.group(1), m.group(2), int(m.group(3))
            eps = [f"{season}E{e:02d}" for e in range(1, count + 1)]
            tv_added[show].extend(eps)
            continue

        m = MOVIE_RE.search(line)
        if m:
            movies_added.append(m.group(1).strip())
            continue

        m = NF_TV_RE.search(line)
        if m:
            not_found[m.group(1)].append(m.group(2))
            continue

        m = NF_MOVIE_RE.search(line)
        if m:
            not_found[m.group(1).strip()].append("movie")

# ── load & update state.json ────────────────────────────────────────────────
state = json.loads(STATE.read_text(encoding="utf-8"))
state["last_scan_summary"] = {
    "tv_added":     dict(tv_added),
    "tv_paths":     {},
    "movies_added": list(dict.fromkeys(movies_added)),   # dedupe
    "movie_paths":  {},
    "not_found":    dict(not_found),
    "duration_sec": 7300,   # ~2hr scan duration
    "sent_at":      "2026-05-26T12:33:00",
    "note":         "Reconstructed from logs — scan was interrupted before notification fired",
}
STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

print("Summary written to state.json")
print(f"  TV shows queued : {len(tv_added)}")
for show, eps in sorted(tv_added.items()):
    print(f"    {show}: {len(eps)} episode(s)")
print(f"  Movies queued   : {len(movies_added)}")
print(f"  Not found shows : {len(not_found)}")
for show in sorted(not_found)[:10]:
    print(f"    {show}: {len(not_found[show])} ep(s)")
if len(not_found) > 10:
    print(f"    ... and {len(not_found)-10} more")
