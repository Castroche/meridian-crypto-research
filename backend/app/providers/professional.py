from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..models import ProfessionalMetricsSnapshot


class ProfessionalDataProvider:
    """Vendor-neutral gateway for licensed ETF, on-chain and institutional data."""

    def __init__(self, client: httpx.AsyncClient, url: str | None, token: str | None = None):
        self.client = client
        self.url = url
        self.token = token

    async def fetch(self) -> dict[str, ProfessionalMetricsSnapshot]:
        if not self.url:
            return {}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = await self.client.get(self.url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("assets", [])
        snapshots = [ProfessionalMetricsSnapshot.model_validate(item) for item in items]
        now = datetime.now(timezone.utc)
        fresh = [
            item for item in snapshots
            if item.as_of.tzinfo is not None
            and now - timedelta(hours=72) <= item.as_of.astimezone(timezone.utc)
            and item.as_of.astimezone(timezone.utc) <= now + timedelta(minutes=5)
        ]
        return {item.symbol: item for item in fresh}
