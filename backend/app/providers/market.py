from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from ..models import Candle, MarketSnapshot


ASSETS = {
    "BTC": {"pair": "BTCUSDT", "okx": "BTC-USDT", "coinbase": "BTC-USD", "coingecko": "bitcoin"},
    "ETH": {"pair": "ETHUSDT", "okx": "ETH-USDT", "coinbase": "ETH-USD", "coingecko": "ethereum"},
    "SOL": {"pair": "SOLUSDT", "okx": "SOL-USDT", "coinbase": "SOL-USD", "coingecko": "solana"},
}


class MarketProvider:
    """Collects spot, cross-source and derivatives snapshots from public APIs."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch_all(self) -> list[MarketSnapshot]:
        try:
            cg = await self._coingecko_markets()
        except Exception:
            cg = {}
        results = await asyncio.gather(
            *(self._asset_snapshot(symbol, cfg, cg.get(cfg["coingecko"], {})) for symbol, cfg in ASSETS.items()),
            return_exceptions=True,
        )
        snapshots: list[MarketSnapshot] = []
        failures: list[str] = []
        for symbol, result in zip(ASSETS, results):
            if isinstance(result, Exception):
                failures.append(f"{symbol}: {type(result).__name__} {result!r}")
            else:
                snapshots.append(result)
        if not snapshots:
            raise RuntimeError("all market providers failed: " + "; ".join(failures))
        return snapshots

    async def _coingecko_markets(self) -> dict[str, dict]:
        last_error = None
        for _ in range(2):
            try:
                response = await self.client.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "ids": "bitcoin,ethereum,solana",
                        "price_change_percentage": "24h",
                    },
                )
                response.raise_for_status()
                return {item["id"]: item for item in response.json()}
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(.4)
        raise last_error or RuntimeError("CoinGecko market data unavailable")

    async def _asset_snapshot(self, symbol: str, cfg: dict, cg: dict) -> MarketSnapshot:
        try:
            return await self._okx_snapshot(symbol, cfg, cg)
        except Exception:
            try:
                return await self._binance_snapshot(symbol, cfg, cg)
            except Exception:
                # Coinbase is the final publication fallback so scheduled reports
                # never depend on derivatives data being available.
                return await self._coinbase_snapshot(symbol, cfg, cg)

    async def _okx_snapshot(self, symbol: str, cfg: dict, cg: dict) -> MarketSnapshot:
        spot = cfg["okx"]
        swap = f"{spot}-SWAP"
        required = await asyncio.gather(
            self.client.get("https://www.okx.com/api/v5/market/candles", params={"instId": spot, "bar": "1H", "limit": 240}),
            self.client.get("https://www.okx.com/api/v5/market/candles", params={"instId": spot, "bar": "15m", "limit": 192}),
            self.client.get("https://www.okx.com/api/v5/market/candles", params={"instId": spot, "bar": "1D", "limit": 10}),
            self.client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": spot}),
        )
        for response in required:
            response.raise_for_status()
            if response.json().get("code") != "0":
                raise ValueError(response.text[:200])
        hourly = self._okx_candles(required[0].json().get("data") or [])
        candles_15m = self._okx_candles(required[1].json().get("data") or [])
        daily = self._okx_candles(required[2].json().get("data") or [])
        if len(hourly) < 72 or len(candles_15m) < 48:
            raise ValueError(f"insufficient OKX candles for {symbol}")
        ticker = (required[3].json().get("data") or [])[0]

        optional = await asyncio.gather(
            self.client.get("https://www.okx.com/api/v5/public/funding-rate-history", params={"instId": swap, "limit": 2}),
            self.client.get("https://www.okx.com/api/v5/public/open-interest", params={"instType": "SWAP", "instId": swap}),
            self.client.get("https://www.okx.com/api/v5/public/mark-price", params={"instType": "SWAP", "instId": swap}),
            self.client.get("https://www.okx.com/api/v5/market/index-tickers", params={"instId": spot}),
            self.client.get("https://api.bybit.com/v5/market/open-interest", params={"category": "linear", "symbol": cfg["pair"], "intervalTime": "1h", "limit": 2}),
            return_exceptions=True,
        )

        def okx_rows(index: int) -> list[dict]:
            response = optional[index]
            if not isinstance(response, httpx.Response) or not response.is_success:
                return []
            payload = response.json()
            return payload.get("data") or [] if payload.get("code") == "0" else []

        funding_rows, oi_rows, mark_rows, index_rows = (okx_rows(index) for index in range(4))
        funding = funding_rows[0] if funding_rows else {}
        oi = oi_rows[0] if oi_rows else {}
        mark = mark_rows[0] if mark_rows else {}
        index = index_rows[0] if index_rows else {}
        bybit_response = optional[4]
        bybit_rows = []
        if isinstance(bybit_response, httpx.Response) and bybit_response.is_success:
            bybit_rows = (bybit_response.json().get("result") or {}).get("list") or []

        price = float(ticker["last"])
        open_24h = float(ticker.get("open24h") or price)
        cg_price = float(cg["current_price"]) if cg.get("current_price") else None
        deviation = abs(price - cg_price) / price * 100 if cg_price else None
        mark_price = float(mark["markPx"]) if mark.get("markPx") else None
        index_price = float(index["idxPx"]) if index.get("idxPx") else None
        basis = (mark_price - index_price) / index_price * 100 if mark_price and index_price else None
        oi_change = None
        if len(bybit_rows) >= 2:
            current_oi, previous_oi = float(bybit_rows[0]["openInterest"]), float(bybit_rows[1]["openInterest"])
            oi_change = (current_oi / previous_oi - 1) * 100 if previous_oi else None
        derivative_times = [
            datetime.fromtimestamp(int(value) / 1000, timezone.utc)
            for value in (funding.get("fundingTime"), oi.get("ts")) if value
        ]
        quality_flags: list[str] = []
        if deviation is None:
            quality_flags.append("CoinGecko 交叉价格缺失")
        elif deviation > .5:
            quality_flags.append(f"跨源价格偏差 {deviation:.2f}%")
        if not funding_rows:
            quality_flags.append("OKX 资金费率暂时缺失")
        if not oi_rows:
            quality_flags.append("OKX 未平仓量暂时缺失")
        if not bybit_rows:
            quality_flags.append("Bybit 持仓变化交叉验证缺失")
        return MarketSnapshot(
            symbol=symbol,
            as_of=datetime.fromtimestamp(int(ticker["ts"]) / 1000, timezone.utc),
            price=price, change_24h_pct=(price / open_24h - 1) * 100 if open_24h else 0,
            volume_24h_usd=float(ticker.get("volCcy24h") or 0),
            market_cap_usd=float(cg["market_cap"]) if cg.get("market_cap") else None,
            fully_diluted_valuation_usd=float(cg["fully_diluted_valuation"]) if cg.get("fully_diluted_valuation") else None,
            circulating_supply=float(cg["circulating_supply"]) if cg.get("circulating_supply") else None,
            total_supply=float(cg["total_supply"]) if cg.get("total_supply") else None,
            max_supply=float(cg["max_supply"]) if cg.get("max_supply") else None,
            ath_change_pct=float(cg["ath_change_percentage"]) if cg.get("ath_change_percentage") is not None else None,
            comparison_price_usd=cg_price, comparison_as_of=cg.get("last_updated"),
            cross_source_deviation_pct=deviation,
            funding_rate=float(funding["fundingRate"]) if funding.get("fundingRate") else None,
            annualized_basis_pct=basis,
            open_interest_usd=float(oi["oiUsd"]) if oi.get("oiUsd") else None,
            open_interest_change_1h_pct=oi_change,
            derivatives_as_of=max(derivative_times) if derivative_times else None,
            candle_volume_unit=symbol, candles=hourly, candles_15m=candles_15m, daily_candles=daily,
            sources=["OKX Spot", "OKX Perpetual", "Bybit Open Interest", "CoinGecko"],
            quality_flags=quality_flags,
        )

    @staticmethod
    def _okx_candles(rows: list[list]) -> list[Candle]:
        return sorted([
            Candle(
                opened_at=datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc),
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),  # OKX spot base-asset quantity, like Binance and Coinbase
            )
            for row in rows
        ], key=lambda item: item.opened_at)

    async def _binance_snapshot(self, symbol: str, cfg: dict, cg: dict) -> MarketSnapshot:
        pair = cfg["pair"]
        klines_req = self.client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": pair, "interval": "1h", "limit": 240},
        )
        ticker_req = self.client.get(
            "https://api.binance.com/api/v3/ticker/24hr", params={"symbol": pair}
        )
        # Spot data is the publication-critical dependency. Optional derivatives
        # requests run only after spot has succeeded so transient rate limits do
        # not cancel the entire asset snapshot.
        klines_res, ticker_res = await asyncio.gather(klines_req, ticker_req)
        for response in (klines_res, ticker_res):
            response.raise_for_status()

        optional = await asyncio.gather(
            self.client.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": pair}),
            self.client.get("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": pair}),
            return_exceptions=True,
        )
        premium_res = optional[0] if isinstance(optional[0], httpx.Response) and optional[0].is_success else None
        oi_res = optional[1] if isinstance(optional[1], httpx.Response) and optional[1].is_success else None

        candles = [
            Candle(
                opened_at=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
            )
            for row in klines_res.json()
        ]
        ticker = ticker_res.json()
        premium = premium_res.json() if premium_res else {}
        oi = oi_res.json() if oi_res else {}
        price = float(ticker["lastPrice"])
        cg_price = float(cg["current_price"]) if cg.get("current_price") else None
        deviation = abs(price - cg_price) / price * 100 if cg_price else None
        index_price = float(premium["indexPrice"]) if premium.get("indexPrice") else None
        mark_price = float(premium["markPrice"]) if premium.get("markPrice") else price
        # A perpetual contract has no maturity date, so this is deliberately the
        # instantaneous mark/index premium, not a falsely annualized futures basis.
        basis = (mark_price - index_price) / index_price * 100 if index_price else None
        quality_flags: list[str] = []
        if deviation is None:
            quality_flags.append("CoinGecko 交叉价格缺失")
        elif deviation > 0.5:
            quality_flags.append(f"跨源价格偏差 {deviation:.2f}%")
        if premium_res is None:
            quality_flags.append("资金费率与基差暂时缺失")
        if oi_res is None:
            quality_flags.append("未平仓量暂时缺失")
        return MarketSnapshot(
            symbol=symbol,
            as_of=datetime.now(timezone.utc),
            price=price,
            change_24h_pct=float(ticker["priceChangePercent"]),
            volume_24h_usd=float(ticker["quoteVolume"]),
            market_cap_usd=float(cg["market_cap"]) if cg.get("market_cap") else None,
            fully_diluted_valuation_usd=float(cg["fully_diluted_valuation"]) if cg.get("fully_diluted_valuation") else None,
            circulating_supply=float(cg["circulating_supply"]) if cg.get("circulating_supply") else None,
            total_supply=float(cg["total_supply"]) if cg.get("total_supply") else None,
            max_supply=float(cg["max_supply"]) if cg.get("max_supply") else None,
            ath_change_pct=float(cg["ath_change_percentage"]) if cg.get("ath_change_percentage") is not None else None,
            comparison_price_usd=cg_price, comparison_as_of=cg.get("last_updated"),
            cross_source_deviation_pct=deviation,
            funding_rate=float(premium["lastFundingRate"]) if premium.get("lastFundingRate") else None,
            annualized_basis_pct=basis,
            open_interest_usd=float(oi["openInterest"]) * mark_price if oi.get("openInterest") else None,
            derivatives_as_of=datetime.now(timezone.utc) if premium_res or oi_res else None,
            candle_volume_unit=symbol, candles=candles,
            sources=["Binance Spot", "Binance USDⓈ-M Futures", "CoinGecko"],
            quality_flags=quality_flags,
        )

    async def _coinbase_snapshot(self, symbol: str, cfg: dict, cg: dict) -> MarketSnapshot:
        product = cfg["coinbase"]
        candles_req = self.client.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 3600},
            headers={"Accept": "application/json"},
        )
        stats_req = self.client.get(
            f"https://api.exchange.coinbase.com/products/{product}/stats",
            headers={"Accept": "application/json"},
        )
        intraday_req = self.client.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 900}, headers={"Accept": "application/json"},
        )
        candles_res, stats_res, intraday_res = await asyncio.gather(candles_req, stats_req, intraday_req)
        candles_res.raise_for_status(); stats_res.raise_for_status()
        rows = sorted(candles_res.json(), key=lambda row: row[0])[-240:]
        if len(rows) < 72:
            raise ValueError(f"insufficient Coinbase candles for {symbol}")
        candles = [
            Candle(
                opened_at=datetime.fromtimestamp(row[0], tz=timezone.utc),
                low=float(row[1]), high=float(row[2]), open=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
            )
            for row in rows
        ]
        candles_15m = []
        if intraday_res.is_success:
            candles_15m = [
                Candle(
                    opened_at=datetime.fromtimestamp(row[0], tz=timezone.utc),
                    low=float(row[1]), high=float(row[2]), open=float(row[3]),
                    close=float(row[4]), volume=float(row[5]),
                )
                for row in sorted(intraday_res.json(), key=lambda value: value[0])[-192:]
            ]
        stats = stats_res.json()
        price = float(stats["last"])
        open_24h = float(stats["open"])
        cg_price = float(cg["current_price"]) if cg.get("current_price") else None
        deviation = abs(price - cg_price) / price * 100 if cg_price else None
        change_24h = ((price / open_24h) - 1) * 100 if open_24h else float(cg.get("price_change_percentage_24h") or 0)
        quality_flags = ["Binance 在当前云区域不可用，已切换 Coinbase 现货行情", "资金费率、基差与未平仓量暂时缺失"]
        if deviation is None:
            quality_flags.append("CoinGecko 交叉价格缺失")
        elif deviation > .5:
            quality_flags.append(f"跨源价格偏差 {deviation:.2f}%")
        return MarketSnapshot(
            symbol=symbol, as_of=datetime.now(timezone.utc), price=price,
            change_24h_pct=change_24h, volume_24h_usd=float(stats["volume"]) * price,
            market_cap_usd=float(cg["market_cap"]) if cg.get("market_cap") else None,
            fully_diluted_valuation_usd=float(cg["fully_diluted_valuation"]) if cg.get("fully_diluted_valuation") else None,
            circulating_supply=float(cg["circulating_supply"]) if cg.get("circulating_supply") else None,
            total_supply=float(cg["total_supply"]) if cg.get("total_supply") else None,
            max_supply=float(cg["max_supply"]) if cg.get("max_supply") else None,
            ath_change_pct=float(cg["ath_change_percentage"]) if cg.get("ath_change_percentage") is not None else None,
            comparison_price_usd=cg_price, comparison_as_of=cg.get("last_updated"),
            cross_source_deviation_pct=deviation, candle_volume_unit=symbol, candles=candles, candles_15m=candles_15m,
            sources=["Coinbase Exchange", "CoinGecko"], quality_flags=quality_flags,
        )
