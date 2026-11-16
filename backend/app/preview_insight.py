"""Generate an isolated editorial preview, never publish or send notifications."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import settings
from .editorial import InsightGenerationError, visible_body_char_count
from .insights import _fetch_public_html, prepare_research_packet
from trafilatura import extract_metadata
from .models import NewsItem
from .providers.news import NewsProvider
from .reporting import build_insight_report
from .storage_local import LocalFileReportStore
from .validation import validate_report


async def run(args):
    config = replace(settings, editor_enabled=True)
    if not config.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required for a real preview")
    output = Path(args.output).resolve()
    store = LocalFileReportStore(output)
    now = datetime.now(timezone.utc)
    packet = store.get_research_packet(now.date()) if args.reuse_packet else None
    async with httpx.AsyncClient(timeout=config.http_timeout, follow_redirects=True) as client:
        if args.source:
            news = []
            for url in args.source:
                page, resolved = await _fetch_public_html(client, url)
                meta = extract_metadata(page, default_url=resolved, date_config={"original_date": True})
                if not meta or not meta.date:
                    raise SystemExit(f"无法核验资料发表日期：{url}")
                published = datetime.fromisoformat(meta.date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                news.append(NewsItem(id=hashlib.sha256(url.encode()).hexdigest()[:12], title=meta.title or url,
                                     url=url, resolved_url=resolved, source=urlparse(resolved).netloc,
                                     published_at=published, assets=[], sentiment=0, source_score=80))
        else:
            news = await NewsProvider(client, 40, insight=True).fetch()
        if packet is None:
            packet = await prepare_research_packet(news, config, client, now.date(), topic_request=args.topic)
    store.save_research_packet(packet)
    print(f"Packet: {packet.topic}; sources={len(packet.sources)}; facts={len(packet.facts)}", flush=True)
    if packet.error:
        raise SystemExit(packet.error)
    report = build_insight_report([], news, now, packet)
    # Persist the reviewed fragment too, so unsuccessful publication never loses a draft.
    from .deep_writing import fragment_specs, write_fragment, assemble
    from .models import InsightDraftFragment
    spec = fragment_specs(packet)[0]
    if args.reuse_packet and not args.fresh_draft:
        prior = next((f for f in store.list_insight_fragments(packet.research_date) if f.fragment_key == spec["fragment_key"]), None)
        if prior and prior.status == "failed":
            spec["previous_draft"] = prior.payload.get("article")
            spec["editor_feedback"] = prior.payload.get("review")
    if args.feedback:
        spec["editor_feedback"] = {"previous_review": spec.get("editor_feedback"), "human_editor": args.feedback}
    try:
        payload, metrics = await write_fragment(report, packet, spec, config)
    except InsightGenerationError as exc:
        store.save_insight_fragment(InsightDraftFragment(
            id=spec["fragment_key"], research_date=packet.research_date, packet_id=packet.id,
            fragment_key=spec["fragment_key"], kind="article", position=0, status="failed",
            payload={"article": exc.draft, "review": exc.review}, error=str(exc),
            created_at=now, updated_at=datetime.now(timezone.utc),
        ))
        raise SystemExit(str(exc)) from exc
    fragment = InsightDraftFragment(
        id=spec["fragment_key"], research_date=packet.research_date, packet_id=packet.id,
        fragment_key=spec["fragment_key"], kind="article", position=0, status="complete",
        payload=payload, created_at=now, updated_at=datetime.now(timezone.utc), **metrics,
    )
    store.save_insight_fragment(fragment)
    report = assemble(report, [fragment])
    validate_report(report)
    store.save(report)
    print(f"Preview only: {report.id}; body_chars={visible_body_char_count(report.article)}; output={output}", flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", help="Optional focused editorial question")
    parser.add_argument("--source", action="append", default=[], help="Public source URL; repeat as needed")
    parser.add_argument("--output", required=True, help="Isolated local output directory, never production storage")
    parser.add_argument("--reuse-packet", action="store_true", help="Reuse today's local research packet")
    parser.add_argument("--feedback", help="Optional human editorial notes for whole-draft revision")
    parser.add_argument("--fresh-draft", action="store_true", help="Do not reuse an unsuccessful prior draft")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
