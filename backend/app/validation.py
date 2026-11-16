from __future__ import annotations

import re

from .models import ResearchReport
from .editorial import visible_body_char_count
from .article_policy import publication_issues


FUZZY_LANGUAGE = re.compile(r"可能|或许|大概|若(?:价格|市场|行情|资金|出现|发生|突破|跌破)")


class ReportValidationError(ValueError):
    pass


def validate_report(report: ResearchReport) -> None:
    errors: list[str] = []
    if report.schema_version.startswith("5"):
        minimum_length = 500 if report.report_type == "standard" else 700 if report.report_type == "daily" else 1
        if not report.markdown or len(report.markdown) < minimum_length:
            errors.append("V5 完整 Markdown 报告缺失或过短")
        products = [report.daily_strategy is not None, report.tactical_monitor is not None, report.deep_research is not None]
        if sum(products) != 1:
            errors.append("V5 报告必须且只能包含一个产品 payload")
        if report.report_type == "daily":
            if report.daily_strategy is None:
                errors.append("Daily 缺少 daily_strategy schema")
            else:
                for asset in report.daily_strategy.executive_dashboard:
                    for observation in asset.observations:
                        if not observation.is_available and observation.value is not None:
                            errors.append(f"{asset.symbol} {observation.key} 缺失值被伪装成零或其他值")
                for scenarios in report.daily_strategy.scenarios.values():
                    if any(hasattr(item, "probability") for item in scenarios):
                        errors.append("未校准 Daily 情景不得包含精确概率")
                if report.schema_version.startswith("5.1"):
                    daily = report.daily_strategy
                    if daily.preferred_trades:
                        errors.append("Daily 自然日复盘不得包含独立交易计划")
                    if not daily.session_window or not daily.market_day:
                        errors.append("Daily 缺少北京时间自然日窗口或大盘总结")
                    if {item.symbol for item in daily.chain_activity} != {"BTC", "ETH", "SOL"}:
                        errors.append("Daily 链上积极度必须覆盖 BTC、ETH、SOL")
                    if len(daily.outlook) != 3 or {item.name for item in daily.outlook} != {"BASE", "BULL", "BEAR"}:
                        errors.append("Daily 必须包含 BASE/BULL/BEAR 三种 24–72H 情景")
                    if any(not item.supports or not item.resistances or
                           (daily.session_window and daily.session_window.completeness == "COMPLETE" and item.return_day_pct is None)
                           for item in daily.executive_dashboard):
                        errors.append("Daily 三资产自然日走势或支撑压力不完整")
                elif len(report.daily_strategy.preferred_trades) > len(report.daily_strategy.executive_dashboard):
                    errors.append("Daily 每个资产最多一个 preferred trade")
        elif report.report_type == "standard":
            if report.tactical_monitor is None:
                errors.append("Tactical 缺少 tactical_monitor schema")
            else:
                tactical = report.tactical_monitor
                if tactical.no_action and tactical.active_setups:
                    errors.append("NO_ACTION Tactical 不得包含 active setup")
                if any(observation.horizon == "STRUCTURAL" for state in tactical.dashboard for observation in state.observations):
                    errors.append("Structural 慢变量不得进入 Tactical dashboard")
                symbols = {state.symbol for state in tactical.dashboard}
                if symbols != {"BTC", "ETH", "SOL"}:
                    errors.append("Tactical 必须覆盖 BTC、ETH、SOL")
                for state in tactical.dashboard:
                    if not state.reference_setups:
                        errors.append(f"{state.symbol} 缺少参考计划")
                    if report.schema_version.startswith("5.1"):
                        if state.decision == "UNCLEAR" and (state.primary_setup is not None or {item.direction for item in state.conditional_setups} != {"LONG", "SHORT"}):
                            errors.append(f"{state.symbol} 走势不明时必须观望并保留双向条件计划")
                        if state.decision in {"LONG", "SHORT"} and (state.primary_setup is None or state.primary_setup.direction != state.decision):
                            errors.append(f"{state.symbol} 方向明确时缺少同向主计划")
                        if state.price_freshness_seconds is not None and state.price_freshness_seconds > 900 and state.active_setup is not None:
                            errors.append(f"{state.symbol} 价格过期时不得显示已激活计划")
                    for setup in state.reference_setups:
                        if setup.direction not in {"LONG", "SHORT"} or setup.entry_low is None or setup.entry_high is None or setup.stop is None:
                            errors.append(f"{state.symbol} 参考计划字段不完整")
                            continue
                        targets = [target.price for target in setup.target_plan] or setup.targets
                        if len(targets) != 3:
                            errors.append(f"{state.symbol} 参考计划必须有三级止盈")
                            continue
                        if setup.target_plan and sum(target.reduce_pct for target in setup.target_plan) != 100:
                            errors.append(f"{state.symbol} 参考计划减仓比例必须合计 100%")
                        if setup.direction == "LONG" and not (setup.stop < setup.entry_low <= setup.entry_high < targets[0] < targets[1] < targets[2]):
                            errors.append(f"{state.symbol} 多单参考计划几何非法")
                        if setup.direction == "SHORT" and not (setup.stop > setup.entry_high >= setup.entry_low > targets[0] > targets[1] > targets[2]):
                            errors.append(f"{state.symbol} 空单参考计划几何非法")
        elif report.report_type == "insight":
            if report.deep_research is None or report.article is None:
                errors.append("Deep Research 缺少结构化研究 payload 或正文")
            else:
                valid_ids = {fact.id for fact in report.facts}
                cited = set(report.article.lead_fact_ids + report.article.consensus_fact_ids + report.article.meridian_fact_ids + report.article.conclusion_fact_ids)
                cited.update(fact_id for section in report.article.sections for paragraph in section.paragraphs for fact_id in paragraph.fact_ids)
                if not cited.issubset(valid_ids):
                    errors.append("Deep Research 引用无法映射到事实账本")
                errors.extend(publication_issues(report))
                if report.insight_status != "full":
                    errors.append("新 Deep Research 不得通过 short 路径发布")
        else:
            errors.append(f"V5 不支持的报告类型：{report.report_type}")
        if errors:
            raise ReportValidationError("；".join(errors))
        return

    symbols = {asset.symbol for asset in report.assets}
    if symbols != {"BTC", "ETH", "SOL"}:
        errors.append(f"资产集合错误：{sorted(symbols)}")
    if not report.markdown or len(report.markdown) < 1000:
        errors.append("完整 Markdown 报告缺失或过短")
    fact_ids = {fact.id for fact in report.facts}
    if report.schema_version.startswith("4") and not fact_ids:
        errors.append("V4 报告缺少事实账本")
    if report.report_type == "insight":
        if report.article is None:
            errors.append("深度文章缺少结构化正文")
        if len(report.charts) < 2:
            errors.append("深度文章图表不足 2 张")
    for asset in report.assets:
        prefix = asset.symbol
        if asset.target_low >= asset.target_high:
            errors.append(f"{prefix} 目标区间非法")
        if sum(item.probability for item in asset.scenarios) != 100:
            errors.append(f"{prefix} 情景概率不等于 100%")
        if len(asset.dimensions) != 4 or {item.key for item in asset.dimensions} != {"news", "technical", "flow", "fundamental"}:
            errors.append(f"{prefix} 四维证据不完整")
        if not asset.evidence_ids or not asset.data_sources:
            errors.append(f"{prefix} 证据或数据源缺失")
        if not asset.trade_setups:
            errors.append(f"{prefix} 缺少交易计划")
        if asset.stance in {"看多", "中性偏多"} and asset.invalidation_price >= asset.price:
            errors.append(f"{prefix} 多头观点的失效价没有位于现价下方")
        if asset.stance in {"看空", "中性偏空"} and asset.invalidation_price <= asset.price:
            errors.append(f"{prefix} 空头观点的失效价没有位于现价上方")
        generated_text = " ".join([
            asset.thesis, *(scenario.path for scenario in asset.scenarios), *(dimension.headline for dimension in asset.dimensions),
            *(setup.trigger for setup in asset.trade_setups), *(setup.advice for setup in asset.trade_setups),
            *(setup.cancel_condition for setup in asset.trade_setups),
        ])
        if FUZZY_LANGUAGE.search(generated_text):
            errors.append(f"{prefix} 研究结论含空泛条件词")
        for dimension in asset.dimensions:
            if not dimension.evidence:
                errors.append(f"{prefix} {dimension.label}无证据")
        for setup in asset.trade_setups:
            if setup.entry_low >= setup.entry_high:
                errors.append(f"{prefix} {setup.name}入场区非法")
            if len(setup.targets) != 3 or sum(target.reduce_pct for target in setup.targets) != 100:
                errors.append(f"{prefix} {setup.name}止盈拆分非法")
            if setup.risk_per_trade_pct > .5 or setup.max_leverage > 2:
                errors.append(f"{prefix} {setup.name}风险上限过高")
            if len(setup.rationale) < 4:
                errors.append(f"{prefix} {setup.name}四维依据不足")
            prices = [target.price for target in setup.targets]
            if setup.direction == "做多":
                if setup.stop_loss >= setup.entry_low or not (setup.entry_high < prices[0] < prices[1] < prices[2]):
                    errors.append(f"{prefix} {setup.name}多单止损止盈方向错误")
            else:
                if setup.stop_loss <= setup.entry_high or not (setup.entry_low > prices[0] > prices[1] > prices[2]):
                    errors.append(f"{prefix} {setup.name}空单止损止盈方向错误")
    if errors:
        raise ReportValidationError("；".join(errors))
