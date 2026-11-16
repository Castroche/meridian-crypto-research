from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from ..models import (
    LiquidationAssetBreakdown,
    LiquidationSnapshot,
    MacroCalendarEvent,
    MarketAggregateSnapshot,
    MarketContextSnapshot,
    SentimentSnapshot,
)


ASSETS = ("BTC", "ETH", "SOL")


class MarketContextProvider:
    """Public-first aggregate, sentiment and macro context with licensed liquidation overrides."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        coinglass_api_key: str | None = None,
        professional_url: str | None = None,
        professional_token: str | None = None,
    ):
        self.client = client
        self.coinglass_api_key = coinglass_api_key
        self.professional_url = professional_url
        self.professional_token = professional_token

    async def fetch(self) -> MarketContextSnapshot:
        now = datetime.now(timezone.utc)
        aggregate, sentiment, liquidations, macro = await asyncio.gather(
            self._aggregate(), self._sentiment(), self._liquidations(), self._macro_calendar(),
            return_exceptions=True,
        )
        flags: list[str] = []
        if isinstance(aggregate, Exception):
            flags.append(f"全市场聚合数据失败：{type(aggregate).__name__}")
            aggregate = None
        if isinstance(sentiment, Exception):
            flags.append(f"恐慌贪婪指数失败：{type(sentiment).__name__}")
            sentiment = None
        if isinstance(liquidations, Exception):
            flags.append(f"爆仓数据失败：{type(liquidations).__name__}")
            liquidations = LiquidationSnapshot(
                as_of=now, coverage="UNAVAILABLE", source="未配置",
                note="未取得可核验的全市场或交易所样本爆仓数据。",
            )
        if isinstance(macro, Exception):
            flags.append(f"宏观日历失败：{type(macro).__name__}")
            macro = []
        return MarketContextSnapshot(
            as_of=now, aggregate=aggregate, sentiment=sentiment,
            liquidations=liquidations, macro_calendar=macro, quality_flags=flags,
        )

    async def _aggregate(self) -> MarketAggregateSnapshot:
        response = await self.client.get("https://api.coingecko.com/api/v3/global")
        response.raise_for_status()
        row = response.json()["data"]
        percentages = row.get("market_cap_percentage") or {}
        advancers = decliners = unchanged = universe = None
        try:
            breadth_response = await self.client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd", "order": "market_cap_desc", "per_page": 100,
                    "page": 1, "sparkline": "false", "price_change_percentage": "24h",
                },
            )
            breadth_response.raise_for_status()
            changes = [
                _float(item.get("price_change_percentage_24h"))
                for item in breadth_response.json() if isinstance(item, dict)
            ]
            changes = [value for value in changes if value is not None]
            universe = len(changes)
            advancers = sum(value > .05 for value in changes)
            decliners = sum(value < -.05 for value in changes)
            unchanged = universe - advancers - decliners
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return MarketAggregateSnapshot(
            as_of=datetime.now(timezone.utc),
            total_market_cap_usd=(row.get("total_market_cap") or {}).get("usd"),
            total_volume_24h_usd=(row.get("total_volume") or {}).get("usd"),
            market_cap_change_24h_pct=row.get("market_cap_change_percentage_24h_usd"),
            btc_dominance_pct=percentages.get("btc"), eth_dominance_pct=percentages.get("eth"),
            active_cryptocurrencies=row.get("active_cryptocurrencies"), source="CoinGecko Global",
            breadth_advancers=advancers, breadth_decliners=decliners,
            breadth_unchanged=unchanged, breadth_universe=universe,
        )

    async def _sentiment(self) -> SentimentSnapshot:
        response = await self.client.get("https://api.alternative.me/fng/", params={"limit": 2})
        response.raise_for_status()
        rows = response.json().get("data") or []
        if not rows:
            raise ValueError("empty Fear & Greed response")
        current = rows[0]
        previous = rows[1] if len(rows) > 1 else {}
        return SentimentSnapshot(
            as_of=datetime.fromtimestamp(int(current["timestamp"]), timezone.utc),
            value=int(current["value"]), classification=current.get("value_classification") or "UNAVAILABLE",
            previous_value=int(previous["value"]) if previous.get("value") is not None else None,
        )

    async def _liquidations(self) -> LiquidationSnapshot:
        gateway = await self._gateway_liquidations()
        if gateway is not None:
            return gateway
        if not self.coinglass_api_key:
            return LiquidationSnapshot(
                as_of=datetime.now(timezone.utc), coverage="UNAVAILABLE", source="公开交易所接口",
                note="未配置 CoinGlass 或专业数据网关；公开交易所实时流无法完整还原全市场 24H 爆仓总额。",
            )
        headers = {"CG-API-KEY": self.coinglass_api_key}

        async def fetch_symbol(symbol: str):
            response = await self.client.get(
                "https://open-api-v4.coinglass.com/api/futures/liquidation/exchange-list",
                params={"symbol": symbol, "range": "24h"}, headers=headers,
            )
            response.raise_for_status()
            rows = response.json().get("data") or []
            return symbol, next((item for item in rows if str(item.get("exchange", "")).lower() == "all"), rows[0] if rows else None)

        rows = await asyncio.gather(*(fetch_symbol(symbol) for symbol in ASSETS))
        assets: list[LiquidationAssetBreakdown] = []
        for symbol, row in rows:
            if not row:
                continue
            assets.append(LiquidationAssetBreakdown(
                symbol=symbol,
                total_usd_24h=_float(row.get("liquidation_usd")),
                long_usd_24h=_float(row.get("long_liquidation_usd")),
                short_usd_24h=_float(row.get("short_liquidation_usd")),
            ))
        total = sum(item.total_usd_24h or 0 for item in assets) or None
        longs = sum(item.long_usd_24h or 0 for item in assets) or None
        shorts = sum(item.short_usd_24h or 0 for item in assets) or None
        return LiquidationSnapshot(
            as_of=datetime.now(timezone.utc), coverage="MARKET_WIDE", total_usd_24h=total,
            long_usd_24h=longs, short_usd_24h=shorts, assets=assets,
            venues=["Aggregated exchanges"], source="CoinGlass API V4",
            note="BTC/ETH/SOL 跨交易所 24H 聚合值；不等于所有加密资产总额。",
        )

    async def _gateway_liquidations(self) -> LiquidationSnapshot | None:
        if not self.professional_url:
            return None
        headers = {"Authorization": f"Bearer {self.professional_token}"} if self.professional_token else {}
        try:
            response = await self.client.get(self.professional_url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            candidate = payload.get("market_context", {}).get("liquidations") or payload.get("liquidations")
            return LiquidationSnapshot.model_validate(candidate) if candidate else None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _macro_calendar(self) -> list[MacroCalendarEvent]:
        response = await self.client.get("https://www.bls.gov/schedule/news_release/bls.ics")
        response.raise_for_status()
        text = re.sub(r"\r?\n[ \t]", "", response.text)
        now = datetime.now(timezone.utc)
        lower, upper = now - timedelta(hours=24), now + timedelta(hours=96)
        events: list[MacroCalendarEvent] = []
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S):
            start_match = re.search(r"DTSTART(?:;TZID=([^:]+))?:(\d{8}T\d{6})", block)
            summary_match = re.search(r"SUMMARY:(.+)", block)
            if not start_match or not summary_match:
                continue
            zone = ZoneInfo("America/New_York" if (start_match.group(1) or "").upper() in {"US-EASTERN", "AMERICA/NEW_YORK"} else "UTC")
            scheduled = datetime.strptime(start_match.group(2), "%Y%m%dT%H%M%S").replace(tzinfo=zone).astimezone(timezone.utc)
            if not lower <= scheduled <= upper:
                continue
            name = summary_match.group(1).replace("\\,", ",").strip()
            lowered = name.lower()
            category = "NONFARM" if "employment situation" in lowered else "CPI" if "consumer price" in lowered else "LABOR" if any(term in lowered for term in ("employment", "job", "labor")) else "INFLATION" if "price" in lowered else "MACRO"
            events.append(MacroCalendarEvent(
                name=name, scheduled_at=scheduled, source="U.S. Bureau of Labor Statistics",
                url="https://www.bls.gov/schedule/news_release/bls.ics", category=category,
            ))
        return sorted(events, key=lambda item: item.scheduled_at)


def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
