from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from ..models import OptionsSnapshot


def _weighted_average(rows: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for _, weight in rows)
    return sum(value * weight for value, weight in rows) / total_weight if total_weight else None


def _parse_instrument(name: str):
    # Deribit option convention: BTC-25DEC26-100000-C
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        expiry = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
        return expiry, float(parts[2]), parts[3]
    except (ValueError, TypeError):
        return None


class OptionsProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self) -> dict[str, OptionsSnapshot]:
        results = await asyncio.gather(*(self._currency(symbol) for symbol in ("BTC", "ETH")), return_exceptions=True)
        return {symbol: result for symbol, result in zip(("BTC", "ETH"), results) if isinstance(result, OptionsSnapshot)}

    async def _currency(self, symbol: str) -> OptionsSnapshot:
        response = await self.client.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": symbol, "kind": "option"},
        )
        response.raise_for_status()
        rows = response.json()["result"]
        now = datetime.now(timezone.utc)
        calls_oi = puts_oi = 0.0
        total_oi_usd = 0.0
        near_atm: list[tuple[float, float]] = []
        near_calls: list[tuple[float, float]] = []
        near_puts: list[tuple[float, float]] = []
        near_term: list[tuple[float, float]] = []
        far_term: list[tuple[float, float]] = []
        valid = 0
        for row in rows:
            parsed = _parse_instrument(row.get("instrument_name", ""))
            if not parsed:
                continue
            expiry, strike, option_type = parsed
            days = (expiry - now).total_seconds() / 86400
            if days <= 0:
                continue
            underlying = float(row.get("underlying_price") or 0)
            oi_coin = float(row.get("open_interest") or 0)
            iv = float(row.get("mark_iv") or 0)
            if underlying <= 0 or oi_coin < 0:
                continue
            oi_usd = oi_coin * underlying
            valid += 1
            total_oi_usd += oi_usd
            if option_type == "C": calls_oi += oi_usd
            elif option_type == "P": puts_oi += oi_usd
            if iv > 0 and 7 <= days <= 45 and abs(strike / underlying - 1) <= .10:
                near_atm.append((iv, max(oi_usd, 1)))
                (near_calls if option_type == "C" else near_puts).append((iv, max(oi_usd, 1)))
            if iv > 0 and 7 <= days <= 30:
                near_term.append((iv, max(oi_usd, 1)))
            if iv > 0 and 60 <= days <= 120:
                far_term.append((iv, max(oi_usd, 1)))
        call_iv, put_iv = _weighted_average(near_calls), _weighted_average(near_puts)
        near_iv, far_iv = _weighted_average(near_term), _weighted_average(far_term)
        return OptionsSnapshot(
            symbol=symbol, as_of=now, total_open_interest_usd=total_oi_usd,
            put_call_oi_ratio=puts_oi / calls_oi if calls_oi else 0,
            near_atm_iv_pct=_weighted_average(near_atm),
            near_call_put_iv_spread_pct=(call_iv - put_iv) if call_iv is not None and put_iv is not None else None,
            term_iv_slope_pct=(far_iv - near_iv) if far_iv is not None and near_iv is not None else None,
            contracts_count=valid, sources=["Deribit Options"],
        )
