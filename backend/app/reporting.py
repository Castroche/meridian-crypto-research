from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .analysis import atr, flow_assessment, fundamental_assessment, news_assessment, technical_assessment, weighted_score
from .models import (
    ArticleNarrative, ArticleParagraph, ArticleSection, AssetReport, ChartSeries, ChartSpec,
    AssetLevelSummary, AttributionItem, ChainActivityView, ConfidenceProfile, DailyAssetView,
    DailyScenario, DailySessionWindow, DailyStrategyPayload, DataHealthGroup, DataHealthItem,
    DeepResearchPayload, EvidenceItem, FeatureObservation,
    FundamentalSnapshot, KeyLevel, MarketSnapshot, NewsItem, OptionsSnapshot,
    MarketContextSnapshot, MarketDaySummary, MarketOutlookScenario, NewsImpact,
    ProfessionalMetricsSnapshot, RelativeValueView, ResearchFact, ResearchPacket, ResearchReport,
    Scenario, TacticalAssetState, TacticalMonitorPayload, TradeIdea, TradePlanTarget, TradeSetup, TradeTarget,
)


def _stance(score: int) -> str:
    if score >= 45: return "看多"
    if score >= 15: return "中性偏多"
    if score <= -45: return "看空"
    if score <= -15: return "中性偏空"
    return "中性"


def _slot(now: datetime) -> str:
    hour = now.hour
    return "08:00" if hour == 8 else "20:00" if hour == 20 else "24:00" if hour == 0 else "manual"


def _article_slot(kind: str) -> str:
    return "12:00" if kind == "midday" else "24:00"


def _round_price(value: float) -> float:
    digits = 2 if value >= 1 else 6
    return round(value, digits)


def _volume_ratio(snapshot: MarketSnapshot) -> float:
    volumes = [c.volume for c in snapshot.candles]
    if len(volumes) < 31:
        return 1.0
    # Exclude the current, still-forming 1H candle.
    recent = sum(volumes[-7:-1]) / 6
    baseline = sum(volumes[-31:-7]) / 24
    return recent / baseline if baseline else 1.0


def _targets(direction: int, entry_mid: float, stop: float) -> list[TradeTarget]:
    risk = abs(entry_mid - stop)
    multiples = (("TP1", 1.0, 30), ("TP2", 1.8, 40), ("TP3", 2.8, 30))
    return [
        TradeTarget(label=label, price=_round_price(entry_mid + direction * risk * rr), risk_reward=rr, reduce_pct=reduce)
        for label, rr, reduce in multiples
    ]


def _evidence_rationale(dimensions) -> list[str]:
    ordered = {item.key: item for item in dimensions}
    result = []
    for key in ("technical", "flow", "fundamental", "news"):
        item = ordered[key]
        evidence = item.evidence[0] if item.evidence else "无可用细项"
        result.append(f"{item.label} {item.score:+d}｜{item.headline}｜{evidence}")
    return result


def _make_setup(*, name: str, priority: str, direction: int, status: str, entry_low: float, entry_high: float,
                stop: float, trigger: str, advice: str, dimensions, risk_pct: float, max_leverage: float) -> TradeSetup:
    entry_low, entry_high = sorted((_round_price(entry_low), _round_price(entry_high)))
    entry_mid = (entry_low + entry_high) / 2
    stop = _round_price(stop)
    direction_label = "做多" if direction > 0 else "做空"
    cancel_verb = "跌破" if direction > 0 else "突破"
    return TradeSetup(
        name=name, priority=priority, direction=direction_label, status=status,
        entry_low=entry_low, entry_high=entry_high,
        entry_method="触发后分两笔执行：首笔 50%，回踩/反抽不破入场区再执行 50%；禁止在区间外追价。",
        trigger=trigger, stop_loss=stop, targets=_targets(direction, entry_mid, stop),
        cancel_condition=f"未成交前出现 4H 收盘{cancel_verb} {stop:g}，计划立即取消；成交后同一价位执行硬止损。",
        risk_per_trade_pct=risk_pct, max_leverage=max_leverage,
        position_size_formula=f"名义仓位 = 账户权益 × {risk_pct:.2f}% ÷（入场均价至止损的百分比距离）",
        advice=advice, rationale=_evidence_rationale(dimensions),
    )


def build_trade_setups(snapshot: MarketSnapshot, direction: int, dimensions, volatility: float,
                       invalidation: float, support: float, resistance: float, conviction: int, coverage: int) -> list[TradeSetup]:
    price = snapshot.price
    confirmed_close = snapshot.candles[-2].close if len(snapshot.candles) >= 2 else price
    vol_ratio = _volume_ratio(snapshot)
    risk_pct = .25 if coverage < 70 else .5
    max_leverage = 2.0 if conviction >= 72 and coverage >= 80 else 1.0
    setups: list[TradeSetup] = []
    if direction > 0:
        low, high = price - .28 * volatility, price + .02 * volatility
        entry_mid = (low + high) / 2
        status = "可执行" if low <= price <= high and confirmed_close >= entry_mid and vol_ratio >= 1 else "等待确认"
        setups.append(_make_setup(
            name="趋势回踩做多", priority="首选", direction=1, status=status,
            entry_low=low, entry_high=high, stop=min(invalidation, low - .72 * volatility),
            trigger=f"价格进入入场区，最近完成的 1H 收盘重新站上 {_round_price(entry_mid):g}，且完整 6H 成交量比达到 1.00；当前已完成量比 {vol_ratio:.2f}。",
            advice="主方向保持做多，等待回踩确认后执行；没有确认信号时不提前挂满仓位。",
            dimensions=dimensions, risk_pct=risk_pct, max_leverage=max_leverage,
        ))
        breakout_low = max(resistance, price + .45 * volatility) + .08 * volatility
        setups.append(_make_setup(
            name="放量突破做多", priority="备选", direction=1, status="等待触发",
            entry_low=breakout_low, entry_high=breakout_low + .16 * volatility,
            stop=breakout_low - .82 * volatility,
            trigger=f"1H 收盘高于 {_round_price(breakout_low):g}，成交量比达到 1.20，随后 15M 回踩不破突破位。",
            advice="只交易放量后的有效突破；单根快速插针不构成入场依据。",
            dimensions=dimensions, risk_pct=risk_pct, max_leverage=max_leverage,
        ))
    elif direction < 0:
        low, high = price - .02 * volatility, price + .28 * volatility
        entry_mid = (low + high) / 2
        status = "可执行" if low <= price <= high and confirmed_close <= entry_mid and vol_ratio >= 1 else "等待确认"
        setups.append(_make_setup(
            name="趋势反抽做空", priority="首选", direction=-1, status=status,
            entry_low=low, entry_high=high, stop=max(invalidation, high + .72 * volatility),
            trigger=f"价格进入入场区，最近完成的 1H 收盘重新跌破 {_round_price(entry_mid):g}，且完整 6H 成交量比达到 1.00；当前已完成量比 {vol_ratio:.2f}。",
            advice="主方向保持做空，等待反抽衰竭后执行；价格没有转弱确认时不抢跑。",
            dimensions=dimensions, risk_pct=risk_pct, max_leverage=max_leverage,
        ))
        breakdown_high = min(support, price - .45 * volatility) - .08 * volatility
        setups.append(_make_setup(
            name="放量跌破做空", priority="备选", direction=-1, status="等待触发",
            entry_low=breakdown_high - .16 * volatility, entry_high=breakdown_high,
            stop=breakdown_high + .82 * volatility,
            trigger=f"1H 收盘低于 {_round_price(breakdown_high):g}，成交量比达到 1.20，随后 15M 反抽不越跌破位。",
            advice="只交易放量后的有效跌破；快速下影线不构成追空依据。",
            dimensions=dimensions, risk_pct=risk_pct, max_leverage=max_leverage,
        ))
    else:
        breakout = max(resistance, price + .55 * volatility) + .08 * volatility
        breakdown = min(support, price - .55 * volatility) - .08 * volatility
        setups.extend([
            _make_setup(
                name="区间上破做多", priority="条件计划", direction=1, status="等待触发",
                entry_low=breakout, entry_high=breakout + .15 * volatility, stop=breakout - .85 * volatility,
                trigger=f"1H 收盘高于 {_round_price(breakout):g}，成交量比达到 1.20，15M 回踩守住突破位。",
                advice="当前没有方向优势，价格完成上破确认后才转为做多。",
                dimensions=dimensions, risk_pct=risk_pct, max_leverage=1.0,
            ),
            _make_setup(
                name="区间下破做空", priority="条件计划", direction=-1, status="等待触发",
                entry_low=breakdown - .15 * volatility, entry_high=breakdown, stop=breakdown + .85 * volatility,
                trigger=f"1H 收盘低于 {_round_price(breakdown):g}，成交量比达到 1.20，15M 反抽受阻。",
                advice="当前没有方向优势，价格完成下破确认后才转为做空。",
                dimensions=dimensions, risk_pct=risk_pct, max_leverage=1.0,
            ),
        ])
    return setups


def build_asset_report(snapshot: MarketSnapshot, news: list[NewsItem], fundamental: FundamentalSnapshot | None = None, options: OptionsSnapshot | None = None, professional: ProfessionalMetricsSnapshot | None = None) -> AssetReport:
    dimensions = [
        news_assessment(snapshot.symbol, news), technical_assessment(snapshot),
        flow_assessment(snapshot, options, professional), fundamental_assessment(snapshot, fundamental),
    ]
    score, coverage = weighted_score(dimensions)
    stance = _stance(score)
    volatility = max(atr(snapshot), snapshot.price * 0.006)
    direction = 1 if score >= 15 else -1 if score <= -15 else 0
    if direction > 0:
        target_low, target_high = snapshot.price + volatility * .7, snapshot.price + volatility * 1.5
        invalidation = snapshot.price - volatility * 1.1
    elif direction < 0:
        target_low, target_high = snapshot.price - volatility * 1.5, snapshot.price - volatility * .7
        invalidation = snapshot.price + volatility * 1.1
    else:
        target_low, target_high = snapshot.price - volatility, snapshot.price + volatility
        invalidation = snapshot.price - volatility * 1.5
    target_low, target_high, invalidation = map(_round_price, (target_low, target_high, invalidation))
    conviction = min(88, round(48 + abs(score) * .36 + max(0, coverage - 50) * .18))
    ranked = sorted(dimensions, key=lambda d: abs(d.score) * d.coverage, reverse=True)
    driver = ranked[0]
    strongest_support = max(dimensions, key=lambda d: d.score * d.coverage)
    strongest_risk = min(dimensions, key=lambda d: d.score * d.coverage)
    direction_text = "上行" if direction > 0 else "下行" if direction < 0 else "区间"
    invalidation_verb = "跌破" if direction >= 0 else "突破"
    thesis = f"{snapshot.symbol} 的 24 小时主路径为{direction_text}，{driver.headline}是当前主导证据。目标区间 {target_low:g}–{target_high:g}；四小时收盘{invalidation_verb} {invalidation:g} 后，本判断失效。"
    base_prob = min(68, 45 + abs(score) // 3)
    directional_prob = max(12, 27 + score // 5)
    if direction > 0:
        probs = [base_prob, 100 - base_prob - max(12, 28 - score // 5), max(12, 28 - score // 5)]
    elif direction < 0:
        down = min(42, 27 + abs(score) // 5)
        probs = [100 - directional_prob - down, directional_prob, down]
    else:
        probs = [56, 22, 22]
    probs[0] += 100 - sum(probs)
    scenarios = [
        Scenario(name="基准", probability=probs[0], path=f"价格沿{direction_text}主路径运行，波动维持近两周均值。", range_low=target_low, range_high=target_high),
        Scenario(name="上行", probability=probs[1], path="现货成交放大并站稳上方结构位，空头回补强化涨幅。", range_low=_round_price(snapshot.price + volatility * 1.4), range_high=_round_price(snapshot.price + volatility * 2.4)),
        Scenario(name="下行", probability=probs[2], path="现货承接转弱并触发杠杆去化，价格回到下方成交区。", range_low=_round_price(snapshot.price - volatility * 2.4), range_high=_round_price(snapshot.price - volatility * 1.4)),
    ]
    lows = [c.low for c in snapshot.candles[-72:]]
    highs = [c.high for c in snapshot.candles[-72:]]
    support, resistance = _round_price(min(lows)), _round_price(max(highs))
    key_levels = [
        KeyLevel(label="72H 支撑", value=support),
        KeyLevel(label="结构失效", value=invalidation),
        KeyLevel(label="72H 阻力", value=resistance),
        KeyLevel(label="24H 目标", value=target_high if direction >= 0 else target_low),
    ]
    if direction > 0:
        challenge = (strongest_risk.headline if strongest_risk.score < 0
                     else f"{strongest_risk.label}尚未提供额外方向确认")
        hinge_text = f"{strongest_support.headline}，但{challenge}"
        counter_trigger = (f"{strongest_risk.headline}继续强化" if strongest_risk.score < 0
                           else f"{driver.headline}不再成立")
    elif direction < 0:
        offset = (strongest_support.headline if strongest_support.score > 0
                  else f"{strongest_support.label}尚未提供反向缓冲")
        hinge_text = f"{strongest_risk.headline}构成主导压力，而{offset}"
        counter_trigger = "技术与资金转为正向确认"
    else:
        hinge_text = f"{strongest_support.headline}与{strongest_risk.headline}尚未形成同向确认"
        counter_trigger = "区间下沿转为有效阻力，资金同步转弱"
    decision_hinge = (
        f"定价分歧集中在{strongest_support.label}与{strongest_risk.label}：{hinge_text}。"
        f"未来 24 小时以 4H 收盘{invalidation_verb} {invalidation:g} 作为取舍边界。"
    )
    counter_case = (
        f"反方情景成立的先决条件是价格先{invalidation_verb} {invalidation:g}，"
        f"随后{counter_trigger}；在此之前，不用单一逆向指标推翻基准路径。"
    )
    monitoring_points = [
        f"价格结构｜4H 收盘{invalidation_verb} {invalidation:g} 前，基准判断保持有效",
        f"资金确认｜{_dimension_from(dimensions, 'flow').headline}；{_dimension_from(dimensions, 'flow').evidence[0]}",
        f"基本面验证｜{_dimension_from(dimensions, 'fundamental').headline}；{_dimension_from(dimensions, 'fundamental').evidence[0]}",
    ]
    trade_setups = build_trade_setups(snapshot, direction, dimensions, volatility, invalidation, support, resistance, conviction, coverage)
    relevant_news = [item.id for item in news if snapshot.symbol in item.assets]
    flags = list(snapshot.quality_flags)
    if coverage < 70:
        flags.append(f"综合证据覆盖度仅 {coverage}/100")
    evidence_ids = relevant_news + [f"market:{snapshot.symbol}:{snapshot.as_of.isoformat()}"]
    if fundamental:
        evidence_ids.append(f"fundamental:{snapshot.symbol}:{fundamental.as_of.isoformat()}")
    if options:
        evidence_ids.append(f"options:{snapshot.symbol}:{options.as_of.isoformat()}")
    if professional:
        evidence_ids.append(f"professional:{snapshot.symbol}:{professional.as_of.isoformat()}")
    data_sources = list(dict.fromkeys(snapshot.sources + (fundamental.sources if fundamental else []) + (options.sources if options else []) + (professional.sources if professional else [])))
    return AssetReport(
        symbol=snapshot.symbol, as_of=snapshot.as_of, price=snapshot.price,
        change_24h_pct=snapshot.change_24h_pct, stance=stance, conviction=conviction,
        target_low=target_low, target_high=target_high, invalidation_price=invalidation,
        invalidation_condition="4H 收盘", thesis=thesis, dominant_driver=driver.label,
        decision_hinge=decision_hinge, counter_case=counter_case, monitoring_points=monitoring_points,
        dimensions=dimensions, scenarios=scenarios, key_levels=key_levels, trade_setups=trade_setups,
        chart=[c.close for c in snapshot.candles[-48:]], evidence_ids=evidence_ids, data_sources=data_sources,
        data_quality=coverage, quality_flags=flags,
    )


def _dimension_from(dimensions, key: str):
    return next(item for item in dimensions if item.key == key)


def _report_changes(assets: list[AssetReport], previous_report: ResearchReport | None) -> list[str]:
    if previous_report is None:
        return ["本期为当前可比序列的起点；后续报告将持续披露观点、置信度与关键价位的变化。"]
    previous = {asset.symbol: asset for asset in previous_report.assets}
    changes = []
    for asset in assets:
        prior = previous.get(asset.symbol)
        if prior is None:
            continue
        price_move = (asset.price / prior.price - 1) * 100 if prior.price else 0
        stance_change = f"观点由{prior.stance}调整为{asset.stance}" if prior.stance != asset.stance else f"观点维持{asset.stance}"
        conviction_change = asset.conviction - prior.conviction
        changes.append(
            f"{asset.symbol}｜{stance_change}；较上期价格 {price_move:+.2f}%，"
            f"置信度 {conviction_change:+d} 至 {asset.conviction}/100，失效位调整至 {_money(asset.invalidation_price)}。"
        )
    return changes or ["与上一期相比，没有可确认的同口径变化。"]


def _report_takeaways(assets: list[AssetReport]) -> tuple[list[str], list[str]]:
    ranked = sorted(assets, key=lambda asset: asset.conviction, reverse=True)
    leader, laggard = ranked[0], ranked[-1]
    takeaways = [
        f"主判断｜{leader.symbol} 是当前证据完整度最高的资产：{leader.thesis}",
        f"相对价值｜{leader.symbol} 的观点置信度为 {leader.conviction}/100；{laggard.symbol} 为 {laggard.conviction}/100，仓位优先级应体现这一差异。",
        f"风险边界｜{leader.counter_case}",
    ]
    watchlist = [
        f"{asset.symbol}｜{asset.invalidation_condition}{'跌破' if asset.stance in {'看多', '中性偏多', '中性'} else '突破'} {_money(asset.invalidation_price)}"
        for asset in ranked
    ]
    return takeaways, watchlist


def _report_title(regime: str, leaders: list[AssetReport]) -> str:
    leader, laggard = leaders[0], leaders[-1]
    if leader.symbol == laggard.symbol:
        return f"24H 战术执行｜{leader.symbol} 成为短线定价锚"
    return f"24H 战术执行｜{leader.symbol} 方向占优，{laggard.symbol} 维持低优先级"


def _build_facts(snapshots: list[MarketSnapshot], assets: list[AssetReport], news: list[NewsItem]) -> list[ResearchFact]:
    facts: list[ResearchFact] = []
    snap_by_symbol = {item.symbol: item for item in snapshots}
    counter = 1

    def add(category: str, statement: str, source: str, as_of: datetime, url: str | None = None) -> str:
        nonlocal counter
        fact_id = f"F{counter:03d}"
        counter += 1
        facts.append(ResearchFact(id=fact_id, category=category, statement=statement, source=source, as_of=as_of, url=url))
        return fact_id

    for asset in assets:
        snapshot = snap_by_symbol[asset.symbol]
        source = "、".join(snapshot.sources)
        add("market", f"{asset.symbol} 现价 {_money(asset.price)}，过去 24 小时 {asset.change_24h_pct:+.2f}%。", source, asset.as_of)
        if snapshot.market_cap_usd:
            valuation = f"{asset.symbol} 流通市值 ${snapshot.market_cap_usd / 1e9:.2f}B"
            if snapshot.fully_diluted_valuation_usd:
                valuation += f"，FDV ${snapshot.fully_diluted_valuation_usd / 1e9:.2f}B"
            if snapshot.ath_change_pct is not None:
                valuation += f"，较历史高点 {snapshot.ath_change_pct:+.2f}%"
            add("valuation", valuation + "。", "CoinGecko", asset.as_of)
        for dimension in asset.dimensions:
            category = dimension.key if dimension.key in {"technical", "flow", "fundamental", "news"} else "market"
            evidence = "；".join(dimension.evidence)
            add(category, f"{asset.symbol}｜{dimension.headline}：{evidence}。", "、".join(asset.data_sources) or source, asset.as_of)
        add("risk", f"{asset.symbol} 的观点在{asset.invalidation_condition}{'跌破' if asset.stance in {'看多','中性偏多','中性'} else '突破'} {_money(asset.invalidation_price)} 后失效。", "Meridian 结构模型", asset.as_of)
    for item in sorted(news, key=lambda value: (value.source_score, value.published_at), reverse=True):
        if item.source_score >= 72:
            add("news", f"{item.title}，影响资产 {'/'.join(item.assets) or '加密产业'}。", item.source, item.published_at, item.resolved_url or item.url)
    return facts


def _issue_chart(snapshots, session_date=None):
    if not snapshots:
        return []
    tz = ZoneInfo("Asia/Shanghai")
    rows = [{c.opened_at: c for c in snap.candles
             if c.opened_at + timedelta(hours=1) <= snap.as_of and
             (session_date is None or c.opened_at.astimezone(tz).date() == session_date)}
            for snap in snapshots]
    times = sorted(set.intersection(*(set(row) for row in rows)))[-24:]
    if len(times) < 2:
        return []
    return [ChartSpec(id="issue-performance", title="三资产盘中相对表现", subtitle="以窗口首根开盘价为基准",
        labels=[(t+timedelta(hours=1)).isoformat() for t in times], unit="%",
        series=[ChartSeries(name=snap.symbol, values=[round((row[t].close/row[times[0]].open-1)*100,3) for t in times])
                for snap,row in zip(snapshots,rows)], source="、".join(dict.fromkeys(s.sources[0] for s in snapshots if s.sources)),
        insight="同一小时收盘横向比较；时间均为北京时间。")]


def _relative_performance_chart(snapshots: list[MarketSnapshot]) -> ChartSpec:
    count = min(len(item.candles) for item in snapshots)
    count = min(count, 168)
    rows = [item.candles[-count:] for item in snapshots]
    labels = [row.opened_at.isoformat() for row in rows[0]]
    series = []
    for snapshot, candles in zip(snapshots, rows):
        base = candles[0].close or 1
        series.append(ChartSeries(name=snapshot.symbol, values=[round(candle.close / base * 100, 3) for candle in candles]))
    latest = {item.name: item.values[-1] - 100 for item in series}
    leader = max(latest, key=latest.get)
    return ChartSpec(
        id="relative-7d", title="七日相对表现", subtitle="起点标准化为 100",
        unit="指数", labels=labels, series=series,
        source="Binance Spot / Coinbase Exchange",
        insight=f"{leader} 七日相对表现领先，最新累计变化 {latest[leader]:+.2f}%。",
    )


def _valuation_chart(snapshots: list[MarketSnapshot]) -> ChartSpec:
    labels = [item.symbol for item in snapshots]
    cap = [round((item.market_cap_usd or 0) / 1e9, 3) for item in snapshots]
    fdv = [round((item.fully_diluted_valuation_usd or item.market_cap_usd or 0) / 1e9, 3) for item in snapshots]
    gaps = {symbol: (full / market - 1) * 100 if market else 0 for symbol, market, full in zip(labels, cap, fdv)}
    widest = max(gaps, key=gaps.get)
    return ChartSpec(
        id="valuation", title="流通市值与完全稀释估值", subtitle="跨资产供给稀释对照",
        chart_type="bar", unit="十亿美元", labels=labels,
        series=[ChartSeries(name="流通市值", values=cap), ChartSeries(name="FDV", values=fdv)],
        source="CoinGecko", insight=f"{widest} 的 FDV 溢价最高，为 {gaps[widest]:.1f}%。",
    )


def _fundamental_chart(leader: AssetReport, fundamental: FundamentalSnapshot | None) -> ChartSpec | None:
    if fundamental is None:
        return None
    candidates = []
    if leader.symbol == "BTC":
        candidates = [("hashrate_history", "BTC 三十日算力", "EH/s"), ("network_fees_history", "BTC 网络手续费", "美元")]
    else:
        candidates = [("tvl_history", f"{leader.symbol} 链级 TVL", "美元"), ("fees_history", f"{leader.symbol} 链上费用", "美元")]
    for attr, title, unit in candidates:
        points = getattr(fundamental, attr)
        if len(points) >= 2:
            first, last = points[0].value, points[-1].value
            change = (last / first - 1) * 100 if first else 0
            return ChartSpec(
                id=f"fundamental-{leader.symbol.lower()}", title=title, subtitle="公开数据历史序列",
                unit=unit, labels=[item.at.isoformat() for item in points],
                series=[ChartSeries(name=leader.symbol, values=[round(item.value, 4) for item in points])],
                source="、".join(fundamental.sources), insight=f"该指标在图示窗口内变化 {change:+.2f}%。",
            )
    return None


def _price_structure_chart(snapshot: MarketSnapshot) -> ChartSpec:
    candles = snapshot.candles[-72:]
    change = (candles[-1].close / candles[0].close - 1) * 100 if candles and candles[0].close else 0
    return ChartSpec(
        id=f"price-{snapshot.symbol.lower()}", title=f"{snapshot.symbol} 三日价格结构", subtitle="已完成小时线",
        unit="美元", labels=[item.opened_at.isoformat() for item in candles],
        series=[ChartSeries(name=snapshot.symbol, values=[round(item.close, 4) for item in candles])],
        source="、".join(snapshot.sources), insight=f"{snapshot.symbol} 在图示窗口内变化 {change:+.2f}%。",
    )


def _build_charts(snapshots: list[MarketSnapshot], leader: AssetReport, fundamentals: dict[str, FundamentalSnapshot], kind: str) -> list[ChartSpec]:
    charts = [_relative_performance_chart(snapshots)]
    if kind == "daily":
        charts.append(_valuation_chart(snapshots))
        fundamental = _fundamental_chart(leader, fundamentals.get(leader.symbol))
        charts.append(fundamental or _price_structure_chart(next(item for item in snapshots if item.symbol == leader.symbol)))
    return charts[:1] if kind == "midday" else charts[:3]


def _build_insight_charts(snapshots: list[MarketSnapshot], packet: ResearchPacket) -> list[ChartSpec]:
    dates = sorted({item.published_at.date().isoformat() for item in packet.sources})
    dates = dates[-4:] or [packet.research_date.isoformat()]
    all_by_date = Counter(item.published_at.date().isoformat() for item in packet.sources)
    official_by_date = Counter(
        item.published_at.date().isoformat() for item in packet.sources if item.source_type == "official"
    )
    event_chart = ChartSpec(
        id="insight-event-timeline", title="本期事件资料时间线", subtitle="入选公开来源的发布时间分布",
        chart_type="bar", unit="来源数", labels=dates,
        series=[
            ChartSeries(name="全部来源", values=[all_by_date[day] for day in dates]),
            ChartSeries(name="一手来源", values=[official_by_date[day] for day in dates]),
        ], source="本期 ResearchPacket 来源元数据",
        insight=f"本期围绕“{packet.topic}”筛选 {len(packet.sources)} 个来源，观察事件证据在 48–72 小时内的集中程度。",
    )
    relevant = {asset for source in packet.sources for asset in source.assets}
    selected = [item for item in snapshots if item.symbol in relevant] or snapshots
    count = min(72, min(len(item.candles) for item in selected))
    rows = [item.candles[-count:] for item in selected]
    market_series = []
    for snapshot, candles in zip(selected, rows):
        base = candles[0].close or 1
        market_series.append(ChartSeries(
            name=snapshot.symbol, values=[round(candle.close / base * 100, 3) for candle in candles],
        ))
    market_chart = ChartSpec(
        id="insight-market-reaction", title="主题相关资产的三日市场反应", subtitle="起点标准化为 100",
        unit="指数", labels=[item.opened_at.isoformat() for item in rows[0]], series=market_series,
        source="Binance Spot / Coinbase Exchange",
        insight="图表仅展示与本期入选新闻直接关联的资产，用于检验事件叙事是否已经反映为相对价格变化。",
    )
    ranked_sources = sorted(packet.sources, key=lambda item: item.source_score, reverse=True)[:10]
    quality_chart = ChartSpec(
        id="insight-source-quality", title="本期证据来源质量", subtitle="一手性、正文可用性与来源评级",
        chart_type="bar", unit="来源分", labels=[item.source for item in ranked_sources],
        series=[ChartSeries(name="来源评分", values=[item.source_score for item in ranked_sources])],
        source="Meridian 来源评级",
        insight=f"{sum(item.source_type == 'official' for item in ranked_sources)} 个一手来源进入核心证据集；只有具备正文或公开摘要的材料可成为事实依据。",
    )
    return [event_chart, market_chart, quality_chart]


def _deterministic_article(report: ResearchReport, label: str) -> ArticleNarrative:
    ranked = sorted(report.assets, key=lambda asset: asset.conviction, reverse=True)
    leader = ranked[0]
    support = max(leader.dimensions, key=lambda item: item.score * item.coverage)
    risk = min(leader.dimensions, key=lambda item: item.score * item.coverage)
    leader_fact_ids = [fact.id for fact in report.facts if fact.statement.startswith(leader.symbol)][:6]
    market_fact_ids = [fact.id for fact in report.facts if fact.category in {"market", "valuation"}][:6]
    risk_fact_ids = [fact.id for fact in report.facts if fact.category == "risk"][:3]
    question = f"{leader.symbol} 的相对强势能否从价格表现转化为可持续的资金与基本面趋势？"
    deck = f"三项资产正在交易不同的叙事。真正的分歧不在涨跌本身，而在{support.label}能否压过{risk.label}形成的反向约束。"
    sections = [
        ArticleSection(heading="价格已经投票，证据还没有完全站到同一边", paragraphs=[
            ArticleParagraph(text=f"过去 24 小时，" + "、".join(f"{asset.symbol} {asset.change_24h_pct:+.2f}%" for asset in report.assets) + f"。{leader.symbol} 因证据完整度和方向清晰度居首，但相对领先不等于趋势已经完成确认。", fact_ids=market_fact_ids[:3]),
            ArticleParagraph(text=f"市场最容易看到的是价格，容易忽略的是价格背后的承接结构。{leader.decision_hinge}", fact_ids=leader_fact_ids[:3]),
        ]),
        ArticleSection(heading=f"{support.label}提供推力，{risk.label}决定上限", paragraphs=[
            ArticleParagraph(text=f"支持路径来自{support.headline}。{'；'.join(support.evidence)}。这些事实解释了为什么 {leader.symbol} 暂时获得更高优先级。", fact_ids=leader_fact_ids[:4]),
            ArticleParagraph(text=f"反方向的证据同样存在：{risk.headline}。{'；'.join(risk.evidence)}。只有把这部分约束写进判断，价格目标才不是对既有涨跌的线性外推。", fact_ids=leader_fact_ids[2:6]),
        ]),
        ArticleSection(heading="反方路径不是风险清单，而是一条可验证的链", paragraphs=[
            ArticleParagraph(text=leader.counter_case, fact_ids=risk_fact_ids),
            ArticleParagraph(text=f"因此，本期不把三项资产平均配置。{leader.symbol} 是主要观察对象，其余资产的风险预算随证据覆盖和触发状态递减。", fact_ids=leader_fact_ids[:2]),
        ]),
    ]
    return ArticleNarrative(
        research_question=question, deck=deck,
        lead=f"{leader.symbol} 成了这一轮价格表现最清楚的资产，但价格走在前面，资金和基本面是否跟得上，才决定这段行情会变成趋势还是一次短暂的重新定价。",
        consensus_view=f"市场当前把 {leader.symbol} 视为三项资产中的相对强者。",
        meridian_view=f"Meridian 的判断更克制：{leader.thesis}", sections=sections,
        conclusion=f"这一期最重要的不是追逐相对强势，而是验证强势背后的证据能否继续扩散。{leader.counter_case}",
    )


def build_report(snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime, fundamentals: dict[str, FundamentalSnapshot] | None = None, options: dict[str, OptionsSnapshot] | None = None, professional: dict[str, ProfessionalMetricsSnapshot] | None = None, slot_override: str | None = None, previous_report: ResearchReport | None = None) -> ResearchReport:
    fundamentals = fundamentals or {}
    options = options or {}
    professional = professional or {}
    assets = [build_asset_report(snapshot, news, fundamentals.get(snapshot.symbol), options.get(snapshot.symbol), professional.get(snapshot.symbol)) for snapshot in snapshots]
    avg_score = sum({"看多":2,"中性偏多":1,"中性":0,"中性偏空":-1,"看空":-2}[a.stance] for a in assets)
    regime = "风险偏好占优" if avg_score >= 2 else "风险规避占优" if avg_score <= -2 else "方向分化"
    leaders = sorted(assets, key=lambda a: a.conviction, reverse=True)
    judgement = "；".join(f"{a.symbol} {a.stance}（{a.conviction}/100）" for a in leaders)
    key_takeaways, watchlist = _report_takeaways(assets)
    as_of = max(snapshot.as_of for snapshot in snapshots)
    report_id = hashlib.sha256(f"{as_of.isoformat()}:{judgement}".encode()).hexdigest()[:20]
    report = ResearchReport(
        id=report_id, as_of=as_of, slot=slot_override or _slot(now_local), title=_report_title(regime, leaders),
        market_regime=regime, executive_judgement=judgement,
        key_takeaways=key_takeaways, what_changed=_report_changes(assets, previous_report), watchlist=watchlist,
        assets=assets, news=news,
        created_at=datetime.now(timezone.utc), methodology_version="meridian-tactical-v4",
    )
    report.facts = _build_facts(snapshots, assets, news)
    report.markdown = render_markdown(report)
    return report


def build_article_report(snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
                         kind: str, fundamentals: dict[str, FundamentalSnapshot] | None = None,
                         options: dict[str, OptionsSnapshot] | None = None,
                         professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
                         previous_report: ResearchReport | None = None) -> ResearchReport:
    """Build the independent noon/daily research letter from the same auditable evidence model."""
    if kind not in {"midday", "daily"}:
        raise ValueError("article report kind must be midday or daily")
    report = build_report(snapshots, news, now_local, fundamentals, options, professional, previous_report=previous_report)
    label = "午间投研" if kind == "midday" else "加密市场日报"
    leader = max(report.assets, key=lambda asset: asset.conviction)
    report.id = hashlib.sha256(f"article:{kind}:{report.as_of.isoformat()}:{report.executive_judgement}".encode()).hexdigest()[:20]
    report.slot = _article_slot(kind)
    report.report_type = kind
    direction_phrase = ("上行逻辑占优" if leader.stance in {"看多", "中性偏多"}
                        else "下行压力最集中" if leader.stance in {"看空", "中性偏空"}
                        else "维持区间定价")
    report.title = f"{label}｜价格已经投票，{leader.symbol} 的证据仍待扩散"
    report.methodology_version = "meridian-editorial-v4"
    report.charts = _build_charts(snapshots, leader, fundamentals or {}, kind)
    report.article = _deterministic_article(report, label)
    report.markdown = render_article(report, label)
    return report


def session_candle_url(source, symbol, window):
    start_ms = int(window.start_at.timestamp() * 1000)
    end_ms = start_ms + 86_400_000
    if "OKX" in source:
        return f"https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT&bar=1D&after={end_ms}&limit=1"
    if "Binance" in source:
        return f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1h&startTime={start_ms}&endTime={end_ms-1}"
    if "Coinbase" in source:
        return f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles?granularity=3600&start={start_ms//1000}&end={end_ms//1000}"
    return None


def aggregate_facts(report, aggregate):
    if not aggregate:
        return
    def add(statement, url, unit, method):
        report.facts.append(ResearchFact(id=f"F{len(report.facts)+1:03d}", category="market", statement=statement,
            source=aggregate.source, as_of=aggregate.as_of, url=url, unit=unit,
            window="采集时点的滚动 24 小时", method=method))
    if aggregate.breadth_universe and None not in (aggregate.breadth_advancers, aggregate.breadth_decliners):
        add(f"前 {aggregate.breadth_universe} 个有报价币种，上涨 {aggregate.breadth_advancers} 家，下跌 {aggregate.breadth_decliners} 家。",
            "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&price_change_percentage=24h",
            "家", "按市值排序取前百，以滚动涨跌超过正负 0.05% 统计涨跌家数。")
    if aggregate.market_cap_change_24h_pct is not None:
        add(f"全市场总市值滚动 24 小时变化 {aggregate.market_cap_change_24h_pct:+.2f}%。",
            "https://api.coingecko.com/api/v3/global", "%", "采用 CoinGecko 全市场美元市值变化。")


def build_daily_report(
    snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
    fundamentals: dict[str, FundamentalSnapshot] | None = None,
    options: dict[str, OptionsSnapshot] | None = None,
    professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
    previous_report: ResearchReport | None = None,
) -> ResearchReport:
    report = build_report(
        snapshots, news, now_local, fundamentals, options, professional,
        slot_override="24:00", previous_report=previous_report,
    )
    report.id = hashlib.sha256(f"daily:{report.as_of.date().isoformat()}:{report.executive_judgement}".encode()).hexdigest()[:20]
    report.report_type = "daily"
    report.title = f"日度综合研报｜{report.title.split('｜', 1)[-1]}"
    report.methodology_version = "meridian-daily-flash-v5"
    report.markdown = render_markdown(report)
    return report


def build_insight_report(
    snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
    packet: ResearchPacket, fundamentals: dict[str, FundamentalSnapshot] | None = None,
    options: dict[str, OptionsSnapshot] | None = None,
    professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
    previous_report: ResearchReport | None = None,
) -> ResearchReport:
    report = build_report(
        snapshots, news, now_local, fundamentals, options, professional,
        slot_override="12:00", previous_report=previous_report,
    )
    report.id = hashlib.sha256(f"insight:{packet.research_date.isoformat()}:{packet.topic}".encode()).hexdigest()[:20]
    report.report_type = "insight"
    report.title = packet.topic
    report.methodology_version = "meridian-insight-v5"
    report.research_packet_id = packet.id
    report.insight_sources = packet.sources
    report.facts.extend(packet.facts)
    report.charts = _build_insight_charts(snapshots, packet)
    report.article = None
    report.markdown = ""
    return report


def render_insight(report: ResearchReport) -> str:
    if not report.article:
        return ""
    article = report.article
    quality = round(sum(asset.data_quality for asset in report.assets) / len(report.assets))
    lines = [
        f"# {report.title}", "",
        f"> 深度洞察 · 北京时间 12:00 · 数据截至 {report.as_of.isoformat()} · 综合数据覆盖 {quality}/100", "",
        article.deck, "", f"**本期问题：** {article.research_question}", "",
        f"{article.lead} {' '.join(f'[{fact_id}]' for fact_id in article.lead_fact_ids)}".strip(), "",
        "## 市场共识与 Meridian 判断", "",
        f"**市场共识。** {article.consensus_view} {' '.join(f'[{fact_id}]' for fact_id in article.consensus_fact_ids)}".strip(), "",
        f"**Meridian 判断。** {article.meridian_view} {' '.join(f'[{fact_id}]' for fact_id in article.meridian_fact_ids)}".strip(), "",
    ]
    for section in article.sections:
        lines.extend([f"## {section.heading}", ""])
        for paragraph in section.paragraphs:
            citations = " ".join(f"[{fact_id}]" for fact_id in paragraph.fact_ids)
            lines.extend([f"{paragraph.text} {citations}".strip(), ""])
    lines.extend(["## 数据图表", ""])
    for chart in report.charts:
        lines.extend([f"### {chart.title}", "", f"{chart.insight} 数据源：{chart.source}。", ""])
    lines.extend(["## 结论", "", f"{article.conclusion} {' '.join(f'[{fact_id}]' for fact_id in article.conclusion_fact_ids)}".strip(), "", "## 来源", ""])
    for source in report.insight_sources:
        if source.access == "title":
            continue
        lines.append(f"- [{source.title}]({source.url})｜{source.source}｜{source.published_at.isoformat()}")
    lines.extend([
        "", f"生成状态：{report.insight_status or '待确认'}；模型：{report.editorial_audit.model or '未启用'}。",
        "", f"*{report.disclaimer} 本文基于公开可访问资料形成，不构成个性化投资建议。*",
    ])
    return "\n".join(lines)


def _money(value: float) -> str:
    digits = 2 if value >= 1 else 6
    return f"${value:,.{digits}f}"


def _dimension(asset: AssetReport, key: str):
    return next(item for item in asset.dimensions if item.key == key)


def render_article(report: ResearchReport, label: str) -> str:
    ranked = sorted(report.assets, key=lambda asset: asset.conviction, reverse=True)
    quality = round(sum(asset.data_quality for asset in report.assets) / len(report.assets))
    article = report.article or _deterministic_article(report, label)
    lines = [
        f"# {report.title}", "",
        f"> {label} · 北京时间 {report.slot} · 数据截至 {report.as_of.isoformat()} · 综合数据覆盖 {quality}/100", "",
        article.deck, "", f"**本期问题：** {article.research_question}", "", article.lead, "",
        "## 市场共识与 Meridian 判断", "",
        f"**市场共识。** {article.consensus_view}", "",
        f"**Meridian 判断。** {article.meridian_view}", "",
    ]
    for section in article.sections:
        lines.extend([f"## {section.heading}", ""])
        for paragraph in section.paragraphs:
            citations = " ".join(f"[{fact_id}]" for fact_id in paragraph.fact_ids)
            lines.extend([f"{paragraph.text} {citations}".strip(), ""])
    lines.extend(["## 图表证据", ""])
    if report.charts:
        for chart in report.charts:
            lines.extend([f"### {chart.title}", "", f"{chart.insight} 数据源：{chart.source}。", ""])
    else:
        lines.extend(["本期没有满足完整性要求的历史序列，未生成图表。", ""])
    lines.extend(["## 投资含义与执行边界", ""])
    for asset in ranked:
        base = next(item for item in asset.scenarios if item.name == "基准")
        setup = asset.trade_setups[0]
        lines.extend([
            f"### {asset.symbol}｜{asset.stance}", "",
            f"{asset.thesis} 基准情景概率 {base.probability}%，区间 {_money(base.range_low)}–{_money(base.range_high)}。", "",
            f"首选执行方案为“{setup.name}”：观察区 {_money(setup.entry_low)}–{_money(setup.entry_high)}，硬止损 {_money(setup.stop_loss)}。{setup.trigger}", "",
        ])
    lines.extend(["## 结论", "", article.conclusion, "", "## 事实与来源", ""])
    for fact in report.facts:
        lines.append(f"- **[{fact.id}]** {fact.statement}｜{fact.source}｜{fact.as_of.isoformat()}")
    lines.extend([
        "", f"编辑状态：{report.editorial_audit.status}；模型：{report.editorial_audit.model or '未启用'}。",
        "", f"*{report.disclaimer} 本报告为基于可追溯市场数据生成的研究文章，不构成个性化投资建议。*",
    ])
    return "\n".join(lines)


def render_markdown(report: ResearchReport) -> str:
    lines = [
        f"# {report.title}", "",
        f"- 数据时点：{report.as_of.isoformat()}",
        f"- 报告时段：{report.slot}",
        f"- 市场状态：{report.market_regime}",
        f"- 综合判断：{report.executive_judgement}",
        f"- 方法版本：{report.methodology_version}", "",
        "## 投资结论", "",
    ]
    lines.extend(f"- {item}" for item in report.key_takeaways)
    lines.extend(["", "## 相对上期变化", ""])
    lines.extend(f"- {item}" for item in report.what_changed)
    lines.extend(["", "> 风险纪律：以下方案是标准化研究计划，不构成个性化投资建议。单笔账户权益风险上限由报告给出，仓位按入场价到止损价的实际距离反推；禁止用固定币数替代风险预算。", ""])
    for asset in report.assets:
        lines.extend([
            f"## {asset.symbol}｜{asset.stance}｜置信度 {asset.conviction}/100", "",
            f"现价 {_money(asset.price)}，24 小时涨跌 {asset.change_24h_pct:+.2f}%。", "",
            f"**核心判断：** {asset.thesis}", "",
            f"**判断关键：** {asset.decision_hinge}", "",
            f"**反方观点：** {asset.counter_case}", "",
            f"**目标区间：** {_money(asset.target_low)}–{_money(asset.target_high)}  ",
            f"**失效条件：** {asset.invalidation_condition}触及 {_money(asset.invalidation_price)}  ",
            f"**主导变量：** {asset.dominant_driver}  ",
            f"**数据质量：** {asset.data_quality}/100", "",
            f"**数据源：** {'、'.join(asset.data_sources)}", "",
            "### 可执行交易计划", "",
        ])
        for setup in asset.trade_setups:
            lines.extend([
                f"#### {setup.priority}｜{setup.name}｜{setup.direction}｜{setup.status}", "",
                f"- **入场区：** {_money(setup.entry_low)}–{_money(setup.entry_high)}",
                f"- **触发条件：** {setup.trigger}",
                f"- **执行方式：** {setup.entry_method}",
                f"- **硬止损：** {_money(setup.stop_loss)}",
                f"- **持有周期：** {setup.holding_period}",
                f"- **风险预算：** 单笔账户权益 {setup.risk_per_trade_pct:.2f}%；杠杆上限 {setup.max_leverage:g}x",
                f"- **仓位公式：** {setup.position_size_formula}",
                f"- **执行建议：** {setup.advice}",
                f"- **取消条件：** {setup.cancel_condition}", "",
                "| 止盈 | 点位 | 盈亏比 | 减仓比例 |", "|---|---:|---:|---:|",
            ])
            for target in setup.targets:
                lines.append(f"| {target.label} | {_money(target.price)} | {target.risk_reward:.1f}R | {target.reduce_pct}% |")
            lines.extend(["", "**做单依据：**"])
            lines.extend(f"- {item}" for item in setup.rationale)
            lines.append("")
        lines.extend(["### 证据账本", ""])
        for dimension in asset.dimensions:
            lines.append(f"- **{dimension.label}｜{dimension.headline}｜方向分 {dimension.score:+d}｜覆盖 {dimension.coverage}/100**")
            lines.extend(f"  - {evidence}" for evidence in dimension.evidence)
        lines.extend(["", "### 24H 情景路径", "", "| 情景 | 概率 | 价格区间 | 路径 |", "|---|---:|---:|---|"])
        for scenario in asset.scenarios:
            lines.append(f"| {scenario.name} | {scenario.probability}% | {_money(scenario.range_low)}–{_money(scenario.range_high)} | {scenario.path} |")
        lines.extend(["", "### 关键价位", ""])
        lines.append("；".join(f"{level.label} {_money(level.value)}" for level in asset.key_levels) + "。")
        if asset.quality_flags:
            lines.extend(["", "**数据质量提示：** " + "；".join(asset.quality_flags) + "。"])
        lines.extend(["", "### 下一步监测", ""])
        lines.extend(f"- {item}" for item in asset.monitoring_points)
        lines.append("")
    lines.extend(["## 高相关事件", ""])
    publishable_news = [item for item in report.news if item.source_score >= 72]
    if publishable_news:
        for item in publishable_news[:12]:
            assets = "/".join(item.assets)
            lines.append(f"- [{item.title}]({item.url})｜{item.source}｜{assets}｜来源分 {item.source_score}｜{item.published_at.isoformat()}")
    else:
        lines.append("本批次没有达到来源引用门槛的高影响事件；低质量线索没有写入结论。")
    lines.extend(["", "## 方法与风险", "", report.disclaimer, "", "报告区分市场事实、研究判断和预测。缺失的链上、ETF、期权数据会降低覆盖度，不会被其他指标替代。"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Research Product V2
#
# The report object is now an envelope. Daily Strategy, Tactical Monitor and
# Deep Research have independent payloads and renderers while sharing facts,
# sources and market observations. The later definitions intentionally replace
# the V1 builders above so stored V1 reports remain readable without driving new
# production output.


def _money_or_na(value: float | None) -> str:
    return "N/A" if value is None else _money(value)


def _quality(coverage: int) -> str:
    return "HIGH" if coverage >= 80 else "MEDIUM" if coverage >= 60 else "LOW" if coverage > 0 else "UNAVAILABLE"


def _feature(
    key: str, label: str, value: float | str | None, *, unit: str, timestamp: datetime,
    source: str, horizon: str, quality: str = "HIGH",
) -> FeatureObservation:
    available = value is not None
    return FeatureObservation(
        key=key, label=label, is_available=available, value=value if available else None,
        unit=unit, timestamp=timestamp if available else None, source=source if available else None,
        freshness_seconds=max(0, round((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())) if available else None,
        quality=quality if available else "UNAVAILABLE", horizon=horizon,
    )


def _asset_observations(
    snapshot: MarketSnapshot, fundamental: FundamentalSnapshot | None,
    options: OptionsSnapshot | None, professional: ProfessionalMetricsSnapshot | None,
) -> list[FeatureObservation]:
    market_source = "、".join(snapshot.sources)
    derivatives_at = snapshot.derivatives_as_of or snapshot.as_of
    rows = [
        _feature("spot", "现货价格", snapshot.price, unit="USD", timestamp=snapshot.as_of, source=market_source, horizon="INTRADAY"),
        _feature("return_24h", "24H 涨跌", snapshot.change_24h_pct, unit="%", timestamp=snapshot.as_of, source=market_source, horizon="DAILY"),
        _feature("funding", "资金费率", snapshot.funding_rate * 100 if snapshot.funding_rate is not None else None, unit="%", timestamp=derivatives_at, source=market_source, horizon="INTRADAY", quality="MEDIUM"),
        _feature("basis", "永续标记/指数溢价", snapshot.annualized_basis_pct, unit="%", timestamp=derivatives_at, source=market_source, horizon="INTRADAY", quality="MEDIUM"),
        _feature("open_interest", "未平仓名义价值", snapshot.open_interest_usd, unit="USD", timestamp=derivatives_at, source=market_source, horizon="INTRADAY", quality="MEDIUM"),
        _feature("open_interest_change_1h", "未平仓量 1H 变化", snapshot.open_interest_change_1h_pct, unit="%", timestamp=derivatives_at, source=market_source, horizon="INTRADAY", quality="MEDIUM"),
    ]
    if options is not None:
        option_source = "、".join(options.sources)
        rows.extend([
            _feature("atm_iv", "近 ATM IV", options.near_atm_iv_pct, unit="vol pts", timestamp=options.as_of, source=option_source, horizon="INTRADAY", quality="MEDIUM"),
            _feature("iv_term_slope", "IV 期限斜率", options.term_iv_slope_pct, unit="vol pts", timestamp=options.as_of, source=option_source, horizon="INTRADAY", quality="MEDIUM"),
            _feature("put_call_oi", "Put/Call OI", options.put_call_oi_ratio, unit="ratio", timestamp=options.as_of, source=option_source, horizon="INTRADAY", quality="MEDIUM"),
        ])
    else:
        rows.extend([
            _feature("atm_iv", "近 ATM IV", None, unit="vol pts", timestamp=snapshot.as_of, source="", horizon="INTRADAY"),
            _feature("iv_term_slope", "IV 期限斜率", None, unit="vol pts", timestamp=snapshot.as_of, source="", horizon="INTRADAY"),
        ])
    if professional is not None:
        professional_source = "、".join(professional.sources)
        rows.extend([
            _feature("etf_flow", "ETF 净流", professional.spot_etf_net_flow_usd, unit="USD", timestamp=professional.as_of, source=professional_source, horizon="DAILY", quality="HIGH"),
            _feature("exchange_netflow", "交易所净流", professional.exchange_netflow_usd, unit="USD", timestamp=professional.as_of, source=professional_source, horizon="DAILY", quality="HIGH"),
            _feature("options_25d_skew", "25D Risk Reversal", professional.options_25d_skew_pct, unit="vol pts", timestamp=professional.as_of, source=professional_source, horizon="INTRADAY", quality="HIGH"),
        ])
    else:
        rows.extend([
            _feature("etf_flow", "ETF 净流", None, unit="USD", timestamp=snapshot.as_of, source="", horizon="DAILY"),
            _feature("exchange_netflow", "交易所净流", None, unit="USD", timestamp=snapshot.as_of, source="", horizon="DAILY"),
            _feature("options_25d_skew", "25D Risk Reversal", None, unit="vol pts", timestamp=snapshot.as_of, source="", horizon="INTRADAY"),
        ])
    if fundamental is not None:
        fundamental_source = "、".join(fundamental.sources)
        rows.extend([
            _feature("tvl_7d", "TVL 7D 变化", fundamental.tvl_change_7d_pct, unit="%", timestamp=fundamental.as_of, source=fundamental_source, horizon="SWING", quality="MEDIUM"),
            _feature("fees_24h", "链上费用", fundamental.fees_24h_usd, unit="USD", timestamp=fundamental.as_of, source=fundamental_source, horizon="DAILY", quality="MEDIUM"),
            _feature("hashrate_30d", "算力 30D 变化", fundamental.hashrate_change_30d_pct, unit="%", timestamp=fundamental.as_of, source=fundamental_source, horizon="STRUCTURAL", quality="MEDIUM"),
            _feature("transactions_24h", "链上交易数", getattr(fundamental, "transaction_count_24h", None), unit="", timestamp=fundamental.as_of, source=fundamental_source, horizon="DAILY", quality="MEDIUM"),
        ])
    if professional is not None:
        rows.extend([
            _feature("active_addresses_24h", "活跃地址", professional.active_addresses_24h, unit="", timestamp=professional.as_of, source="、".join(professional.sources), horizon="DAILY", quality="HIGH"),
            _feature("professional_transactions_24h", "专业源链上交易数", professional.transaction_count_24h, unit="", timestamp=professional.as_of, source="、".join(professional.sources), horizon="DAILY", quality="HIGH"),
        ])
    return rows


def _signal(
    snapshot: MarketSnapshot, news: list[NewsItem], fundamental: FundamentalSnapshot | None,
    options: OptionsSnapshot | None, professional: ProfessionalMetricsSnapshot | None,
) -> dict:
    dimensions = [
        news_assessment(snapshot.symbol, news), technical_assessment(snapshot),
        flow_assessment(snapshot, options, professional), fundamental_assessment(snapshot, fundamental),
    ]
    score, coverage = weighted_score(dimensions)
    ranked = sorted(dimensions, key=lambda item: abs(item.score) * item.coverage, reverse=True)
    support = sorted(dimensions, key=lambda item: item.score * item.coverage, reverse=True)
    observations = _asset_observations(snapshot, fundamental, options, professional)
    available = sum(item.is_available for item in observations)
    data_coverage = round(available / max(1, len(observations)) * 100)
    evidence_quality = round(sum(item.coverage for item in dimensions) / len(dimensions))
    confidence = ConfidenceProfile(
        view_conviction=min(88, round(45 + abs(score) * .45)),
        evidence_quality=evidence_quality, data_coverage=data_coverage,
        model_confidence=None,
        trade_quality=min(90, round(35 + abs(score) * .7 + max(0, data_coverage - 60) * .25)) if abs(score) >= 25 and data_coverage >= 50 else None,
    )
    return {
        "snapshot": snapshot, "dimensions": dimensions, "score": score, "coverage": coverage,
        "stance": _stance(score), "driver": ranked[0], "support": support[0], "risk": support[-1],
        "observations": observations, "confidence": confidence,
    }


def _v2_facts(signals: list[dict], news: list[NewsItem]) -> list[ResearchFact]:
    facts: list[ResearchFact] = []

    def add(category: str, statement: str, source: str, as_of: datetime, url: str | None = None):
        facts.append(ResearchFact(
            id=f"F{len(facts)+1:03d}", category=category, statement=statement,
            source=source, as_of=as_of, url=url, claim_type="FACT",
        ))

    for item in signals:
        snapshot = item["snapshot"]
        symbol = snapshot.symbol
        source = snapshot.sources[0] if snapshot.sources else "行情源"
        spot_url = (f"https://www.okx.com/api/v5/market/ticker?instId={symbol}-USDT" if "OKX" in source
                    else f"https://api.exchange.coinbase.com/products/{symbol}-USD/stats" if "Coinbase" in source
                    else f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT" if "Binance" in source else None)
        add("market", f"{symbol} 现价 {_money(snapshot.price)}，过去 24 小时 {snapshot.change_24h_pct:+.2f}%。",
            source, snapshot.as_of, spot_url)
        facts[-1].unit = "USDT" if "OKX" in source or "Binance" in source else "USD"
        facts[-1].window = "现价及滚动 24 小时"
        facts[-1].method = "交易所现货报价与滚动窗口开盘价比较"
        if snapshot.comparison_price_usd is not None:
            asset_id = {"BTC":"bitcoin", "ETH":"ethereum", "SOL":"solana"}[symbol]
            add("market", f"{symbol} CoinGecko 聚合报价 {_money(snapshot.comparison_price_usd)} USD。",
                "CoinGecko", snapshot.comparison_as_of or snapshot.as_of,
                f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={asset_id}")
            facts[-1].unit = "USD"
            facts[-1].window = "聚合源快照" if snapshot.comparison_as_of else "以交易所采集时点作参照，聚合源时间未知"
            facts[-1].method = "独立保留聚合报价与现货交易所报价，两者的计价货币、取样时刻及交易场所可能不同。"
            if snapshot.cross_source_deviation_pct is not None:
                facts[-1].method += f" 两报价绝对差除以交易所价为 {snapshot.cross_source_deviation_pct:.3f}%。"

        for observation in item["observations"]:
            if not observation.is_available or observation.key in {"spot", "return_24h"}:
                continue
            value = observation.value
            display = f"{value:,.5f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
            add("flow" if observation.horizon == "INTRADAY" else "fundamental",
                f"{symbol} {observation.label}为 {display}{observation.unit}。",
                observation.source or source, observation.timestamp or snapshot.as_of)
            chain = {"ETH":"Ethereum","SOL":"Solana"}.get(symbol)
            swap = f"{symbol}-USDT-SWAP"
            endpoint = {
                "open_interest_change_1h":f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}USDT&intervalTime=1h&limit=2",
                "funding":f"https://www.okx.com/api/v5/public/funding-rate-history?instId={swap}&limit=2",
                "open_interest":f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={swap}",
                "basis":f"https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId={swap}",
            }.get(observation.key) if "OKX" in source else None
            if observation.key in {"atm_iv","iv_term_slope","put_call_oi"} and "Deribit" in str(observation.source):
                endpoint = f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={symbol}&kind=option"
            if chain and observation.key in {"tvl_7d","fees_24h"}:
                endpoint = f"https://api.llama.fi/v2/historicalChainTvl/{chain}" if observation.key=="tvl_7d" else f"https://api.llama.fi/overview/fees/{chain}"
            if symbol=="BTC" and observation.key in {"hashrate_30d","fees_24h"}:
                metric = "hash-rate" if observation.key=="hashrate_30d" else "transaction-fees-usd"
                endpoint = f"https://api.blockchain.info/charts/{metric}?format=json"
            facts[-1].url = endpoint
            facts[-1].unit = observation.unit
            facts[-1].window = observation.label
            facts[-1].method = "采集时点的提供方指标"
    for event in sorted(news, key=lambda value: (value.price_relevance, value.source_score), reverse=True):
        if event.category == "NOISE" or event.source_score < 72 or event.price_relevance < .45 or not event.causal_path:
            continue
        add("news", f"{event.title}；分类 {event.category}，相关资产 {'/'.join(event.assets) or '市场整体'}。", event.source, event.published_at, event.resolved_url or event.url)
    for fact in facts:
        if fact.category=="news":
            fact.published_at = fact.as_of
    return facts


def _trade_for_signal(item: dict, now: datetime, horizon: str) -> TradeIdea | None:
    snapshot = item["snapshot"]
    score = item["score"]
    confidence = item["confidence"]
    if abs(score) < 25 or confidence.trade_quality is None:
        return None
    volatility = max(atr(snapshot), snapshot.price * .006)
    direction = 1 if score > 0 else -1
    action = "LONG" if direction > 0 else "SHORT"
    entry_low, entry_high = sorted((snapshot.price - volatility * .18, snapshot.price + volatility * .18))
    stop = snapshot.price - direction * volatility * 1.1
    targets = [snapshot.price + direction * volatility * multiple for multiple in (1, 1.8, 2.8)]
    target_plan = [
        TradePlanTarget(label=label, price=_round_price(value), risk_reward=rr, reduce_pct=reduce)
        for label, value, rr, reduce in zip(("TP1", "TP2", "TP3"), targets, (1.0, 1.8, 2.8), (40, 30, 30))
    ]
    return TradeIdea(
        action=action, direction=action, state="ARMED", symbol=snapshot.symbol,
        thesis=f"{item['driver'].headline}与{item['support'].headline}形成同向确认。",
        trigger=f"完成 1H 收盘确认，并保持成交量不低于最近 24H 同时段均值。",
        entry_low=_round_price(entry_low), entry_high=_round_price(entry_high), stop=_round_price(stop),
        targets=[_round_price(value) for value in targets], target_plan=target_plan, expected_r=2.8,
        entry_method="等待触发条件确认后，在观察入场区分批执行。",
        cancel_condition=f"到期前未触发，或价格先越过 {_round_price(stop):g}。",
        expected_return_pct=round(abs(targets[-1] / snapshot.price - 1) * 100, 2),
        time_horizon=horizon, expiration=now.astimezone(timezone.utc) + timedelta(hours=72 if "72" in horizon else 12),
        invalidation=f"价格以 4H 收盘越过 {_round_price(stop):g}，或主导资金信号反转。",
        distance_to_trigger_pct=0,
    )


def _trend_label(snapshot: MarketSnapshot, hours: int) -> str:
    return _series_trend(snapshot.candles, hours, .003 if hours == 1 else .008 if hours == 4 else .02)


def _series_trend(candles: list, periods: int, threshold: float) -> str:
    closes = [candle.close for candle in candles if candle.close]
    if len(closes) <= periods:
        return "UNAVAILABLE"
    change = closes[-1] / closes[-periods - 1] - 1
    return "UP" if change >= threshold else "DOWN" if change <= -threshold else "RANGE"


def _structure_levels(snapshot: MarketSnapshot) -> tuple[list[float], list[float]]:
    candles = snapshot.candles_15m[-96:] or snapshot.candles[-48:]
    volatility = max(atr(snapshot), snapshot.price * .006)
    supports = sorted({candle.low for candle in candles if candle.low < snapshot.price}, reverse=True)
    resistances = sorted({candle.high for candle in candles if candle.high > snapshot.price})

    def distinct(values: list[float], fallback_sign: int) -> list[float]:
        selected: list[float] = []
        for value in values:
            if not selected or abs(value / selected[-1] - 1) >= .0025:
                selected.append(_round_price(value))
            if len(selected) == 2:
                break
        while len(selected) < 2:
            multiple = .8 + len(selected) * .8
            selected.append(_round_price(snapshot.price + fallback_sign * volatility * multiple))
        return selected

    return distinct(supports, -1), distinct(resistances, 1)


def _session_stats(snapshot: MarketSnapshot, session_date, timezone_name: str) -> dict:
    tz = ZoneInfo(timezone_name)
    def in_session(rows):
        return sorted((c for c in rows if c.opened_at.astimezone(tz).date() == session_date),
                      key=lambda c: c.opened_at)

    quarters, hours = in_session(snapshot.candles_15m), in_session(snapshot.candles)
    use_quarters = len({c.opened_at for c in quarters}) == 96 or (not hours and quarters)
    session_rows, expected = (quarters, 96) if use_quarters else (hours, 24)
    complete = len({c.opened_at for c in session_rows}) == expected
    daily_row = next((c for c in in_session(snapshot.daily_candles)
                      if c.opened_at.astimezone(tz).hour == 0 and c.opened_at.minute == 0), None)
    completeness = "COMPLETE" if complete else "PARTIAL" if daily_row or session_rows else "FALLBACK"
    if daily_row:
        open_price, high, low, close, volume = daily_row.open, daily_row.high, daily_row.low, daily_row.close, daily_row.volume
    elif complete:
        open_price, close = session_rows[0].open, session_rows[-1].close
        high, low, volume = max(c.high for c in session_rows), min(c.low for c in session_rows), sum(c.volume for c in session_rows)
    else:
        open_price = high = low = close = volume = None
    peak, max_drawdown = 0.0, 0.0
    for candle in session_rows:
        peak = max(peak, candle.high)
        if peak:
            max_drawdown = min(max_drawdown, (candle.low / peak - 1) * 100)
    high_row = max(session_rows, key=lambda item: item.high) if session_rows else None
    low_row = min(session_rows, key=lambda item: item.low) if session_rows else None
    return {
        "open": open_price, "high": high, "low": low, "close": close, "volume": volume,
        "return_pct": (close / open_price - 1) * 100 if open_price else None,
        "max_drawdown_pct": max_drawdown if complete else None, "completeness": completeness,
        "high_at": high_row.opened_at if high_row else None, "low_at": low_row.opened_at if low_row else None,
    }


def _chain_activity_view(
    symbol: str, fundamental: FundamentalSnapshot | None, professional: ProfessionalMetricsSnapshot | None,
) -> ChainActivityView:
    metrics = [
        fundamental.fees_24h_usd if fundamental else None,
        fundamental.fees_change_1d_pct if fundamental else None,
        fundamental.tvl_usd if fundamental else None,
        fundamental.tvl_change_7d_pct if fundamental else None,
        fundamental.stablecoin_supply_usd if fundamental else None,
        professional.exchange_netflow_usd if professional else None,
        professional.active_addresses_24h if professional else fundamental.active_addresses_24h if fundamental else None,
        professional.transaction_count_24h if professional else fundamental.transaction_count_24h if fundamental else None,
    ]
    available = sum(value is not None for value in metrics)
    coverage = round(available / len(metrics) * 100)
    score_parts = []
    if fundamental and fundamental.fees_change_1d_pct is not None:
        score_parts.append(max(-40, min(40, fundamental.fees_change_1d_pct * 2)))
    if fundamental and fundamental.tvl_change_7d_pct is not None:
        score_parts.append(max(-35, min(35, fundamental.tvl_change_7d_pct * 5)))
    if professional and professional.exchange_netflow_usd is not None:
        score_parts.append(max(-25, min(25, -professional.exchange_netflow_usd / 50_000_000)))
    score = round(sum(score_parts) / max(1, len(score_parts))) if score_parts else None
    activity = "数据不足" if coverage < 25 else "积极" if score is not None and score >= 18 else "改善" if score is not None and score >= 6 else "降温" if score is not None and score <= -10 else "中性"
    sources = list(dict.fromkeys((fundamental.sources if fundamental else []) + (professional.sources if professional else [])))
    conclusion = (
        f"{symbol} 链上积极度为{activity}，可用指标覆盖 {coverage}%；"
        + (f"费用日变化 {fundamental.fees_change_1d_pct:+.1f}%，TVL 七日变化 {fundamental.tvl_change_7d_pct:+.1f}%。" if fundamental and fundamental.fees_change_1d_pct is not None and fundamental.tvl_change_7d_pct is not None else "结论仅基于当前可取得指标，不外推缺失的活跃地址或交易数。")
    )
    return ChainActivityView(
        symbol=symbol, activity=activity, score=score, coverage_pct=coverage,
        transactions_24h=professional.transaction_count_24h if professional and professional.transaction_count_24h is not None else fundamental.transaction_count_24h if fundamental else None,
        active_addresses_24h=professional.active_addresses_24h if professional and professional.active_addresses_24h is not None else fundamental.active_addresses_24h if fundamental else None,
        fees_24h_usd=fundamental.fees_24h_usd if fundamental else None,
        fees_change_1d_pct=fundamental.fees_change_1d_pct if fundamental else None,
        tvl_usd=fundamental.tvl_usd if fundamental else None,
        tvl_change_7d_pct=fundamental.tvl_change_7d_pct if fundamental else None,
        stablecoin_supply_usd=fundamental.stablecoin_supply_usd if fundamental else None,
        stablecoin_supply_change_7d_pct=professional.stablecoin_supply_change_7d_pct if professional else None,
        exchange_netflow_usd=professional.exchange_netflow_usd if professional else None,
        conclusion=conclusion, sources=sources,
    )


def _data_health_groups(observations: list[FeatureObservation], context: MarketContextSnapshot | None) -> list[DataHealthGroup]:
    grouped: dict[str, list[FeatureObservation]] = {}
    for observation in observations:
        source = observation.source or "未接入"
        grouped.setdefault(source, []).append(observation)
    rows: list[DataHealthGroup] = []
    for source, items in grouped.items():
        available = sorted({item.label for item in items if item.is_available})
        missing = sorted({item.label for item in items if not item.is_available})
        freshness_values = [item.freshness_seconds for item in items if item.freshness_seconds is not None]
        availability = "AVAILABLE" if not missing else "PARTIAL" if available else "UNAVAILABLE"
        quality = min((item.quality for item in items), key=lambda value: {"UNAVAILABLE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}[value])
        rows.append(DataHealthGroup(
            source=source, availability=availability, quality=quality,
            freshness=f"{max(freshness_values)}s" if freshness_values else "N/A",
            available_features=available, missing_features=missing,
        ))
    if context:
        rows.append(DataHealthGroup(
            source="市场聚合/情绪/爆仓/宏观",
            availability="PARTIAL" if context.quality_flags or not context.liquidations or context.liquidations.coverage == "UNAVAILABLE" else "AVAILABLE",
            quality="MEDIUM" if context.quality_flags else "HIGH", freshness=f"{max(0, int((datetime.now(timezone.utc)-context.as_of).total_seconds()))}s",
            available_features=[name for name, value in (("全市场", context.aggregate), ("恐慌贪婪", context.sentiment), ("爆仓", context.liquidations), ("宏观日历", context.macro_calendar)) if value],
            missing_features=context.quality_flags,
        ))
    return rows


def _counter_dimension(item: dict, direction: str | None):
    driver = item["driver"]
    if direction == "LONG":
        ordered = sorted(item["dimensions"], key=lambda value: value.score * value.coverage)
    elif direction == "SHORT":
        ordered = sorted(item["dimensions"], key=lambda value: value.score * value.coverage, reverse=True)
    elif driver.score >= 0:
        ordered = sorted(item["dimensions"], key=lambda value: value.score * value.coverage)
    else:
        ordered = sorted(item["dimensions"], key=lambda value: value.score * value.coverage, reverse=True)
    return next((value for value in ordered if value.key != driver.key), ordered[0])


from .comparisons import chain_percentile, checkpoint_notes, daily_notes, venue


def _reference_setup(item: dict, now: datetime, direction: str, active: bool = False) -> TradeIdea:
    snapshot = item["snapshot"]
    sign = 1 if direction == "LONG" else -1
    volatility = max(atr(snapshot), snapshot.price * .006)
    supports, resistances = _structure_levels(snapshot)
    structural_trigger = resistances[0] if sign > 0 else supports[0]
    minimum_trigger = snapshot.price + sign * volatility * (.18 if active else .30)
    trigger_price = _round_price(max(structural_trigger, minimum_trigger) if sign > 0 else min(structural_trigger, minimum_trigger))
    entry_low, entry_high = sorted((trigger_price - volatility * .10, trigger_price + volatility * .10))
    structural_stop = supports[0] if sign > 0 else resistances[0]
    stop = min(entry_low - volatility * .75, structural_stop) if sign > 0 else max(entry_high + volatility * .75, structural_stop)
    risk = (entry_high - stop) if sign > 0 else (stop - entry_low)
    reward_prices = [
        entry_high + risk * rr if sign > 0 else entry_low - risk * rr
        for rr in (1.0, 1.8, 2.8)
    ]
    entry_low, entry_high, stop = (_round_price(value) for value in (entry_low, entry_high, stop))
    targets = [_round_price(value) for value in reward_prices]
    tick = .1 if snapshot.price >= 100 else .01
    if sign > 0:
        stop = min(stop, entry_low - tick)
        targets = [max(targets[index], (entry_high + tick * (index + 1)) if index == 0 else targets[index - 1] + tick) for index in range(3)]
    else:
        stop = max(stop, entry_high + tick)
        targets = [min(targets[index], (entry_low - tick * (index + 1)) if index == 0 else targets[index - 1] - tick) for index in range(3)]
    target_plan = [
        TradePlanTarget(label=label, price=price, risk_reward=rr, reduce_pct=reduce)
        for label, price, rr, reduce in zip(("TP1", "TP2", "TP3"), targets, (1.0, 1.8, 2.8), (40, 30, 30))
    ]
    reference = entry_high if sign > 0 else entry_low
    for target in target_plan:
        target.risk_reward = round(abs(target.price-reference)/abs(reference-stop), 2)
    now_utc = now.astimezone(timezone.utc)
    freshness = max(0, int((now_utc - snapshot.as_of.astimezone(timezone.utc)).total_seconds()))
    stale = freshness > 900
    distance = abs(trigger_price / snapshot.price - 1) * 100
    executable = active and not stale and distance <= 1.5
    action = direction if executable else "WAIT"
    state = "ARMED" if executable else "APPROACHING" if active else "INACTIVE"
    label = "向上突破" if sign > 0 else "向下跌破"
    counter = _counter_dimension(item, direction)
    return TradeIdea(
        action=action, direction=direction, state=state, symbol=snapshot.symbol, trigger_price=trigger_price,
        thesis=f"{label}参考计划：{item['driver'].headline}；反方约束来自{counter.label}，{counter.headline}。",
        trigger=f"价格完成 1H {'收于上方' if sign > 0 else '收于下方'} {_money(trigger_price)}，且该小时现货成交量不低于此前 24 根已收盘小时线的均量。",
        entry_low=entry_low, entry_high=entry_high, stop=stop,
        targets=targets, target_plan=target_plan, expected_r=target_plan[-1].risk_reward,
        expected_return_pct=round(abs(targets[-1] / snapshot.price - 1) * 100, 2),
        time_horizon="until next checkpoint", expiration=now_utc + timedelta(hours=12),
        invalidation=f"价格先越过硬止损 {_round_price(stop):g}，或主导资金信号反向。",
        entry_method="触发后在观察入场区分两笔执行，不在区间外追价。",
        cancel_condition="下一检查点前未触发、数据转为过期或反方证据增强时取消。",
        data_status="STALE" if stale else "FRESH", freshness_seconds=freshness,
        distance_to_trigger_pct=round(distance, 2),
    )


def _daily_scenario(item: dict) -> list[DailyScenario]:
    snapshot = item["snapshot"]
    score = item["score"]
    volatility = max(atr(snapshot), snapshot.price * .006)
    direction = 1 if score >= 15 else -1 if score <= -15 else 0
    base_low = snapshot.price - volatility * (0.6 if direction >= 0 else 1.2)
    base_high = snapshot.price + volatility * (1.2 if direction >= 0 else 0.6)
    return [
        DailyScenario(
            name="BASE", likelihood="HIGH" if abs(score) >= 20 else "MEDIUM",
            condition="当前主导变量与价格结构延续。", expected_range_low=_round_price(base_low), expected_range_high=_round_price(base_high),
            catalyst=item["driver"].headline, confirmation="4H 价格结构与资金信号保持同向。",
            invalidation=f"{item['risk'].headline}成为主导变量。",
        ),
        DailyScenario(
            name="BULL", likelihood="MEDIUM" if direction > 0 else "LOW",
            condition="现货需求增强且杠杆没有同步过热。", expected_range_low=_round_price(snapshot.price + volatility), expected_range_high=_round_price(snapshot.price + volatility * 2.2),
            catalyst="现货流入或事件催化超预期。", confirmation="价格突破 72H 结构上沿并由成交量确认。", invalidation="突破后重新跌回原成交区。",
        ),
        DailyScenario(
            name="BEAR", likelihood="MEDIUM" if direction < 0 else "LOW",
            condition="现货承接下降并触发杠杆去化。", expected_range_low=_round_price(snapshot.price - volatility * 2.2), expected_range_high=_round_price(snapshot.price - volatility),
            catalyst="宏观风险、资金流出或安全事件。", confirmation="价格跌破 72H 结构下沿并伴随 OI 去化。", invalidation="跌破后快速收复原成交区。",
        ),
    ]


def _relative_value(signals: list[dict]) -> list[RelativeValueView]:
    by_symbol = {item["snapshot"].symbol: item for item in signals}
    rows = []
    for left, right in (("BTC", "ETH"), ("ETH", "SOL"), ("SOL", "BTC")):
        a, b = by_symbol.get(left), by_symbol.get(right)
        if not a or not b:
            continue
        spread = a["snapshot"].change_24h_pct - b["snapshot"].change_24h_pct
        if abs(spread) < 1.0 or abs(a["score"] - b["score"]) < 12:
            view = "No relative edge"
        else:
            view = f"Long {left} / Short {right}" if spread > 0 else f"Long {right} / Short {left}"
        rows.append(RelativeValueView(
            pair=f"{left}/{right}", view=view,
            rationale=f"24H 相对收益差 {spread:+.2f} 个百分点；方向信号差 {a['score']-b['score']:+d}。",
            evidence=[a["driver"].headline, b["driver"].headline],
        ))
    return rows


def _news_impacts(news: list[NewsItem], session_stats: dict[str, dict], session_start: datetime, session_end: datetime) -> list[NewsImpact]:
    candidates = [
        item for item in news
        if session_start.astimezone(timezone.utc) <= item.published_at.astimezone(timezone.utc) <= session_end.astimezone(timezone.utc)
        and item.category in {"MACRO", "REGULATION", "SECURITY", "FLOW", "POSITIONING", "CATALYST", "FUNDAMENTAL"}
        and item.source_score >= 72
    ]
    candidates.sort(key=lambda item: (item.price_relevance, item.source_score, item.novelty), reverse=True)
    impacts: list[NewsImpact] = []
    for event in candidates[:8]:
        affected = event.assets or ["BTC", "ETH", "SOL"]
        reaction = "；".join(
            f"{symbol} 当日 {session_stats[symbol]['return_pct']:+.2f}%"
            for symbol in affected if symbol in session_stats and session_stats[symbol]["return_pct"] is not None
        ) or "事件窗口内没有足够价格数据用于观察反应"
        if event.category == "MACRO":
            transmission = "宏观事件 → 美元流动性与风险偏好 → 加密资产估值和杠杆需求"
        elif event.category in {"REGULATION", "SECURITY"}:
            transmission = f"{event.category} 事件 → 风险溢价与资金可达性 → 现货承接和链上活动"
        else:
            transmission = event.causal_path or f"{event.category} 事件 → 资金预期 → 价格与链上行为"
        confidence = "HIGH" if event.source_score >= 90 and event.price_relevance >= .75 else "MEDIUM" if event.price_relevance >= .5 else "LOW"
        impacts.append(NewsImpact(
            event_id=event.id, headline=event.title, occurred_at=event.published_at,
            source=event.source, url=event.resolved_url or event.url, category=event.category,
            affected_assets=affected, observed_market_reaction=reaction,
            transmission_path=transmission,
            onchain_sentiment_impact=(
                "待验证假设：若风险偏好上升，应能观察到同窗口的使用量或净流入变化。"
                if event.sentiment > .2 else "待验证假设：若风险偏好收缩，应能观察到同窗口的使用量或净流出变化。"
                if event.sentiment < -.2 else "情绪影响暂不明确，当前仅记录价格反应，不把时间重合写成已证实因果。"
            ), confidence=confidence, is_inference=True,
        ))
    return impacts


def _market_outlook(signals: list[dict], risk_events: list[str]) -> list[MarketOutlookScenario]:
    bullish = sum(item["score"] >= 15 for item in signals)
    bearish = sum(item["score"] <= -15 for item in signals)
    base_path = "震荡中等待跨资产方向收敛" if bullish == bearish else "强势资产继续领涨，弱势资产维持分化" if bullish > bearish else "风险偏好收缩，反弹优先视为修复"
    return [
        MarketOutlookScenario(
            name="BASE", likelihood="HIGH" if abs(bullish - bearish) >= 2 else "MEDIUM",
            condition="当前价格结构、资金费率和链上活跃度没有发生方向反转。",
            market_path=base_path, confirmation="BTC/ETH/SOL 的 4H 结构与市场宽度保持当前关系。",
            invalidation="两项以上核心变量在同一交易日反向。", risk_events=risk_events[:3],
        ),
        MarketOutlookScenario(
            name="BULL", likelihood="MEDIUM" if bullish >= 2 else "LOW",
            condition="现货成交放大、市场宽度改善且杠杆没有同步过热。",
            market_path="BTC 稳住关键压力上方后，ETH 与 SOL 获得扩散性资金。",
            confirmation="三个资产中至少两个突破首要压力并由成交量确认。",
            invalidation="突破后重新跌回当日主要成交区。", risk_events=risk_events[:3],
        ),
        MarketOutlookScenario(
            name="BEAR", likelihood="MEDIUM" if bearish >= 2 else "LOW",
            condition="支撑失守、OI 去化或宏观风险事件压低风险偏好。",
            market_path="BTC 防御性相对占优，ETH/SOL 波动和回撤扩大。",
            confirmation="三个资产中至少两个跌破首要支撑，且长仓爆仓或资金流出扩大。",
            invalidation="跌破后快速收复支撑并伴随市场宽度修复。", risk_events=risk_events[:3],
        ),
    ]


def _daily_payload(
    signals: list[dict], previous_report: ResearchReport | None, now_local: datetime,
    fundamentals: dict[str, FundamentalSnapshot], professional: dict[str, ProfessionalMetricsSnapshot],
    news: list[NewsItem], market_context: MarketContextSnapshot | None,
) -> DailyStrategyPayload:
    timezone_name = getattr(now_local.tzinfo, "key", None) or "Asia/Shanghai"
    tz = ZoneInfo(timezone_name)
    session_date = now_local.astimezone(tz).date() - timedelta(days=1)
    session_start = datetime.combine(session_date, datetime.min.time(), tzinfo=tz)
    session_end = session_start + timedelta(days=1) - timedelta(seconds=1)
    previous_views = {
        item.symbol: item for item in previous_report.daily_strategy.executive_dashboard
    } if previous_report and previous_report.daily_strategy else {}
    stats = {item["snapshot"].symbol: _session_stats(item["snapshot"], session_date, timezone_name) for item in signals}
    ranking = {
        symbol: index + 1 for index, (symbol, _) in enumerate(
            sorted(((key, value) for key, value in stats.items() if value["return_pct"] is not None), key=lambda row: row[1]["return_pct"], reverse=True)
        )
    }
    dashboard: list[DailyAssetView] = []
    scenarios = {}
    levels: dict[str, AssetLevelSummary] = {}
    for item in signals:
        snapshot = item["snapshot"]
        symbol = snapshot.symbol
        session = stats[symbol]
        prior = previous_views.get(symbol)
        previous_view = prior.view if prior else None
        view_change = f"{previous_view} → {item['stance']}" if previous_view and previous_view != item["stance"] else f"维持{item['stance']}" if previous_view else "建立 24–72H 基准观点"
        volatility = max(atr(snapshot), snapshot.price * .006)
        supports, resistances = _structure_levels(snapshot)
        supporting = [value for value in item["support"].evidence if "N/A" not in value][:3]
        contradicting = [value for value in item["risk"].evidence if "N/A" not in value][:2]
        trend_summary = "自然日行情覆盖不足，无法比较全天涨跌。"
        if symbol in ranking:
            drawdown = session["max_drawdown_pct"]
            drawdown_text = f"最大回撤 {drawdown:.2f}%" if drawdown is not None else "日内采样不足，回撤待核"
            trend_summary = (f"全天 {session['return_pct']:+.2f}%，日内高低区间 {_money(session['low'])}–{_money(session['high'])}，"
                             f"{drawdown_text}；已覆盖资产强弱第 {ranking[symbol]}。")
        invalidation = f"4H 收盘跌破 {_money(supports[0])}" if item["score"] >= 0 else f"4H 收盘突破 {_money(resistances[0])}"
        if prior and not prior.comparison_source and previous_report:
            previous_spot = next((f.source for f in previous_report.facts if f.category == "market" and
                                  f.statement.startswith(symbol + " 现价 ")), None)
            if previous_spot and snapshot.sources and previous_spot == snapshot.sources[0]:
                prior = prior.model_copy(update={"comparison_source": venue(snapshot)})
        history = [_session_stats(snapshot, session_date - timedelta(days=i), timezone_name) for i in range(1, 8)]
        notes = daily_notes(snapshot, session, history, prior, previous_report.as_of if previous_report else None)
        if snapshot.funding_rate is not None and snapshot.open_interest_change_1h_pct is not None:
            notes.append(f"截稿时资金费率 {snapshot.funding_rate:+.4%}，持仓量过去一小时变化 {snapshot.open_interest_change_1h_pct:+.2f}%。两项反映杠杆定价与仓位变化，无法据此识别机构买卖。")
        if professional.get(symbol) and professional[symbol].spot_etf_net_flow_usd is not None:
            notes.append(f"专业数据源记录 ETF 净流量 {professional[symbol].spot_etf_net_flow_usd:+,.0f} USD，观测时点 {professional[symbol].as_of.isoformat()}。")
        dashboard.append(DailyAssetView(
            comparison_source=venue(snapshot), comparison_notes=notes, session_volume_unit=snapshot.candle_volume_unit,
            symbol=symbol, spot=snapshot.price, return_24h_pct=snapshot.change_24h_pct,
            view=item["stance"], view_change=view_change,
            thesis=f"{view_change}。{trend_summary} 接下来的验证条件为{invalidation}；届时重新评估。",
            what_changed=" ".join([trend_summary, *notes]), key_driver=item["driver"].headline,
            supporting_evidence=supporting or [item["support"].headline],
            contradicting_evidence=contradicting or [item["risk"].headline],
            catalysts=["现货成交与衍生品仓位同向变化", "宏观或行业事件改变风险溢价"],
            invalidation=invalidation,
            expected_range_low=_round_price(snapshot.price - volatility * 1.3),
            expected_range_high=_round_price(snapshot.price + volatility * 1.3),
            preferred_expression="复盘与情景预测，不提供日内交易计划",
            session_open=_round_price(session["open"]) if session["open"] is not None else None, session_high=_round_price(session["high"]) if session["high"] is not None else None,
            session_low=_round_price(session["low"]) if session["low"] is not None else None, session_close=_round_price(session["close"]) if session["close"] is not None else None,
            session_volume=session["volume"], return_day_pct=round(session["return_pct"], 2) if session["return_pct"] is not None else None,
            max_drawdown_pct=round(session["max_drawdown_pct"], 2) if session["max_drawdown_pct"] is not None else None, strength_rank=ranking.get(symbol),
            trend_summary=trend_summary, supports=supports, resistances=resistances,
            confidence=item["confidence"], observations=item["observations"],
        ))
        scenarios[symbol] = _daily_scenario(item)
        levels[symbol] = AssetLevelSummary(symbol=symbol, supports=supports, resistances=resistances, invalidation=invalidation)

    day_moves = [row["return_pct"] for row in stats.values() if row["return_pct"] is not None]
    avg_move = sum(abs(value) for value in day_moves) / max(1, len(day_moves))
    up_count, down_count = sum(value > .2 for value in day_moves), sum(value < -.2 for value in day_moves)
    asset_breadth = f"三资产上涨 {up_count} / 下跌 {down_count} / 震荡 {max(0, len(day_moves)-up_count-down_count)}"
    bullish = sum(item["score"] >= 15 for item in signals)
    bearish = sum(item["score"] <= -15 for item in signals)
    regime = "自然日样本不足" if not day_moves else "波动扩张" if avg_move >= 4 else "风险偏好改善" if up_count >= 2 else "风险偏好收缩" if down_count >= 2 else "震荡分化"
    aggregate = market_context.aggregate if market_context else None
    breadth = (
        f"市值前 {aggregate.breadth_universe} 上涨 {aggregate.breadth_advancers} / 下跌 {aggregate.breadth_decliners} / 震荡 {aggregate.breadth_unchanged}"
        if aggregate and aggregate.breadth_universe else asset_breadth
    )
    market_one_liner = (
        f"{session_date.isoformat()} 加密市场呈{regime}，{breadth}，"
        f"已覆盖资产中表现最强的是 {min(ranking, key=ranking.get) if ranking else '暂无可比样本'}。"
    )
    if (aggregate and aggregate.breadth_universe and aggregate.breadth_advancers is not None and aggregate.breadth_decliners is not None
            and aggregate.market_cap_change_24h_pct is not None
            and (aggregate.breadth_advancers - aggregate.breadth_decliners) * aggregate.market_cap_change_24h_pct < 0):
        regime = "市场宽度与总市值分化"
        market_one_liner = (f"{breadth}。采集时全市场总市值滚动 24 小时变化 "
                            f"{aggregate.market_cap_change_24h_pct:+.2f}%。前者按币种计数，后者按市值汇总；"
                            "缺少单币市值贡献分解，资金流向仍待验证。")
    market_day = MarketDaySummary(
        one_liner=market_one_liner, trend=regime,
        total_market_cap_usd=aggregate.total_market_cap_usd if aggregate else None,
        total_market_cap_change_24h_pct=aggregate.market_cap_change_24h_pct if aggregate else None,
        total_volume_24h_usd=aggregate.total_volume_24h_usd if aggregate else None,
        btc_dominance_pct=aggregate.btc_dominance_pct if aggregate else None,
        breadth=breadth,
        main_up_phase="日内高点时段：" + "；".join(f"{symbol} {stats[symbol]['high_at'].astimezone(tz).strftime('%H:%M') if stats[symbol]['high_at'] else 'N/A'}" for symbol in stats),
        main_down_phase="日内低点时段：" + "；".join(f"{symbol} {stats[symbol]['low_at'].astimezone(tz).strftime('%H:%M') if stats[symbol]['low_at'] else 'N/A'}" for symbol in stats),
        attribution=[f"{item['snapshot'].symbol}：{item['driver'].headline}" for item in signals],
    )
    chain_activity = [
        _chain_activity_view(symbol, fundamentals.get(symbol), professional.get(symbol))
        for symbol in ("BTC", "ETH", "SOL")
    ]
    for row in chain_activity:
        row.conclusion += chain_percentile(fundamentals.get(row.symbol))
    impacts = _news_impacts(news, stats, session_start, session_end)
    risk_events = [impact.headline for impact in impacts[:3]]
    if market_context:
        risk_events.extend(event.name for event in market_context.macro_calendar[:3])
    all_observations = [observation for item in signals for observation in item["observations"]]
    positioning = [item for item in all_observations if item.key in {"funding", "basis", "open_interest", "open_interest_change_1h", "etf_flow", "exchange_netflow"}]
    volatility_rows = [item for item in all_observations if item.key in {"atm_iv", "iv_term_slope", "options_25d_skew", "put_call_oi"}]
    health_groups = _data_health_groups(all_observations, market_context)
    legacy_health = [DataHealthItem(
        source=group.source, feature="、".join(group.available_features + group.missing_features) or "N/A",
        freshness=group.freshness, availability=group.availability, quality=group.quality,
    ) for group in health_groups]
    attribution = [AttributionItem(
        driver={"technical": "TECHNICAL", "flow": "DERIVATIVES", "news": "EVENT", "fundamental": "ON_CHAIN"}[item["driver"].key],
        contribution="POSITIVE" if item["driver"].score > 10 else "NEGATIVE" if item["driver"].score < -10 else "MIXED",
        mechanism=f"{item['snapshot'].symbol}：{item['driver'].headline}。该观测作为候选解释，现有数据无法量化它对当日回报的贡献。",
    ) for item in signals]
    completeness = "COMPLETE" if stats and all(row["completeness"] == "COMPLETE" for row in stats.values()) else "FALLBACK" if any(row["completeness"] == "FALLBACK" for row in stats.values()) else "PARTIAL"
    takeaways = [market_one_liner] + [f"{item.symbol}｜{item.trend_summary}" for item in sorted(dashboard, key=lambda row: row.strength_rank or 9)[:2]]
    return DailyStrategyPayload(
        executive_dashboard=dashboard, key_takeaways=takeaways, market_regime=regime,
        regime_evidence=[f"三资产平均绝对自然日波动 {avg_move:.2f}%", asset_breadth, breadth],
        what_changed_24h=[item.what_changed for item in dashboard], attribution=attribution,
        relative_value=_relative_value(signals), positioning_and_flows=positioning,
        volatility_and_options=volatility_rows, scenarios=scenarios, preferred_trades=[],
        catalyst_calendar=[f"{event.name}｜{event.scheduled_at.astimezone(tz).strftime('%m-%d %H:%M')}" for event in (market_context.macro_calendar if market_context else [])] or ["未来 96H 未取得可核验的官方宏观日历事件。"],
        risk_monitor=[item.invalidation for item in dashboard], data_health=legacy_health,
        session_window=DailySessionWindow(
            session_date=session_date, start_at=session_start, end_at=session_end,
            timezone=timezone_name, completeness=completeness,
        ), market_day=market_day, chain_activity=chain_activity,
        sentiment=market_context.sentiment if market_context else None,
        liquidations=market_context.liquidations if market_context else None,
        news_impacts=impacts, levels=levels, outlook=_market_outlook(signals, risk_events),
        macro_calendar=market_context.macro_calendar if market_context else [],
        data_health_groups=health_groups,
    )


def _next_checkpoint(now_local: datetime) -> datetime:
    if now_local.hour < 8:
        return now_local.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_local.hour < 20:
        return now_local.replace(hour=20, minute=0, second=0, microsecond=0)
    return (now_local + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)


def _tactical_payload(
    signals: list[dict], previous_report: ResearchReport | None, now_local: datetime,
    market_context: MarketContextSnapshot | None = None,
) -> TacticalMonitorPayload:
    previous_states = {}
    previous_checkpoint = None
    if previous_report and previous_report.tactical_monitor:
        previous_checkpoint = previous_report.tactical_monitor.current_checkpoint
        previous_states = {item.symbol: item for item in previous_report.tactical_monitor.dashboard}
    dashboard: list[TacticalAssetState] = []
    setups: list[TradeIdea] = []
    for item in signals:
        snapshot = item["snapshot"]
        prior = previous_states.get(snapshot.symbol)
        comparable = prior and previous_checkpoint < snapshot.as_of and prior.comparison_source == venue(snapshot)
        change = (snapshot.price / prior.price - 1) * 100 if comparable and prior.price else None
        prior_view = prior.current_view if prior else None
        if prior_view and prior_view != item["stance"]:
            thesis_status = "THESIS_INVALIDATED"
        elif change is not None and change * item["score"] < 0:
            thesis_status = "THESIS_WEAKENING"
        elif change is not None and abs(change) >= 1:
            thesis_status = "THESIS_STRENGTHENING"
        else:
            thesis_status = "THESIS_VALID"
        bias = "UP" if item["score"] >= 15 else "DOWN" if item["score"] <= -15 else "NEUTRAL"
        tradable = (snapshot.open_interest_change_1h_pct is not None and snapshot.funding_rate is not None
                    and snapshot.derivatives_as_of is not None
                    and 0 <= (now_local - snapshot.derivatives_as_of).total_seconds() <= 3600
                    and 0 <= (now_local - snapshot.as_of).total_seconds() <= 900)
        active_direction = ("LONG" if item["score"] >= 35 else "SHORT" if item["score"] <= -35 else None) if tradable else None
        primary_setup = _reference_setup(item, now_local, active_direction, active=True) if active_direction else None
        conditional_setups = [] if active_direction else [
            _reference_setup(item, now_local, "LONG"), _reference_setup(item, now_local, "SHORT"),
        ]
        reference_setups = [primary_setup] if primary_setup else conditional_setups
        active_setup = primary_setup if primary_setup and primary_setup.action in {"LONG", "SHORT"} else None
        if active_setup:
            setups.append(active_setup)
        intraday = [observation for observation in item["observations"] if observation.horizon == "INTRADAY"]
        available_intraday = [observation for observation in intraday if observation.is_available]
        changed = []
        if change is not None and abs(change) >= .35:
            changed.append(f"价格较上个 checkpoint {change:+.2f}%")
        if prior_view and prior_view != item["stance"]:
            changed.append(f"观点由{prior_view}调整为{item['stance']}")
        supports, resistances = _structure_levels(snapshot)
        price_freshness = max(0, int((now_local.astimezone(timezone.utc) - snapshot.as_of.astimezone(timezone.utc)).total_seconds()))
        derivatives_freshness = max(0, int((now_local.astimezone(timezone.utc) - snapshot.derivatives_as_of.astimezone(timezone.utc)).total_seconds())) if snapshot.derivatives_as_of else None
        decision = active_direction or "UNCLEAR"
        counter = _counter_dimension(item, active_direction or ("LONG" if item["score"] > 0 else "SHORT" if item["score"] < 0 else None))
        trend_summary = (
            f"方向明确，当前以{('上破做多' if active_direction == 'LONG' else '下破做空')}为主计划。"
            if active_direction else "多周期和资金证据没有形成明确方向，当前观望，仅保留上下突破条件计划。"
        )
        trends = {"trend_15m": _series_trend(snapshot.candles_15m, 1, .0015),
                  "trend_1h": _trend_label(snapshot, 1), "trend_4h": _trend_label(snapshot, 4),
                  "trend_1d": _trend_label(snapshot, 24)}
        notes = checkpoint_notes(snapshot, prior, previous_checkpoint, trends)
        changed.extend(notes)
        dashboard.append(TacticalAssetState(
            comparison_source=venue(snapshot), comparison_notes=notes,
            symbol=snapshot.symbol, previous_view=prior_view, current_view=item["stance"], price=snapshot.price,
            price_change_since_checkpoint_pct=round(change, 2) if change is not None else None,
            signal_change="；".join(changed) if changed else "没有达到显著阈值的信号变化",
            return_24h_pct=snapshot.change_24h_pct,
            volume_24h_usd=snapshot.volume_24h_usd, funding_rate=snapshot.funding_rate,
            open_interest_usd=snapshot.open_interest_usd,
            open_interest_change_1h_pct=snapshot.open_interest_change_1h_pct,
            **trends,
            action=active_setup.action if active_setup else "WAIT", state=item["driver"].headline,
            bias=bias,
            momentum=item["dimensions"][1].headline, positioning=item["dimensions"][2].headline,
            volatility="EXPANDING" if abs(snapshot.change_24h_pct) >= 4 else "NORMAL",
            dominant_driver=item["driver"].headline, counter_evidence=f"{counter.label}：{counter.headline}",
            trend_summary=trend_summary, decision=decision, supports=supports, resistances=resistances,
            price_freshness_seconds=price_freshness, derivatives_freshness_seconds=derivatives_freshness,
            primary_setup=primary_setup, conditional_setups=conditional_setups,
            trigger_distance_pct=reference_setups[0].distance_to_trigger_pct,
            trade_state=active_setup.state if active_setup else "INACTIVE", thesis_status=thesis_status,
            thesis_status_reason=f"{item['driver'].headline}；反向约束为{counter.label}的{counter.headline}。",
            what_changed=changed, before_next_checkpoint=[
                "观察价格是否接近结构触发位", "观察 funding/OI/basis 是否发生同向增量变化",
            ], confidence=item["confidence"], active_setup=active_setup,
            reference_setups=reference_setups, observations=available_intraday,
        ))
    changed_variables = [f"{state.symbol}｜{value}" for state in dashboard for value in state.what_changed]
    no_action = not setups
    ordered = sorted(signals, key=lambda value: value["score"], reverse=True)
    leadership = " > ".join(item["snapshot"].symbol for item in ordered) if ordered else "UNAVAILABLE"
    bullish = sum(item["score"] >= 15 for item in signals)
    bearish = sum(item["score"] <= -15 for item in signals)
    avg_move = sum(abs(item["snapshot"].change_24h_pct) for item in signals) / max(1, len(signals))
    asset_breadth = f"三资产偏多 {bullish} / 偏空 {bearish} / 中性 {max(0, len(signals)-bullish-bearish)}"
    volatility_state = "EXPANDING" if avg_move >= 4 else "NORMAL" if signals else "UNAVAILABLE"
    positioning_state = "；".join(f"{state.symbol}: {state.positioning}" for state in dashboard) or "UNAVAILABLE"
    aggregate = market_context.aggregate if market_context else None
    market_breadth = (
        f"市值前 {aggregate.breadth_universe} 上涨 {aggregate.breadth_advancers} / 下跌 {aggregate.breadth_decliners} / 震荡 {aggregate.breadth_unchanged}"
        if aggregate and aggregate.breadth_universe else asset_breadth
    )
    market_brief = (
        f"当前大盘{('偏强' if bullish >= 2 else '偏弱' if bearish >= 2 else '方向不明、震荡分化')}，"
        f"{market_breadth}，强弱排序 {leadership}；"
        + (f"全市场总市值 24H {aggregate.market_cap_change_24h_pct:+.2f}%，BTC 市占率 {aggregate.btc_dominance_pct:.2f}%。" if aggregate and aggregate.market_cap_change_24h_pct is not None and aggregate.btc_dominance_pct is not None else "全市场聚合指标暂不完整。")
    )
    return TacticalMonitorPayload(
        previous_checkpoint=previous_checkpoint, current_checkpoint=now_local,
        next_checkpoint=_next_checkpoint(now_local + timedelta(minutes=1)),
        market_overview=[
            f"大盘方向：{market_breadth}；{asset_breadth}", f"强弱排序：{leadership}",
            f"平均绝对 24H 变动 {avg_move:.2f}%｜波动状态 {volatility_state}",
            f"资金与期权：{positioning_state}",
        ], market_brief=market_brief, market_metrics=aggregate,
        market_breadth=market_breadth, leadership=leadership,
        volatility_state=volatility_state, positioning_state=positioning_state,
        since_last_checkpoint=changed_variables or ["自上个 checkpoint 以来没有达到显著阈值的变化。"],
        dashboard=dashboard, active_setups=setups, changed_variables=changed_variables,
        before_next_checkpoint=[
            "价格进入触发位 1.5% 范围时重新评估 setup。",
            "funding、OI、basis 或 IV/skew 出现异常变化时重新调用研究生成器。",
            "4H 结构失效时立即更新 thesis status。",
        ], no_action=no_action,
    )


def build_report(
    snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
    fundamentals: dict[str, FundamentalSnapshot] | None = None,
    options: dict[str, OptionsSnapshot] | None = None,
    professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
    slot_override: str | None = None, previous_report: ResearchReport | None = None,
    market_context: MarketContextSnapshot | None = None,
) -> ResearchReport:
    fundamentals, options, professional = fundamentals or {}, options or {}, professional or {}
    signals = [_signal(item, news, fundamentals.get(item.symbol), options.get(item.symbol), professional.get(item.symbol)) for item in snapshots]
    as_of = max((item.as_of for item in snapshots), default=now_local.astimezone(timezone.utc))
    slot = slot_override or _slot(now_local)
    tactical = _tactical_payload(signals, previous_report, now_local, market_context)
    since = tactical.previous_checkpoint or now_local - timedelta(hours=1)
    events = sorted((n for n in news if since < n.published_at <= as_of and n.source_score >= 72 and n.category != "NOISE"),
                    key=lambda n: (n.price_relevance, n.source_score, n.published_at), reverse=True)
    for event in events[:3]:
        tactical.market_overview.append(f"同期事件（传导待证）｜{event.published_at.astimezone(now_local.tzinfo):%m-%d %H:%M} {event.source}：{event.title}")
    # Keep the shared envelope useful to homepage and API consumers that render
    # the four-dimension view. This projection comes from the exact same market
    # snapshots as Tactical Monitor, so its price and conclusions stay aligned.
    homepage_assets = [
        build_asset_report(
            item,
            news,
            fundamentals.get(item.symbol),
            options.get(item.symbol),
            professional.get(item.symbol),
        )
        for item in snapshots
    ]
    directional = [state for state in tactical.dashboard if state.decision in {"LONG", "SHORT"}]
    title = "定时交易监测｜" + (
        "、".join(f"{state.symbol} {'偏多' if state.decision == 'LONG' else '偏空'}" for state in directional)
        if directional else "三资产方向待确认"
    )
    report = ResearchReport(
        id=hashlib.sha256(f"tactical-v2:{slot}:{as_of.isoformat()}".encode()).hexdigest()[:20],
        as_of=as_of, slot=slot, report_type="standard", title=title,
        market_regime="实时交易决策", executive_judgement=tactical.market_brief,
        key_takeaways=tactical.since_last_checkpoint[:5], what_changed=tactical.changed_variables or ["没有显著变化"],
        watchlist=tactical.before_next_checkpoint, assets=homepage_assets, news=news, created_at=datetime.now(timezone.utc),
        methodology_version="meridian-tactical-monitor-v3", schema_version="5.1",
        tactical_monitor=tactical,
    )
    report.facts = _v2_facts(signals, news)
    report.charts = _issue_chart(snapshots)
    report.markdown = render_tactical_monitor(report)
    return report


def build_daily_report(
    snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
    fundamentals: dict[str, FundamentalSnapshot] | None = None,
    options: dict[str, OptionsSnapshot] | None = None,
    professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
    previous_report: ResearchReport | None = None,
    market_context: MarketContextSnapshot | None = None,
) -> ResearchReport:
    fundamentals, options, professional = fundamentals or {}, options or {}, professional or {}
    signals = [_signal(item, news, fundamentals.get(item.symbol), options.get(item.symbol), professional.get(item.symbol)) for item in snapshots]
    as_of = max((item.as_of for item in snapshots), default=now_local.astimezone(timezone.utc))
    daily = _daily_payload(signals, previous_report, now_local, fundamentals, professional, news, market_context)
    session_date = daily.session_window.session_date if daily.session_window else now_local.date() - timedelta(days=1)
    report = ResearchReport(
        id=hashlib.sha256(f"daily-recap-v3:{session_date.isoformat()}:{as_of.isoformat()}".encode()).hexdigest()[:20],
        as_of=as_of, slot="00:10", report_type="daily",
        title=f"加密市场日报｜{session_date.isoformat()}｜{daily.market_regime}", market_regime=daily.market_regime,
        executive_judgement=daily.key_takeaways[0] if daily.key_takeaways else "数据覆盖不足，维持低确信度观察。",
        key_takeaways=daily.key_takeaways, what_changed=daily.what_changed_24h,
        watchlist=daily.risk_monitor, assets=[], news=news, created_at=datetime.now(timezone.utc),
        methodology_version="meridian-daily-recap-v3", schema_version="5.1", daily_strategy=daily,
    )
    report.facts = _v2_facts(signals, news)
    for row in daily.executive_dashboard:
        if row.return_day_pct is None:
            continue
        spot = next(f for f in report.facts if f.category == "market" and f.statement.startswith(row.symbol))
        report.facts.append(ResearchFact(id=f"F{len(report.facts)+1:03d}", category="market",
            statement=f"{row.symbol} 北京时间 {session_date} 自然日涨跌 {row.return_day_pct:+.2f}%，收盘 {_money(row.session_close)}。",
            source=spot.source, as_of=daily.session_window.end_at,
            url=session_candle_url(spot.source, row.symbol, daily.session_window), unit="%", window=str(session_date), method="(自然日收盘 / 开盘 − 1) × 100"))
    report.charts = _issue_chart(snapshots, session_date)
    agg = market_context.aggregate if market_context else None
    aggregate_facts(report, agg)
    if agg and daily.market_regime=="市场宽度与总市值分化":
        breadth = (agg.breadth_advancers - agg.breadth_decliners)/agg.breadth_universe*100
        report.charts = [ChartSpec(id="breadth-divergence", title="市场宽度与总市值变化", chart_type="bar",
            subtitle="前百净上涨家数占比与滚动 24H 总市值变化", unit="%", labels=["市场宽度","总市值"],
            series=[ChartSeries(name="采集时点",values=[round(breadth,2),agg.market_cap_change_24h_pct])],
            source="CoinGecko", insight="按家数计数与按市值汇总反映不同侧面，不能由此推断资金流向。")]
    report.markdown = render_daily_strategy(report)
    return report


def _thesis_chart(snapshots: list[MarketSnapshot], packet: ResearchPacket) -> list[ChartSpec]:
    relevant_assets = {asset for source in packet.sources if source.thesis_relevance >= .65 for asset in source.assets}
    selected = [item for item in snapshots if item.symbol in relevant_assets]
    if not selected:
        return []
    count = min(72, min(len(item.candles) for item in selected))
    if count < 2:
        return []
    rows = [item.candles[-count:] for item in selected]
    series = []
    for snapshot, candles in zip(selected, rows):
        base = candles[0].close or 1
        series.append(ChartSeries(name=snapshot.symbol, values=[round(candle.close / base * 100, 3) for candle in candles]))
    return [ChartSpec(
        id="thesis-market-reaction", title="核心命题相关资产的市场反应", subtitle="起点标准化为 100",
        unit="指数", labels=[item.opened_at.isoformat() for item in rows[0]], series=series,
        source="Binance Spot / Coinbase Exchange", insight="用于判断核心事件是否已经反映为相对价格变化。",
        chart_question="市场是否已经对核心命题重新定价？",
        claim_supported=packet.thesis_angle, why_it_matters="价格反应决定投资含义是否仍有未定价空间。",
    )]


def build_insight_report(
    snapshots: list[MarketSnapshot], news: list[NewsItem], now_local: datetime,
    packet: ResearchPacket, fundamentals: dict[str, FundamentalSnapshot] | None = None,
    options: dict[str, OptionsSnapshot] | None = None,
    professional: dict[str, ProfessionalMetricsSnapshot] | None = None,
    previous_report: ResearchReport | None = None,
) -> ResearchReport:
    as_of = now_local.astimezone(timezone.utc)
    deep = DeepResearchPayload(
        news_brief=packet.news_brief,
        research_type=packet.research_type, topic_origin=packet.topic_origin,
        primary_topic=packet.topic, backup_topic=packet.backup_topic,
        thesis=packet.thesis_candidate, evidence_graph=packet.evidence_graph,
        narrative_plan=packet.narrative_plan,
        publication_notes=["深度文章按材料决定篇幅；补证与编辑检查通过后发布，不发布模板保底稿。"],
    )
    report = ResearchReport(
        id=uuid.uuid4().hex[:20],
        as_of=as_of, slot="12:00", report_type="insight", title=packet.topic,
        market_regime="Deep Research", executive_judgement=packet.thesis_angle,
        key_takeaways=[], what_changed=[], watchlist=[], assets=[], news=news,
        created_at=datetime.now(timezone.utc), methodology_version="meridian-narrative-v5",
        schema_version="5.0", research_packet_id=packet.id, insight_sources=packet.sources,
        deep_research=deep,
    )
    report.facts = [fact.model_copy(deep=True) for fact in packet.facts]
    report.charts = []
    return report


def _format_observation(item: FeatureObservation) -> str:
    if not item.is_available:
        return f"{item.label}: N/A"
    if isinstance(item.value, float):
        value = f"{item.value:,.4f}".rstrip("0").rstrip(".")
    else:
        value = str(item.value)
    return f"{item.label}: {value}{item.unit}"


def _trade_plan_lines(item: TradeIdea) -> list[str]:
    direction = item.direction or (item.action if item.action in {"LONG", "SHORT"} else "WAIT")
    targets = item.target_plan or [
        TradePlanTarget(label=label, price=price, risk_reward=rr, reduce_pct=reduce)
        for label, price, rr, reduce in zip(("TP1", "TP2", "TP3"), item.targets, (1.0, 1.8, 2.8), (40, 30, 30))
    ]
    lines = [
        f"### {item.symbol}｜{direction}｜{item.state}", "", item.thesis, "",
        f"- 观察入场区：{_money_or_na(item.entry_low)}–{_money_or_na(item.entry_high)}",
        f"- 触发条件：{item.trigger}", f"- 硬止损：{_money_or_na(item.stop)}",
        f"- 执行方式：{item.entry_method or '触发后分批执行'}",
    ]
    for target in targets:
        lines.append(f"- {target.label}：{_money_or_na(target.price)}｜{target.risk_reward:g}R｜减仓 {target.reduce_pct}%")
    lines.extend([
        f"- 取消条件：{item.cancel_condition or item.invalidation}",
        f"- 有效期：{item.expiration.isoformat() if item.expiration else item.time_horizon}",
        f"- 计划风险收益比：{item.expected_r:g}R" if item.expected_r is not None else "- 计划风险收益比：N/A",
        f"- 数据状态：{item.data_status}" + (f"（{item.freshness_seconds // 60} 分钟）" if item.freshness_seconds is not None else ""), "",
    ])
    return lines


def render_daily_strategy(report: ResearchReport) -> str:
    daily = report.daily_strategy
    if daily is None:
        return ""
    window = daily.session_window
    market = daily.market_day
    lines = [f"# {report.title}", ""]
    if window:
        lines.extend([f"> 北京时间自然日 {window.session_date.isoformat()} 00:00–23:59｜完整度 {window.completeness}｜生成于 {report.created_at.isoformat()}", ""])
    lines.extend(["## 全天大盘结论", "", market.one_liner if market else daily.key_takeaways[0] if daily.key_takeaways else "数据不足。", ""])
    if market:
        lines.extend([
            f"- 趋势：{market.trend}｜市场宽度：{market.breadth}",
            f"- 全市场总市值：{_money_or_na(market.total_market_cap_usd)}｜24H 变化：{market.total_market_cap_change_24h_pct:+.2f}%" if market.total_market_cap_change_24h_pct is not None else f"- 全市场总市值：{_money_or_na(market.total_market_cap_usd)}｜24H 变化：N/A",
            f"- 全市场成交量：{_money_or_na(market.total_volume_24h_usd)}｜BTC 市占率：{market.btc_dominance_pct:.2f}%" if market.btc_dominance_pct is not None else f"- 全市场成交量：{_money_or_na(market.total_volume_24h_usd)}｜BTC 市占率：N/A",
            f"- {market.main_up_phase}", f"- {market.main_down_phase}", "",
        ])
    lines.extend(["## 情绪与爆仓", ""])
    if daily.sentiment and daily.sentiment.value is not None:
        delta = daily.sentiment.value - daily.sentiment.previous_value if daily.sentiment.previous_value is not None else None
        lines.append(f"- 恐慌贪婪指数：{daily.sentiment.value}（{daily.sentiment.classification}）" + (f"，较前日 {delta:+d}" if delta is not None else "") + f"｜来源：{daily.sentiment.source}")
    else:
        lines.append("- 恐慌贪婪指数：N/A")
    liquidation = daily.liquidations
    if liquidation and liquidation.total_usd_24h is not None:
        lines.append(f"- 爆仓：总计 {_money_or_na(liquidation.total_usd_24h)}｜多仓 {_money_or_na(liquidation.long_usd_24h)}｜空仓 {_money_or_na(liquidation.short_usd_24h)}｜覆盖 {liquidation.coverage}｜窗口 {liquidation.window}｜来源 {liquidation.source}")
        lines.extend(f"  - {item.symbol}：总计 {_money_or_na(item.total_usd_24h)}｜多仓 {_money_or_na(item.long_usd_24h)}｜空仓 {_money_or_na(item.short_usd_24h)}" for item in liquidation.assets)
        if liquidation.note:
            lines.append(f"  - 覆盖说明：{liquidation.note}")
    else:
        lines.append(f"- 爆仓数据：N/A｜覆盖 {liquidation.coverage if liquidation else 'UNAVAILABLE'}｜{liquidation.note if liquidation else '未取得数据'}")
    lines.extend(["", "## BTC / ETH / SOL 自然日复盘", "", "| 资产 | 开盘 | 最高 | 最低 | 收盘 | 成交量 | 当日涨跌 | 最大回撤 | 强弱 | 观点 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"])
    for item in daily.executive_dashboard:
        session_volume = f"{item.session_volume:,.2f} {item.session_volume_unit}".strip() if item.session_volume is not None else "N/A"
        day_return = f"{item.return_day_pct:+.2f}%" if item.return_day_pct is not None else "N/A"
        drawdown = f"{item.max_drawdown_pct:.2f}%" if item.max_drawdown_pct is not None else "N/A"
        lines.append(f"| {item.symbol} | {_money_or_na(item.session_open)} | {_money_or_na(item.session_high)} | {_money_or_na(item.session_low)} | {_money_or_na(item.session_close)} | {session_volume} | {day_return} | {drawdown} | {item.strength_rank or 'N/A'}/3 | {item.view} |")
    lines.extend(["", "## 三资产走势、支撑与压力", ""])
    for item in daily.executive_dashboard:
        lines.extend([
            f"### {item.symbol}｜{item.view}", "", item.thesis, "",
            f"- 全天走势：{item.trend_summary}", *(f"- {note}" for note in item.comparison_notes), f"- 主导因素：{item.key_driver}",
            f"- 支持证据：{'；'.join(item.supporting_evidence)}",
            f"- 反方证据：{'；'.join(item.contradicting_evidence)}",
            f"- 支撑：{' / '.join(_money(value) for value in item.supports)}｜压力：{' / '.join(_money(value) for value in item.resistances)}",
            f"- 未来 24–72H 预期区间：{_money_or_na(item.expected_range_low)}–{_money_or_na(item.expected_range_high)}",
            f"- 失效条件：{item.invalidation}", "",
        ])
    lines.extend(["## 链上积极度", ""])
    for item in daily.chain_activity:
        stablecoin_change = f"{item.stablecoin_supply_change_7d_pct:+.2f}%" if item.stablecoin_supply_change_7d_pct is not None else "N/A"
        lines.extend([f"### {item.symbol}｜{item.activity}｜覆盖 {item.coverage_pct}%", "", item.conclusion, "", f"- 费用：{_money_or_na(item.fees_24h_usd)}｜TVL：{_money_or_na(item.tvl_usd)}｜稳定币：{_money_or_na(item.stablecoin_supply_usd)}｜7D 变化：{stablecoin_change}", f"- 活跃地址：{item.active_addresses_24h if item.active_addresses_24h is not None else 'N/A'}｜交易数：{item.transactions_24h if item.transactions_24h is not None else 'N/A'}", ""])
    lines.extend(["## 新闻、宏观与链上情绪", ""])
    if daily.news_impacts:
        for item in daily.news_impacts:
            lines.extend([f"### [{item.headline}]({item.url})", "", f"- 时间：{item.occurred_at.isoformat()}｜来源：{item.source}｜分类：{item.category}｜置信度：{item.confidence}", f"- 已观察市场反应：{item.observed_market_reaction}", f"- 传导路径：{item.transmission_path}", f"- 链上情绪影响：{item.onchain_sentiment_impact}", f"- 说明：{'分析推断，仍需后续数据确认' if item.is_inference else '已由数据直接观察'}", ""])
    else:
        lines.extend(["自然日内没有取得达到来源和相关性门槛的重大事件；不使用低质量新闻填充归因。", ""])
    lines.extend(["## 未来 24–72H 大盘情景", "", "| 情景 | 权重 | 市场路径 | 触发条件 | 失效条件 |", "|---|---|---|---|---|"])
    for item in daily.outlook:
        lines.append(f"| {item.name} | {item.likelihood} | {item.market_path} | {item.condition} | {item.invalidation} |")
    lines.extend(["", "## 官方宏观日历", ""])
    lines.extend(f"- {item}" for item in daily.catalyst_calendar)
    lines.extend(["", "## 数据质量（按来源分组）", ""])
    for group in daily.data_health_groups:
        lines.append(f"- **{group.source}｜{group.availability}｜{group.quality}｜{group.freshness}**：可用 {'、'.join(group.available_features) or '无'}；缺失 {'、'.join(group.missing_features) or '无'}")
    lines.extend(["", "## 附录｜事实账本", ""])
    lines.extend(f"- **[{fact.id}]** {fact.statement}｜{fact.source}｜{fact.as_of.isoformat()}" for fact in report.facts)
    lines.extend(["", f"*{report.disclaimer}*" ])
    return "\n".join(lines)


def render_tactical_monitor(report: ResearchReport) -> str:
    tactical = report.tactical_monitor
    if tactical is None:
        return ""
    lines = [f"# {report.title}", "", f"> 北京时间 {report.slot}｜数据时点 {report.as_of.isoformat()}｜下一检查点 {tactical.next_checkpoint.isoformat()}", "", "## 一句话大盘", "", tactical.market_brief or (tactical.market_overview[0] if tactical.market_overview else "数据不足"), "", "## BTC / ETH / SOL 具体做单建议", ""]
    for item in tactical.dashboard:
        lines.extend([
            f"### {item.symbol}｜{'走势明确 ' + item.decision if item.decision != 'UNCLEAR' else '走势不明，当前观望'}", "",
            f"- 现价：{_money_or_na(item.price)}｜检查点变化：{item.price_change_since_checkpoint_pct:+.2f}%" if item.price_change_since_checkpoint_pct is not None else f"- 现价：{_money_or_na(item.price)}｜检查点变化：N/A",
            (f"- 24H：{item.return_24h_pct:+.2f}%｜成交额：{_money_or_na(item.volume_24h_usd)}" if item.return_24h_pct is not None else f"- 24H：N/A｜成交额：{_money_or_na(item.volume_24h_usd)}"),
            f"- 多周期结构：15M {item.trend_15m}｜1H {item.trend_1h}｜4H {item.trend_4h}｜1D {item.trend_1d}",
            f"- 趋势判断：{item.trend_summary}",
            f"- 支撑：{' / '.join(_money(value) for value in item.supports)}｜压力：{' / '.join(_money(value) for value in item.resistances)}",
            f"- 资金定位：{item.positioning}",
            f"- 资金费率：{item.funding_rate * 100:+.4f}%" if item.funding_rate is not None else "- 资金费率：N/A",
            f"- 未平仓：{_money_or_na(item.open_interest_usd)}｜1H 变化：{item.open_interest_change_1h_pct:+.2f}%" if item.open_interest_change_1h_pct is not None else f"- 未平仓：{_money_or_na(item.open_interest_usd)}｜1H 变化：N/A",
            f"- 波动：{item.volatility}",
            f"- 主导因素：{item.dominant_driver}", f"- 反方证据：{item.counter_evidence}",
            f"- 数据新鲜度：价格 {item.price_freshness_seconds // 60 if item.price_freshness_seconds is not None else 'N/A'} 分钟｜衍生品 {item.derivatives_freshness_seconds // 60 if item.derivatives_freshness_seconds is not None else 'N/A'} 分钟", "",
        ])
        plans = [item.primary_setup] if item.primary_setup else item.conditional_setups or item.reference_setups
        for plan in [value for value in plans if value is not None]:
            lines.extend(_trade_plan_lines(plan))
    lines.extend(["", "## 大盘分析", ""])
    lines.extend(f"- {item}" for item in tactical.market_overview)
    lines.extend(["", "## 相对上个检查点", ""])
    lines.extend(f"- {item}" for item in tactical.since_last_checkpoint)
    lines.extend(["", "## 变量变化", ""])
    lines.extend(f"- {item}" for item in tactical.changed_variables or ["没有达到显著阈值的变量变化。"])
    lines.extend(["", "## Before Next Checkpoint", ""])
    lines.extend(f"- {item}" for item in tactical.before_next_checkpoint)
    lines.extend(["", f"*{report.disclaimer}*" ])
    return "\n".join(lines)


def render_insight(report: ResearchReport) -> str:
    if not report.article:
        return ""
    article = report.article
    lines = [f"# {report.title}", "", f"> 深度文章 · 材料截至 {report.as_of.isoformat()} · {report.editorial_audit.model or report.editorial_audit.provider}", ""]
    if article.deck:
        lines.extend([article.deck, ""])
    for text, ids in ((article.lead, article.lead_fact_ids),
                      (article.consensus_view, article.consensus_fact_ids),
                      (article.meridian_view, article.meridian_fact_ids)):
        if text:
            lines.extend([f"{text} {' '.join(f'[{fid}]' for fid in ids)}".strip(), ""])
    for section in article.sections:
        lines.extend([f"## {section.heading}", ""])
        for paragraph in section.paragraphs:
            lines.extend([f"{paragraph.text} {' '.join(f'[{fid}]' for fid in paragraph.fact_ids)}".strip(), ""])
    lines.extend([f"{article.conclusion} {' '.join(f'[{fid}]' for fid in article.conclusion_fact_ids)}".strip(), "", "## 来源与演算", ""])
    for source in report.insight_sources:
        if source.access != "title":
            date_label = "收录时间" if source.title.startswith(("https://","http://")) else "来源日期"
            lines.append(f"- [{source.title}]({source.url})｜{source.source}｜{date_label} {source.published_at.isoformat()}")
    for fact in report.facts:
        source = f"[{fact.source}]({fact.url})" if fact.url else fact.source
        lines.append(f"- [{fact.id}] {fact.statement}｜{source}")
    lines.extend(["", f"*{report.disclaimer}*"])
    return "\n".join(lines)


def render_markdown(report: ResearchReport) -> str:
    if report.report_type == "insight":
        return render_insight(report)
    if report.daily_strategy:
        body = render_daily_strategy(report)
    elif report.tactical_monitor:
        body = render_tactical_monitor(report)
    else:
        return report.markdown
    editing = report.editorial_audit.model if report.editorial_audit.status == "model" else "程序按已校验数据编发"
    lines = [body, "", "## 编辑摘要", "", f"资料截至 {report.as_of.isoformat()} · {editing}",
             "", report.executive_judgement, "", *report.key_takeaways, "", "## 资料与统计口径", ""]
    for f in report.facts:
        source = f"[{f.source}]({f.url})" if f.url else f.source
        lines.append(f"- [{f.id}] {f.statement}｜{source}｜{f.as_of.isoformat()}｜{f.window} {f.method}".rstrip())
    return "\n".join(lines)
