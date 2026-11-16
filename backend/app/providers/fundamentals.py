from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from ..models import FundamentalSnapshot, MetricPoint


CHAINS = {"ETH": "Ethereum", "SOL": "Solana"}


class FundamentalProvider:
    """Keyless chain-level activity metrics with published DefiLlama methodology."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self) -> dict[str, FundamentalSnapshot]:
        stable_req = self.client.get("https://stablecoins.llama.fi/stablecoinchains")
        chain_reqs = [self._chain(symbol, chain) for symbol, chain in CHAINS.items()]
        stable_res, *results = await asyncio.gather(stable_req, *chain_reqs, self._bitcoin(), return_exceptions=True)
        stable_by_chain = {}
        if isinstance(stable_res, httpx.Response) and stable_res.is_success:
            stable_by_chain = {
                item["name"]: float(item.get("totalCirculatingUSD", {}).get("peggedUSD", 0))
                for item in stable_res.json()
            }
        snapshots = {}
        for (symbol, chain), values in zip(CHAINS.items(), results[:2]):
            if isinstance(values, Exception):
                continue
            tvl, tvl_change, fees_24h, fees_change, tvl_history, fees_history = values
            snapshots[symbol] = FundamentalSnapshot(
                symbol=symbol, as_of=datetime.now(timezone.utc), tvl_usd=tvl,
                tvl_change_7d_pct=tvl_change, fees_24h_usd=fees_24h,
                fees_change_1d_pct=fees_change,
                stablecoin_supply_usd=stable_by_chain.get(chain, 0),
                sources=["DefiLlama TVL", "DefiLlama Fees", "DefiLlama Stablecoins"],
                tvl_history=tvl_history, fees_history=fees_history,
            )
        bitcoin = results[-1]
        if not isinstance(bitcoin, Exception):
            hashrate, hashrate_change, difficulty, difficulty_change, fees, fees_change, histories = bitcoin
            snapshots["BTC"] = FundamentalSnapshot(
                symbol="BTC", as_of=datetime.now(timezone.utc),
                fees_24h_usd=fees, fees_change_1d_pct=fees_change,
                hashrate_eh_s=hashrate, hashrate_change_30d_pct=hashrate_change,
                difficulty=difficulty, difficulty_change_30d_pct=difficulty_change,
                sources=["Blockchain.com Charts"],
                hashrate_history=histories[0], difficulty_history=histories[1],
                network_fees_history=histories[2],
            )
        return snapshots

    async def _chain(self, symbol: str, chain: str):
        tvl_req = self.client.get(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
        fees_req = self.client.get(
            f"https://api.llama.fi/overview/fees/{chain}",
            params={"excludeTotalDataChart": "false", "excludeTotalDataChartBreakdown": "true", "dataType": "dailyFees"},
        )
        tvl_res, fees_res = await asyncio.gather(tvl_req, fees_req)
        tvl_res.raise_for_status(); fees_res.raise_for_status()
        history = [row for row in tvl_res.json() if row.get("tvl") is not None]
        if len(history) < 8:
            raise ValueError(f"insufficient {symbol} TVL history")
        current, previous = float(history[-1]["tvl"]), float(history[-8]["tvl"])
        change = (current / previous - 1) * 100 if previous else 0
        fees = fees_res.json()
        tvl_points = [
            MetricPoint(at=datetime.fromtimestamp(int(row["date"]), tz=timezone.utc), value=float(row["tvl"]))
            for row in history[-90:]
        ]
        fee_points = [
            MetricPoint(at=datetime.fromtimestamp(int(row[0]), tz=timezone.utc), value=float(row[1]))
            for row in (fees.get("totalDataChart") or [])[-90:]
            if isinstance(row, list) and len(row) >= 2 and row[1] is not None
        ]
        return current, change, float(fees.get("total24h") or 0), float(fees.get("change_1d") or 0), tvl_points, fee_points

    async def _bitcoin(self):
        base = "https://api.blockchain.info/charts"
        params = {"timespan": "30days", "format": "json"}
        responses = await asyncio.gather(
            self.client.get(f"{base}/hash-rate", params=params),
            self.client.get(f"{base}/difficulty", params=params),
            self.client.get(f"{base}/transaction-fees-usd", params=params),
        )
        for response in responses: response.raise_for_status()
        series = [response.json().get("values", []) for response in responses]
        if any(len(values) < 8 for values in series):
            raise ValueError("insufficient Bitcoin fundamental history")
        def change(values, lookback):
            latest, previous = float(values[-1]["y"]), float(values[-lookback]["y"])
            return latest, (latest / previous - 1) * 100 if previous else 0
        hashrate_th, hashrate_change = change(series[0], min(30, len(series[0])))
        difficulty, difficulty_change = change(series[1], min(30, len(series[1])))
        fees, fees_change = change(series[2], min(8, len(series[2])))
        histories = [
            [MetricPoint(at=datetime.fromtimestamp(int(row["x"]), tz=timezone.utc), value=float(row["y"]) / (1_000_000 if index == 0 else 1)) for row in values]
            for index, values in enumerate(series)
        ]
        return hashrate_th / 1_000_000, hashrate_change, difficulty, difficulty_change, fees, fees_change, histories
