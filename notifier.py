"""
Notification dispatcher — Discord webhook and/or Ntfy push.
Sends rich Discord embeds with batch scan summaries.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Discord brand colour
_DISCORD_BLURPLE = 0x5865F2
_GREEN           = 0x57F287
_YELLOW          = 0xFEE75C
_RED             = 0xED4245
_CYAN            = 0x00B0F4


class Notifier:
    def __init__(self, discord_webhook: str = "", ntfy_topic: str = ""):
        self._discord = discord_webhook.strip()
        self._ntfy    = ntfy_topic.strip()

    def _discord_ok(self) -> bool:
        return bool(self._discord and "your_webhook" not in self._discord)

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    def _send_discord(self, payload: dict) -> None:
        """Send a raw Discord webhook payload (supports embeds, content, etc.)."""
        if not self._discord_ok():
            return
        try:
            resp = requests.post(self._discord, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("[Notifier] Discord send failed: %s", exc)

    def _send_discord_components(self, payload: dict) -> None:
        """Send a Discord webhook payload that includes message `components`
        (e.g. link buttons). A plain channel webhook only honours components when
        the URL carries ``?with_components=true`` and the buttons are
        non-interactive link buttons (style 5). On failure we retry with embeds
        only so the notification still lands."""
        if not self._discord_ok():
            return
        sep = "&" if "?" in self._discord else "?"
        url = f"{self._discord}{sep}with_components=true"
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("[Notifier] Discord buttons send failed (%s) — retrying without buttons", exc)
            self._send_discord({"embeds": payload.get("embeds", [])})

    def _send_ntfy(self, title: str, message: str) -> None:
        if not self._ntfy or "your_ntfy" in self._ntfy:
            return
        url = f"https://ntfy.sh/{self._ntfy}"
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Jellyfin Downloader"
        try:
            resp = requests.post(
                url,
                data=message.encode("utf-8"),
                headers={"Title": safe_title, "Priority": "default",
                         "Content-Type": "text/plain; charset=utf-8"},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("[Notifier] Ntfy send failed: %s", exc)

    def notify(self, title: str, message: str, color: int = _DISCORD_BLURPLE) -> None:
        logger.info("[Notify] %s — %s", title, message)
        self._send_discord({"embeds": [{"title": title, "description": message, "color": color}]})
        self._send_ntfy(title, message)

    # ------------------------------------------------------------------
    # Rich scan summary  (main notification the user asked for)
    # ------------------------------------------------------------------

    def scan_summary(
        self,
        *,
        tv_added: dict[str, list[str]],       # {show_name: ["S01E01", ...]}
        tv_paths: dict[str, str] | None = None,      # {show_name: NAS show folder path}
        movies_added: list[str],               # ["Movie Title (2024)", ...]
        movie_paths: dict[str, str] | None = None,   # {movie_label: NAS movie folder path}
        tv_queued: dict[str, list[str]],       # kept for compat, unused
        not_found: dict[str, list[str]],       # {show_name: ["S01E01", ...]} or {"Movie (2024)": ["movie"]}
        duration_sec: float = 0,
    ) -> None:
        """
        Send ONE rich Discord embed summarising the entire scan.
        Groups TV episodes by show. Not-found items are grouped and explained.
        """
        total_eps    = sum(len(v) for v in tv_added.values())
        total_movies = len(movies_added)
        nf_shows     = {k: v for k, v in not_found.items() if v != ["movie"]}
        nf_movies    = [k for k, v in not_found.items() if v == ["movie"]]
        total_nf     = len(nf_shows) + len(nf_movies)
        nothing_new  = (total_eps + total_movies) == 0

        ts  = f"<t:{int(datetime.now(timezone.utc).timestamp())}:f>"
        dur = f"{math.ceil(duration_sec / 60)}m" if duration_sec >= 60 else f"{int(duration_sec)}s"

        fields: list[dict] = []

        # ── TV episodes queued ──────────────────────────────────────────
        if tv_added:
            for show, eps in sorted(tv_added.items()):
                ep_tags = " ".join(f"`{e}`" for e in sorted(eps))
                if len(ep_tags) > 800:
                    ep_tags = ep_tags[:800] + f"… (+{len(eps)} eps total)"
                path = (tv_paths or {}).get(show, "")
                path_line = f"\n`{path}`" if path else ""
                fields.append({"name": f"📺 {show}", "value": ep_tags + path_line, "inline": False})

        # ── Movies queued ───────────────────────────────────────────────
        if movies_added:
            movie_lines = "\n".join(
                f"🎬 **{m}**" + (f"\n`{(movie_paths or {}).get(m, '')}`" if (movie_paths or {}).get(m) else "")
                for m in sorted(movies_added)
            )
            fields.append({"name": "🎬 Movies Queued", "value": movie_lines[:900], "inline": False})

        # ── Not found on tracker ────────────────────────────────────────
        if nf_shows:
            lines = []
            for show, eps in sorted(nf_shows.items()):
                ep_str = " ".join(sorted(eps))
                lines.append(f"**{show}** — {ep_str}")
            nf_text = "\n".join(lines)
            if len(nf_text) > 900:
                nf_text = nf_text[:900] + "…"
            fields.append({
                "name":  "⚠️ Not Found on SceneTime",
                "value": nf_text + "\n*Consider adding Jackett/Prowlarr as a fallback tracker.*",
                "inline": False,
            })
        if nf_movies:
            mv_text = "\n".join(f"🎬 {m}" for m in nf_movies)
            fields.append({
                "name":  "⚠️ Movies Not Found",
                "value": mv_text[:900],
                "inline": False,
            })

        # ── Colour & description ────────────────────────────────────────
        if nothing_new and total_nf == 0:
            color   = 0x5c6370
            summary = "Everything is up to date — nothing new to download."
        elif nothing_new and total_nf > 0:
            color   = _YELLOW
            summary = f"Nothing queued — **{total_nf}** item(s) not found on SceneTime."
        else:
            color   = _GREEN
            parts   = []
            if total_eps:
                parts.append(f"**{total_eps}** episode(s) across **{len(tv_added)}** show(s)")
            if total_movies:
                parts.append(f"**{total_movies}** movie(s)")
            summary = "Queued " + " · ".join(parts) + "."
            if total_nf:
                summary += f" ⚠️ {total_nf} item(s) not found."

        embed = {
            "title":       "📥 Jellyfin Downloader — Scan Complete",
            "description": f"{summary}\n\n🕐 {ts}  ·  ⏱ {dur}",
            "color":       color,
            "fields":      fields[:25],
            "footer":      {"text": "Jellyfin Downloader"},
        }
        self._send_discord({"embeds": [embed]})

        # Ntfy plain-text
        ntfy_lines = [f"Scan complete ({dur}): {summary}"]
        for show, eps in tv_added.items():
            ntfy_lines.append(f"  {show}: {', '.join(sorted(eps))}")
        for m in movies_added:
            ntfy_lines.append(f"  Movie: {m}")
        if nf_shows:
            ntfy_lines.append("Not found:")
            for show, eps in nf_shows.items():
                ntfy_lines.append(f"  {show}: {' '.join(sorted(eps))}")
        self._send_ntfy("📥 Jellyfin Downloader", "\n".join(ntfy_lines))

    # ------------------------------------------------------------------
    # Completion notification (single torrent finished)
    # ------------------------------------------------------------------

    def download_complete(
        self,
        show_name: str,
        episodes: list[str],
        media_type: str = "tv",
        poster_url: str = "",
    ) -> None:
        """
        Called by CompletionWatcher when a torrent finishes downloading.
        Sends a compact notification for just that one item.
        """
        if media_type == "movie":
            embed = {
                "title":       "🎬 Movie Ready",
                "description": f"**{show_name}** has been added to your library.",
                "color":       _GREEN,
                "footer":      {"text": "Jellyfin Downloader"},
            }
        else:
            ep_tags = " ".join(f"`{e}`" for e in sorted(episodes))
            embed = {
                "title":       "📺 Download Complete",
                "description": f"**{show_name}**\n{ep_tags}",
                "color":       _GREEN,
                "footer":      {"text": "Jellyfin Downloader"},
            }
        if poster_url:
            embed["thumbnail"] = {"url": poster_url}

        self._send_discord({"embeds": [embed]})
        self._send_ntfy(
            "Download Complete",
            f"{show_name}: {', '.join(sorted(episodes))}" if episodes else show_name,
        )

    # ------------------------------------------------------------------
    # Corrupt-download review (interactive — link buttons to the web UI)
    # ------------------------------------------------------------------

    def download_review(
        self,
        *,
        broken: int,
        checked: int,
        items: list[str],
        web_url: str = "",
    ) -> None:
        """
        Notify that freshly-downloaded episode(s) failed validation and are
        waiting for the user's decision. When a public web URL is configured,
        attach link buttons that open the dashboard's review actions directly:

          🔁 Re-download — remove the corrupt file(s), blacklist the bad
                            release, and grab a DIFFERENT one.
          ✅ Keep files  — accept them as-is and stop retrying.
          🖥 Dashboard   — open the full review popup.

        Link buttons (style 5) are used because a plain channel webhook can send
        them but cannot receive interactive (custom_id) clicks — opening a URL
        on the LAN web UI is what makes them actionable without a bot.
        """
        body = "\n".join(items[:20]) if items else "—"
        if len(items) > 20:
            body += f"\n… (+{len(items) - 20} more)"

        embed = {
            "title":       "⚠️ Corrupt download flagged for review",
            "description": (
                f"**{broken}** of **{checked}** new download(s) failed validation.\n\n{body}"
            ),
            "color":       _YELLOW,
            "footer":      {"text": "Jellyfin Downloader"},
        }
        payload: dict = {"embeds": [embed]}

        base = (web_url or "").strip().rstrip("/")
        buttons = []
        if base.startswith("http://") or base.startswith("https://"):
            buttons = [
                {"type": 2, "style": 5, "label": "🔁 Re-download", "url": f"{base}/review/redownload"},
                {"type": 2, "style": 5, "label": "✅ Keep files",  "url": f"{base}/review/keep"},
                {"type": 2, "style": 5, "label": "🖥 Dashboard",   "url": f"{base}/"},
            ]

        if buttons:
            payload["components"] = [{"type": 1, "components": buttons}]
            self._send_discord_components(payload)
        else:
            self._send_discord(payload)

        self._send_ntfy(
            "Corrupt download flagged",
            f"{broken}/{checked} new download(s) failed validation — review in the dashboard.",
        )

    # ------------------------------------------------------------------
    # Typed helpers (kept for backward compatibility)
    # ------------------------------------------------------------------

    def episode_downloaded(
        self, show_name: str, season: int, episode: int, episode_title: str = ""
    ) -> None:
        # Individual episodes are batched into the end-of-run scan_summary — log only here
        ep_label = f"S{season:02d}E{episode:02d}"
        logger.info("[Notify] Queued (batched): %s %s", show_name, ep_label)

    def movie_downloaded(self, movie_title: str, year: Optional[int] = None) -> None:
        year_str = f" ({year})" if year else ""
        # Batched into end-of-run scan_summary — log only here
        logger.info("[Notify] Movie queued (batched): %s%s", movie_title, year_str)

    def season_filled(self, show_name: str, season: int, episode_count: int) -> None:
        # Batched into end-of-run scan_summary — log only here
        logger.info("[Notify] Season pack queued (batched): %s S%02d — %d eps", show_name, season, episode_count)

    def torrent_not_found(self, query: str) -> None:
        # Log only — individual not-found pings are batched into the end-of-run scan_summary
        logger.info("[Notify] Not found (batched): %s", query)

    def error(self, context: str, detail: str = "") -> None:
        msg = context
        if detail:
            msg += f"\n```{detail[:500]}```"
        self.notify(title="❌ Error", message=msg, color=_RED)

    def dry_run_would_download(
        self, media_type: str, name: str, result_name: str
    ) -> None:
        self.notify(
            title=f"[DRY RUN] Would download {media_type}",
            message=f"**{name}**\n→ `{result_name}`",
            color=_DISCORD_BLURPLE,
        )
