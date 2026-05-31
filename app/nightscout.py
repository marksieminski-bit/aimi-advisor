"""Nightscout API client. Supports token auth (?token=) and API-SECRET (SHA1 header)."""
import hashlib
import time
from datetime import datetime, timedelta, timezone

import requests


class NightscoutClient:
    def __init__(self, base_url: str, token: str | None = None, api_secret: str | None = None,
                 timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.api_secret = api_secret or None
        self.timeout = timeout

    def _headers(self):
        h = {"Accept": "application/json"}
        if self.api_secret:
            # Nightscout accepts the SHA1 hex digest of the API secret
            h["api-secret"] = hashlib.sha1(self.api_secret.encode()).hexdigest()
        return h

    def _params(self, extra: dict | None = None):
        p = dict(extra or {})
        if self.token:
            p["token"] = self.token
        return p

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.base_url}{path}"
        r = requests.get(url, headers=self._headers(), params=self._params(params),
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def test_connection(self) -> dict:
        """Returns status JSON or raises. Used to validate credentials."""
        return self._get("/api/v1/status.json")

    def get_entries(self, days: int = 15) -> list[dict]:
        """Fetch CGM entries (sgv) for the last `days`. Paginates to get full window."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_ms = int(since.timestamp() * 1000)
        all_entries: list[dict] = []
        # Nightscout caps count; loop backward using date filter
        # ~288 readings/day * days, fetch in chunks of 10000
        count = min(days * 320, 20000)
        batch = self._get(
            "/api/v1/entries/sgv.json",
            {"count": count, "find[date][$gte]": since_ms},
        )
        for e in batch:
            if e.get("sgv") is not None and e.get("date", 0) >= since_ms:
                all_entries.append(e)
        # Sort chronologically
        all_entries.sort(key=lambda x: x.get("date", 0))
        return all_entries

    def get_treatments(self, days: int = 15) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            return self._get(
                "/api/v1/treatments.json",
                {"count": 50000, "find[created_at][$gte]": since_iso},
            )
        except Exception:
            return []

    def get_devicestatus(self, days: int = 15) -> list[dict]:
        """AIMI/loop decisions: openaps.suggested/enacted, IOB, COB, predictions, reason."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            return self._get(
                "/api/v1/devicestatus.json",
                {"count": 50000, "find[created_at][$gte]": since_iso},
            )
        except Exception:
            return []

    def get_profile(self) -> dict | None:
        try:
            prof = self._get("/api/v1/profile.json")
            if isinstance(prof, list) and prof:
                return prof[0]
            return prof
        except Exception:
            return None
