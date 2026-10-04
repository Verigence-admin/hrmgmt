"""The street address for a position, from Google, printed on the attendance photo.

Optional and best effort: with no key, or if Google does not answer, the photo carries the
coordinates only and the record says so. One request with a short timeout and no retry; the key is
never logged."""

from __future__ import annotations

from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

_URL = "https://maps.googleapis.com/maps/api/geocode/json"


class ReverseGeocoder(Protocol):
    def address(self, latitude: float, longitude: float) -> str | None: ...


class GoogleReverseGeocoder:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 4.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def address(self, latitude: float, longitude: float) -> str | None:
        try:
            response = self._client.get(
                _URL,
                params={
                    "latlng": f"{latitude:.6f},{longitude:.6f}",
                    "key": self._key,
                    "region": "in",
                },
            )
            if response.status_code != 200:
                logger.warning("hr_reverse_geocode_failed", http_status=response.status_code)
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("hr_reverse_geocode_failed", reason="unreachable")
            return None
        if body.get("status") != "OK" or not body.get("results"):
            logger.warning("hr_reverse_geocode_failed", google_status=str(body.get("status")))
            return None
        value = body["results"][0].get("formatted_address")
        return value if isinstance(value, str) and value.strip() else None
