"""
TorrentFinder — calls enabled sources and applies the quality filter pipeline.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sources.base_source import BaseSource, TorrentResult
from swarm_health import SwarmHealthChecker

logger = logging.getLogger(__name__)


class TorrentFinder:
    # Adaptive group-reliability thresholds. The validator bumps a counter
    # every time it deletes a corrupt download attributed to a release group;
    # once the count reaches these levels the finder applies penalties / bans.
    GROUP_DEPRIO_THRESHOLD = 3   # ≥3 corrupt downloads → deprioritise this group
    GROUP_BLOCK_THRESHOLD  = 5   # ≥5 corrupt downloads → exclude entirely

    def __init__(self, sources: list[BaseSource], quality_config: dict,
                 group_failure_count: Optional[dict] = None):
        self._sources = sources
        self._q = quality_config
        # Mapping {GROUP_UPPERCASE: int_failures}, supplied by the downloader.
        self._group_failures: dict[str, int] = group_failure_count or {}
        # Groups that must NEVER be hard-excluded by the adaptive failure block,
        # no matter how many prior failures they have. Used for uploaders that
        # are the ONLY source for some content (e.g. MeGusta for many cartoon
        # episodes) — we'd rather download their copy and let deep validation
        # decide than lose the episode entirely. They remain deprioritised (see
        # ranking) so any other source still wins when one exists.
        self._never_block = {g.upper() for g in self._q.get("never_block_groups", [])}
        # Per-instance search cache: avoids re-hitting the trackers with the same
        # query during a run / retry loop. Keyed by (query, must_contain); short
        # TTL so a long-running watcher still refreshes swarm health periodically.
        self._gather_cache: dict[tuple, tuple] = {}
        self._gather_ttl: float = float(self._q.get("search_cache_ttl_sec", 600))
        # When a gating source (e.g. Jackett) is unreachable, pause searching
        # and re-probe every this-many seconds until it returns. 0 max-wait =
        # wait indefinitely (the user wants the run to resume, not skip ahead).
        self._source_retry_sec: float = float(self._q.get("source_retry_sec", 20))
        self._source_wait_max_min: float = float(self._q.get("source_wait_max_min", 0))
        # Optional callback(msg|None) so the caller can surface the pause on a
        # dashboard / status line. None clears the paused status.
        self.on_wait_status: Optional[callable] = None
        # ── Live swarm health check ────────────────────────────────────────
        # Indexer seeder counts are captured at index time and go stale, so a
        # "healthy" release is often dead by download time. When enabled we
        # scrape the trackers at SELECTION time for the real current seeder
        # count and skip dead swarms in favour of the next-best live release.
        self._verify_swarm = bool(self._q.get("verify_swarm_health", True))
        # Cap how many of the ranked candidates we'll scrape per selection so a
        # huge pool can't blow up latency; the rest fall back to indexer counts.
        self._swarm_check_top_n = int(self._q.get("swarm_check_top_n", 6))
        # When we CAN'T live-verify a release (private-tracker .torrent with no
        # magnet to scrape, or every tracker failed to answer), the indexer's
        # reported seeder count is the only signal — and it's frequently stale or
        # inflated (e.g. SceneTime listing "3 seeds" on a swarm that's actually
        # dead). For those unverifiable releases we demand a HIGHER seeder count
        # before trusting them, so a marginal-but-unconfirmable release isn't
        # queued only to stall at 0%. Verifiable releases keep the normal
        # ``min_seeders`` bar since we can confirm they're truly alive.
        self._unverified_min_seeders = int(
            self._q.get("unverified_min_seeders",
                        max(self._q.get("min_seeders", 3) + 3, self._q.get("prefer_seeders", 5) + 1))
        )
        # Last-resort: when NOTHING clears the bar, the finder used to queue the
        # top-ranked release anyway (even at 0 seeds), which is exactly how dead
        # torrents ended up stalling in qBittorrent. Default OFF now — better to
        # report "no working release" and retry later than to queue a corpse.
        self._queue_unseeded_last_resort = bool(self._q.get("queue_unseeded_last_resort", False))
        self._health: Optional[SwarmHealthChecker] = None
        if self._verify_swarm:
            self._health = SwarmHealthChecker(
                timeout=float(self._q.get("swarm_scrape_timeout_sec", 5)),
                max_trackers=int(self._q.get("swarm_max_trackers", 10)),
                cache_ttl=float(self._q.get("swarm_cache_ttl_sec", 600)),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Leading phrases that indexers usually drop from release names — e.g.
    # "The Adventures of Jimmy Neutron: Boy Genius" is released as plain
    # "Jimmy Neutron". Used to build simplified title variants for the fallback.
    _LEADING_FILLER = re.compile(
        r"^(?:the\s+adventures\s+of|the\s+new\s+adventures\s+of|"
        r"the\s+amazing\s+world\s+of|the\s+legend\s+of|the\s+epic\s+tales\s+of|"
        r"the\s+marvelous\s+misadventures\s+of)\s+",
        re.IGNORECASE,
    )

    @classmethod
    def _title_variants(cls, name: str) -> list[str]:
        """Full title first, then progressively simpler aliases (drop the
        subtitle after a colon, strip common leading filler). Used so a search
        falls back to how releases are ACTUALLY named when the full TVMaze title
        returns nothing — without ever guessing a wholly different name."""
        variants: list[str] = []

        def add(v: str) -> None:
            v = v.strip(" -:")
            if v and v not in variants:
                variants.append(v)

        add(name)
        if ":" in name:
            add(name.split(":")[0])
        stripped = cls._LEADING_FILLER.sub("", name)
        if stripped != name:
            add(stripped)
            if ":" in stripped:
                add(stripped.split(":")[0])
        return variants

    def find_tv(
        self, show_name: str, season: int, episode: int,
        blacklist: list[str] | None = None,
    ) -> Optional[TorrentResult]:
        # NOTE: no resolution term in the query — searching "... 1080p" made
        # indexers return ONLY 1080p, so well-seeded 720p releases were never
        # seen and dead 1080p torrents got queued. Resolution is handled by the
        # filter (allowed_resolutions) + ranking (_res_tier) instead.
        if blacklist:
            logger.info("[Finder] Blacklisting %d previously-corrupt release(s)", len(blacklist))
        variants = self._title_variants(show_name)
        for i, title in enumerate(variants):
            query = f"{title} S{season:02d}E{episode:02d}"
            if i == 0:
                logger.info("[Finder] TV search: %r", query)
            else:
                logger.info("[Finder] TV search (alias fallback): %r", query)
            res = self._find(
                query, media_type="tv", must_contain=title,
                blacklist=blacklist, require_se=(season, episode),
            )
            if res:
                return res
        return None

    def find_season_pack(
        self, show_name: str, season: int,
        blacklist: list[str] | None = None,
    ) -> Optional[TorrentResult]:
        """Search for a full season pack (e.g. 'The Bear S03')."""
        variants = self._title_variants(show_name)
        for i, title in enumerate(variants):
            query = f"{title} S{season:02d}"
            if i == 0:
                logger.info("[Finder] Season pack search: %r", query)
            else:
                logger.info("[Finder] Season pack search (alias fallback): %r", query)
            res = self._find(
                query, media_type="tv", must_contain=title, season_pack=True,
                blacklist=blacklist, require_season=season,
            )
            if res:
                return res
        return None

    def find_movie(self, title: str, year: Optional[int] = None) -> Optional[TorrentResult]:
        year_str = f" {year}" if year else ""
        query = f"{title}{year_str}"
        logger.info("[Finder] Movie search: %r", query)
        return self._find(query, media_type="movie", must_contain=title)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _phrase_matches(phrase: str, text: str) -> bool:
        """
        True iff `phrase` appears in `text` and the token immediately following
        it is a technical tag (contains digits, e.g. S03E02 / 1080p / 2024) or a
        known release keyword — NOT a plain word that would signal a different
        show variant (e.g. "Canada", "US"). This stops "Project Runway" from
        matching "Project Runway Canada S03E02".
        """
        idx = text.find(phrase)
        if idx == -1:
            return False
        rest = text[idx + len(phrase):].lstrip()
        if not rest:
            return True  # phrase at end of string
        tokens = rest.split()
        next_word = tokens[0]
        _TECH = frozenset({
            "web", "blu", "hdtv", "repack", "proper", "complete",
            "bluray", "season", "amzn", "nf", "hbo", "dl", "hdr",
            "uhd", "sdr", "extended", "theatrical", "x264", "x265",
            "hevc", "h264", "h265", "avc", "remux", "hybrid",
            "docu", "mkv", "mp4", "remastered", "unrated", "uncensored",
            "directors", "cut", "mini", "series", "collection",
        })
        if any(c.isdigit() for c in next_word):
            return True
        if len(next_word) == 1:
            # A lone letter right after the title (e.g. the "A" in
            # "The Fairly OddParents A New Wish") only belongs to THIS release if
            # a technical/numeric tag follows it. If more title WORDS follow, it's
            # a different / sub-titled / reboot series — reject it.
            after = tokens[1:]
            if not after:
                return True
            nxt = after[0].lower()
            if any(c.isdigit() for c in nxt) or nxt in _TECH:
                return True
            return False
        nl = next_word.lower()
        if nl in _TECH:
            return True
        # Region/country codes (e.g. "Clarence US S02E04", "The Office US",
        # "Shameless UK") are part of the release name, NOT a different show —
        # accept them AS LONG AS the token after the code is itself a season/
        # episode marker or technical tag (so we still reject genuine variants
        # like "... Canada" / "... Australia" written out as full words).
        _REGION = frozenset({"us", "uk", "au", "ca", "nz", "ie", "gb"})
        if nl in _REGION:
            after = tokens[1:]
            if not after:
                return True
            nxt = after[0]
            if any(c.isdigit() for c in nxt) or len(nxt) == 1 or nxt.lower() in _TECH:
                return True
        return False

    # Tracker/site tags that get appended in varying forms (" EZTV" vs
    # "[EZTVx.to]") and must NOT count toward release identity — otherwise a
    # blacklisted corrupt release re-appears with a different tag and slips
    # past the blacklist (which then blocks the 720p fallback from firing).
    _TRACKER_TAGS = frozenset({
        "eztv", "eztvx", "eztvxto", "eztvio", "eztvre", "ettv", "ettvdvd",
        "rarbg", "rartv", "tgx", "galaxytv", "galaxyrg", "mega", "publichd",
        "to", "io", "re", "com", "org",
    })

    @staticmethod
    def _norm(s: str) -> str:
        # Drop apostrophes outright (so "Bob's" == "Bobs"), then turn EVERY
        # other non-alphanumeric run — dots, dashes, underscores, colons,
        # commas, brackets — into a single space. Without stripping the colon,
        # a TVMaze title like "Jimmy Neutron: Boy Genius" could never match a
        # release named "Jimmy Neutron Boy Genius".
        s = s.replace("'", "").replace("\u2019", "")
        return re.sub(r"[^0-9a-zA-Z]+", " ", s).lower().strip()

    @classmethod
    def _norm_bl(cls, s: str) -> str:
        # Drop [..] and (..) tracker tags, then strip tag tokens, then
        # collapse to a tag-free alphanumeric identity.
        s = re.sub(r"[\[(][^\])]*[\])]", " ", s)
        s = re.sub(r"[\.\-_ ]+", " ", s).lower()
        toks = [t for t in s.split() if t not in cls._TRACKER_TAGS]
        return "".join(toks)

    def _notify_wait(self, msg: Optional[str]) -> None:
        """Surface (or clear) a 'paused, waiting for source' status, if a hook
        was installed by the caller. Never let a status-hook error break a
        search."""
        if self.on_wait_status is None:
            return
        try:
            self.on_wait_status(msg)
        except Exception:
            pass

    def _wait_for_gating_sources(self) -> None:
        """Block until every gating source (e.g. Jackett) is reachable again.

        While one is down we re-probe every ``source_retry_sec`` seconds and
        resume the instant it returns, so a flaky aggregator pauses the run
        instead of letting it race through the library finding nothing. Waits
        indefinitely unless ``source_wait_max_min`` > 0 imposes a give-up cap."""
        gating = [s for s in self._sources if getattr(s, "gate_when_down", False)]
        if not gating:
            return

        # Re-probe at a SHORT cadence (capped at 5s) so the run resumes within a
        # few seconds of the source returning, regardless of the user-facing
        # "retry every N seconds" figure. health_check() already absorbs a
        # momentarily-slow source via its own internal retries, so this loop
        # only ever sees a TRUE outage — meaning a fast re-probe is safe and
        # never causes flapping.
        probe_interval = min(self._source_retry_sec, 5.0)
        waited = 0.0
        announced = False
        last_log = 0.0
        while True:
            down = [s for s in gating if not s.health_check()]
            if not down:
                if announced:
                    logger.info(
                        "[Finder] %s back online — resuming searches.",
                        ", ".join(s.name for s in gating),
                    )
                    self._notify_wait(None)
                return

            names = ", ".join(s.name for s in down)
            if not announced:
                logger.warning(
                    "[Finder] %s unreachable — PAUSING searches; re-checking every "
                    "%.0fs and will resume automatically when it returns.",
                    names, probe_interval,
                )
                announced = True
                last_log = waited
            elif waited - last_log >= 300:   # heartbeat the wait every ~5 min
                logger.info("[Finder] still waiting for %s (%.0f min so far)…",
                            names, waited / 60)
                last_log = waited
            self._notify_wait(
                f"Paused — {names} offline; re-checking every "
                f"{int(probe_interval)}s, will resume automatically…"
            )

            if self._source_wait_max_min and waited >= self._source_wait_max_min * 60:
                logger.warning(
                    "[Finder] %s still down after %.0f min — proceeding without it.",
                    names, waited / 60,
                )
                self._notify_wait(None)
                return

            time.sleep(probe_interval)
            waited += probe_interval

    def _gather(self, query: str, must_contain: str = "") -> list[TorrentResult]:
        """
        Query EVERY enabled source CONCURRENTLY (one thread per source) and
        return the pooled, relevance-filtered results — WITHOUT applying the
        episode/season/blacklist constraints yet, so the same pool can be reused
        to satisfy many episodes (see find_tv_season_batch).

        Parallelism is safe here because each source is touched by exactly one
        thread, so a source's own (non-thread-safe) requests.Session / RSS cache
        is never accessed concurrently. The win is latency overlap: SceneTime
        and Jackett run at the same time instead of back to back.

        Results are cached per (query, must_contain) for a short TTL so repeated
        searches in the same run/retry loop don't re-hit the trackers. Blacklist
        and episode/season filtering happen later in _select, so a cached pool is
        always safe to reuse — the cache only skips the network round-trip.
        """
        cache_key = (query, must_contain)
        cached = self._gather_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self._gather_ttl:
            logger.debug("[Finder] Search cache hit for %r", query)
            return list(cached[1])

        # Don't fire a search into a dead aggregator — pause until it's back.
        self._wait_for_gating_sources()

        phrase = self._norm(must_contain) if must_contain else ""

        def _query(source: BaseSource):
            try:
                return source, (source.search(query) or [])
            except Exception as exc:
                logger.warning("[Finder] %s search error (non-fatal): %s", source.name, exc)
                return source, []

        if not self._sources:
            return []
        with ThreadPoolExecutor(max_workers=len(self._sources)) as ex:
            gathered = list(ex.map(_query, self._sources))

        pooled: list[TorrentResult] = []
        for source, results in gathered:
            if not results:
                continue
            if phrase:
                relevant = [r for r in results if self._phrase_matches(phrase, self._norm(r.name))]
                if not relevant:
                    logger.info("[Finder] %d results from %s but none match %r",
                                len(results), source.name, must_contain)
                    continue
                logger.info("[Finder] %d/%d results from %s match %r",
                            len(relevant), len(results), source.name, must_contain)
            else:
                relevant = list(results)
            pooled.extend(relevant)

        self._gather_cache[cache_key] = (time.time(), list(pooled))
        return pooled

    def _first_live_candidate(
        self, candidates: list[TorrentResult], min_seed: int, quiet: bool = False,
    ) -> Optional[TorrentResult]:
        """Walk the ranked candidates and return the first one whose swarm is
        actually alive RIGHT NOW.

        For each candidate (up to ``swarm_check_top_n``) we scrape its trackers
        for the current seeder count and skip releases the indexer claimed were
        seeded but are in fact dead. A candidate is accepted when its live seeder
        count meets ``min_seed``.

        When a release CAN'T be live-verified (no magnet to scrape — typical of
        private-tracker .torrent results like SceneTime — or every tracker failed
        to answer), we fall back to the indexer's count but demand the HIGHER
        ``unverified_min_seeders`` bar, since that count is unconfirmed and often
        stale/inflated. This is what stops a SceneTime pack listed at "3 seeds"
        (really 0) from being queued.

        When the swarm check is disabled this degrades to the previous
        behaviour: the first candidate whose indexer count meets ``min_seed``.
        """
        if not candidates:
            return None

        if not self._verify_swarm or self._health is None:
            for c in candidates:
                if (c.seeders or 0) >= min_seed:
                    return c
            return None

        unverified_bar = max(min_seed, self._unverified_min_seeders)
        scrapes = 0
        for c in candidates:
            stale = c.seeders or 0

            # Can we live-verify this release? Only magnet-bearing ones, and only
            # while we still have scrape budget left for this selection.
            if not c.magnet or scrapes >= self._swarm_check_top_n:
                if stale >= unverified_bar:
                    return c
                if not quiet and stale >= min_seed:
                    logger.info(
                        "[Finder] Skipping unverifiable %r — %d indexer seed(s) is below the "
                        "unverified-confidence bar of %d (no magnet to confirm the swarm is alive)",
                        c.name, stale, unverified_bar,
                    )
                continue

            scrapes += 1
            health = self._health.check(c.magnet, stop_at=min_seed)
            if not health.checked:
                # No tracker answered — treat as unverifiable, same higher bar.
                if stale >= unverified_bar:
                    if not quiet:
                        logger.info(
                            "[Finder] Swarm scrape inconclusive for %r — indexer count %d clears the "
                            "unverified bar (%d), accepting", c.name, stale, unverified_bar,
                        )
                    return c
                if not quiet and stale >= min_seed:
                    logger.info(
                        "[Finder] Skipping %r — swarm scrape inconclusive and %d indexer seed(s) is "
                        "below the unverified bar of %d", c.name, stale, unverified_bar,
                    )
                continue

            if health.seeders >= min_seed:
                if not quiet and health.seeders != stale:
                    logger.info(
                        "[Finder] Live swarm check: %r has %d seeds now (indexer said %d)",
                        c.name, health.seeders, stale,
                    )
                c.seeders = health.seeders
                c.leechers = health.leechers or c.leechers
                return c

            if not quiet:
                logger.info(
                    "[Finder] Skipping %r — indexer claimed %d seeds but swarm scrape shows only %d (dead/dying)",
                    c.name, stale, health.seeders,
                )
        return None

    def _select(
        self, pooled: list[TorrentResult], media_type: str, *,
        season_pack: bool = False, blacklist: list[str] | None = None,
        require_se: tuple | None = None, require_season: int | None = None,
        query: str = "(pool)", quiet: bool = False,
    ) -> Optional[TorrentResult]:
        """
        Apply the episode/season/blacklist constraints to an already-gathered
        pool, rank the survivors once, and return the single best release (by
        quality + seeder health) regardless of which source it came from.

        require_se=(season, episode) restricts to the EXACT episode (without it a
        whole-season result set would let the ranker pick the highest-seeded
        WRONG episode). require_season restricts a season-pack search to the
        right season. quiet=True downgrades the "nothing found" log lines to
        debug — used by the batch path, where most episodes are expected to be
        absent from a single season-wide pool.
        """
        if not pooled:
            (logger.debug if quiet else logger.warning)(
                "[Finder] No relevant results from any source for: %r", query)
            return None

        relevant = list(pooled)

        # ── Exact-episode filter ───────────────────────────────────────────
        if require_se:
            s_num, e_num = require_se
            se_re = re.compile(
                rf"(?:s0*{s_num}[\s._-]*e0*{e_num}|{s_num}x0*{e_num})(?![0-9])",
                re.IGNORECASE,
            )
            pre = len(relevant)
            relevant = [r for r in relevant if se_re.search(r.name)]
            if len(relevant) < pre and not quiet:
                logger.info("[Finder] kept %d/%d after exact-episode filter", len(relevant), pre)

        # ── Exact-season filter (season packs only) ────────────────────────
        if require_season is not None:
            season_re = re.compile(
                rf"(?:s0*{require_season}(?![0-9e])|season[\s._-]*0*{require_season}(?![0-9]))",
                re.IGNORECASE,
            )
            pre = len(relevant)
            relevant = [r for r in relevant if season_re.search(r.name)]
            if len(relevant) < pre and not quiet:
                logger.info("[Finder] kept %d/%d after exact-season filter (S%02d)",
                            len(relevant), pre, require_season)

        # ── Blacklist filter — skip previously-corrupt releases ────────────
        if blacklist:
            bl_norm = [self._norm_bl(b) for b in blacklist]
            pre = len(relevant)
            relevant = [r for r in relevant if self._norm_bl(r.name) not in bl_norm]
            if len(relevant) < pre:
                logger.info("[Finder] Skipped %d blacklisted release(s)", pre - len(relevant))

        if not relevant:
            return None

        # Rank the COMBINED pool once — best seeder-tier/quality wins globally.
        candidates = self._filter_and_rank(relevant, media_type, season_pack=season_pack)
        min_seed = self._q.get("min_seeders", 3)

        best = self._first_live_candidate(candidates, min_seed, quiet=quiet)
        if best:
            logger.info(
                "[Finder] Best of %d pooled result(s): %r (%.2f GB, %d seeds, via %s)",
                len(relevant), best.name, best.size_gb or 0.0, best.seeders, best.source_name,
            )
            return best

        # ── Last-resort resolution fallback ────────────────────────────────
        fb = self._q.get("fallback_resolutions") or []
        if fb and not season_pack:
            wider = self._filter_and_rank(relevant, media_type, season_pack=season_pack, extra_res=fb)
            best = self._first_live_candidate(wider, min_seed, quiet=quiet)
            if best:
                logger.info(
                    "[Finder] No seeded %s release — falling back to %s: %r (%.2f GB, %d seeds, via %s)",
                    self._q.get("allowed_resolutions", ["1080P"]), fb,
                    best.name, best.size_gb or 0.0, best.seeders, best.source_name,
                )
                return best

        # Last-resort: queue the top-ranked release even though nothing cleared
        # the seeder bar. OFF by default — returning a dead/unconfirmable torrent
        # just clogs qBittorrent at 0%. Opt in via queue_unseeded_last_resort.
        if candidates and self._queue_unseeded_last_resort:
            logger.warning(
                "[Finder] No release cleared the seeder bar — queuing best-ranked anyway "
                "(queue_unseeded_last_resort=on): %r (%d indexer seeds)",
                candidates[0].name, candidates[0].seeders or 0,
            )
            return candidates[0]

        if not quiet:
            if candidates:
                logger.info(
                    "[Finder] %d candidate(s) matched but none could be confirmed seeded "
                    "(need %d live, or %d unverified) — treating as no working release for: %r",
                    len(candidates), min_seed, max(min_seed, self._unverified_min_seeders), query,
                )
            else:
                logger.info("[Finder] %d pooled result(s) but all failed quality filter", len(relevant))
                logger.warning("[Finder] No passing results found for: %r", query)
        return None

    def _find(
        self, query: str, media_type: str, must_contain: str = "",
        season_pack: bool = False, blacklist: list[str] | None = None,
        require_se: tuple | None = None, require_season: int | None = None,
    ) -> Optional[TorrentResult]:
        """Gather (parallel, all sources) then select the single best release."""
        pooled = self._gather(query, must_contain)
        return self._select(
            pooled, media_type, season_pack=season_pack, blacklist=blacklist,
            require_se=require_se, require_season=require_season, query=query,
        )

    def find_tv_season_batch(
        self, show_name: str, season: int, episodes: list[int],
        blacklist_map: Optional[dict[int, list[str]]] = None,
    ) -> dict[int, TorrentResult]:
        """
        Resolve as many of `episodes` as possible from a SINGLE season-wide
        search per source, instead of one network search per episode. Returns
        {episode_number: TorrentResult} for every episode whose release was
        present in the pooled season results; episodes not found are simply
        absent, and the caller falls back to a per-episode find_tv() for those.

        This collapses N slow per-episode searches into one per season — the
        common case for a show with many gaps in a season (e.g. SpongeBob),
        where it cuts the search phase dramatically.
        """
        blacklist_map = blacklist_map or {}
        variants = self._title_variants(show_name)
        pooled: list[TorrentResult] = []
        used_title = show_name
        for title in variants:
            query = f"{title} S{season:02d}"
            logger.info(
                "[Finder] Season batch search: %r (%d episode(s) wanted)", query, len(episodes)
            )
            pooled = self._gather(query, must_contain=title)
            if pooled:
                used_title = title
                break

        out: dict[int, TorrentResult] = {}
        if not pooled:
            return out
        for ep in episodes:
            best = self._select(
                pooled, "tv", require_se=(season, ep),
                blacklist=blacklist_map.get(ep),
                query=f"{used_title} S{season:02d}E{ep:02d} (batch)", quiet=True,
            )
            if best:
                out[ep] = best
        if out:
            logger.info(
                "[Finder] Season batch resolved %d/%d episode(s) from one search: %s",
                len(out), len(episodes),
                ", ".join(f"E{e:02d}" for e in sorted(out)),
            )
        return out

    @staticmethod
    def _extract_group(release_name: str) -> Optional[str]:
        """Return the scene group suffix (uppercased) from a release name, or None.

        Handles both the canonical "-GROUP" form and the " GROUP"/".GROUP" form
        that indexers like Jackett produce when they normalise hyphens to
        spaces (which previously let known-bad groups such as NTb slip past the
        reliability block). The space/dot fallback only fires on names that
        clearly look like scene releases, so plain title words aren't misread.
        """
        if not release_name:
            return None
        stem = re.sub(r"\.(mkv|mp4|avi|mov|wmv|m4v|ts)$", "", release_name, flags=re.IGNORECASE)
        # Drop trailing tracker tags like [rartv] / [rarbg] / {tag}
        stem = re.sub(r"[\[\{][^\]\}]*[\]\}]\s*$", "", stem).strip()
        m = re.search(r"-([A-Za-z0-9]{2,16})$", stem)
        if m:
            return m.group(1).upper()
        looks_scene = (
            re.search(r"\b(480p|576p|720p|1080p|2160p|4k)\b", stem, re.I)
            and re.search(
                r"\b(x ?26[45]|h ?\.?26[45]|hevc|avc|xvid|divx|web[ .-]?dl|webrip|"
                r"bluray|hdtv|amzn|nf|dsnp|hmax|atvp|nick|max)\b",
                stem, re.I,
            )
        )
        if looks_scene:
            m2 = re.search(r"[ .]([A-Za-z][A-Za-z0-9]{1,15})$", stem)
            if m2:
                return m2.group(1).upper()
        return None

    def _filter_and_rank(
        self, results: list[TorrentResult], media_type: str, season_pack: bool = False,
        extra_res: Optional[list] = None,
    ) -> list[TorrentResult]:
        passing = []
        for r in results:
            # Season packs: skip single-episode results (must NOT have SxxExx pattern)
            if season_pack:
                import re as _re
                if _re.search(r"[Ss]\d{1,2}[Ee]\d{1,2}", r.name):
                    continue
                # Season packs are much larger — use movie-style size limits
                ok, reason = self.passes_filter(r.name, r.size_gb, "movie", extra_res=extra_res)
            else:
                ok, reason = self.passes_filter(r.name, r.size_gb, media_type, extra_res=extra_res)
            if not ok:
                logger.info("[Finder] Skipped %r — %s", r.name, reason)
                continue

            # ── Adaptive group reliability filter ─────────────────────────────
            # If this group has produced N+ corrupt downloads, exclude it.
            grp = self._extract_group(r.name)
            if (grp and grp not in self._never_block
                    and self._group_failures.get(grp, 0) >= self.GROUP_BLOCK_THRESHOLD):
                logger.info(
                    "[Finder] Skipped %r — release group %s has %d prior validation failure(s) (auto-blocked)",
                    r.name, grp, self._group_failures[grp],
                )
                continue

            passing.append(r)

        deprio_static  = [g.upper() for g in self._q.get("deprioritized_groups", [])]

        def _seed_tier(seeders: int) -> int:
            """Bucket seeders so swarm HEALTH dominates ranking, but among
            similarly-healthy torrents resolution/codec can still decide.
            Lower tier = healthier swarm = preferred.
            The top bucket is deliberately wide (20+) so a healthy 1080p and a
            healthy 720p land in the same tier and resolution then breaks the
            tie — while any dead/near-dead torrent (tier 3-4) always loses."""
            if seeders >= 20:
                return 0  # healthy — resolution preference decides among these
            if seeders >= 10:
                return 1
            if seeders >= 3:
                return 2
            if seeders >= 1:
                return 3  # marginal — only if nothing better exists
            return 4      # zero-seed — dead/partial, last resort

        pref_res = str(self._q.get("resolution", "1080p")).upper()

        def _res_tier(name_up: str) -> int:
            """Resolution preference (lower = better). Preferred res from config
            wins; otherwise 1080p > 720p > anything else."""
            if pref_res in name_up:
                return 0
            if "1080P" in name_up:
                return 1
            if "720P" in name_up:
                return 2
            return 3

        def _rank_score(r: TorrentResult) -> tuple:
            n = r.name.upper()
            grp = self._extract_group(r.name)
            # PROPER always wins
            proper_score = 0 if "PROPER" in n else 1
            # Adaptive group-failure penalty (placed early so unreliable
            # uploaders are pushed behind even unknown groups).
            failures = self._group_failures.get(grp, 0) if grp else 0
            adaptive_penalty = 1 if failures >= self.GROUP_DEPRIO_THRESHOLD else 0
            # Static deprioritised groups (config: MeGusta, YIFY, YTS, …)
            is_deprio = any(g in n for g in deprio_static)
            deprio_score = 1 if is_deprio else 0
            # Seeder health tier — a well-seeded torrent is far more likely to
            # download completely and uncorrupted, so this now outranks codec /
            # source preference. Only PROPER + release-group reliability (both
            # stronger integrity signals) sit above it.
            seed_tier = _seed_tier(r.seeders or 0)
            # Resolution preference — ranked just below swarm health so a healthy
            # 720p beats a DEAD 1080p, but a healthy 1080p beats a healthy 720p.
            res_tier = _res_tier(n)
            # Prefer H264/AVC (most compatible) over HEVC/x265
            is_h264 = any(t in n for t in ("H264", "H.264", "X264", "AVC"))
            is_hevc = any(t in n for t in ("HEVC", "H265", "H.265", "X265"))
            codec_score = 0 if is_h264 else (1 if is_hevc else 2)
            # Prefer WEB-DL / WEB over other sources
            is_web = "WEB" in n
            source_score = 0 if is_web else 1
            # Among otherwise-identical releases of the SAME content (the search
            # is already constrained to one episode / season), the larger file
            # usually carries the higher bitrate / better encode. Bucketed to
            # 0.5 GB so only a MEANINGFULLY bigger release wins, and ranked below
            # codec/source so it never overrides those. The file-size GATE in
            # passes_filter already bounds this to a sane min/max, so this can't
            # pull in a bloated remux or an implausibly tiny upscale.
            size_bucket = -int((r.size_gb or 0) / 0.5)
            return (
                proper_score,
                adaptive_penalty,
                deprio_score,
                seed_tier,
                res_tier,
                codec_score,
                source_score,
                size_bucket,
                -r.seeders,
            )

        passing.sort(key=_rank_score)
        return passing

    # ------------------------------------------------------------------
    # Quality filter
    # ------------------------------------------------------------------

    def passes_filter(
        self,
        release_name: str,
        size_gb: Optional[float],
        media_type: str,
        extra_res: Optional[list] = None,
    ) -> tuple[bool, str]:
        """
        Run the full decision tree from the spec.
        Returns (True, "OK") or (False, reason_string).

        extra_res: additional resolutions to accept on top of the configured
        allowed_resolutions (used for the last-resort 720p fallback pass).
        """
        name_up = release_name.upper()

        # 1. Resolution — accept any allowed resolution (default 1080p only).
        # The configured set is normally just 1080p; extra_res lets the caller
        # widen it (e.g. add 720p) only as a last-resort fallback when no seeded
        # 1080p release exists.
        allowed_res = [r.upper() for r in self._q.get(
            "allowed_resolutions", ["2160P", "1080P", "720P"]
        )]
        if extra_res:
            allowed_res = allowed_res + [r.upper() for r in extra_res]
        if allowed_res and not any(r in name_up for r in allowed_res):
            return False, f"Resolution not in {allowed_res}"

        # 1b. Foreign-language / fan-sub / hardcoded-sub releases. The library is
        # English, so reject CJK/Hangul titles, fan-sub group tags and hardcoded
        # subtitle markers. (These slipped in via the 720p fallback.)
        if re.search(r"[\u3000-\u9fff\uac00-\ud7af\u3040-\u30ff]", release_name):
            return False, "Foreign-language release (CJK/Hangul characters)"
        for tag in ("VOSTFR", "VOSTA", "SUBFRENCH", "KORSUB", "HC-SUB", "HARDSUB"):
            if tag in name_up:
                return False, f"Foreign/hardsub release tag: {tag}"
        if re.search(r"\bHC\b", name_up):
            return False, "Hardcoded subs (HC)"

        # 2. Blocked sources (CAM, TS, etc.)
        for blocked in self._q.get("blocked_sources", []):
            # Match as a word boundary to avoid false positives
            if re.search(r"\b" + re.escape(blocked.upper()) + r"\b", name_up):
                return False, f"Blocked source: {blocked}"

        # 2b. Blocked release groups (hard block — e.g. known RAR-only scene groups)
        # Use prefix match so "SuccessfulCrab" also catches "SuccessfulCrabNew" etc.
        for grp in self._q.get("blocked_groups", []):
            if re.search(r"[-.]" + re.escape(grp.upper()), name_up):
                return False, f"Blocked group: {grp}"

        # 3. Allowed source must be present (only hard-block if require_source_tag: true)
        allowed       = self._q.get("allowed_sources", [])
        require_source = self._q.get("require_source_tag", False)
        if allowed and require_source and not any(
            re.search(r"\b" + re.escape(s.upper()) + r"\b", name_up)
            for s in allowed
        ):
            return False, "No recognised source tag (need BluRay/WEB-DL/WEBRip/HDTV)"

        # 4. Video codec
        preferred_video = self._q.get("preferred_video", [])
        require_video   = self._q.get("require_video_codec", False)
        if preferred_video and require_video and not any(
            re.search(r"\b" + re.escape(v.upper()) + r"\b", name_up)
            for v in preferred_video
        ):
            return False, "No valid video codec (x264/x265/HEVC/H264/H265)"

        # 5. Audio — skip bad tags
        for tag in self._q.get("skip_audio_tags", []):
            if tag.upper() in name_up:
                return False, f"Bad audio tag: {tag}"

        # 6. Audio — must have valid tag
        preferred_audio = self._q.get("preferred_audio", [])
        require_audio   = self._q.get("require_audio_codec", False)
        if preferred_audio and require_audio and not any(a.upper() in name_up for a in preferred_audio):
            return False, "No valid audio tag (DTS/AC3/AAC/TrueHD/EAC3/DD/DDP)"

        # 7. Trusted release groups (only a hard gate if require_trusted_group is true)
        trusted = self._q.get("trusted_groups", [])
        require_group = self._q.get("require_trusted_group", False)
        if trusted and require_group:
            group_match = any(
                re.search(r"[-.]" + re.escape(g.upper()) + r"(?:\b|$)", name_up)
                for g in trusted
            )
            if not group_match:
                return False, "Release group not in whitelist"

        # 8. REENC (skip bad re-encodes, but allow PROPER)
        if self._q.get("skip_reenc", True):
            if "REENC" in name_up and "PROPER" not in name_up:
                return False, "Re-encode (REENC)"

        # 9. File size
        if size_gb is not None:
            if media_type == "tv":
                min_s = self._q.get("min_size_episode_gb", 0.5)
                max_s = self._q.get("max_size_episode_gb", 4.0)
            else:
                min_s = self._q.get("min_size_movie_gb", 2.0)
                max_s = self._q.get("max_size_movie_gb", 20.0)
            if size_gb < min_s or size_gb > max_s:
                return False, f"File size {size_gb:.2f} GB outside [{min_s}, {max_s}]"

        # 10. Minimum seeders
        return True, "OK"

    def audio_rank(self, release_name: str) -> int:
        """Higher = better audio. Used for tie-breaking."""
        name_up = release_name.upper()
        order = self._q.get("preferred_audio", ["DTS", "TRUEHD", "AC3", "EAC3", "AAC"])
        for i, codec in enumerate(order):
            if codec.upper() in name_up:
                return len(order) - i
        return 0
