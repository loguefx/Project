"""
Unit tests for the quality filter, ranking, blacklist identity, episode/season
selection, and the season-batch search bucketing in torrent_finder.

These are pure/offline tests — sources are stubbed, so no network is touched.
Run from the repo root with either:

    python -m unittest discover -s tests
    python -m pytest tests            (if pytest is installed)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from torrent_finder import TorrentFinder            # noqa: E402
from sources.base_source import TorrentResult       # noqa: E402


QCFG = {
    "resolution": "1080p",
    "allowed_resolutions": ["1080P"],
    "fallback_resolutions": ["720P"],
    "blocked_sources": ["CAM", "TS", "TELESYNC", "SCREENER"],
    "min_seeders": 3,
    "min_size_episode_gb": 0.3,
    "max_size_episode_gb": 6.0,
}


def _r(name, seeders=20, size_gb=1.5, source="Test"):
    return TorrentResult(name=name, size_gb=size_gb, seeders=seeders, source_name=source)


class FakeSource:
    """Duck-typed stand-in for a BaseSource — _gather only needs .name/.search."""

    def __init__(self, name, results):
        self.name = name
        self._results = results
        self.search_calls = 0

    def search(self, query):
        self.search_calls += 1
        return list(self._results)


def _finder(sources=None, qcfg=None):
    return TorrentFinder(sources or [], qcfg or dict(QCFG))


class QualityFilterTests(unittest.TestCase):
    def setUp(self):
        self.f = _finder()

    def test_rejects_disallowed_resolution(self):
        ok, reason = self.f.passes_filter("Show S01E01 480p WEB x264-GRP", 1.0, "tv")
        self.assertFalse(ok)
        self.assertIn("Resolution", reason)

    def test_accepts_1080p(self):
        ok, _ = self.f.passes_filter("Show S01E01 1080p WEB-DL x264-GRP", 1.5, "tv")
        self.assertTrue(ok)

    def test_blocks_cam_source(self):
        ok, reason = self.f.passes_filter("Show S01E01 1080p CAM x264-GRP", 1.5, "tv")
        self.assertFalse(ok)
        self.assertIn("CAM", reason)

    def test_size_too_large_rejected(self):
        ok, reason = self.f.passes_filter("Show S01E01 1080p WEB-DL x264-GRP", 99.0, "tv")
        self.assertFalse(ok)
        self.assertIn("size", reason.lower())

    def test_foreign_cjk_rejected(self):
        ok, reason = self.f.passes_filter("Show \u30a2\u30cb\u30e1 1080p WEB x264", 1.5, "tv")
        self.assertFalse(ok)

    def test_fallback_resolution_only_with_extra_res(self):
        name = "Show S01E01 720p WEB-DL x264-GRP"
        self.assertFalse(self.f.passes_filter(name, 1.0, "tv")[0])
        self.assertTrue(self.f.passes_filter(name, 1.0, "tv", extra_res=["720P"])[0])


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.f = _finder()

    def test_proper_wins(self):
        proper = _r("Show S01E01 1080p WEB-DL x264-GRP PROPER", seeders=5)
        normal = _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=30)
        ranked = self.f._filter_and_rank([normal, proper], "tv")
        self.assertIn("PROPER", ranked[0].name)

    def test_healthy_seeders_beat_dead(self):
        healthy = _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=50)
        dead = _r("Show S01E01 1080p WEB-DL x264-OTHER", seeders=0)
        ranked = self.f._filter_and_rank([dead, healthy], "tv")
        self.assertEqual(ranked[0].seeders, 50)

    def test_1080p_beats_720p_when_equally_healthy(self):
        hd = _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=40)
        sd = _r("Show S01E01 720p WEB-DL x264-GRP", seeders=40)
        ranked = self.f._filter_and_rank([sd, hd], "tv", extra_res=["720P"])
        self.assertIn("1080p", ranked[0].name)

    def test_prefers_larger_file_same_resolution(self):
        small = _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=30, size_gb=1.0)
        big = _r("Show S01E01 1080p WEB-DL x264-OTH", seeders=30, size_gb=2.5)
        ranked = self.f._filter_and_rank([small, big], "tv")
        self.assertEqual(ranked[0].size_gb, 2.5)

    def test_codec_still_outranks_size(self):
        # Larger HEVC vs smaller H264: H264 (more compatible) must still win.
        h264 = _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=30, size_gb=1.0)
        hevc = _r("Show S01E01 1080p WEB-DL x265-GRP", seeders=30, size_gb=3.5)
        ranked = self.f._filter_and_rank([hevc, h264], "tv")
        self.assertIn("x264", ranked[0].name)


class BlacklistIdentityTests(unittest.TestCase):
    def test_tracker_tag_does_not_change_identity(self):
        a = TorrentFinder._norm_bl("Show.S01E01.1080p.WEB.x264-GRP[EZTV]")
        b = TorrentFinder._norm_bl("Show.S01E01.1080p.WEB.x264-GRP")
        self.assertEqual(a, b)

    def test_different_release_differs(self):
        a = TorrentFinder._norm_bl("Show.S01E01.1080p.WEB.x264-GRP")
        b = TorrentFinder._norm_bl("Show.S01E01.1080p.WEB.x264-OTHER")
        self.assertNotEqual(a, b)


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.f = _finder()

    def test_exact_episode_filter(self):
        pool = [
            _r("Show S01E01 1080p WEB-DL x264-GRP", seeders=20),
            _r("Show S01E02 1080p WEB-DL x264-GRP", seeders=99),
        ]
        best = self.f._select(pool, "tv", require_se=(1, 1))
        self.assertIsNotNone(best)
        self.assertIn("S01E01", best.name)

    def test_blacklisted_only_release_returns_none(self):
        pool = [_r("Show S01E01 1080p WEB-DL x264-GRP", seeders=20)]
        best = self.f._select(
            pool, "tv", require_se=(1, 1),
            blacklist=["Show.S01E01.1080p.WEB-DL.x264-GRP"],
        )
        self.assertIsNone(best)

    def test_empty_pool_returns_none(self):
        self.assertIsNone(self.f._select([], "tv", require_se=(1, 1)))

    def test_unverifiable_marginal_seeders_rejected(self):
        # A no-magnet (private-tracker) release the indexer lists at 4 seeds is
        # below the unverified-confidence bar (6) and CAN'T be live-scraped, so
        # it must NOT be selected — this is the SceneTime "3 seeds but dead" case.
        pool = [_r("Show S01E01 1080p WEB-DL x265-GRP", seeders=4)]
        best = self.f._select(pool, "tv", require_se=(1, 1))
        self.assertIsNone(best)

    def test_unverifiable_well_seeded_accepted(self):
        # The same kind of release with a comfortably high indexer count clears
        # the unverified bar and is accepted.
        pool = [_r("Show S01E01 1080p WEB-DL x265-GRP", seeders=25)]
        best = self.f._select(pool, "tv", require_se=(1, 1))
        self.assertIsNotNone(best)

    def test_last_resort_opt_in_queues_unseeded(self):
        # With the opt-in flag, a marginal unverifiable release is queued as a
        # last resort instead of being dropped.
        f = _finder(qcfg={**QCFG, "queue_unseeded_last_resort": True})
        pool = [_r("Show S01E01 1080p WEB-DL x265-GRP", seeders=4)]
        best = f._select(pool, "tv", require_se=(1, 1))
        self.assertIsNotNone(best)


class SeasonBatchTests(unittest.TestCase):
    def test_buckets_multiple_episodes_from_one_search(self):
        results = [
            _r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=30),
            _r("Test Show S01E02 1080p WEB-DL x264-GRP", seeders=25),
            _r("Test Show S01E03 1080p WEB-DL x264-GRP", seeders=10),
        ]
        f = _finder(sources=[FakeSource("Test", results)])
        out = f.find_tv_season_batch("Test Show", 1, [1, 2, 3])
        self.assertEqual(set(out), {1, 2, 3})
        self.assertIn("S01E01", out[1].name)
        self.assertIn("S01E03", out[3].name)

    def test_missing_episode_absent_from_result(self):
        results = [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=30)]
        f = _finder(sources=[FakeSource("Test", results)])
        out = f.find_tv_season_batch("Test Show", 1, [1, 5])
        self.assertIn(1, out)
        self.assertNotIn(5, out)

    def test_gather_cache_avoids_repeat_search(self):
        src = FakeSource("Test", [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=20)])
        f = _finder(sources=[src])
        f._gather("Test Show S01", must_contain="Test Show")
        f._gather("Test Show S01", must_contain="Test Show")
        self.assertEqual(src.search_calls, 1)  # second call served from cache

    def test_gather_cache_expires_with_ttl(self):
        src = FakeSource("Test", [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=20)])
        f = _finder(sources=[src])
        f._gather_ttl = 0  # disable caching
        f._gather("Test Show S01", must_contain="Test Show")
        f._gather("Test Show S01", must_contain="Test Show")
        self.assertEqual(src.search_calls, 2)

    def test_concurrent_sources_are_pooled(self):
        s1 = FakeSource("A", [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=5, source="A")])
        s2 = FakeSource("B", [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=50, source="B")])
        f = _finder(sources=[s1, s2])
        out = f.find_tv_season_batch("Test Show", 1, [1])
        self.assertIn(1, out)
        # Both sources pooled, then ranked once → the healthier (B) wins.
        self.assertEqual(out[1].source_name, "B")


class FakeGatingSource(FakeSource):
    """A gating source (like Jackett) that's 'down' for the first N health
    checks, then recovers — so the wait loop can be exercised without sleeping
    on a real network."""

    gate_when_down = True

    def __init__(self, name, results, down_for=0):
        super().__init__(name, results)
        self._down_for = down_for
        self.health_calls = 0

    def health_check(self):
        self.health_calls += 1
        return self.health_calls > self._down_for


class GatingSourceWaitTests(unittest.TestCase):
    def test_healthy_gating_source_does_not_block(self):
        src = FakeGatingSource("Jackett", [], down_for=0)
        f = _finder(sources=[src])
        f._source_retry_sec = 0  # no real sleeping
        f._wait_for_gating_sources()
        self.assertEqual(src.health_calls, 1)  # checked once, already up

    def test_pauses_until_gating_source_recovers(self):
        src = FakeGatingSource("Jackett", [], down_for=3)
        f = _finder(sources=[src])
        f._source_retry_sec = 0  # don't actually sleep between retries
        statuses = []
        f.on_wait_status = statuses.append
        f._wait_for_gating_sources()
        # 3 failed probes + the 4th that succeeds.
        self.assertEqual(src.health_calls, 4)
        # A "paused" status was surfaced, then cleared (None) on recovery.
        self.assertTrue(any(s for s in statuses if s))
        self.assertIsNone(statuses[-1])

    def test_gather_blocks_then_searches_after_recovery(self):
        src = FakeGatingSource(
            "Jackett",
            [_r("Test Show S01E01 1080p WEB-DL x264-GRP", seeders=20)],
            down_for=2,
        )
        f = _finder(sources=[src])
        f._source_retry_sec = 0
        pooled = f._gather("Test Show S01", must_contain="Test Show")
        # It waited out the outage, THEN performed exactly one real search.
        self.assertEqual(src.search_calls, 1)
        self.assertEqual(len(pooled), 1)

    def test_max_wait_cap_gives_up(self):
        src = FakeGatingSource("Jackett", [], down_for=999)  # never recovers
        f = _finder(sources=[src])
        f._source_retry_sec = 0
        f._source_wait_max_min = 1e-9  # effectively "give up immediately"
        f._wait_for_gating_sources()   # must return rather than loop forever
        self.assertGreaterEqual(src.health_calls, 1)


if __name__ == "__main__":
    unittest.main()
