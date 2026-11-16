from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from test_pipeline import insight_packet, insight_payload, approved_fixture, approved_review
from backend.app.article_calculations import register_calculations
from backend.app.article_policy import (
    EDITORIAL_VERSION, LENGTH_GUIDANCE, article_blocks, article_fingerprint,
    packet_readiness, publication_issues, review_issues,
)
from backend.app.config import Settings
from backend.app.editorial import EditorialError, InsightGenerationError, _validate_insight, write_insight
from backend.app.insights import prepare_research_packet
from backend.app.models import ArticleNarrative, ResearchFact
from backend.app.providers.news import NewsProvider, INSIGHT_QUERIES, QUERIES
from backend.app.reporting import build_insight_report
from backend.app.service import ResearchService
from backend.app.validation import ReportValidationError, validate_report


class DeepEditorialTests(unittest.TestCase):
    def setUp(self):
        self.packet = insight_packet()
        self.report = build_insight_report([], [], datetime.now(timezone.utc), self.packet)

    def test_no_forced_market_inputs_or_price_charts(self):
        self.assertEqual(self.report.facts, self.packet.facts)
        self.assertEqual(self.report.assets, [])
        self.assertEqual(self.report.charts, [])
        class Service(ResearchService):
            async def collect_market(self):
                raise AssertionError("deep articles must not fetch unrelated prices")
        inputs = asyncio.run(Service(Settings(), None)._collect_insight_inputs(self.packet))
        self.assertEqual(inputs[0], [])

    def test_deep_discovery_is_broad_without_changing_trading_news(self):
        self.assertEqual(NewsProvider(None).queries, QUERIES)
        self.assertEqual(NewsProvider(None, insight=True).queries, INSIGHT_QUERIES)
        self.assertEqual(set(INSIGHT_QUERIES), {"WEB3", "ONCHAIN", "AI", "PEOPLE", "CAPITAL", "EQUITIES"})
        self.assertNotIn("crypto", INSIGHT_QUERIES["CAPITAL"])
        self.assertIn("不设硬性字数上下限", LENGTH_GUIDANCE)

    def test_title_only_and_missing_raw_sources_cannot_enter_writing(self):
        self.packet.source_texts = {}
        self.assertTrue(packet_readiness(self.packet))
        with self.assertRaises(InsightGenerationError):
            asyncio.run(write_insight(self.report, self.packet, Settings(editor_enabled=True, deepseek_api_key="test")))

    def test_single_primary_document_can_support_a_narrow_article(self):
        self.packet.sources = self.packet.sources[:1]
        self.packet.facts = self.packet.facts[:1]
        self.assertEqual(packet_readiness(self.packet), [])
        self.packet.sources[0].source_type = "media"
        self.assertTrue(packet_readiness(self.packet))

    def test_optional_consensus_and_uncited_transition_are_not_forced(self):
        payload = insight_payload()
        payload.pop("consensus_view")
        payload.pop("meridian_view")
        payload["sections"][0]["paragraphs"].append({"text": "理解这一点，需要先看这件事是怎样发生的。", "fact_ids": []})
        _, article, _ = _validate_insight(self.report, payload, False)
        self.assertEqual(article.consensus_view, "")
        self.assertEqual(article.meridian_view, "")

    def test_empty_paragraph_is_rejected_but_numeric_attribution_is_non_blocking(self):
        payload = insight_payload()
        payload["sections"][0]["paragraphs"][0]["text"] = " "
        with self.assertRaises(EditorialError):
            _validate_insight(self.report, payload, False)
        payload = insight_payload()
        payload["sections"][0]["paragraphs"][0] = {"text": "来源提到了 6。", "fact_ids": ["I001"]}
        _validate_insight(self.report, payload, False)

    def test_repeated_sentences_block_even_a_positive_model_review(self):
        payload = insight_payload()
        paragraph = payload["sections"][0]["paragraphs"][0]
        paragraph["text"] *= 3
        self.report.title, self.report.article, _ = _validate_insight(self.report, payload, False)
        self.assertTrue(any("重复" in p for p in review_issues(self.report, approved_review(self.report.article))))

    def test_incomplete_review_blocks_publication(self):
        report = approved_fixture(self.report, self.packet)
        report.deep_research.editorial_review["checked_paths"].pop()
        with self.assertRaises(ReportValidationError):
            validate_report(report)
        self.assertFalse(ResearchService._deep_contract(report))

    def test_review_binds_the_exact_article_and_evidence(self):
        report = approved_fixture(self.report, self.packet)
        self.assertFalse(publication_issues(report))
        report.article.lead += "新增判断。"
        self.assertTrue(publication_issues(report))
        report = approved_fixture(self.report, self.packet)
        report.facts[0].statement += "被修改。"
        self.assertTrue(publication_issues(report))

    def test_no_fixed_report_sections_in_export(self):
        report = approved_fixture(self.report, self.packet)
        for template in ("What Would Prove Us Wrong", "Meridian thesis", "Market consensus", "Thesis Charts"):
            self.assertNotIn(template, report.markdown)

    def calculation(self):
        self.report.facts.append(ResearchFact(id="I100", category="event", statement="输入价格为 0.30 美元/百万token。",
                                             source="example", url=self.packet.sources[0].url, as_of=self.report.as_of))
        return {"id": "D001", "inputs": [
            {"name": "price", "value": "0.30", "unit": "美元/百万token", "fact_ids": ["I100"], "assumption": False, "description": "输入单价"},
            {"name": "volume", "value": "10", "unit": "百万token", "fact_ids": [], "assumption": True, "description": "示例用量"}],
            "expression": "price * volume", "result": "3", "unit": "美元", "explanation": "未包含输出费用"}

    def test_transparent_calculation_is_recomputed_and_labeled(self):
        calculation = self.calculation()
        register_calculations(self.report, [calculation])
        derived = self.report.facts[-1]
        self.assertEqual(derived.id, "D001")
        self.assertEqual(derived.claim_type, "INFERENCE")
        self.assertIn("假设演算，非实际观测", derived.statement)
        payload = insight_payload()
        payload["calculations"] = [calculation]
        payload["sections"][0]["paragraphs"][0] = {"text": "假设用量为 10 百万token，单价 0.30 美元，相乘得到示例费用 3 美元。", "fact_ids": ["D001"]}
        _validate_insight(self.report, payload, False)
        payload["sections"][0]["paragraphs"][0]["text"] = "实际费用为 3 美元。"
        with self.assertRaises(EditorialError):
            _validate_insight(self.report, payload, False)

    def test_wrong_result_unattributed_input_and_code_are_rejected(self):
        calculation = self.calculation()
        for field, value in (("result", "30"), ("expression", "__import__('os')"), ("expression", "price / (volume - volume)")):
            bad = copy.deepcopy(calculation)
            bad[field] = value
            with self.assertRaises(EditorialError):
                register_calculations(self.report, [bad])
        bad = copy.deepcopy(calculation)
        bad["inputs"][0]["value"] = "999"
        with self.assertRaises(EditorialError):
            register_calculations(self.report, [bad])
        self.assertFalse(any(f.id.startswith("D") for f in self.report.facts))

    def test_independent_review_before_publication(self):
        calls = []
        async def completion(client, settings, system, payload, tokens):
            calls.append(payload)
            return (payload["expected"] if "blocks" in payload else insight_payload()), {}
        with patch("backend.app.deep_writing._completion", side_effect=completion):
            report = asyncio.run(write_insight(self.report, self.packet, Settings(editor_enabled=True, deepseek_api_key="test"), client=object()))
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all("blocks" in call for call in calls[1:]))
        validate_report(report)

    def test_missing_review_blocks_generated_draft(self):
        async def completion(client, settings, system, payload, tokens):
            return insight_payload(), {}
        with patch("backend.app.deep_writing._completion", side_effect=completion):
            with self.assertRaises(InsightGenerationError):
                asyncio.run(write_insight(self.report, self.packet, Settings(editor_enabled=True, deepseek_api_key="test"), client=object()))

    def test_packet_extraction_requires_actual_source_excerpt(self):
        source = self.packet.sources[0]
        source.title = "模型如何读取上下文"
        raw = "该模型会复用已处理输入的缓存，避免重复执行同一部分计算。"
        packet_payload = {"topic": "模型缓存", "research_question": "缓存怎样减少重复工作？", "thesis_angle": "解释复用机制", "source_notes": [
            {"source_id": source.id, "thesis_relevance": .9, "key_facts": [
                {"statement": raw, "excerpt": raw},
                {"statement": "研究者声称模型能完全取代真实工程师。", "excerpt": "正文并不存在的引语"}]}]}
        with patch("backend.app.insights.collect_research_sources", return_value=([source], {source.id: raw})), \
             patch("backend.app.insights._flash_packet", return_value=packet_payload):
            packet = asyncio.run(prepare_research_packet([], Settings(editor_enabled=True, deepseek_api_key="test"), None, self.packet.research_date))
        self.assertEqual(len(packet.facts), 1)
        self.assertEqual(packet.source_texts[source.id], raw)
        self.assertIsNone(packet.thesis_candidate)
        self.assertEqual(packet.status, "failed")
        self.assertIn("新闻事件", packet.error)


if __name__ == "__main__":
    unittest.main()
