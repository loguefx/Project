import sys, json
from pathlib import Path
sys.path.insert(0, '.')
sys.stdout = __import__('io').TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from qbit_client import QBitClient

cfg = json.loads(Path('config.json').read_text())
qb_cfg = cfg.get('qbittorrent', {})
qb = QBitClient(
    url=qb_cfg.get('url', 'http://localhost:8083'),
    username=qb_cfg.get('username', ''),
    password=qb_cfg.get('password', ''),
)

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv'}

# ── Find SpongeBob folder across all libraries ──────────────────────────────
libraries = cfg.get('libraries', [])
sponge_dirs = []
for lib in libraries:
    p = Path(lib.get('path', ''))
    candidate = p / 'SpongeBob SquarePants'
    if candidate.exists():
        sponge_dirs.append(candidate)

for sponge in sponge_dirs:
    print(f"\n{'='*70}")
    print(f"PATH: {sponge}")
    print('='*70)
    try:
        season_dirs = sorted([d for d in sponge.iterdir() if d.is_dir()])
    except Exception as e:
        print(f"  ERROR listing: {e}")
        continue

    for sd in season_dirs:
        try:
            files = sorted(sd.iterdir())
        except Exception:
            continue
        video_files = [f for f in files if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        part_files  = [f for f in files if f.is_file() and f.suffix.lower() in {'.part', '.!qb'}]
        if not video_files and not part_files:
            continue
        print(f"\n  {sd.name}  ({len(video_files)} video file(s), {len(part_files)} incomplete)")
        for f in video_files:
            size_mb = f.stat().st_size / (1024*1024)
            print(f"    [{f.suffix}] {f.name}  ({size_mb:.1f} MB)")
        for f in part_files:
            size_mb = f.stat().st_size / (1024*1024)
            print(f"    [INCOMPLETE] {f.name}  ({size_mb:.1f} MB)")

# ── Check qBittorrent for SpongeBob torrents ────────────────────────────────
print(f"\n{'='*70}")
print("QBITTORRENT — SpongeBob torrents")
print('='*70)
try:
    torrents = qb.get_torrents()
    sb_torrents = [t for t in torrents if 'spongebob' in t.get('name','').lower()]
    if not sb_torrents:
        print("  None found in queue.")
    for t in sb_torrents:
        pct = t.get('progress', 0) * 100
        print(f"  [{t.get('state','?')}] {t['name']}")
        print(f"    Progress: {pct:.1f}%  |  Size: {t.get('size',0)/1024/1024:.1f} MB")
        print(f"    Save path: {t.get('save_path','?')}")
except Exception as e:
    print(f"  qBit error: {e}")
