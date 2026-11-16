from __future__ import annotations

import tempfile
import unittest
import asyncio
import json
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.analysis import rsi, technical_assessment
from backend.app.evaluation import evaluate_asset
from backend.app.models import ArticleNarrative, Candle, InsightDraftFragment, LiquidationAssetBreakdown, LiquidationSnapshot, MarketAggregateSnapshot, MarketContextSnapshot, MarketSnapshot, ResearchFact, ResearchNote, ResearchPacket, ResearchSource, SentimentSnapshot
from backend.app.reporting import build_asset_report, build_daily_report, build_insight_report, build_report
from backend.app.scheduler import create_scheduler
from backend.app.storage_local import LocalFileReportStore as ReportStore
from backend.app.validation import ReportValidationError, validate_report
from backend.app.models import FundamentalSnapshot, OptionsSnapshot, ProfessionalMetricsSnapshot
from backend.app.analysis import flow_assessment, fundamental_assessment
from backend.app.providers.options import _parse_instrument
from backend.app.providers.market import MarketProvider
from backend.app.providers.market_context import MarketContextProvider
from backend.app.publishing import Publisher
from backend.app.config import Settings
from backend.app.editorial import EditorialError, InsightGenerationError, _authorize_numeric_fact_ids, _validate_insight, assemble_insight, build_guaranteed_insight, edit_report, insight_fragment_specs, visible_body_char_count, write_insight
from backend.app.insights import (
    _cluster_sources, _extract_source, _numbers_supported, _public_http_url, _score_factors, _select_context,
    collect_research_sources, enrich_packet_facts,
    prepare_research_packet,
)
from backend.app.article_policy import EDITORIAL_VERSION, REVIEW_CRITERIA, article_blocks, article_fingerprint
from backend.app.models import NewsItem
from backend.app.service import ResearchService


def insight_packet() -> ResearchPacket:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    sources = [ResearchSource(
        id=f"S{index:03d}", title=f"公开来源 {index}", url=f"https://source{index}.example/article",
        source=f"source{index}.example", published_at=now, source_type="official" if index == 1 else "media",
        access="full", assets=["BTC"], summary=f"来源 {index} 摘要", key_facts=[f"来源 {index} 的可核验事实"],
        source_score=90, content_chars=2000, text_hash=str(index),
    ) for index in range(1, 7)]
    facts = [ResearchFact(
        id=f"I{index:03d}", category="event", statement=f"来源 {index} 的可核验事实",
        source=f"source{index}.example", as_of=now, url=f"https://source{index}.example/article",
    ) for index in range(1, 7)]
    return ResearchPacket(
        editorial_version=EDITORIAL_VERSION, source_texts={source.id: source.summary for source in sources},
        id=f"insight-packet-{now.date()}", research_date=now.date(), created_at=now, updated_at=now,
        topic="ETF 资金流背后的市场结构变化", research_question="本轮资金流为何在此时改变市场结构？",
        thesis_angle="价格只是结果，资金传导路径才是核心变量。",
        news_brief={**dict.fromkeys(("event","why_now","known","open_question","stakeholders","mechanism","counterpoint","increment","latest"), "测试事件"),
                    "event_date":now.date().isoformat(), "latest_date":now.date().isoformat(), "source_id":sources[0].id, "excerpt":sources[0].summary},
        outline=["异常从何发生", "事件时间线", "为什么是现在", "产业传导", "反方证据", "投资含义"],
        sources=sources, facts=facts, counter_evidence=["资金流可能快速逆转。"],
    )


def insight_payload(chars_per_section: int = 650) -> dict:
    themes = ["事件发生", "时间脉络", "政策窗口", "产业传导", "反方路径", "投资含义"]
    paragraphs = []
    for index, theme in enumerate(themes, 1):
        sentence = f"{theme}要求把新闻事实、资金路径与市场反应放在同一条因果链上核验。"
        paragraphs.append({
            "heading": theme,
            "paragraphs": [{"text": sentence, "fact_ids": [f"I{index:03d}", f"I{(index % 6) + 1:03d}"]}],
        })
    return {
        "narrative_plan": {"central_question": "资金路径如何改变？", "progression": ["具体事件", "机制与限制"]},
        "title": "价格之外：ETF 资金如何重写加密市场结构",
        "research_question": "本轮资金流为何在此时改变市场结构？", "deck": "市场看到的是价格，真正变化的是资金进入产业的路径。",
        "lead": "一项可核验的新变化，正在迫使市场重新审视原有共识。",
        "lead_fact_ids": ["I001", "I002"],
        "consensus_view": "共识认为短期资金流只会放大既有趋势。", "consensus_fact_ids": ["I001", "I003"],
        "meridian_view": "多来源证据表明，资金路径本身正在改变定价结构。", "meridian_fact_ids": ["I002", "I004"],
        "sections": paragraphs, "conclusion": "这不是单纯的价格脉冲，而是一场仍需反方证据持续检验的结构变化。", "conclusion_fact_ids": ["I001", "I005"],
    }


def approved_review(article):
    return {
        "decision": "publish",
        "checks": {key: {"passed": True, "reason": "测试用审核结论"} for key in REVIEW_CRITERIA},
        "block_checks": [{"path": path, "supported": True, "fact_ids": ids, "reason": "测试用引用核验"}
                         for path, (_, ids) in article_blocks(article).items()],
    }


def approved_fixture(report, packet):
    payload = insight_payload()
    report.title, report.article, report.insight_status = _validate_insight(report, payload, False)
    review = approved_review(report.article)
    review.update(fingerprint=article_fingerprint(report), checked_paths=list(article_blocks(report.article)), factual_errors=[])
    spec = insight_fragment_specs(packet)[0]
    now = datetime.now(timezone.utc)
    return assemble_insight(report, [InsightDraftFragment(
        id="fixture", packet_id=packet.id, research_date=packet.research_date,
        fragment_key=spec["fragment_key"], kind="article", position=0, status="complete",
        payload={"article": payload, "review": review, "editorial_version": EDITORIAL_VERSION},
        attempts=1, created_at=now, updated_at=now,
    )])


def snapshot(symbol: str, rising: bool = True) -> MarketSnapshot:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    candles = []
    price = {"BTC": 60000, "ETH": 2500, "SOL": 150}[symbol]
    for index in range(168):
        drift = index * (20 if rising else -20)
        close = price + drift
        candles.append(Candle(
            opened_at=start + timedelta(hours=index), open=close - 5,
            high=close + 25, low=close - 25, close=close, volume=100 + index,
        ))
    return MarketSnapshot(
        symbol=symbol, as_of=candles[-1].opened_at, price=candles[-1].close,
        change_24h_pct=1.8 if rising else -1.8, volume_24h_usd=1_000_000_000,
        market_cap_usd=100_000_000_000, cross_source_deviation_pct=.05,
        funding_rate=.0001, annualized_basis_pct=5, open_interest_usd=2_000_000_000,
        candles=candles, sources=["fixture"],
    )


def flat_snapshot(symbol: str, now: datetime | None = None) -> MarketSnapshot:
    now = now or datetime.now(timezone.utc)
    base = {"BTC": 60000, "ETH": 2500, "SOL": 150}[symbol]
    candles = [Candle(
        opened_at=now - timedelta(hours=167-index), open=base, high=base * 1.001,
        low=base * .999, close=base, volume=100,
    ) for index in range(168)]
    intraday = [Candle(
        opened_at=now - timedelta(minutes=15*(191-index)), open=base, high=base * 1.001,
        low=base * .999, close=base, volume=25,
    ) for index in range(192)]
    return MarketSnapshot(
        symbol=symbol, as_of=now, price=base, change_24h_pct=0, volume_24h_usd=500_000_000,
        market_cap_usd=10_000_000_000, funding_rate=0, annualized_basis_pct=0,
        open_interest_usd=1_000_000_000, derivatives_as_of=now,
        candles=candles, candles_15m=intraday, sources=["fixture"],
    )


def market_context(now: datetime) -> MarketContextSnapshot:
    return MarketContextSnapshot(
        as_of=now,
        aggregate=MarketAggregateSnapshot(
            as_of=now, total_market_cap_usd=2_500_000_000_000,
            total_volume_24h_usd=120_000_000_000, market_cap_change_24h_pct=1.2,
            btc_dominance_pct=55.3, eth_dominance_pct=12.1,
        ),
        sentiment=SentimentSnapshot(as_of=now, value=61, previous_value=55, classification="Greed"),
        liquidations=LiquidationSnapshot(
            as_of=now, coverage="MARKET_WIDE", total_usd_24h=300_000_000,
            long_usd_24h=180_000_000, short_usd_24h=120_000_000,
            assets=[LiquidationAssetBreakdown(symbol="BTC", total_usd_24h=150_000_000, long_usd_24h=90_000_000, short_usd_24h=60_000_000)],
            venues=["Aggregated exchanges"], source="fixture",
        ),
    )


class AnalysisTests(unittest.TestCase):
    def test_rsi_bounds(self):
        self.assertEqual(rsi(list(range(30))), 100)
        self.assertGreaterEqual(rsi([1, 2, 1, 3, 2, 4] * 5), 0)
        self.assertLessEqual(rsi([1, 2, 1, 3, 2, 4] * 5), 100)

    def test_rising_market_scores_positive(self):
        assessment = technical_assessment(snapshot("BTC", rising=True))
        self.assertGreater(assessment.score, 20)
        self.assertEqual(assessment.coverage, 95)


class ReportTests(unittest.TestCase):
    def test_source_fetch_blocks_private_network_and_paywall_body(self):
        self.assertFalse(_public_http_url("http://127.0.0.1/admin"))
        self.assertFalse(_public_http_url("file:///etc/passwd"))
        body = "<html><body><article><h1>Headline</h1><p>Subscribe to continue reading. " + ("paywall filler " * 300) + "</p></article></body></html>"
        async def run():
            import httpx
            transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type":"text/html"}, text=body, request=request))
            item = NewsItem(
                id="N-paywall", sentiment=0,
                title="ETF filing changes market expectations", url="https://publisher.example/story",
                resolved_url=None, source="publisher.example", published_at=datetime.now(timezone.utc),
                summary="This RSS summary provides enough public context to classify the page without treating its paywalled body as evidence. " * 3,
                assets=["BTC"], source_score=90,
            )
            async with httpx.AsyncClient(transport=transport) as client:
                with patch("backend.app.insights._public_http_url", return_value=True):
                    return await _extract_source(client, item)
        source, text = asyncio.run(run())
        self.assertEqual(source.access, "summary")
        self.assertNotIn("paywall filler", text)

    def test_source_deduplication_and_event_clustering(self):
        now = datetime.now(timezone.utc)
        news = [NewsItem(id=f"N{index}", sentiment=0, title="SEC approves new Bitcoin ETF structure", url=f"https://wire.example/{index}", source="wire.example", published_at=now, summary="public summary " * 30, assets=["BTC"], source_score=90) for index in range(2)]
        async def fake_extract(client, item):
            source = ResearchSource(id=f"S{item.url[-1]}", title=item.title, url=item.url, source=item.source, published_at=item.published_at, access="full", assets=item.assets, source_score=90, content_chars=2000, text_hash=item.url)
            return source, item.summary
        with patch("backend.app.insights._extract_source", side_effect=fake_extract):
            sources, _ = asyncio.run(collect_research_sources(news, object()))
        self.assertEqual(len(sources), 1)
        related = insight_packet().sources[:3]
        related[0].title = "SEC approves Bitcoin ETF market structure filing"
        related[1].title = "Bitcoin ETF structure wins SEC approval"
        related[2].title = "Solana validator clients publish performance update"
        clusters = _cluster_sources(related)
        self.assertEqual(sorted(map(len, clusters)), [1, 2])
        factors = _score_factors(related[:2])
        self.assertEqual(set(factors), {"importance", "abnormality", "source_quality", "cross_validation", "data_support", "industry_impact", "timeliness", "primary_documents", "novelty", "follow_up", "investigative_lead"})
        self.assertGreater(factors["cross_validation"], 0)

    def test_insufficient_sources_are_held_without_market_backlog(self):
        packet = asyncio.run(prepare_research_packet([], Settings(editor_enabled=False, deepseek_api_key=None), None, datetime.now(timezone.utc).date()))
        self.assertEqual(packet.status, "failed")
        self.assertEqual(packet.facts, [])
        self.assertIsNone(packet.thesis_candidate)
        self.assertIsNone(packet.narrative_plan)
        self.assertIn("补充研究", packet.error)
        self.assertNotIn("价格", packet.topic)

    def test_good_and_bad_case_source_relevance_regressions(self):
        now = datetime.now(timezone.utc)
        def source(index, title, assets, official=False):
            return ResearchSource(
                id=f"S{index}", title=title, url=f"https://source{index}.example/story",
                source=f"source{index}.example", published_at=now,
                source_type="official" if official else "media", access="full", assets=assets,
                summary=title, source_score=96 if official else 85, source_tier=1 if official else 3,
            )
        good = [
            source(1, "Cosmos vulnerability remained underestimated for four months", ["ATOM"], True),
            source(2, "Four month Cosmos security disclosure timeline", ["ATOM"]),
            source(3, "Cosmos vulnerability remediation and validator impact", ["ATOM"]),
            source(4, "UK crypto tax reporting rules take effect", []),
        ]
        selected_good = _select_context(good)
        self.assertEqual({item.id for item in selected_good}, {"S1", "S2", "S3"})

        bad = [
            source(10, "Stellar tokenized asset growth reaches new milestone", ["XLM"], True),
            source(11, "Stellar tokenized assets expand institutional issuance", ["XLM"]),
            source(12, "Kalshi prediction market enforcement action", []),
            source(13, "Bitcoin Knots governance fork debate", ["BTC"]),
            source(14, "Polygon vulnerability affects bridge security", []),
            source(15, "UK crypto tax reporting statistics", []),
        ]
        selected_bad = _select_context(bad)
        self.assertEqual({item.id for item in selected_bad}, {"S10", "S11"})

    def test_fact_numbers_must_exist_in_the_source_text(self):
        self.assertTrue(_numbers_supported("ETF net inflow reached 320 million", "The filing recorded 320 million in net inflow."))
        self.assertFalse(_numbers_supported("ETF net inflow reached 420 million", "The filing recorded 320 million in net inflow."))

    def test_research_packet_success_error_round_trips_as_a_string(self):
        packet = insight_packet()
        packet.status = "published"
        packet.error = ""
        restored = ResearchPacket.model_validate_json(packet.model_dump_json())
        self.assertEqual(restored.error, "")

    def test_whole_article_fragments_are_content_addressed_and_reviewed(self):
        packet = insight_packet()
        specs = insight_fragment_specs(packet)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["kind"], "article")
        self.assertEqual(specs, insight_fragment_specs(packet))
        packet.source_texts["S001"] += " 新材料"
        self.assertNotEqual(specs[0]["fragment_key"], insight_fragment_specs(packet)[0]["fragment_key"])
        report = build_insight_report([], [], datetime.now(timezone.utc), packet)
        assembled = approved_fixture(report, packet)
        self.assertEqual(assembled.insight_status, "full")
        validate_report(assembled)

    def test_fragment_and_job_audit_survive_storage_round_trip(self):
        packet = insight_packet()
        now = datetime.now(timezone.utc)
        fragment = InsightDraftFragment(
            id="fragment-one", research_date=packet.research_date, packet_id=packet.id,
            fragment_key="sections-01", kind="sections", position=1,
            headings=packet.outline[:2], status="complete", payload={"sections": []},
            attempts=2, created_at=now, updated_at=now,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            store.save_research_packet(packet)
            store.save_insight_fragment(fragment)
            run = store.start_insight_job(packet.research_date, "generate")
            store.finish_insight_job(run, "failed", {"fragments_complete": 1}, "timeout")
            self.assertEqual(store.list_insight_fragments(packet.research_date)[0].attempts, 2)
            restored_run = store.list_insight_jobs(packet.research_date)[0]
            self.assertEqual(restored_run.status, "failed")
            self.assertEqual(restored_run.detail["fragments_complete"], 1)

    def test_failed_legacy_insight_attempt_can_be_removed_without_deleting_current_draft(self):
        packet = insight_packet()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            for key, status in (("sections-legacy", "failed"), ("story-free-v1", "complete")):
                store.save_insight_fragment(InsightDraftFragment(
                    id=key, research_date=packet.research_date, packet_id=packet.id,
                    fragment_key=key, kind="sections", position=1, status=status,
                    payload={"sections": []}, attempts=1, created_at=now, updated_at=now,
                ))
            failed = store.start_insight_job(packet.research_date, "generate")
            store.finish_insight_job(failed, "failed", error="legacy failure")
            succeeded = store.start_insight_job(packet.research_date, "generate")
            store.finish_insight_job(succeeded, "succeeded", {"report_id": "kept"})

            removed = store.purge_failed_insight_attempts(packet.research_date, {"story-free-v1"})

            self.assertEqual(removed, {"fragments": 1, "failed_runs": 1})
            self.assertEqual([item.fragment_key for item in store.list_insight_fragments(packet.research_date)], ["story-free-v1"])
            self.assertEqual([item.status for item in store.list_insight_jobs(packet.research_date)], ["succeeded"])

    def test_insight_working_set_can_be_deleted_without_touching_reports(self):
        packet = insight_packet()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            report = build_insight_report(
                [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)],
                [], now, packet,
            )
            store.save(report)
            store.save_research_packet(packet)
            store.save_insight_fragment(InsightDraftFragment(
                id="draft", research_date=packet.research_date, packet_id=packet.id,
                fragment_key="story-free-v1", kind="sections", position=1,
                status="complete", payload={"sections": []}, attempts=1,
                created_at=now, updated_at=now,
            ))
            run = store.start_insight_job(packet.research_date, "generate")
            store.finish_insight_job(run, "succeeded", {"report_id": report.id})
            date_text = packet.research_date.isoformat()
            self.assertTrue(store.acquire_lock(f"insight-prepare:{date_text}"))
            self.assertTrue(store.acquire_lock(f"insight-generate:{date_text}"))

            removed = store.delete_insight_working_set(packet.research_date)

            self.assertEqual(removed, {"packets": 1, "fragments": 1, "runs": 1, "locks": 2})
            self.assertIsNone(store.get_research_packet(packet.research_date))
            self.assertEqual(store.list_insight_fragments(packet.research_date), [])
            self.assertEqual(store.list_insight_jobs(packet.research_date), [])
            self.assertEqual(store.get(report.id).id, report.id)
            self.assertTrue(store.acquire_lock(f"insight-prepare:{date_text}"))
            self.assertTrue(store.acquire_lock(f"insight-generate:{date_text}"))

    def test_validated_packet_notes_are_promoted_to_citable_facts(self):
        packet = insight_packet()
        packet.timeline = [ResearchNote(label="2026 年 8 月", text="官方发布协议升级说明。", source_ids=[packet.sources[0].id])]
        enrich_packet_facts(packet)
        promoted = [fact for fact in packet.facts if fact.id.startswith("R")]
        self.assertEqual(len(promoted), 1)
        self.assertIn("2026 年 8 月", promoted[0].statement)
        self.assertEqual(promoted[0].source, packet.sources[0].source)

    def test_market_provider_falls_back_to_coinbase_when_binance_is_blocked(self):
        now = int(datetime.now(timezone.utc).timestamp())

        class Response:
            def __init__(self, payload, status=200):
                self.payload, self.status_code = payload, status
                self.is_success = status < 400
            def json(self):
                return self.payload
            def raise_for_status(self):
                if not self.is_success:
                    import httpx
                    raise httpx.HTTPStatusError("blocked", request=httpx.Request("GET", "https://api.binance.com"), response=httpx.Response(self.status_code))

        class Client:
            async def get(self, url, **kwargs):
                if "binance.com" in url:
                    return Response({}, 451)
                if url.endswith("/candles"):
                    return Response([[now - hour * 3600, 99, 102, 100, 101, 10] for hour in reversed(range(168))])
                if url.endswith("/stats"):
                    return Response({"last": "101", "open": "100", "volume": "5000"})
                raise AssertionError(url)

        provider = MarketProvider(Client())
        result = asyncio.run(provider._asset_snapshot(
            "BTC", {"pair": "BTCUSDT", "coinbase": "BTC-USD", "coingecko": "bitcoin"},
            {"current_price": 101, "market_cap": 1_000_000},
        ))
        self.assertEqual(result.sources[0], "Coinbase Exchange")
        self.assertEqual(len(result.candles), 168)
        self.assertAlmostEqual(result.change_24h_pct, 1.0)
        self.assertTrue(any("Binance" in flag for flag in result.quality_flags))

    def test_public_market_context_reports_breadth_and_honest_liquidation_coverage(self):
        macro_at = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).strftime("%Y%m%dT%H%M%S")

        class Response:
            def __init__(self, payload=None, text=""):
                self.payload, self.text = payload, text
            def json(self):
                return self.payload
            def raise_for_status(self):
                return None

        class Client:
            async def get(self, url, **kwargs):
                if url.endswith("/global"):
                    return Response({"data": {
                        "total_market_cap": {"usd": 2_500_000_000_000},
                        "total_volume": {"usd": 100_000_000_000},
                        "market_cap_change_percentage_24h_usd": 1.2,
                        "market_cap_percentage": {"btc": 55, "eth": 12},
                    }})
                if url.endswith("/coins/markets"):
                    return Response([
                        {"price_change_percentage_24h": 2},
                        {"price_change_percentage_24h": -1},
                        {"price_change_percentage_24h": 0},
                    ])
                if "alternative.me" in url:
                    return Response({"data": [
                        {"value": "60", "value_classification": "Greed", "timestamp": "1785542400"},
                        {"value": "52", "value_classification": "Neutral", "timestamp": "1785456000"},
                    ]})
                if url.endswith("bls.ics"):
                    return Response(text=f"BEGIN:VEVENT\nDTSTART;TZID=US-Eastern:{macro_at}\nSUMMARY:Employment Situation\nEND:VEVENT")
                raise AssertionError(url)

        context = asyncio.run(MarketContextProvider(Client()).fetch())
        self.assertEqual(context.aggregate.breadth_universe, 3)
        self.assertEqual(context.aggregate.breadth_advancers, 1)
        self.assertEqual(context.aggregate.breadth_decliners, 1)
        self.assertEqual(context.sentiment.previous_value, 52)
        self.assertEqual(context.liquidations.coverage, "UNAVAILABLE")
        self.assertIn("CoinGlass", context.liquidations.note)
        self.assertTrue(context.macro_calendar)

    def test_vercel_crons_match_all_beijing_report_slots(self):
        config = json.loads((Path(__file__).parents[1] / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["functions"]["api/index.py"]["maxDuration"], 300)
        jobs = {(job["path"], job["schedule"]) for job in config["crons"]}
        fixed = {
            ("/api/cron/standard/08", "0 0 * * *"),
            ("/api/cron/insight/prepare", "0 2 * * *"),
            ("/api/cron/insight/generate", "0 4 * * *"),
            ("/api/cron/insight/retry", "0 6 * * *"),
            ("/api/cron/standard/20", "0 12 * * *"),
            ("/api/cron/daily", "10 16 * * *"),
        }
        self.assertTrue(fixed.issubset(jobs))
        reconcile = {(path, schedule) for path, schedule in jobs if path.startswith("/api/cron/reconcile/")}
        expected = {
            ("/api/cron/reconcile/0035", "35 0 * * *"),
            ("/api/cron/reconcile/0435", "35 4 * * *"),
            ("/api/cron/reconcile/0635", "35 6 * * *"),
            ("/api/cron/reconcile/1235", "35 12 * * *"),
            ("/api/cron/reconcile/1635", "35 16 * * *"),
        }
        self.assertEqual(reconcile, expected)
        prepress = {(f"/api/cron/prepress/{key}", schedule) for key, schedule in (
            ("daily", "2 16 * * *"), ("08", "57 23 * * *"), ("20", "57 11 * * *"),
            ("article", "15 2 * * *"), ("reserve", "25 1 * * *"))}
        self.assertEqual(jobs, fixed | reconcile | prepress)

    def test_empty_insight_pipeline_is_hidden(self):
        root = Path(__file__).parents[1]
        html = (root / "insights.html").read_text(encoding="utf-8")
        script = (root / "insights.js").read_text(encoding="utf-8")
        self.assertIn('id="pipeline-panel" aria-live="polite" hidden', html)
        self.assertIn('if(empty){panel.hidden=true;return}', script)
        self.assertIn('panel.hidden=false', script)

    def test_report_contract_and_storage_round_trip(self):
        report = build_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)],
            [], datetime(2026, 7, 11, 8, tzinfo=timezone.utc),
        )
        self.assertEqual({asset.symbol for asset in report.assets}, {"BTC", "ETH", "SOL"})
        tactical_prices = {asset.symbol: asset.price for asset in report.tactical_monitor.dashboard}
        self.assertEqual({asset.symbol: asset.price for asset in report.assets}, tactical_prices)
        self.assertIsNotNone(report.tactical_monitor)
        self.assertIsNone(report.daily_strategy)
        self.assertIn("## 相对上个检查点", report.markdown)
        self.assertIn("## BTC / ETH / SOL 具体做单建议", report.markdown)
        self.assertNotIn("56%", report.markdown)
        self.assertEqual(len(report.tactical_monitor.dashboard), 3)
        for asset in report.tactical_monitor.dashboard:
            self.assertTrue(all(item.horizon == "INTRADAY" for item in asset.observations))
            self.assertIn(asset.action, {"WAIT", "NO_ACTION"})
        validate_report(report)
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            store.save(report)
            loaded = store.latest()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.id, report.id)
            self.assertEqual(len(store.list()), 1)

    def test_scheduler_has_daily_briefs_and_insight_pipeline(self):
        class Service:
            async def generate(self, report_type="standard", slot_override=None, prepare_only=False):
                return None
            async def collect_market(self):
                return None
            async def collect_context(self):
                return None
            async def prewrite_insight(self):
                return None
            async def prepare_insight(self):
                return None
            async def generate_insight(self, short=False):
                return None
            async def reconcile(self, force=False):
                return None
            def cleanup_reports(self):
                return 0
        scheduler = create_scheduler(Service(), "Asia/Shanghai")
        job_ids = {job.id for job in scheduler.get_jobs()}
        self.assertEqual({"report-08:00", "report-20:00"}, {item for item in job_ids if item.startswith("report-")})
        self.assertIn("monitor-market", job_ids)
        self.assertIn("monitor-context", job_ids)
        self.assertIn("cleanup-reports", job_ids)
        self.assertIn("daily-00:10", job_ids)
        self.assertIn("insight-prepare-10:00", job_ids)
        self.assertIn("insight-pro-12:00", job_ids)
        self.assertIn("insight-retry-14:00", job_ids)
        self.assertIn("reconcile-products", job_ids)
        self.assertEqual(scheduler.get_job("report-08:00").kwargs["slot_override"], "08:00")

    def test_daily_is_flash_report_and_insight_is_separate(self):
        snapshots = [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)]
        packet = insight_packet()
        daily = build_daily_report(snapshots, [], datetime(2026, 7, 12, 0, tzinfo=timezone.utc))
        insight = build_insight_report(snapshots, [], datetime.now(timezone.utc), packet)
        self.assertEqual((daily.report_type, daily.slot), ("daily", "00:10"))
        self.assertIsNotNone(daily.daily_strategy)
        self.assertIsNone(daily.tactical_monitor)
        self.assertIsNone(daily.article)
        self.assertEqual(daily.editorial_audit.status, "deterministic")
        self.assertEqual((insight.report_type, insight.slot), ("insight", "12:00"))
        self.assertIsNotNone(insight.deep_research)
        self.assertIsNone(insight.article)
        self.assertEqual(insight.research_packet_id, packet.id)
        self.assertEqual(len(daily.charts), 0)
        self.assertEqual(len(insight.charts), 0)
        self.assertNotEqual(set(type(daily.daily_strategy).model_fields), set(type(insight.deep_research).model_fields))

    def test_trading_products_publish_but_deep_requires_editorial_approval(self):
        now = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
        tactical = build_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], now, slot_override="08:00")
        daily = build_daily_report([], [], now)
        packet = insight_packet()
        deep = build_insight_report([], [], now, packet)
        self.assertTrue(all(state.reference_setups for state in tactical.tactical_monitor.dashboard))
        validate_report(tactical)
        validate_report(daily)
        with self.assertRaises(ReportValidationError):
            validate_report(deep)
        deep = approved_fixture(deep, packet)
        validate_report(deep)

    def test_service_holds_deep_research_when_writer_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            packet = insight_packet()
            store.save_research_packet(packet)
            service = ResearchService(Settings(editor_enabled=False, deepseek_api_key=None), store)
            with self.assertRaises(InsightGenerationError):
                asyncio.run(service.generate_insight())
            self.assertIsNone(store.latest(report_types={"insight"}))
            self.assertEqual(store.get_research_packet(packet.research_date).status, "failed")
            self.assertEqual(store.list_insight_jobs(packet.research_date)[0].status, "failed")

    def test_daily_and_tactical_schemas_are_distinct_and_probabilities_are_not_fabricated(self):
        snapshots = [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)]
        now = datetime(2026, 7, 12, 8, tzinfo=timezone.utc)
        daily = build_daily_report(snapshots, [], now)
        tactical = build_report(snapshots, [], now, slot_override="08:00")
        self.assertIsNone(daily.tactical_monitor)
        self.assertIsNone(tactical.daily_strategy)
        self.assertNotIn('"probability"', daily.daily_strategy.model_dump_json())
        self.assertLessEqual(len(daily.daily_strategy.preferred_trades), 3)
        self.assertTrue(all(item.action in {"WAIT", "NO_ACTION"} for item in tactical.tactical_monitor.dashboard))

    def test_unclear_tactical_view_keeps_two_conditional_plans_per_asset(self):
        now = datetime.now(timezone.utc)
        report = build_report(
            [flat_snapshot("BTC", now), flat_snapshot("ETH", now), flat_snapshot("SOL", now)],
            [], now.astimezone(ZoneInfo("Asia/Shanghai")), slot_override="08:00",
            market_context=market_context(now),
        )
        for state in report.tactical_monitor.dashboard:
            self.assertEqual(state.decision, "UNCLEAR")
            self.assertIsNone(state.primary_setup)
            self.assertEqual({plan.direction for plan in state.conditional_setups}, {"LONG", "SHORT"})
            self.assertTrue(all(plan.action == "WAIT" for plan in state.conditional_setups))
        self.assertIn("走势不明，当前观望", report.markdown)
        validate_report(report)

    def test_tactical_primary_or_conditional_plan_has_complete_geometry(self):
        now = datetime.now(timezone.utc)
        report = build_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)], [],
            now.astimezone(ZoneInfo("Asia/Shanghai")), slot_override="20:00",
        )
        for state in report.tactical_monitor.dashboard:
            self.assertTrue(state.primary_setup or len(state.conditional_setups) == 2)
            for plan in state.reference_setups:
                self.assertEqual(len(plan.target_plan), 3)
                self.assertEqual(sum(item.reduce_pct for item in plan.target_plan), 100)
        validate_report(report)

    def test_daily_uses_previous_beijing_calendar_day_without_trades(self):
        local_now = datetime(2026, 7, 12, 0, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        utc_now = local_now.astimezone(timezone.utc)
        report = build_daily_report(
            [flat_snapshot("BTC", utc_now), flat_snapshot("ETH", utc_now), flat_snapshot("SOL", utc_now)],
            [], local_now, market_context=market_context(utc_now),
        )
        daily = report.daily_strategy
        self.assertEqual(daily.session_window.session_date.isoformat(), "2026-07-11")
        self.assertEqual(len(daily.executive_dashboard), 3)
        self.assertEqual(len(daily.chain_activity), 3)
        self.assertEqual({item.name for item in daily.outlook}, {"BASE", "BULL", "BEAR"})
        self.assertEqual(daily.preferred_trades, [])
        self.assertNotIn("Preferred Trades", report.markdown)
        self.assertIn("复盘", daily.objective)
        self.assertIn("完整自然日", daily.objective)
        validate_report(report)

    def test_report_page_prioritizes_tactical_cards_and_hides_daily_trade_placeholder(self):
        script = (Path(__file__).parents[1] / "reports.js").read_text(encoding="utf-8")
        self.assertIn('className="tactical-action-grid"', script)
        self.assertIn('className="daily-asset-grid"', script)
        self.assertIn('className="asset-market-tape"', script)
        self.assertIn('open_interest_change_1h_pct', script)
        self.assertNotIn('productSection("TRADES","Preferred Trades")', script)

    def test_thesis_uses_explicit_invalidation_language(self):
        report = build_daily_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime(2026, 7, 11, 8, tzinfo=timezone.utc))
        for asset in report.daily_strategy.executive_dashboard:
            self.assertTrue(asset.invalidation)
            self.assertNotEqual(asset.confidence.data_coverage, asset.confidence.view_conviction)
        validate_report(report)

    def test_standard_report_accepts_cron_slot_override(self):
        report = build_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [],
            datetime(2026, 7, 12, 18, 30, tzinfo=timezone.utc), slot_override="16:00",
        )
        self.assertEqual(report.slot, "16:00")

    def test_missing_market_fields_are_null_and_render_as_na(self):
        snapshots = [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")]
        snapshots[0].funding_rate = None
        snapshots[0].open_interest_usd = None
        report = build_daily_report(snapshots, [], datetime(2026, 7, 11, 8, tzinfo=timezone.utc))
        btc = next(item for item in report.daily_strategy.executive_dashboard if item.symbol == "BTC")
        observations = {item.key: item for item in btc.observations}
        self.assertIsNone(observations["funding"].value)
        self.assertIsNone(observations["open_interest"].value)
        self.assertTrue(any(group.availability in {"PARTIAL", "UNAVAILABLE"} for group in report.daily_strategy.data_health_groups))
        self.assertNotIn("Preferred Trades", report.markdown)
        self.assertNotIn("未平仓名义价值：$0.00B", report.markdown)
        self.assertNotIn("+0.0000%", report.markdown)
        validate_report(report)

    def test_options_and_bitcoin_fundamental_raise_coverage(self):
        market = snapshot("BTC")
        options = OptionsSnapshot(
            symbol="BTC", as_of=datetime.now(timezone.utc), total_open_interest_usd=10_000_000_000,
            put_call_oi_ratio=.8, near_atm_iv_pct=45, near_call_put_iv_spread_pct=1,
            term_iv_slope_pct=3, contracts_count=500, sources=["fixture"],
        )
        self.assertEqual(flow_assessment(market, options).coverage, 88)
        professional = ProfessionalMetricsSnapshot(
            symbol="BTC", as_of=datetime.now(timezone.utc), spot_etf_net_flow_usd=500_000_000,
            exchange_netflow_usd=-100_000_000, options_25d_skew_pct=2,
            cme_basis_annualized_pct=8, sources=["fixture"],
        )
        self.assertEqual(flow_assessment(market, options, professional).coverage, 98)
        fundamental = FundamentalSnapshot(
            symbol="BTC", as_of=datetime.now(timezone.utc), hashrate_eh_s=900,
            hashrate_change_30d_pct=2, difficulty=130e12, difficulty_change_30d_pct=1,
            fees_24h_usd=500_000, fees_change_1d_pct=5, sources=["fixture"],
        )
        self.assertEqual(fundamental_assessment(market, fundamental).coverage, 70)

    def test_deribit_instrument_parser(self):
        expiry, strike, option_type = _parse_instrument("BTC-25DEC26-100000-C")
        self.assertEqual((expiry.year, expiry.month, expiry.day), (2026, 12, 25))
        self.assertEqual(strike, 100000)
        self.assertEqual(option_type, "C")

    def test_publisher_without_targets_is_noop(self):
        report = build_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime(2026, 7, 11, 8, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            settings = Settings(data_dir=Path(directory), telegram_bot_token=None, telegram_chat_id=None, webhook_urls=())
            self.assertEqual(asyncio.run(Publisher(settings, store).publish(report)), [])

    def test_deepseek_routes_v41_flash_for_reports(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): return None
            def json(self): return self.payload

        class Client:
            def __init__(self): self.requests = []
            async def post(self, url, **kwargs):
                self.requests.append(kwargs["json"])
                report_type = "standard" if kwargs["json"]["model"].endswith("flash") else "article"
                if report_type == "standard":
                    content = {"title":"战术简报｜价格与资金出现分歧","executive_judgement":"价格结构占优，但资金确认仍是执行边界。","takeaways":[
                        {"text":"主判断沿价格结构展开。","fact_ids":["F001"]},
                        {"text":"相对变化由主线资产贡献。","fact_ids":["F008"]},
                        {"text":"风险以结构失效位定义。","fact_ids":["F007"]},
                    ]}
                else:
                    long_text = "价格先给出方向，但资金和基本面仍在决定这次变化究竟是趋势还是重新定价。" * 20
                    content = {
                        "title":"价格走在证据前面",
                        "research_question":"相对强势能否变成持续趋势？",
                        "deck":"市场已经投票，证据仍需扩散。",
                        "lead":long_text,
                        "consensus_view":"市场把主线资产视为相对强者。",
                        "meridian_view":"当前判断保留明确的验证边界。",
                        "sections":[
                            {"heading":"价格先动了","paragraphs":[{"text":long_text,"fact_ids":["F001","F008"]}]},
                            {"heading":"资金决定持续性","paragraphs":[{"text":long_text,"fact_ids":["F005","F012"]}]},
                            {"heading":"反方路径","paragraphs":[{"text":long_text,"fact_ids":["F007","F014"]}]},
                        ],
                        "conclusion":"强势只有获得更多证据确认，才会从相对表现升级为趋势。",
                    }
                return Response({"choices":[{"message":{"content":json.dumps(content, ensure_ascii=False)}}],"usage":{"prompt_tokens":100,"completion_tokens":50}})

        settings = Settings(editor_enabled=True, deepseek_api_key="test-key")
        client = Client()
        standard = build_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc))
        edited_standard = asyncio.run(edit_report(standard, settings, client))
        self.assertEqual(client.requests[-1]["model"], "deepseek-flash")
        self.assertEqual(edited_standard.editorial_audit.status, "model")
        daily = build_daily_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc))
        edited_daily = asyncio.run(edit_report(daily, settings, client))
        self.assertEqual(client.requests[-1]["model"], "deepseek-flash")
        self.assertEqual(edited_daily.editorial_audit.status, "model")

    def test_insight_streams_one_whole_draft_and_publishes(self):
        class Response:
            def __init__(self, content): self.content = content
            def raise_for_status(self): return None
            async def aiter_lines(self):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": self.content}}]})
                yield 'data: {"usage":{"prompt_tokens":100,"completion_tokens":9000},"choices":[]}'
                yield "data: [DONE]"
        class Stream:
            def __init__(self, content): self.content = content
            async def __aenter__(self): return Response(self.content)
            async def __aexit__(self, *args): return None
        class Client:
            def __init__(self): self.requests = []
            def stream(self, method, url, **kwargs):
                body = kwargs["json"]
                self.requests.append(body)
                incoming = json.loads(body["messages"][1]["content"])
                payload = incoming["expected"] if "blocks" in incoming else insight_payload()
                return Stream(json.dumps(payload, ensure_ascii=False))
        packet = insight_packet()
        report = build_insight_report([], [], datetime.now(timezone.utc), packet)
        client = Client()
        edited = asyncio.run(write_insight(report, packet, Settings(editor_enabled=True, deepseek_api_key="test-key"), client=client))
        self.assertGreaterEqual(len(client.requests), 2)
        self.assertTrue(all(request["stream"] for request in client.requests))
        self.assertEqual(client.requests[0]["max_tokens"], 20000)
        self.assertEqual(edited.insight_status, "full")
        self.assertLess(visible_body_char_count(edited.article), 2000)
        self.assertIsNone(edited.deep_research.critique)
        validate_report(edited)

    def test_deep_body_has_no_hard_lower_or_upper_length_bound(self):
        packet = insight_packet()
        report = build_insight_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc), packet)

        def payload_for(target):
            payload = {
                "title": "边界测试",
                "research_question": "什么构成可见正文？", "deck": "标题与副标题不计入正文。",
                "lead": "导语", "lead_fact_ids": ["I001"],
                "consensus_view": "共识", "consensus_fact_ids": ["I002"],
                "meridian_view": "判断", "meridian_fact_ids": ["I003"],
                "sections": [{"heading": "正文", "paragraphs": [{"text": "研", "fact_ids": ["I004"]}]}],
                "conclusion": "结论", "conclusion_fact_ids": ["I005"],
            }
            base = visible_body_char_count(ArticleNarrative.model_validate({key: value for key, value in payload.items() if key != "title"}))
            payload["sections"][0]["paragraphs"][0]["text"] = "研" * (target - base + 1)
            return payload

        for target in (50, 1999, 2000, 5000, 12000):
            _, article, status = _validate_insight(report, payload_for(target), False)
            self.assertEqual(visible_body_char_count(article), target)
            self.assertEqual(status, "full")
        _, longform, status = _validate_insight(report, payload_for(5001), False)
        self.assertEqual(visible_body_char_count(longform), 5001)
        self.assertEqual(status, "full")

    def test_legacy_template_fallback_is_disabled(self):
        packet = insight_packet()
        report = build_insight_report([], [], datetime.now(timezone.utc), packet)
        with self.assertRaises(InsightGenerationError):
            build_guaranteed_insight(report, packet, "writer timeout")
        self.assertIsNone(report.article)

    def test_numeric_fact_ids_are_repaired_only_from_the_fact_ledger(self):
        packet = insight_packet()
        report = build_insight_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc), packet,
        )
        numeric_fact = report.facts[0]
        unrelated_fact = next(fact for fact in report.facts if fact.id != numeric_fact.id)
        repaired = _authorize_numeric_fact_ids(
            report, numeric_fact.statement, [unrelated_fact.id],
        )
        self.assertIn(numeric_fact.id, repaired)
        with self.assertRaises(EditorialError):
            _authorize_numeric_fact_ids(report, "未经事实支持的数值为 987654321。", [numeric_fact.id])

    def test_numeric_fact_ids_allow_display_rounding_and_iso_dates(self):
        packet = insight_packet()
        report = build_insight_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc), packet,
        )
        numeric_fact = report.facts[0]
        numeric_fact.statement = "资金费率为 -0.044125971285960616%，观察窗口为 1H。"
        numeric_fact.as_of = datetime(2026, 9, 11, tzinfo=timezone.utc)
        repaired = _authorize_numeric_fact_ids(
            report,
            "截至 2026-09-11，资金费率约为 -0.044%，观察窗口为 1 小时。",
            [numeric_fact.id],
        )
        self.assertEqual(repaired, [numeric_fact.id])

    def test_tactical_no_action_keeps_complete_reference_plans(self):
        report = build_report(
            [snapshot("BTC"), snapshot("ETH"), snapshot("SOL", rising=False)], [],
            datetime.now(timezone.utc), slot_override="08:00",
        )
        for state in report.tactical_monitor.dashboard:
            self.assertTrue(state.reference_setups)
            for plan in state.reference_setups:
                self.assertEqual(len(plan.target_plan), 3)
                self.assertEqual([target.reduce_pct for target in plan.target_plan], [40, 30, 30])
                prices = [target.price for target in plan.target_plan]
                if plan.direction == "LONG":
                    self.assertLess(plan.stop, plan.entry_low)
                    self.assertLess(plan.entry_high, prices[0])
                else:
                    self.assertGreater(plan.stop, plan.entry_high)
                    self.assertGreater(plan.entry_low, prices[0])
        self.assertIn("## 一句话大盘", report.markdown)
        self.assertIn("观察入场区", report.markdown)
        self.assertIn("硬止损", report.markdown)
        self.assertIn("TP3", report.markdown)
        validate_report(report)

    def test_publication_revision_hides_superseded_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            first = build_daily_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc))
            first.publication_key = "daily:2026-08-31:00:10"
            store.publish_current(first)
            second = first.model_copy(deep=True)
            second.id = "daily-revision-two"
            second.created_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            store.publish_current(second)
            self.assertEqual(store.latest(report_types={"daily"}).id, second.id)
            self.assertEqual(len(store.list(report_types={"daily"})), 1)
            audit = store.list(report_types={"daily"}, include_superseded=True)
            self.assertEqual(len(audit), 2)
            self.assertEqual(store.get(first.id).publication_status, "SUPERSEDED")
            self.assertEqual(store.get(second.id).revision, 2)

            third = second.model_copy(deep=True)
            third.id = first.id
            third.created_at = datetime.now(timezone.utc) + timedelta(seconds=2)
            store.publish_current(third)
            self.assertNotEqual(third.id, first.id)
            self.assertEqual(len(store.list(report_types={"daily"}, include_superseded=True)), 3)
            self.assertEqual(store.get(first.id).revision, 1)

    def test_daily_editor_failure_falls_back_and_still_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            service = ResearchService(Settings(editor_enabled=True, deepseek_api_key="fixture"), store)

            async def fixture_inputs():
                return [flat_snapshot("BTC"), flat_snapshot("ETH"), flat_snapshot("SOL")], [], {}, {}, {}

            service._collect_inputs = fixture_inputs
            with patch("backend.app.service.edit_report", side_effect=ReportValidationError("非法交易区间")):
                report = asyncio.run(service.generate("daily"))
            self.assertEqual(report.publication_status, "CURRENT")
            self.assertEqual(report.editorial_audit.status, "fallback")
            self.assertIn("编辑或合同校验失败", report.editorial_audit.warnings[0])
            self.assertIsNotNone(store.latest(report_types={"daily"}))
            validate_report(report)

    def test_deep_hold_does_not_block_trading_products(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            packet = insight_packet()

            class OfflineService(ResearchService):
                async def _collect_inputs(self):
                    return [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], {}, {}, {}

                async def _collect_insight_inputs(self, current_packet):
                    return [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], {}, {}, {}

                async def prepare_insight(self, force=False):
                    local_date = datetime.now(timezone.utc).astimezone(ZoneInfo(self.settings.timezone)).date()
                    packet.research_date = local_date
                    packet.id = f"insight-packet-{local_date.isoformat()}"
                    self.store.save_research_packet(packet)
                    return packet

            service = OfflineService(Settings(editor_enabled=False, deepseek_api_key=None), store)
            result = asyncio.run(service.reconcile(force=True))
            self.assertEqual(set(result["products"]), {"daily", "deep", "tactical"})
            self.assertEqual(result["products"]["deep"]["action"], "held")
            self.assertEqual(result["products"]["daily"]["action"], "published")
            self.assertEqual(result["products"]["tactical"]["action"], "published")
            self.assertEqual(result["status"], "partial")
            self.assertEqual(len(store.list(limit=10)), 2)
            self.assertIsNone(store.latest(report_types={"insight"}))

    def test_force_reconcile_can_leave_deep_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")

            class TacticalDailyOnlyService(ResearchService):
                async def _collect_inputs(self):
                    return [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], {}, {}, {}

                async def prepare_insight(self, force=False):
                    raise AssertionError("Deep must not be prepared by a Tactical/Daily-only reconcile")

                async def generate_insight(self, *args, **kwargs):
                    raise AssertionError("Deep must not be generated by a Tactical/Daily-only reconcile")

            service = TacticalDailyOnlyService(Settings(editor_enabled=False, deepseek_api_key=None), store)
            result = asyncio.run(service.reconcile(force=True, include_deep=False))
            self.assertEqual(set(result["products"]), {"daily", "tactical"})
            self.assertTrue(all(item["action"] == "published" for item in result["products"].values()))
            self.assertIsNone(store.latest(report_types={"insight"}))

    def test_reconcile_hides_miskeyed_post_midnight_legacy_daily(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            service = ResearchService(Settings(editor_enabled=False, deepseek_api_key=None), store)
            tz = ZoneInfo(service.settings.timezone)
            publication_date = datetime.now(tz).date() - timedelta(days=1)
            current = build_daily_report(
                [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [],
                datetime.combine(
                    publication_date + timedelta(days=1), datetime.min.time().replace(minute=10), tzinfo=tz,
                ),
                {}, {}, {}, None, None,
            )
            current.publication_key = f"daily:{publication_date.isoformat()}:00:10"
            store.save(current)
            legacy = current.model_copy(deep=True)
            legacy.id = "miskeyed-next-day-daily"
            legacy.slot = "24:00"
            legacy.publication_key = f"daily:{(publication_date + timedelta(days=1)).isoformat()}:24:00"
            legacy.as_of = datetime.combine(
                publication_date + timedelta(days=1), datetime.min.time().replace(minute=30), tzinfo=tz,
            ).astimezone(timezone.utc)
            store.save(legacy)

            service._retire_miskeyed_legacy_daily(current, publication_date)
            retired = store.get(legacy.id)
            self.assertEqual(retired.publication_status, "SUPERSEDED")
            self.assertEqual(retired.superseded_by_id, current.id)

    def test_report_retention_save_and_delete(self):
        report = build_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc))
        report.id = "old-report"
        report.as_of = datetime.now(timezone.utc) - timedelta(days=4)
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            store.save(report)
            self.assertTrue(store.set_saved(report.id, True))
            self.assertEqual(store.purge_old_reports(3), 0)
            self.assertTrue(store.list()[0]["is_saved"])
            self.assertTrue(store.set_saved(report.id, False))
            self.assertEqual(store.purge_old_reports(3), 1)
            self.assertIsNone(store.get(report.id))
            report.id = "delete-me"
            report.as_of = datetime.now(timezone.utc)
            store.save(report)
            self.assertTrue(store.delete_report(report.id))
            self.assertFalse(store.delete_report(report.id))

    def test_insight_retention_is_seven_days_and_saved_is_permanent(self):
        snapshots = [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")]
        packet = insight_packet()
        old_report = build_report(snapshots, [], datetime.now(timezone.utc)); old_report.id="brief-four-days"; old_report.as_of=datetime.now(timezone.utc)-timedelta(days=4)
        insight = build_insight_report(snapshots, [], datetime.now(timezone.utc), packet); insight.id="insight-four-days"; insight.as_of=datetime.now(timezone.utc)-timedelta(days=4)
        with tempfile.TemporaryDirectory() as directory:
            store=ReportStore(Path(directory)/"reports.db"); store.save(old_report); store.save(insight)
            self.assertEqual(store.purge_old_reports(3,7),1)
            self.assertIsNotNone(store.get(insight.id))
            insight.as_of=datetime.now(timezone.utc)-timedelta(days=8);store.save(insight);store.set_saved(insight.id,True)
            self.assertEqual(store.purge_old_reports(3,7),0)
            self.assertIsNotNone(store.get(insight.id))

    def test_retry_skips_when_today_already_has_an_insight(self):
        packet = insight_packet()
        existing = build_insight_report([snapshot("BTC"), snapshot("ETH"), snapshot("SOL")], [], datetime.now(timezone.utc), packet)
        existing = approved_fixture(existing, packet)
        existing.as_of = datetime.now(timezone.utc)
        class Store:
            def latest(self, report_types=None, exclude_types=None): return existing
            def acquire_lock(self, *args, **kwargs): raise AssertionError("retry must skip before acquiring a lock")
        result = asyncio.run(ResearchService(Settings(), Store()).generate_insight(short=True))
        self.assertIs(result, existing)

    def test_database_task_lock_is_exclusive_and_releasable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory) / "reports.db")
            self.assertTrue(store.acquire_lock("insight:2026-08-26"))
            self.assertFalse(store.acquire_lock("insight:2026-08-26"))
            store.release_lock("insight:2026-08-26")
            self.assertTrue(store.acquire_lock("insight:2026-08-26"))

    def test_forecast_evaluation(self):
        base = snapshot("BTC")
        asset = build_asset_report(base, [])
        # Move the forecast timestamp into the historical candle range.
        asset.as_of = base.candles[-30].opened_at
        asset.price = base.candles[-30].close
        outcome = evaluate_asset("legacy-evaluation", asset, base)
        self.assertIsNotNone(outcome)
        self.assertGreater(outcome.return_pct, 0)
        self.assertTrue(outcome.direction_correct)
        self.assertGreaterEqual(outcome.brier_score, 0)


if __name__ == "__main__":
    unittest.main()
