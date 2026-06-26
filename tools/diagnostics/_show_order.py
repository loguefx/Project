"""Print the campaign's show processing order (sorted) around a given index so
we can pick the right --start-at to skip a show."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from scanner import scan_tv_library

PATH = r"\\192.168.0.181\Jellyfin1\TV Shows"
inv = scan_tv_library(PATH)
names = sorted(inv.keys())
for i, n in enumerate(names, 1):
    if 20 <= i <= 28:
        print(f"  {i:3d}/{len(names)}: {n}")
