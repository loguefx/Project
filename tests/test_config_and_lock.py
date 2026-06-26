"""
Unit tests for validate_config() (startup config sanity checks) and the
single-watcher filesystem lock (_claim_watcher_lock).

No network and no real Downloader construction — the lock helper only uses
os/time/pathlib, so we exercise it on an unconstructed instance.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader  # noqa: E402
from downloader import Downloader, validate_config  # noqa: E402


def _good_config(lib_path):
    return {
        "libraries": [
            {"name": "TV", "type": "tv", "path": lib_path, "enabled": True},
        ],
        "torrent_sources": [
            {"name": "Jackett", "type": "jackett", "enabled": True,
             "url": "http://127.0.0.1:9117", "api_key": "realkey123"},
        ],
        "notifications": {"discord_webhook": "https://discord.com/api/webhooks/1/abc"},
        "web_public_url": "http://192.168.0.50:5000",
        "schedule_hours": 6,
    }


class ValidateConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_clean_config_has_no_warnings(self):
        self.assertEqual(validate_config(_good_config(self.tmp)), [])

    def test_unreachable_library_path_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["libraries"][0]["path"] = os.path.join(self.tmp, "nonexistent-subdir-xyz")
        warns = validate_config(cfg)
        self.assertTrue(any("not reachable" in w for w in warns), warns)

    def test_duplicate_paths_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["libraries"].append({"name": "TV2", "type": "tv", "path": self.tmp, "enabled": True})
        self.assertTrue(any("duplicates" in w for w in validate_config(cfg)))

    def test_unknown_library_type_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["libraries"][0]["type"] = "anime"
        self.assertTrue(any("unknown type" in w for w in validate_config(cfg)))

    def test_jackett_missing_api_key_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["torrent_sources"][0]["api_key"] = ""
        self.assertTrue(any("api_key" in w for w in validate_config(cfg)))

    def test_scenetime_missing_creds_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["torrent_sources"] = [
            {"name": "SceneTime", "enabled": True, "url": "https://scenetime.com"},
        ]
        self.assertTrue(any("username/password" in w for w in validate_config(cfg)))

    def test_placeholder_webhook_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["notifications"]["discord_webhook"] = "https://your_webhook_here"
        self.assertTrue(any("notifications are off" in w for w in validate_config(cfg)))

    def test_bad_web_url_scheme_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["web_public_url"] = "192.168.0.50:5000"
        self.assertTrue(any("web_public_url should start with" in w for w in validate_config(cfg)))

    def test_button_hint_when_web_url_missing(self):
        cfg = _good_config(self.tmp)
        cfg["web_public_url"] = ""
        self.assertTrue(any("buttons will be hidden" in w for w in validate_config(cfg)))

    def test_bad_schedule_hours_flagged(self):
        cfg = _good_config(self.tmp)
        cfg["schedule_hours"] = 0
        self.assertTrue(any("schedule_hours" in w for w in validate_config(cfg)))


class WatcherLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "watcher.lock"
        self.hb = self.tmp / "watcher.heartbeat"
        # Unconstructed instance — _claim_watcher_lock uses no instance state.
        self.dl = Downloader.__new__(Downloader)

    def test_first_claim_succeeds_and_creates_lock(self):
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))
        self.assertTrue(self.lock.exists())

    def test_second_claim_blocked_while_heartbeat_fresh(self):
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))
        self.hb.write_text(str(time.time()))   # live watcher heartbeating
        self.assertFalse(self.dl._claim_watcher_lock(self.lock, self.hb))

    def test_stale_lock_is_reclaimed(self):
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))
        # Heartbeat went stale (previous watcher died) → next claim reclaims it.
        self.hb.write_text(str(time.time()))
        old = time.time() - 200
        os.utime(self.hb, (old, old))
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))

    def test_second_claim_blocked_when_lock_fresh_but_no_heartbeat(self):
        # Race window: the holder just created the lock and hasn't written its
        # first heartbeat yet. A simultaneous starter must fall back to the
        # lock's own (fresh) mtime and back off, not steal it.
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))
        self.assertFalse(self.hb.exists())
        self.assertFalse(self.dl._claim_watcher_lock(self.lock, self.hb))

    def test_old_lock_with_no_heartbeat_is_reclaimed(self):
        # Holder died before ever writing a heartbeat → the lock itself is old.
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))
        self.assertFalse(self.hb.exists())
        old = time.time() - 200
        os.utime(self.lock, (old, old))
        self.assertTrue(self.dl._claim_watcher_lock(self.lock, self.hb))


if __name__ == "__main__":
    unittest.main()
