import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.article_policy import publication_issues
from backend.app.config import Settings
import httpx
from backend.app.editorial import _asset_commentary
from backend.app.front_page import front_page, BJ
from backend.app.ready_desk import publish_ready
from backend.app.reporting import build_report, build_daily_report, build_insight_report
from backend.app.service import ResearchService
from backend.app.storage_local import LocalFileReportStore
from tests.test_pipeline import approved_fixture, flat_snapshot, insight_packet, market_context


class ResearchDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalFileReportStore(Path(self.tmp.name))
        self.now = datetime.now(BJ)
        self.rows = [flat_snapshot(s,self.now) for s in ("BTC","ETH","SOL")]
        self.service = ResearchService(Settings(),self.store)

    def test_old_issue_never_becomes_current_with_fresh_ticks(self):
        report=build_report(self.rows, [], self.now)
        self.store.save(report)
        for row in self.rows:
            row.as_of += timedelta(days=4)
        self.store.save_market(self.rows)
        self.assertIsNone(front_page(self.store,self.now+timedelta(days=4))["headline"])

    def test_breadth_and_cap_divergence_uses_two_different_measures(self):
        ctx=market_context(self.now)
        ctx.aggregate.breadth_universe=100
        ctx.aggregate.breadth_advancers=60
        ctx.aggregate.breadth_decliners=30
        ctx.aggregate.breadth_unchanged=10
        ctx.aggregate.market_cap_change_24h_pct=-1.2
        report=build_daily_report(self.rows,[],self.now,market_context=ctx)
        self.assertIn("分化",report.daily_strategy.market_regime)
        self.assertEqual(report.charts[0].series[0].values,[30,-1.2])

    def test_outage_is_published_without_directional_forecast(self):
        self.service._collect_inputs=AsyncMock(return_value=([],[],{},{},{}))
        report=asyncio.run(self.service.generate("standard"))
        self.assertIn("数据说明",report.title)
        self.assertIsNotNone(self.store.get(report.id))
        self.assertIsNone(front_page(self.store)["headline"])

    def test_ready_reserve_is_invisible_until_release_and_consumed_once(self):
        packet=insight_packet()
        report=approved_fixture(build_insight_report([],[],self.now,packet),packet)
        self.store.queue_article(report,packet)
        self.assertEqual(self.store.list_reports(),[])
        with patch("backend.app.ready_desk.sources_unchanged",new=AsyncMock(return_value=True)):
            published=asyncio.run(publish_ready(self.service,self.now.date()))
        self.assertFalse(publication_issues(published))
        self.assertEqual(published.as_of,report.as_of)
        self.assertFalse(self.store.queued_articles())
        self.assertEqual(len(self.store.list_reports()),1)

    def test_atomic_release_lock(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results=list(executor.map(lambda i:self.store.acquire_lock("same-issue"),range(8)))
        self.assertEqual(sum(results),1)

    def test_truncated_asset_section_retries_only_the_affected_asset(self):
        report = build_report(self.rows, [], self.now)
        row = report.tactical_monitor.dashboard[0]
        fact = next(f for f in report.facts if f.statement.startswith(row.symbol + " "))
        client = AsyncMock()
        request = httpx.Request("POST", "https://example.org/chat")
        def response(reason, content):
            return httpx.Response(200, request=request, json={"choices":[{"finish_reason":reason, "message":{"content":content}}]})
        import json
        client.post.side_effect = [response("length", "{"), response("stop", json.dumps(
            {"text":"现货报价已有记录，后续方向仍需验证。", "fact_ids":[fact.id]}))]
        text, finish = asyncio.run(_asset_commentary(client, report, Settings(), row))
        self.assertEqual(client.post.await_count, 2)
        self.assertIn(f"[{fact.id}]", text)
        self.assertEqual(finish, "stop")

    def test_unrelated_candles_cannot_be_reported_as_yesterdays_return(self):
        report = build_daily_report(self.rows, [], self.now+timedelta(days=8))
        self.assertTrue(all(row.return_day_pct is None for row in report.daily_strategy.executive_dashboard))
        self.assertFalse(any("自然日涨跌" in fact.statement for fact in report.facts))

    def test_purge_preserves_unsettled_daily_forecast_outside_archive(self):
        report = build_daily_report(self.rows, [], self.now)
        report.as_of -= timedelta(days=4)
        self.store.save(report)
        self.assertEqual(self.store.purge_old_reports(), 1)
        self.assertFalse(self.store.list_reports())
        due = self.store.reports_due_for_evaluation(self.now)
        self.assertEqual([(item.id, item.revision) for item in due], [(report.id, report.revision)])

    def test_conflicting_prices_retain_separate_sources_units_and_times(self):
        btc = self.rows[0]
        btc.sources = ["OKX Spot"]
        btc.comparison_price_usd = btc.price * 1.015
        btc.comparison_as_of = btc.as_of - timedelta(minutes=3)
        btc.cross_source_deviation_pct = 1.5
        report = build_report(self.rows, [], self.now)
        spot, comparison = report.facts[:2]
        self.assertEqual((spot.unit, comparison.unit), ("USDT", "USD"))
        self.assertEqual(comparison.as_of, btc.comparison_as_of)
        self.assertNotEqual(spot.url, comparison.url)
        self.assertIn("1.500%", comparison.method)

    def test_published_scheduled_issue_is_idempotent_on_retry(self):
        report = build_report(self.rows, [], self.now, slot_override="20:00")
        published = self.service._publish_current(report, self.now.date())
        self.service._collect_inputs = AsyncMock(side_effect=AssertionError("recollected"))
        retried = asyncio.run(self.service.generate("standard", slot_override="20:00", publication_date=self.now.date()))
        self.assertEqual(retried.id, published.id)
        self.assertEqual(len(self.store.list_reports(include_superseded=True)), 1)
        self.assertEqual(self.store.list_jobs()[0]["stages"][-1]["step"], "listed")

    def test_empty_performance_shows_zero_samples(self):
        self.assertEqual(self.store.performance(90)["total_samples"],0)

