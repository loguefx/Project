from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TorrentResult:
    name: str
    magnet: Optional[str] = None
    torrent_url: Optional[str] = None
    size_gb: Optional[float] = None
    seeders: int = 0
    leechers: int = 0
    source_name: str = ""
    info_url: Optional[str] = None

    def __repr__(self):
        return (
            f"TorrentResult(name={self.name!r}, size_gb={self.size_gb}, "
            f"seeders={self.seeders}, source={self.source_name!r})"
        )


class BaseSource(ABC):
    """Abstract base class that all torrent sources must implement."""

    # When True, the finder PAUSES all searching while this source is
    # unreachable (instead of silently degrading to the remaining sources) and
    # resumes automatically once it returns. Sources that are merely optional
    # leave this False. Jackett/Prowlarr (the aggregator that backs most
    # indexers) sets it True.
    gate_when_down: bool = False

    def health_check(self) -> bool:
        """Lightweight reachability probe. Default: always healthy.

        Sources whose outage should pause the search pipeline override this to
        return False when the backing service can't be reached."""
        return True

    def __init__(self, config: dict):
        self.config = config
        self.name = config.get("name", "Unknown")
        self.base_url = config.get("url", "").rstrip("/")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.rss_feed = config.get("rss_feed", "")
        self._session_valid = False

    @abstractmethod
    def login(self) -> bool:
        """Authenticate and store session. Returns True on success."""

    @abstractmethod
    def search(self, query: str) -> list[TorrentResult]:
        """Search for a query string. Returns a list of TorrentResult."""

    def ensure_logged_in(self) -> bool:
        if not self._session_valid:
            return self.login()
        return True

    def get_download_headers(self) -> dict:
        """
        Return HTTP headers (including session cookies) needed to download
        a .torrent file from this source. Override in subclasses that use
        cookie-based auth.
        """
        return {}

    def parse_size_to_gb(self, size_str: str) -> Optional[float]:
        """Convert a human-readable size string like '1.45 GB' or '892 MiB' to float GB."""
        if not size_str:
            return None
        size_str = size_str.strip().upper().replace(",", "")
        try:
            for unit, factor in [("TIB", 1024), ("GIB", 1), ("MIB", 1 / 1024),
                                  ("KIB", 1 / (1024 ** 2)), ("TB", 1000),
                                  ("GB", 1), ("MB", 1 / 1000), ("KB", 1 / (1000 ** 2))]:
                if unit in size_str:
                    value = float(size_str.replace(unit, "").strip())
                    return round(value * factor, 4)
        except (ValueError, AttributeError):
            pass
        return None
