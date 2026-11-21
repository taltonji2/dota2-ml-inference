"""Small OpenDota API client that respects the free-tier rate limits.

Free tier without a key: about 60 calls/minute and 2,000 calls/day. A parse
request costs 10 calls. The client reads the X-Rate-Limit-Remaining-* headers,
spaces calls out, and backs off on HTTP 429.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"


class DailyBudgetExceeded(RuntimeError):
    pass


@dataclass
class RateState:
    remaining_minute: int | None = None
    remaining_day: int | None = None
    calls_made: int = 0
    last_headers: dict[str, str] = field(default_factory=dict)


class OpenDotaClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        min_interval_s: float = 1.1,
        daily_budget: int = 1800,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENDOTA_API_KEY") or None
        self.min_interval_s = min_interval_s
        self.daily_budget = daily_budget
        self.rate = RateState()
        self._last_call = 0.0
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=30.0,
            transport=transport,
            headers={"User-Agent": "dota2-ml-inference/0.1"},
        )

    # -- low level -------------------------------------------------------
    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, cost: int = 1) -> Any:
        if self.rate.calls_made + cost > self.daily_budget:
            raise DailyBudgetExceeded(f"daily budget of {self.daily_budget} calls reached")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.api_key:
            params["api_key"] = self.api_key

        for attempt in range(4):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            if self.rate.remaining_minute is not None and self.rate.remaining_minute <= cost:
                log.info("minute quota nearly used, sleeping 60s")
                self._sleep(60)
            self._last_call = time.monotonic()
            resp = self._http.request(method, path, params=params)
            self._record(resp, cost)
            if resp.status_code == 429:
                backoff = 60 * (attempt + 1)
                log.warning("429 from OpenDota, sleeping %ss", backoff)
                self._sleep(backoff)
                continue
            if resp.status_code >= 500 and attempt < 3:
                self._sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def _record(self, resp: httpx.Response, cost: int) -> None:
        if resp.status_code not in (404, 429) and resp.status_code < 500:
            self.rate.calls_made += cost
        h = resp.headers
        self.rate.last_headers = {k: v for k, v in h.items() if k.lower().startswith("x-rate-limit")}
        if "x-rate-limit-remaining-minute" in h:
            self.rate.remaining_minute = int(h["x-rate-limit-remaining-minute"])
        if "x-rate-limit-remaining-day" in h:
            self.rate.remaining_day = int(h["x-rate-limit-remaining-day"])

    # -- endpoints -------------------------------------------------------
    def public_matches(
        self,
        *,
        min_rank: int | None = None,
        max_rank: int | None = None,
        less_than_match_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recent public matches (about 100 per page), filterable by average rank tier."""
        return self._request(
            "GET",
            "/publicMatches",
            {"min_rank": min_rank, "max_rank": max_rank, "less_than_match_id": less_than_match_id},
        )

    def match(self, match_id: int) -> dict[str, Any]:
        return self._request("GET", f"/matches/{match_id}")

    def request_parse(self, match_id: int) -> dict[str, Any]:
        """Ask OpenDota to fetch and parse the replay. Costs 10 calls."""
        return self._request("POST", f"/request/{match_id}", cost=10)

    def hero_names(self) -> dict[str, int]:
        """Map internal hero names (npc_dota_hero_axe) to hero ids."""
        heroes = self._request("GET", "/heroes")
        return {h["name"]: h["id"] for h in heroes}

    def close(self) -> None:
        self._http.close()


def is_parsed(match: dict[str, Any]) -> bool:
    """A match has time series once OpenDota has parsed its replay."""
    players = match.get("players") or []
    return bool(players) and players[0].get("gold_t") is not None
