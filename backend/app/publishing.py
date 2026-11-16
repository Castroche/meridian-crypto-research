from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .config import Settings
from .models import ResearchReport
from .storage import ReportStore


class Publisher:
    def __init__(self, settings: Settings, store: ReportStore):
        self.settings = settings
        self.store = store

    async def publish(self, report: ResearchReport) -> list[dict]:
        targets: list[tuple[str, str]] = []
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            targets.append(("telegram", "telegram"))
        targets.extend((f"webhook:{urlparse(url).netloc or 'configured'}", url) for url in self.settings.webhook_urls)
        if not targets:
            return []
        results = []
        async with httpx.AsyncClient(timeout=self.settings.http_timeout, follow_redirects=True) as client:
            for channel, target in targets:
                try:
                    if channel == "telegram":
                        await self._telegram(client, report)
                    else:
                        response = await client.post(target, json=report.model_dump(mode="json"))
                        response.raise_for_status()
                    status, detail = "sent", "HTTP delivery accepted"
                except Exception as exc:
                    status, detail = "failed", f"{type(exc).__name__}: {exc}"
                self.store.save_delivery(report.id, channel, status, detail)
                results.append({"channel": channel, "status": status, "detail": detail})
        return results

    async def _telegram(self, client: httpx.AsyncClient, report: ResearchReport) -> None:
        if report.report_type == "insight" and report.article:
            lines = [
                report.title, report.article.deck, "", f"核心判断：{report.article.meridian_view}", "",
                f"阅读全文：{self.settings.public_base_url}/insights?id={report.id}",
                "研究信息不构成投资建议。",
            ]
            url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
            response = await client.post(url, json={"chat_id": self.settings.telegram_chat_id, "text": "\n".join(lines), "disable_web_page_preview": False})
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "Telegram rejected message"))
            return
        lines = [report.title, f"数据时点：{report.as_of.isoformat()}", ""]
        for asset in report.assets:
            lines.append(
                f"{asset.symbol}｜{asset.stance}｜置信度 {asset.conviction}/100｜"
                f"目标 {asset.target_low:g}–{asset.target_high:g}｜失效 {asset.invalidation_price:g}"
            )
        lines.extend(["", f"完整报告：{self.settings.public_base_url}/api/reports/{report.id}/markdown", "研究信息不构成投资建议。"])
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        response = await client.post(url, json={"chat_id": self.settings.telegram_chat_id, "text": "\n".join(lines), "disable_web_page_preview": True})
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram rejected message"))
