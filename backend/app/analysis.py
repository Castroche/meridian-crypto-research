from __future__ import annotations

import math
from statistics import mean

from .models import DimensionAssessment, FundamentalSnapshot, MarketSnapshot, NewsItem, OptionsSnapshot, ProfessionalMetricsSnapshot


def sma(values: list[float], period: int) -> float:
    window = values[-period:]
    return mean(window) if window else 0.0


def rsi(values: list[float], period: int = 14) -> float:
    changes = [b - a for a, b in zip(values, values[1:])][-period:]
    if not changes:
        return 50.0
    gains = mean([max(c, 0) for c in changes])
    losses = mean([max(-c, 0) for c in changes])
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def atr(snapshot: MarketSnapshot, period: int = 14) -> float:
    candles = snapshot.candles[-period - 1:]
    ranges = []
    for prev, current in zip(candles, candles[1:]):
        ranges.append(max(current.high - current.low, abs(current.high - prev.close), abs(current.low - prev.close)))
    return mean(ranges) if ranges else snapshot.price * 0.02


def _clamp(value: float, low: int = -100, high: int = 100) -> int:
    return round(max(low, min(high, value)))


def technical_assessment(snapshot: MarketSnapshot) -> DimensionAssessment:
    closes = [c.close for c in snapshot.candles]
    volumes = [c.volume for c in snapshot.candles]
    ma20, ma50, current_rsi = sma(closes, 20), sma(closes, 50), rsi(closes)
    momentum_24h = (closes[-1] / closes[-25] - 1) * 100 if len(closes) >= 25 else snapshot.change_24h_pct
    volume_ratio = mean(volumes[-6:]) / max(mean(volumes[-30:-6]), 1e-9) if len(volumes) >= 30 else 1
    score = 0
    price_gap = snapshot.price / ma20 - 1 if ma20 else 0
    average_gap = ma20 / ma50 - 1 if ma50 else 0
    score += 28 if price_gap > .0015 else -28 if price_gap < -.0015 else 0
    score += 22 if average_gap > .001 else -22 if average_gap < -.001 else 0
    score += max(-25, min(25, momentum_24h * 5))
    score += -12 if current_rsi > 75 else 12 if current_rsi < 30 else 0
    score = _clamp(score)
    direction = "多周期结构偏强" if score >= 20 else "多周期结构偏弱" if score <= -20 else "价格处于结构均衡区"
    return DimensionAssessment(
        key="technical", label="技术面", score=score, coverage=95, headline=direction,
        evidence=[
            f"现价相对 SMA20：{(snapshot.price / ma20 - 1) * 100:+.2f}%",
            f"SMA20 相对 SMA50：{(ma20 / ma50 - 1) * 100:+.2f}%",
            f"RSI14：{current_rsi:.1f}；近 6 小时量比：{volume_ratio:.2f}",
        ],
    )


def flow_assessment(snapshot: MarketSnapshot, options: OptionsSnapshot | None = None, professional: ProfessionalMetricsSnapshot | None = None) -> DimensionAssessment:
    funding_pct = snapshot.funding_rate * 100 if snapshot.funding_rate is not None else None
    basis = snapshot.annualized_basis_pct
    score = snapshot.change_24h_pct * 5
    crowding = ((funding_pct is not None and abs(funding_pct) > 0.05)
                or (basis is not None and abs(basis) > 0.25))
    if funding_pct is not None:
        if funding_pct > 0.05:
            score -= 20
        elif funding_pct < -0.02:
            score += 12
    if basis is not None:
        if basis > 0.25:
            score -= 12
        elif basis < -0.10:
            score += 8
    evidence = [
        f"最近资金费率：{funding_pct:+.4f}%" if funding_pct is not None else "最近资金费率：N/A（数据不可用）",
        f"期货年化基差：{basis:+.3f}%" if basis is not None else "期货年化基差：N/A（数据不可用）",
        f"未平仓名义价值：${snapshot.open_interest_usd / 1e9:.2f}B" if snapshot.open_interest_usd is not None else "未平仓名义价值：N/A（数据不可用）",
    ]
    coverage = 15 + 18 * sum(value is not None for value in (funding_pct, basis, snapshot.open_interest_usd))
    if options is not None:
        coverage = min(88, coverage + 28)
        ratio = options.put_call_oi_ratio
        if ratio > 1.15: score -= 9
        elif ratio < .70: score += 6
        if options.term_iv_slope_pct is not None:
            if options.term_iv_slope_pct < -5: score -= 10
            elif options.term_iv_slope_pct > 5: score += 3
        if options.near_call_put_iv_spread_pct is not None:
            score += max(-8, min(8, options.near_call_put_iv_spread_pct * 1.5))
        evidence.extend([
            f"Deribit 期权名义持仓：${options.total_open_interest_usd / 1e9:.2f}B；Put/Call OI：{ratio:.2f}",
            f"7–45 日近 ATM IV：{options.near_atm_iv_pct:.1f}%" if options.near_atm_iv_pct is not None else "近月 ATM IV 数据不足",
            f"远月减近月 IV：{options.term_iv_slope_pct:+.1f} 个波动率点" if options.term_iv_slope_pct is not None else "IV 期限结构数据不足",
            "Call/Put IV 差基于近 ATM 合约，不等同于 25 Delta Risk Reversal。",
        ])
    else:
        evidence.append("期权数据尚未接入，资金面覆盖度已降级。")
    if professional is not None:
        available_professional = sum(value is not None for value in (
            professional.spot_etf_net_flow_usd, professional.exchange_netflow_usd,
            professional.stablecoin_supply_change_7d_pct, professional.options_25d_skew_pct,
            professional.cme_basis_annualized_pct,
        ))
        coverage = min(98, coverage + available_professional * 5)
        if professional.spot_etf_net_flow_usd is not None:
            etf_impact = professional.spot_etf_net_flow_usd / max(snapshot.volume_24h_usd, 1) * 400
            score += max(-20, min(20, etf_impact))
            evidence.append(f"现货 ETF 净流：${professional.spot_etf_net_flow_usd / 1e6:+.1f}M")
        if professional.exchange_netflow_usd is not None:
            exchange_impact = professional.exchange_netflow_usd / max(snapshot.volume_24h_usd, 1) * 300
            score -= max(-18, min(18, exchange_impact))
            evidence.append(f"交易所净流入：${professional.exchange_netflow_usd / 1e6:+.1f}M（正值代表流入交易所）")
        if professional.stablecoin_supply_change_7d_pct is not None:
            score += max(-10, min(10, professional.stablecoin_supply_change_7d_pct * 3))
            evidence.append(f"稳定币供给 7 日变化：{professional.stablecoin_supply_change_7d_pct:+.2f}%")
        if professional.options_25d_skew_pct is not None:
            score -= max(-10, min(10, professional.options_25d_skew_pct))
            evidence.append(f"25D Put–Call Skew：{professional.options_25d_skew_pct:+.2f} 波动率点")
        if professional.cme_basis_annualized_pct is not None:
            evidence.append(f"CME 年化基差：{professional.cme_basis_annualized_pct:+.2f}%")
    else:
        evidence.append("ETF、CME 与交易所净流需要配置专业数据网关。")
    headline = "杠杆与期权定价保持克制" if options is not None and not crowding else "杠杆定价保持克制" if not crowding else "衍生品拥挤度抬升"
    return DimensionAssessment(
        key="flow", label="资金面", score=_clamp(score), coverage=coverage, headline=headline,
        evidence=evidence,
    )


def news_assessment(symbol: str, news: list[NewsItem]) -> DimensionAssessment:
    relevant = sorted(
        (item for item in news if symbol in item.assets and item.category != "NOISE"
         and item.asset_relevance >= .5 and item.price_relevance >= .45 and item.causal_path),
        key=lambda item: (item.source_score, item.published_at), reverse=True,
    )
    if not relevant:
        return DimensionAssessment(key="news", label="新闻面", score=0, coverage=20, headline="未发现已核验的高影响事件", evidence=["过去 36 小时新闻源无可用事件。"])
    verified = [item for item in relevant if item.source_score >= 72]
    if not verified:
        return DimensionAssessment(
            key="news", label="新闻面", score=0, coverage=min(40, 20 + len(relevant) * 3),
            headline="没有达到引用门槛的高影响事件",
            evidence=[f"检索到 {len(relevant)} 条相关线索，但来源分均低于 72，未纳入方向判断。"],
        )
    weighted = sum(item.sentiment * item.source_score for item in verified) / max(sum(item.source_score for item in verified), 1)
    score = _clamp(weighted * 65)
    high_quality = sum(item.source_score >= 80 for item in verified)
    headline = "事件流形成正向催化" if score >= 18 else "事件流形成风险压力" if score <= -18 else "事件流未改变主导趋势"
    evidence = [f"{item.title}（{item.source}，来源分 {item.source_score}）" for item in verified[:3]]
    return DimensionAssessment(key="news", label="新闻面", score=score, coverage=min(85, 35 + high_quality * 15 + len(verified) * 4), headline=headline, evidence=evidence)


def fundamental_assessment(snapshot: MarketSnapshot, chain: FundamentalSnapshot | None = None) -> DimensionAssessment:
    if chain is not None and snapshot.symbol == "BTC" and chain.hashrate_eh_s is not None:
        hash_change = chain.hashrate_change_30d_pct
        diff_change = chain.difficulty_change_30d_pct
        fee_change = chain.fees_change_1d_pct
        score = _clamp(
            (max(-35, min(35, hash_change * 2)) if hash_change is not None else 0)
            + (max(-20, min(20, diff_change)) if diff_change is not None else 0)
            + (max(-10, min(10, fee_change * .15)) if fee_change is not None else 0)
        )
        headline = "网络安全预算与算力结构增强" if score >= 18 else "网络安全预算与算力结构承压" if score <= -18 else "网络安全与矿工活动保持稳定"
        return DimensionAssessment(
            key="fundamental", label="基本面", score=score, coverage=70, headline=headline,
            evidence=[
                f"估算算力：{chain.hashrate_eh_s:.1f} EH/s；30 日变化 {hash_change:+.2f}%" if hash_change is not None else f"估算算力：{chain.hashrate_eh_s:.1f} EH/s；30 日变化 N/A",
                f"网络难度：{chain.difficulty / 1e12:.2f}T；30 日变化 {diff_change:+.2f}%" if chain.difficulty is not None and diff_change is not None else "网络难度：N/A",
                f"日交易手续费：${chain.fees_24h_usd / 1e6:.2f}M；日变化 {fee_change:+.2f}%" if chain.fees_24h_usd is not None and fee_change is not None else "日交易手续费：N/A",
                "交易所余额、矿工实体流和长期持有者结构仍需专业链上数据源。",
            ],
        )
    if chain is not None:
        tvl_change = chain.tvl_change_7d_pct
        fee_change = chain.fees_change_1d_pct
        score = _clamp(
            (max(-35, min(35, tvl_change * 4)) if tvl_change is not None else 0)
            + (max(-20, min(20, fee_change * .5)) if fee_change is not None else 0)
        )
        headline = "链上资本与活动同步扩张" if score >= 18 else "链上资本与活动同步收缩" if score <= -18 else "链上基本面保持稳定"
        return DimensionAssessment(
            key="fundamental", label="基本面", score=score, coverage=72, headline=headline,
            evidence=[
                f"链级 TVL：${chain.tvl_usd / 1e9:.2f}B；7 日变化 {tvl_change:+.2f}%" if chain.tvl_usd is not None and tvl_change is not None else "链级 TVL：N/A",
                f"24 小时链上费用：${chain.fees_24h_usd / 1e6:.2f}M；日变化 {fee_change:+.2f}%" if chain.fees_24h_usd is not None and fee_change is not None else "24 小时链上费用：N/A",
                f"链上稳定币供给：${chain.stablecoin_supply_usd / 1e9:.2f}B" if chain.stablecoin_supply_usd is not None else "链上稳定币供给：N/A",
                "该口径衡量链级资本与使用强度，不等同于代币持有者收入。",
            ],
        )
    turnover = snapshot.volume_24h_usd / snapshot.market_cap_usd * 100 if snapshot.market_cap_usd else None
    market_cap_evidence = (
        f"流通市值：${snapshot.market_cap_usd / 1e9:.1f}B；24 小时成交额/市值：{turnover:.2f}%"
        if snapshot.market_cap_usd else "CoinGecko 市值与换手数据本批次不可用"
    )
    return DimensionAssessment(
        key="fundamental", label="基本面", score=0, coverage=25,
        headline=f"{snapshot.symbol} 基本面等待专业数据确认",
        evidence=[
            market_cap_evidence,
            f"{snapshot.symbol} 的网络收入、活跃实体、交易所余额与长期持有者数据本批次不可用；本维度不参与方向加权。",
        ],
    )


def weighted_score(dimensions: list[DimensionAssessment]) -> tuple[int, int]:
    weights = {"technical": 0.40, "flow": 0.28, "news": 0.20, "fundamental": 0.12}
    effective = [(d, weights[d.key] * d.coverage / 100) for d in dimensions]
    total = sum(weight for _, weight in effective)
    score = sum(d.score * weight for d, weight in effective) / max(total, 1e-9)
    coverage = sum(d.coverage * weights[d.key] for d in dimensions)
    return _clamp(score), round(coverage)
