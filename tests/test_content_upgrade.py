import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.app.article_policy import newsworthiness_issues
from backend.app.reporting import build_daily_report, build_report
from tests.test_pipeline import flat_snapshot, insight_packet


class ContentUpgradeTests(unittest.TestCase):
    def test_old_document_cannot_be_released_with_new_fetch_date(self):
        packet = insight_packet()
        date = packet.research_date
        self.assertFalse(newsworthiness_issues(packet, date))
        self.assertTrue(newsworthiness_issues(packet, date + timedelta(days=7)))
        packet.news_brief["latest_excerpt"] = "unrelated words"
        self.assertTrue(newsworthiness_issues(packet, date))

    def test_checkpoint_cannot_mix_exchanges_or_look_forward(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        snapshot = flat_snapshot("BTC", now)
        prior = build_report([snapshot], [], now)
        snapshot.as_of = now + timedelta(hours=1)
        snapshot.sources = ["different venue"]
        snapshot.price *= 1.02
        row = build_report([snapshot], [], snapshot.as_of, previous_report=prior).tactical_monitor.dashboard[0]
        self.assertIsNone(row.price_change_since_checkpoint_pct)
        self.assertIn("不可比", row.comparison_notes[0])

    def test_daily_notes_reach_export_and_json(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        report = build_daily_report([flat_snapshot("BTC", now)], [], now)
        notes = report.daily_strategy.executive_dashboard[0].comparison_notes
        self.assertTrue(notes)
        self.assertTrue(all(note in report.markdown for note in notes))
        self.assertEqual(report.model_dump()["daily_strategy"]["executive_dashboard"][0]["comparison_notes"], notes)

