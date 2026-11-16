from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import AssetReport, ForecastOutcome, MarketSnapshot
from .storage import ReportStore


def _classify_return(return_pct: float) -> str:
    if return_pct > .5:
        return "上行"
    if return_pct < -.5:
        return "下行"
    return "基准"


def _brier(asset: AssetReport, realized: str) -> float:
    probabilities = {scenario.name: scenario.probability / 100 for scenario in asset.scenarios}
    return round(sum((probabilities.get(name, 0) - (1 if name == realized else 0)) ** 2 for name in ("基准", "上行", "下行")), 6)


def evaluate_asset(report_id: str, asset: AssetReport, snapshot: MarketSnapshot) -> ForecastOutcome | None:
    outcome = _settle_window(report_id, asset.symbol, asset.stance, asset.price, asset.target_low,
                             asset.target_high, asset.as_of, 24, snapshot)
    if outcome:
        outcome.brier_score = _brier(asset, _classify_return(outcome.return_pct))
    return outcome


def _settle_window(report_id, symbol, stance, entry, target_low, target_high, start, hours, snapshot):
    horizon_end = start + timedelta(hours=hours)
    if snapshot.as_of < horizon_end:
        return None
    window = sorted((c for c in snapshot.candles if start <= c.opened_at and
                     c.opened_at + timedelta(hours=1) <= horizon_end), key=lambda c:c.opened_at)
    if len(window) < hours - 1 or any(b.opened_at-a.opened_at != timedelta(hours=1) for a,b in zip(window,window[1:])):
        return None
    exit_price = window[-1].close
    return_pct = (exit_price / entry - 1) * 100
    highest = max(c.high for c in window)
    lowest = min(c.low for c in window)
    bullish = stance in {"看多", "中性偏多"}
    bearish = stance in {"看空", "中性偏空"}
    if bullish:
        direction_correct = return_pct > 0
        target_hit = highest >= target_low
        mfe = (highest / entry - 1) * 100
        mae = (lowest / entry - 1) * 100
    elif bearish:
        direction_correct = return_pct < 0
        target_hit = lowest <= target_high
        mfe = (entry / lowest - 1) * 100
        mae = (entry / highest - 1) * 100
    else:
        direction_correct = abs(return_pct) <= 1
        target_hit = lowest <= target_high and highest >= target_low
        mfe = max((highest / entry - 1) * 100, (entry / lowest - 1) * 100)
        mae = -min(abs((highest / entry - 1) * 100), abs((lowest / entry - 1) * 100))
    return ForecastOutcome(
        report_id=report_id, symbol=symbol, horizon=f"{hours}h", forecast_at=start, evaluated_at=datetime.now(timezone.utc),
        entry_price=entry, exit_price=exit_price, return_pct=round(return_pct, 4),
        direction_correct=direction_correct, target_hit=target_hit,
        max_favorable_excursion_pct=round(mfe, 4), max_adverse_excursion_pct=round(mae, 4),

    )


class ForecastEvaluator:
    def __init__(self, store: ReportStore):
        self.store = store

    def settle(self, snapshots: list[MarketSnapshot]) -> int:
        by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        settled = 0
        for report in self.store.reports_due_for_evaluation(cutoff):
            for asset in report.assets:
                if self.store.outcome_exists(report.id, asset.symbol):
                    continue
                snapshot = by_symbol.get(asset.symbol)
                if snapshot is None:
                    continue
                outcome = _settle_window(report.id, asset.symbol, asset.stance, asset.price,
                                         asset.target_low, asset.target_high, max(report.published_at or report.created_at,asset.as_of),24,snapshot)
                if outcome:
                    outcome.revision = report.revision
                    self.store.save_outcome(outcome)
                    settled += 1
            if report.daily_strategy:
                for row in report.daily_strategy.executive_dashboard:
                    if not row.spot or row.expected_range_low is None or row.expected_range_high is None:
                        continue
                    snap = by_symbol.get(row.symbol)
                    if not snap or self.store.outcome_exists(report.id,row.symbol,"72h"):
                        continue
                    # The daily publishes an interval. Measure containment of the terminal price.
                    outcome = _settle_window(report.id,row.symbol,row.view,row.spot,row.expected_range_low,
                                             row.expected_range_high,report.published_at or report.created_at,72,snap)
                    if outcome:
                        outcome.target_hit = row.expected_range_low <= outcome.exit_price <= row.expected_range_high
                        outcome.revision = report.revision
                        self.store.save_outcome(outcome)
                        settled += 1
        return settled
