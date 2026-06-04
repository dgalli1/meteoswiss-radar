"""Async HTTP client for the MeteoSwiss radar composite data.

The client is intentionally tiny — it knows the three URLs the Niederschlag
(Radar) page uses and exposes a typed accessor for each. The actual data
*shape* lives in :mod:`meteoswiss_radar.decoder`.

Usage (asyncio)::

    async with aiohttp.ClientSession() as session:
        client = MeteoSwissClient(session=session)
        versions = await client.fetch_versions()
        animation = await client.fetch_animation_manifest()
        # Then for each picture URL, fetch the per-time-step data
        for pic in animation.pictures:
            data = await client.fetch_radar_step(pic.url)

Usage (AppDaemon)::

    class MeteoSwissRadarApp(hass.Hass):
        async def initialize(self):
            self._client = MeteoSwissAppDaemonClient(self)
            await self._client.refresh_once()
            self.run_minutely(self._client.refresh_once, datetime.now())

The class is transport-agnostic: pass either an ``aiohttp.ClientSession`` or
any async callable that takes a URL string and returns parsed JSON. This
makes it trivial to mock in tests without a network round-trip.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Union

# --- Data types ---------------------------------------------------------------


class MeteoSwissError(RuntimeError):
    """Raised for any client-side failure: HTTP error, missing field, etc.

    We never propagate underlying library errors (aiohttp.ClientError, ...)
    directly so the AppDaemon layer can catch one exception type."""


@dataclasses.dataclass(frozen=True)
class Picture:
    """One time-step in the radar animation.

    Attributes
    ----------
    url
        Fully-qualified URL of the per-time-step JSON.
    type
        ``"measurement"`` for past radar composites, ``"forecast"`` for the
        INCA model prediction.
    timestamp
        Unix epoch seconds (UTC) for this time-step.
    label
        Human-readable label like ``"13:10"`` (the time-of-day shown in the
        web app's timeline).
    """

    url: str
    type: str
    timestamp: int
    label: str = ""


@dataclasses.dataclass(frozen=True)
class Timeline:
    """A parsed animation.json, split into past (measurement) and future (forecast).

    The two lists are contiguous when sorted by timestamp; the "now" point
    (if present) appears as the first entry of ``future`` and the last of
    ``past``.
    """

    past: tuple[Picture, ...]
    future: tuple[Picture, ...]
    legend: tuple[dict, ...]
    cities: tuple[dict, ...]

    @classmethod
    def from_pictures(
        cls,
        pictures: list[dict],
        now: Optional[int] = None,
        legend: Optional[list[dict]] = None,
        cities: Optional[list[dict]] = None,
    ) -> "Timeline":
        """Build a Timeline from the raw ``map_images[0].pictures`` list.

        ``now`` is the current epoch in seconds; if None, the current wall
        clock is used. Pictures with timestamp <= now go to ``past``, the
        rest to ``future``.
        """
        if now is None:
            now = int(datetime.now(timezone.utc).timestamp())
        # Build Picture objects and split
        converted = [
            Picture(
                # URLs are relative paths; the client handles prefixing when fetching
                url=p["radar_url"],
                type=p["data_type"],
                timestamp=int(p["timestamp"]),
                label=p.get("timepoint", ""),
            )
            for p in pictures
        ]
        # Split such that no picture appears in both buckets. Convention: the
        # entry at exactly `now` goes to "future" (it's the "now" snapshot
        # and the forecast starts there).
        past = tuple(p for p in converted if p.timestamp < now)
        future = tuple(p for p in converted if p.timestamp >= now)
        return cls(
            past=past,
            future=future,
            legend=tuple(legend or []),
            cities=tuple(cities or []),
        )


# --- Client --------------------------------------------------------------------


# Type alias for the transport: any async callable that returns parsed JSON
Transport = Callable[[str], Awaitable[Any]]


class MeteoSwissClient:
    """Async client for the public MeteoSwiss product JSON endpoints.

    Parameters
    ----------
    transport
        Async callable taking a URL and returning parsed JSON. Defaults to a
        aiohttp-backed transport if ``session`` is provided.
    session
        Optional ``aiohttp.ClientSession`` (used to build the default transport).
    base_url
        Base URL of the MeteoSwiss data host. Override for testing.
    max_concurrent
        Maximum number of in-flight HTTP requests at any time. Keeps us from
        hammering the upstream (be a good citizen).
    request_timeout_s
        Per-request timeout in seconds. The radar products are small (<250 KB
        gzipped) so 10 s is plenty.
    """

    DEFAULT_BASE_URL = "https://www.meteoschweiz.admin.ch"

    def __init__(
        self,
        transport: Optional[Transport] = None,
        session: Any = None,
        base_url: str = DEFAULT_BASE_URL,
        max_concurrent: int = 4,
        request_timeout_s: float = 15.0,
    ):
        if transport is not None:
            self._transport = transport
        elif session is not None:
            self._transport = self._make_aiohttp_transport(session, request_timeout_s)
        else:
            raise ValueError("Provide either transport= or session=")
        self._base_url = base_url.rstrip("/")
        self._sem = asyncio.Semaphore(max_concurrent)

    @staticmethod
    def _make_aiohttp_transport(session: Any, timeout: float) -> Transport:
        async def fetch(url: str) -> Any:
            import aiohttp  # imported lazily so the dep is optional
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    raise MeteoSwissError(f"GET {url} -> {resp.status}")
                return await resp.json()
        return fetch

    # -- low-level ----------------------------------------------------------------

    def _abs(self, url_or_path: str) -> str:
        """If the input is a relative path, prefix with the base URL."""
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            return url_or_path
        return f"{self._base_url}{url_or_path}"

    async def get_json(self, url: str) -> Any:
        """Fetch any URL through the (concurrency-bounded) transport."""
        async with self._sem:
            try:
                return await self._transport(self._abs(url))
            except MeteoSwissError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MeteoSwissError(f"GET {url} failed: {exc}") from exc

    # -- high-level ---------------------------------------------------------------

    async def fetch_versions(self) -> dict[str, str]:
        """``/product/output/versions.json`` — manifest of per-product versions.

        Returns the parsed dict. Keys are product slugs, values are timestamps
        like ``"20260604_1719"``."""
        return await self.get_json("/product/output/versions.json")

    async def fetch_animation_manifest(
        self, version: Optional[str] = None, locale: str = "de"
    ) -> Timeline:
        """Fetch the per-version ``animation.json`` and return a Timeline.

        If ``version`` is None, the latest version is fetched from the manifest
        first (one extra HTTP round-trip)."""
        if version is None:
            versions = await self.fetch_versions()
            try:
                version = versions["precipitation/animation"]
            except KeyError as exc:
                raise MeteoSwissError("versions.json missing 'precipitation/animation'") from exc
        url = (
            f"/product/output/precipitation/animation/version__{version}"
            f"/{locale}/animation.json"
        )
        data = await self.get_json(url)
        try:
            pictures = data["map_images"][0]["pictures"]
        except (KeyError, IndexError) as exc:
            raise MeteoSwissError(f"animation.json missing expected fields: {exc}") from exc
        return Timeline.from_pictures(
            pictures,
            legend=data.get("legend", []),
            cities=data.get("cities", []),
        )

    async def fetch_radar_step(self, url_or_path: str) -> dict:
        """Fetch one ``radar_rzc.<ts>.json`` (or ``rate_<ts>.json``).

        The response is the raw decoded JSON; pass it to the decoder
        to extract polygons and look up a point's intensity."""
        return await self.get_json(url_or_path)
