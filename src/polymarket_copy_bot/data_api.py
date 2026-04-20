from __future__ import annotations

import os
from typing import Dict, Iterable, List

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from .models import Position


class DataApiError(RuntimeError):
    pass


class PolymarketDataApi:
    def __init__(self, timeout_seconds: int = 20):
        self.session = requests.Session()
        self.base = os.getenv("PM_DATA_API_BASE", "https://data-api.polymarket.com").rstrip("/")
        api_key = os.getenv("PM_DATA_API_KEY", "").strip()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout_seconds = timeout_seconds

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_exception_type((requests.RequestException, DataApiError)),
    )
    def get_positions(self, wallet: str) -> List[Position]:
        resp = self.session.get(
            f"{self.base}/positions",
            params={"user": wallet, "limit": 500, "sizeThreshold": 0},
            timeout=self.timeout_seconds,
        )
        if resp.status_code != 200:
            raise DataApiError(f"positions failed {resp.status_code}: {resp.text[:300]}")
        raw = resp.json()
        out: List[Position] = []
        for item in raw:
            out.append(
                Position(
                    wallet=wallet.lower(),
                    asset=str(item.get("asset", "")),
                    condition_id=str(item.get("conditionId", "")),
                    size=float(item.get("size") or 0.0),
                    avg_price=float(item.get("avgPrice") or 0.0),
                    cur_price=float(item.get("curPrice") or 0.0),
                    title=str(item.get("title") or ""),
                    slug=str(item.get("slug") or ""),
                    event_slug=str(item.get("eventSlug") or ""),
                    outcome=str(item.get("outcome") or ""),
                    opposite_asset=str(item.get("oppositeAsset") or ""),
                    end_date=str(item.get("endDate") or ""),
                    negative_risk=bool(item.get("negativeRisk") or False),
                )
            )
        return out

    def get_positions_many(self, wallets: Iterable[str]) -> Dict[str, List[Position]]:
        return {wallet.lower(): self.get_positions(wallet) for wallet in wallets}
