"""Hold edited copy outside the public library until its issue is released."""
import hashlib
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .article_policy import newsworthiness_issues, publication_issues
from .deep_writing import write_article
from .insights import _extract_source, prepare_research_packet
from .models import NewsItem, ResearchReport, ResearchPacket
from .reporting import build_insight_report, render_insight
from .validation import validate_report




def source_item(url, now):
    return NewsItem(id=hashlib.sha256(url.encode()).hexdigest()[:12], title=url, url=url,
                    resolved_url=url, source=urlparse(url).netloc, published_at=now,
                    assets=[], sentiment=0, source_score=95)


async def sources_unchanged(packet, client):
    for source in packet.sources:
        if source.access != "full":
            continue
        item = source_item(source.url, source.published_at)
        fresh, _ = await _extract_source(client, item)
        if fresh.access != "full" or fresh.text_hash != source.text_hash:
            return False
    return True


def overlaps_article(report, previous):
    import re
    from .article_policy import article_blocks
    if not report.article or not previous or not previous.article:
        return False
    older = {re.sub(r"\s+", "", text) for text, _ in article_blocks(previous.article).values()}
    return any(len(text) >= 70 and re.sub(r"\s+", "", text) in older
               for text, _ in article_blocks(report.article).values())


async def publish_ready(service, today):
    store = service.store
    candidates = sorted(store.queued_articles(), key=lambda d: (d["edition"] != today.isoformat(), d["report"]["as_of"]))
    if not candidates:
        return None
    key = f"desk-release:{today}"
    if not store.acquire_lock(key, ttl_seconds=300):
        return store.current_for_publication_key(f"deep:{today}:12:00")
    try:
        current = store.current_for_publication_key(f"deep:{today}:12:00")
        if current and not publication_issues(current):
            return current
        async with httpx.AsyncClient(timeout=10) as client:
            for candidate in candidates:
                if candidate["edition"] not in (None, today.isoformat()):
                    continue
                report = ResearchReport.model_validate(candidate["report"])
                packet = ResearchPacket.model_validate(candidate["packet"])
                if newsworthiness_issues(packet, today):
                    continue
                if store.get(report.id) or publication_issues(report):
                    store.retire_draft(report.id)
                    continue
                reused = False
                for recent in store.list_reports(10, report_types={"insight"}):
                    other = store.get(recent["id"])
                    if overlaps_article(report, other):
                        reused = True
                        break
                if reused:
                    store.retire_draft(report.id)
                    continue
                if not await sources_unchanged(packet, client):
                    continue
                if candidate["edition"] is None:
                    report.deep_research.topic_origin = "ROLLING_BACKLOG"
                report.markdown = render_insight(report)
                validate_report(report)
                result = service._publish_current(report, today)
                store.save_cache("desk-used", hashlib.sha256(packet.research_question.encode()).hexdigest(), True, 365*86400)
                store.retire_draft(report.id)
                await service._deliver_without_blocking(result)
                return result
        return None
    finally:
        store.release_lock(key)


async def replenish(service):
    store, config = service.store, service.settings
    if not config.editor_enabled or not store.acquire_lock("desk-reserve", ttl_seconds=600):
        return
    try:
        from .providers.news import NewsProvider
        now = datetime.now(ZoneInfo(config.timezone))
        drafts = [d for d in store.queued_articles() if not d["edition"]
                  and not newsworthiness_issues(ResearchPacket.model_validate(d["packet"]), now.date())]
        if len(drafts) >= 2:
            return
        used = {s["url"] for d in drafts for s in d["packet"]["sources"]}
        recent = store.list_reports(30, report_types={"insight"})
        for issue in recent:
            used.update(s.url for s in store.get(issue["id"]).insight_sources)
        async with httpx.AsyncClient(timeout=config.http_timeout, follow_redirects=True) as client:
            news = await NewsProvider(client, config.news_limit, insight=True).fetch()
            packet = await prepare_research_packet([n for n in news if n.url not in used], config, client,
                now.date(), recent_titles=[r["title"] for r in recent])
        if packet.error or any(s.url in used for s in packet.sources):
            return
        report = await write_article(build_insight_report([], [], now, packet), packet, config)
        validate_report(report)
        store.queue_article(report, packet)
    finally:
        store.release_lock("desk-reserve")

