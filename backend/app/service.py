from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .evaluation import ForecastEvaluator
from .providers.market import MarketProvider
from .providers.news import NewsProvider
from .providers.fundamentals import FundamentalProvider
from .providers.options import OptionsProvider
from .providers.professional import ProfessionalDataProvider
from .providers.market_context import MarketContextProvider
from .reporting import build_daily_report, build_insight_report, build_report
from .storage import ReportStore
from .validation import validate_report
from .publishing import Publisher
from .editorial import (
    InsightGenerationError, assemble_insight, edit_report,
    insight_fragment_specs, visible_body_char_count, write_insight_fragment,
)
from .insights import enrich_packet_facts, prepare_research_packet
from .article_policy import EDITORIAL_VERSION, packet_readiness, publication_issues
from .models import (
    FundamentalSnapshot, InsightDraftFragment, NewsItem, OptionsSnapshot,
    MarketContextSnapshot, ProfessionalMetricsSnapshot,
)


class ResearchService:
    def __init__(self, settings: Settings, store: ReportStore):
        self.settings = settings
        self.store = store

    def _start_insight_job(self, research_date, stage: str):
        return self.store.start_insight_job(research_date, stage) if hasattr(self.store, "start_insight_job") else None

    def _finish_insight_job(self, run, status: str, detail: dict | None = None, error: str = "") -> None:
        if run is not None and hasattr(self.store, "finish_insight_job"):
            self.store.finish_insight_job(run, status, detail, error)

    async def collect_market(self):
        headers = {"User-Agent": "MeridianResearch/0.1 (+local research product)"}
        async with httpx.AsyncClient(timeout=self.settings.http_timeout, follow_redirects=True, headers=headers) as client:
            snapshots = await MarketProvider(client).fetch_all()
        self.store.save_market(snapshots)
        return snapshots

    def cleanup_reports(self):
        return self.store.purge_old_reports(retention_days=3, insight_retention_days=7)

    def _publication_key(self, report_type: str, publication_date: date, slot: str) -> str:
        product = {"standard": "tactical", "daily": "daily", "insight": "deep"}[report_type]
        return f"{product}:{publication_date.isoformat()}:{slot}"

    def _adopt_legacy_current(self, report, publication_date: date) -> None:
        if report.report_type == "daily" and report.slot == "00:10":
            old_key = self._publication_key("daily", publication_date, "24:00")
            old_current = self.store.current_for_publication_key(old_key)
            if old_current:
                old_current.publication_key = report.publication_key
                self.store.save(old_current)
                return
        legacy = self.store.latest(report_types={report.report_type})
        if not legacy or legacy.publication_key or legacy.publication_status != "CURRENT":
            return
        local_date = (
            legacy.daily_strategy.session_window.session_date
            if legacy.report_type == "daily" and legacy.daily_strategy and legacy.daily_strategy.session_window
            else legacy.as_of.astimezone(ZoneInfo(self.settings.timezone)).date()
        )
        compatible_slot = legacy.slot == report.slot or (report.report_type == "daily" and report.slot == "00:10" and legacy.slot == "24:00")
        if local_date == publication_date and compatible_slot:
            legacy.publication_key = report.publication_key
            self.store.save(legacy)

    def _publish_current(self, report, publication_date: date):
        report.publication_key = self._publication_key(report.report_type, publication_date, report.slot)
        lock_key = f"publication:{report.publication_key}"
        if not self.store.acquire_lock(lock_key, ttl_seconds=60):
            current = self.store.current_for_publication_key(report.publication_key)
            if current:
                return current
            raise RuntimeError(f"publication lock held without a CURRENT report: {report.publication_key}")
        try:
            self._adopt_legacy_current(report, publication_date)
            return self.store.publish_current(report)
        finally:
            self.store.release_lock(lock_key)

    def _retire_miskeyed_legacy_daily(self, current, publication_date: date) -> None:
        """Hide pre-upgrade post-midnight Daily records keyed to the following day."""
        if not current or current.report_type != "daily" or current.slot != "00:10":
            return
        legacy_key = self._publication_key("daily", publication_date + timedelta(days=1), "24:00")
        legacy = self.store.current_for_publication_key(legacy_key)
        if not legacy or legacy.id == current.id or legacy.slot != "24:00":
            return
        local_as_of = legacy.as_of.astimezone(ZoneInfo(self.settings.timezone))
        if local_as_of.date() != publication_date + timedelta(days=1) or local_as_of.time() >= datetime_time(12, 0):
            return
        legacy.publication_status = "SUPERSEDED"
        legacy.superseded_by_id = current.id
        self.store.save(legacy)

    async def _deliver_without_blocking(self, report) -> None:
        try:
            await Publisher(self.settings, self.store).publish(report)
        except Exception as exc:
            report.editorial_audit.warnings.append(f"外部分发失败但数据库发布已完成：{type(exc).__name__}: {str(exc)[:160]}")
            self.store.save(report)

    async def collect_context(self):
        headers = {"User-Agent": "MeridianResearch/0.1 (+local research product)"}
        async with httpx.AsyncClient(timeout=self.settings.http_timeout, follow_redirects=True, headers=headers) as client:
            raw = await asyncio.gather(
                NewsProvider(client, self.settings.news_limit).fetch(),
                FundamentalProvider(client).fetch(), OptionsProvider(client).fetch(),
                ProfessionalDataProvider(client, self.settings.professional_data_url, self.settings.professional_data_token).fetch(),
                MarketContextProvider(
                    client, coinglass_api_key=self.settings.coinglass_api_key,
                    professional_url=self.settings.professional_data_url,
                    professional_token=self.settings.professional_data_token,
                ).fetch(),
                return_exceptions=True,
            )
        names = ("news", "fundamentals", "options", "professional", "market_context")
        context = {"news": [], "fundamentals": {}, "options": {}, "professional": {}, "market_context": None}
        errors = set()
        for name, result in zip(names, raw):
            if isinstance(result, Exception):
                errors.add(name)
                continue
            context[name] = result
            if name == "news":
                payload = [item.model_dump(mode="json") for item in result]
            elif name == "market_context":
                payload = result.model_dump(mode="json")
            else:
                payload = {symbol: item.model_dump(mode="json") for symbol, item in result.items()}
            if payload or name != "professional":
                self.store.save_context(name, payload)
        return context, errors

    async def _collect_inputs(self):
        degraded = False
        try:
            snapshots = await self.collect_market()
        except Exception:
            snapshots = self.store.latest_market(max_age_minutes=1440)
            degraded = True
        context, errors = await self.collect_context()
        model_types = {
            "news": NewsItem,
            "fundamentals": FundamentalSnapshot,
            "options": OptionsSnapshot,
            "professional": ProfessionalMetricsSnapshot,
            "market_context": MarketContextSnapshot,
        }
        cache_used = set()
        for category in ("news", "fundamentals", "options", "professional", "market_context"):
            cached = self.store.latest_context(category, max_age_minutes=60)
            if cached is None:
                continue
            if category == "news" and not context[category]:
                context[category] = [NewsItem.model_validate(item) for item in cached]
                cache_used.add(category)
            elif category == "market_context" and context[category] is None:
                context[category] = MarketContextSnapshot.model_validate(cached)
                cache_used.add(category)
            elif category not in {"news", "market_context"}:
                restored = {symbol: model_types[category].model_validate(item) for symbol, item in cached.items()}
                for symbol, item in restored.items():
                    if symbol not in context[category]:
                        context[category][symbol] = item
                        cache_used.add(category)
        news = context["news"]
        fundamentals = context["fundamentals"]
        options = context["options"]
        professional = context["professional"]
        market_context = context["market_context"]
        if not news:
            for snapshot in snapshots:
                snapshot.quality_flags.append("新闻采集失败且无一小时内缓存，本批次新闻面降级")
        for snapshot in snapshots:
            if snapshot.symbol not in fundamentals:
                snapshot.quality_flags.append(f"{snapshot.symbol} 基本面采集不足，本批次该维度降级")
            if snapshot.symbol in {"BTC", "ETH"} and snapshot.symbol not in options:
                snapshot.quality_flags.append(f"{snapshot.symbol} 期权数据采集失败，资金面降级")
        if self.settings.professional_data_url and not professional:
            for snapshot in snapshots:
                snapshot.quality_flags.append("专业资金数据网关采集失败，ETF/链上净流维度降级")
        if cache_used:
            label = "、".join(sorted(cache_used))
            for snapshot in snapshots:
                snapshot.quality_flags.append(f"{label} 使用一小时内研究缓存")
        if degraded:
            for snapshot in snapshots:
                snapshot.quality_flags.append("现场行情采集失败，使用最近可用快照并按实际时效标记")
        return snapshots, news, fundamentals, options, professional, market_context

    async def _collect_insight_inputs(self, packet):
        """Deep articles acquire evidence by topic, not the live market dashboard."""
        news = [
            NewsItem(
                id=source.id, title=source.title, url=source.url, source=source.source,
                published_at=source.published_at, assets=source.assets, sentiment=0,
                source_score=source.source_score, summary=source.summary,
                resolved_url=source.url,
            )
            for source in packet.sources
        ]

        return [], news, {}, {}, {}

    async def generate(self, report_type="standard", slot_override=None, publication_date=None, prepare_only=False, force=False):
        now = datetime.now(ZoneInfo(self.settings.timezone))
        day = publication_date or (now.date() - timedelta(days=1) if report_type == "daily" else now.date())
        slot = "00:10" if report_type == "daily" else slot_override or "manual"
        key = self._publication_key(report_type, day, slot)
        from .models import ResearchReport
        lock = ("preparing-" if prepare_only else "edition-") + key
        if not self.store.acquire_lock(lock, ttl_seconds=290):
            current = self.store.current_for_publication_key(key)
            if current:
                return current
            raise RuntimeError("该期研究正在处理")
        job = None
        try:
            if hasattr(self.store, "save_job"):
                job = self.store.save_job({"kind":"research", "publication_key":key, "step":"triggered"})
            stages = [{"step":"triggered", "at":now.isoformat()}]
            def record(step, **values):
                stages.append({"step":step, "at":datetime.now(timezone.utc).isoformat(), **values})
                if job:
                    self.store.update_job(job["id"], {"step":step, "stages":stages, **values})
            if not prepare_only and not force and slot != "manual":
                existing = self.store.current_for_publication_key(key)
                valid = self._daily_contract(existing) if report_type == "daily" else self._tactical_contract(existing)
                if valid:
                    record("listed", status="succeeded", report_id=existing.id, reused=True)
                    return existing
            staged = self.store.get_cache("prepress", key) if hasattr(self.store, "get_cache") else None
            if staged and not prepare_only:
                report = ResearchReport.model_validate(staged)
                if (now - report.as_of).total_seconds() > 900 or report.methodology_version == "meridian-data-notice":
                    staged = None
            if not staged or prepare_only:
                report = await self._compose_report(report_type, slot_override, record)
            if prepare_only:
                if report.tactical_monitor:
                    hour = int(slot.split(":")[0])
                    deadline = now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(hours=12)
                    report.tactical_monitor.next_checkpoint = deadline
                    for row in report.tactical_monitor.dashboard:
                        for plan in [row.active_setup,row.primary_setup,*row.conditional_setups,*row.reference_setups]:
                            if plan:
                                plan.expiration = deadline
                    from .reporting import render_markdown
                    report.markdown = render_markdown(report)
                self.store.save_cache("prepress", key, report.model_dump(mode="json"), 900)
                record("ready",status="succeeded")
                return report
            existing = self.store.current_for_publication_key(key)
            if existing and existing.id == report.id:
                record("listed",status="succeeded",report_id=existing.id)
                return existing
            report = self._publish_current(report, day)
            record("stored", revision=report.revision, report_id=report.id)
            await self._deliver_without_blocking(report)
            visible = self.store.current_for_publication_key(report.publication_key)
            record("listed" if visible and visible.id == report.id else "visibility_failed",
                   status="succeeded" if visible and visible.id == report.id else "failed")
            return report
        except Exception as exc:
            if job:
                record("failed", status="failed", error=f"{type(exc).__name__}: {str(exc)[:240]}")
            raise
        finally:
            self.store.release_lock(lock)

    async def _compose_report(self, report_type, slot_override, record):
        if report_type not in {"standard", "daily"}:
            raise ValueError(f"不支持的普通报告类型：{report_type}")
        inputs = await self._collect_inputs()
        if len(inputs) == 5:  # Backward-compatible fixture/subclass contract.
            snapshots, news, fundamentals, options, professional = inputs
            market_context = None
        else:
            snapshots, news, fundamentals, options, professional, market_context = inputs
        now_local = datetime.now(ZoneInfo(self.settings.timezone))
        record("collected", symbols=[s.symbol for s in snapshots])
        if hasattr(self.store, "get_cache") and (len(snapshots) != 3 or
                any((now_local - s.as_of).total_seconds() > 900 for s in snapshots) or
                (report_type == "daily" and any(not any(c.opened_at.astimezone(now_local.tzinfo).date() ==
                 now_local.date() - timedelta(days=1) for c in snap.candles + snap.daily_candles) for snap in snapshots))):
            from .models import ResearchReport
            latest = sorted((snap for snap in (snapshots or self.store.latest_market(1440))
                             if snap.price > 0), key=lambda snap: snap.symbol)
            quotes = "；".join(f"{snap.symbol} {snap.price:,.2f}（{snap.sources[0] if snap.sources else '来源未标注'}，"
                             f"{snap.as_of.astimezone(now_local.tzinfo):%Y-%m-%d %H:%M} 北京时间）" for snap in latest)
            next_check = now_local + timedelta(minutes=30)
            message = (f"本期三资产行情覆盖不足。最后可查价格快照：{quotes or '无可靠记录'}。"
                       f"下次检查窗口为北京时间 {next_check:%Y-%m-%d %H:%M}。")
            record("data_interruption", last_snapshots=len(latest), next_check=next_check.isoformat())
            self.store.save_cache("status", "market-interruption", {"message":message},1800)
            return ResearchReport(id=uuid.uuid4().hex[:20], as_of=now_local, created_at=now_local,
                slot=slot_override or ("00:10" if report_type=="daily" else "manual"),
                report_type=report_type, title="行情采集中断｜本期数据说明", market_regime="数据中断",
                executive_judgement=message, news=[], methodology_version="meridian-data-notice",
                markdown="# 本期数据说明\n\n"+message)
        if self.store.get_cache("status", "market-interruption"):
            self.store.save_cache("status","market-interruption",None,0)
        ForecastEvaluator(self.store).settle(snapshots)
        previous_report = self.store.latest(report_types={report_type})
        if report_type == "daily" and previous_report:
            expected = now_local.date() - timedelta(days=2)
            if not previous_report.daily_strategy or not previous_report.daily_strategy.session_window or previous_report.daily_strategy.session_window.session_date != expected:
                previous_report = self.store.current_for_publication_key(self._publication_key("daily", expected, "00:10"))
        elif report_type == "standard" and previous_report and not previous_report.tactical_monitor:
            previous_report = next((r for entry in self.store.list_reports(30, report_types={"standard"})
                                    if (r := self.store.get(entry["id"])) and r.tactical_monitor and r.as_of < now_local), None)
        report = (
            build_report(
                snapshots, news, now_local, fundamentals, options, professional,
                slot_override, previous_report, market_context=market_context,
            )
            if report_type == "standard"
            else build_daily_report(
                snapshots, news, now_local, fundamentals, options, professional,
                previous_report, market_context=market_context,
            )
        )
        deterministic = report.model_copy(deep=True)
        try:
            report = await edit_report(report, self.settings)
            validate_report(report)
        except Exception as exc:
            report = deterministic
            report.editorial_audit.status = "fallback"
            report.editorial_audit.warnings.append(
                f"编辑或合同校验失败，已回退确定性完整报告：{type(exc).__name__}: {str(exc)[:240]}"
            )
            validate_report(report)
        from .reporting import render_markdown
        report.markdown = render_markdown(report)
        record("validated", editor=report.editorial_audit.status, sections=report.editorial_audit.sections)
        return report

    async def prepare_insight(self, force: bool = False):
        research_date = datetime.now(ZoneInfo(self.settings.timezone)).date()
        existing = self.store.get_research_packet(research_date)
        from .article_policy import newsworthiness_issues
        if not force and existing and existing.editorial_version == EDITORIAL_VERSION and not (packet_readiness(existing) or newsworthiness_issues(existing, research_date)):
            run = self._start_insight_job(research_date, "prepare")
            self._finish_insight_job(run, "skipped", {"reason": "research_packet_exists", "packet_id": existing.id})
            return existing
        run = self._start_insight_job(research_date, "prepare")
        lock_key = f"insight-prepare:{research_date.isoformat()}"
        if not self.store.acquire_lock(lock_key, ttl_seconds=600):
            self._finish_insight_job(run, "skipped", {"reason": "lock_held"})
            raise InsightGenerationError("当日新闻研究资料包正在准备")
        try:
            headers = {"User-Agent": "MeridianResearch/0.1 (+public-source research)"}
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout, follow_redirects=True, headers=headers,
            ) as client:
                news = await NewsProvider(client, max(self.settings.news_limit, 40), insight=True).fetch()
                recent = self.store.list_reports(12, report_types={"insight"})
                recent_titles = [r["title"] for r in recent]
                used_urls = set()
                for entry in recent:
                    previous = self.store.get(entry["id"])
                    if previous:
                        used_urls.update(s.url for s in previous.insight_sources)
                        recent_titles.extend(s.title for s in previous.insight_sources)
                pool = [n for n in news if n.url not in used_urls]
                packet = await prepare_research_packet(pool, self.settings, client, research_date, recent_titles=recent_titles)
                if packet.error and packet.sources:
                    rejected = {s.title for s in packet.sources}
                    alternatives = [n for n in pool if n.title not in rejected]
                    if alternatives and len(alternatives) < len(pool):
                        packet = await prepare_research_packet(alternatives, self.settings, client, research_date,
                                                               recent_titles=recent_titles)
            self.store.save_research_packet(packet)
            self._finish_insight_job(run, "failed" if packet.error else "succeeded", {
                "packet_id": packet.id, "sources": len(packet.sources), "facts": len(packet.facts),
            }, packet.error)
            return packet
        except Exception as exc:
            self._finish_insight_job(run, "failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.store.release_lock(lock_key)

    async def _generate_insight_fragment(
        self, report, packet, spec: dict, existing: InsightDraftFragment | None, short: bool,
    ) -> InsightDraftFragment:
        now = datetime.now(timezone.utc)
        prior_error = existing.error if existing else ""
        fragment = existing or InsightDraftFragment(
            id=uuid.uuid4().hex,
            research_date=packet.research_date,
            packet_id=packet.id,
            fragment_key=spec["fragment_key"],
            kind=spec["kind"],
            position=spec["position"],
            headings=spec["headings"],
            created_at=now,
            updated_at=now,
        )
        fragment.packet_id = packet.id
        fragment.kind = spec["kind"]
        fragment.position = spec["position"]
        fragment.headings = spec["headings"]
        fragment.status = "generating"
        fragment.error = ""
        self.store.save_insight_fragment(fragment)
        try:
            generation_spec = dict(spec)
            if prior_error:
                generation_spec["repair_reason"] = prior_error
                generation_spec["previous_draft"] = fragment.payload.get("article")
                generation_spec["editor_feedback"] = fragment.payload.get("review")
            payload, metrics = await write_insight_fragment(
                report, packet, generation_spec, self.settings, short=short,
            )
            fragment.payload = payload
            fragment.status = "complete"
            fragment.attempts += int(metrics.get("attempts") or 1)
            fragment.latency_ms = int(metrics.get("latency_ms") or 0)
            fragment.input_tokens = int(metrics["input_tokens"]) if metrics.get("input_tokens") else None
            fragment.output_tokens = int(metrics["output_tokens"]) if metrics.get("output_tokens") else None
            self.store.save_insight_fragment(fragment)
            return fragment
        except BaseException as exc:
            fragment.status = "failed"
            if isinstance(exc, InsightGenerationError) and exc.draft:
                fragment.payload = {"article": exc.draft, "review": exc.review, "editorial_version": EDITORIAL_VERSION}
            fragment.attempts += max(1, self.settings.insight_fragment_attempts)
            fragment.error = f"{type(exc).__name__}: {exc}"[:1000]
            self.store.save_insight_fragment(fragment)
            raise

    async def _write_insight(self, short: bool = False, force: bool = False, prewrite: bool = False):
        research_date = datetime.now(ZoneInfo(self.settings.timezone)).date()
        existing = self.store.latest(report_types={"insight"})
        if (
            not force and existing and
            existing.as_of.astimezone(ZoneInfo(self.settings.timezone)).date() == research_date and
            self._deep_contract(existing)
        ):
            if hasattr(self.store, "purge_failed_insight_attempts"):
                packet = self.store.get_research_packet(research_date)
                keep_keys = {item["fragment_key"] for item in insight_fragment_specs(packet)} if packet else set()
                self.store.purge_failed_insight_attempts(research_date, keep_keys)
            return existing
        packet = self.store.get_research_packet(research_date)
        from .article_policy import newsworthiness_issues
        if not packet or packet.editorial_version != EDITORIAL_VERSION or packet_readiness(packet) or newsworthiness_issues(packet, research_date):
            packet = await self.prepare_insight(force=True)
        run = self._start_insight_job(research_date, "retry" if short else "generate")
        lock_key = f"insight-generate:{research_date.isoformat()}"
        if not self.store.acquire_lock(lock_key, ttl_seconds=self.settings.insight_task_timeout + 30):
            self._finish_insight_job(run, "skipped", {"reason": "lock_held"})
            raise InsightGenerationError("当日深度文章正在生成")
        packet = enrich_packet_facts(packet)
        started = time.perf_counter()
        try:
            material_issues = packet_readiness(packet) + newsworthiness_issues(packet, research_date)
            if material_issues:
                raise InsightGenerationError("需要补充研究：" + "；".join(material_issues))
            packet.status = "repairing" if short else "drafting"
            packet.error = ""
            self.store.save_research_packet(packet)
            async with asyncio.timeout(self.settings.insight_task_timeout):
                snapshots, news, fundamentals, options, professional = await self._collect_insight_inputs(packet)
                previous_report = self.store.latest(report_types={"daily"})
                report = build_insight_report(
                    snapshots, news, datetime.now(ZoneInfo(self.settings.timezone)), packet,
                    fundamentals, options, professional, previous_report,
                )
                report.editorial_audit.model = self.settings.article_editor_model
                specs = insight_fragment_specs(packet)
                saved = {
                    item.fragment_key: item
                    for item in self.store.list_insight_fragments(research_date)
                    if item.packet_id == packet.id
                }
                fragments: list[InsightDraftFragment] = []
                pending = []
                for spec in specs:
                    fragment = saved.get(spec["fragment_key"])
                    if fragment and fragment.status == "complete" and not force:
                        fragments.append(fragment)
                    else:
                        pending.append(self._generate_insight_fragment(report, packet, spec, fragment, short))
                results = await asyncio.gather(*pending, return_exceptions=True)
                failures = [result for result in results if isinstance(result, BaseException)]
                fragments.extend(result for result in results if isinstance(result, InsightDraftFragment))
                if failures:
                    raise InsightGenerationError(f"{len(failures)} 个文章分段生成失败：{failures[0]}")
                fragments.sort(key=lambda item: item.position)
                report = assemble_insight(
                    report, fragments, short=short,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                )
                from .ready_desk import overlaps_article
                if any(overlaps_article(report,self.store.get(r["id"]))
                       for r in self.store.list_reports(10,report_types={"insight"})):
                    raise InsightGenerationError("与近期特稿存在重复长段，需要重新选材")
                validate_report(report)
                if prewrite:
                    self.store.queue_article(report, packet, research_date)
                    self._finish_insight_job(run, "succeeded", {"step":"ready", "draft_id":report.id})
                    return report
                report = self._publish_current(report, research_date)
                packet.status = "published"
                packet.error = ""
                self.store.save_research_packet(packet)
                await self._deliver_without_blocking(report)
                self._finish_insight_job(run, "succeeded", {
                    "report_id": report.id,
                    "insight_status": report.insight_status,
                    "fragments_complete": len(fragments),
                    "fragment_attempts": sum(item.attempts for item in fragments),
                })
                return report
        except Exception as exc:
            packet.status = "failed"
            packet.error = f"{type(exc).__name__}: {exc}"[:1800]
            self.store.save_research_packet(packet)
            complete = sum(item.status == "complete" for item in self.store.list_insight_fragments(research_date))
            self._finish_insight_job(run, "failed", {"fragments_complete": complete, "publication": "held"}, packet.error)
            # Keep the previous approved article. Never overwrite it with a template.
            raise
        finally:
            self.store.release_lock(lock_key)

    async def prewrite_insight(self):
        return await self._write_insight(prewrite=True)

    async def generate_insight(self, short=False, force=False):
        from .ready_desk import publish_ready
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        if hasattr(self.store, "queued_articles"):
            current = self._report_for_product("insight", today, "12:00")
            if current and not force and self._deep_contract(current):
                return current
            ready = await publish_ready(self, today)
            if ready:
                return ready
        return await self._write_insight(short=short, force=force)

    def _report_for_product(self, report_type: str, publication_date: date, slot: str):
        key = self._publication_key(report_type, publication_date, slot)
        current = self.store.current_for_publication_key(key)
        if current:
            return current
        if report_type == "daily" and slot == "00:10":
            current = self.store.current_for_publication_key(self._publication_key("daily", publication_date, "24:00"))
            if current:
                return current
        legacy = self.store.latest(report_types={report_type})
        compatible_slot = legacy and (legacy.slot == slot or (report_type == "daily" and slot == "00:10" and legacy.slot == "24:00"))
        if not compatible_slot:
            return None
        legacy_date = (
            legacy.daily_strategy.session_window.session_date
            if report_type == "daily" and legacy.daily_strategy and legacy.daily_strategy.session_window
            else legacy.as_of.astimezone(ZoneInfo(self.settings.timezone)).date()
        )
        return legacy if legacy_date == publication_date else None

    @staticmethod
    def _daily_contract(report) -> bool:
        return bool(
            report and report.daily_strategy and
            {item.symbol for item in report.daily_strategy.executive_dashboard} == {"BTC", "ETH", "SOL"} and
            report.daily_strategy.session_window and report.daily_strategy.market_day and
            len(report.daily_strategy.chain_activity) == 3 and
            len(report.daily_strategy.outlook) == 3 and
            len(report.markdown) >= 700
        )

    @staticmethod
    def _tactical_contract(report) -> bool:
        return bool(
            report and report.tactical_monitor and
            {item.symbol for item in report.tactical_monitor.dashboard} == {"BTC", "ETH", "SOL"} and
            all(state.reference_setups for state in report.tactical_monitor.dashboard) and
            all(state.primary_setup or len(state.conditional_setups) == 2 for state in report.tactical_monitor.dashboard)
        )

    @staticmethod
    def _deep_contract(report) -> bool:
        if not report or not report.article or report.insight_status != "full":
            return False
        return not publication_issues(report)

    async def reconcile(self, force: bool = False, include_deep: bool = True) -> dict:
        """Catch up due products without ever backfilling expired Tactical checkpoints."""
        tz = ZoneInfo(self.settings.timezone)
        now_local = datetime.now(tz)
        today = now_local.date()
        lock_key = f"reconcile:{today.isoformat()}:{now_local.hour // 1}"
        if not self.store.acquire_lock(lock_key, ttl_seconds=1500):
            return {"ok": True, "status": "already_running", "at": now_local.isoformat(), "products": {}}
        products: dict[str, dict] = {}
        try:
            daily_date = today - timedelta(days=1)
            if force or now_local.time() >= datetime_time(0, 10):
                current = self._report_for_product("daily", daily_date, "00:10")
                if force or not self._daily_contract(current):
                    report = await self.generate("daily", publication_date=daily_date, **({"force": True} if force else {}))
                    products["daily"] = {"action": "published", "report_id": report.id, "revision": report.revision}
                    current = report
                else:
                    products["daily"] = {"action": "kept", "report_id": current.id}
                self._retire_miskeyed_legacy_daily(current, daily_date)

            if include_deep and (force or now_local.time() >= datetime_time(12, 5)):
                current = self._report_for_product("insight", today, "12:00")
                if force or not self._deep_contract(current):
                    try:
                        if force and hasattr(self.store, "delete_insight_working_set"):
                            self.store.delete_insight_working_set(today)
                        report = await self.generate_insight(force=force)
                        products["deep"] = {
                            "action": "published", "report_id": report.id, "revision": report.revision,
                            "visible_body_chars": visible_body_char_count(report.article),
                        }
                    except Exception as exc:
                        products["deep"] = {"action": "held", "reason": str(exc)[:700]}
                else:
                    products["deep"] = {"action": "kept", "report_id": current.id, "visible_body_chars": visible_body_char_count(current.article)}

            if now_local.hour < 8:
                tactical_date, slot = today - timedelta(days=1), "20:00"
            elif now_local.hour < 20:
                tactical_date, slot = today, "08:00"
            else:
                tactical_date, slot = today, "20:00"
            current = self._report_for_product("standard", tactical_date, slot)
            if force or not self._tactical_contract(current):
                report = await self.generate("standard", slot_override=slot, publication_date=tactical_date, **({"force": True} if force else {}))
                products["tactical"] = {"action": "published", "report_id": report.id, "revision": report.revision, "slot": slot}
            else:
                products["tactical"] = {"action": "kept", "report_id": current.id, "slot": slot}
            return {"ok": True, "status": "partial" if products.get("deep", {}).get("action") == "held" else "complete", "at": now_local.isoformat(), "products": products}
        finally:
            self.store.release_lock(lock_key)

    def insight_status_today(self) -> dict:
        research_date = datetime.now(ZoneInfo(self.settings.timezone)).date()
        packet = self.store.get_research_packet(research_date)
        specs = insight_fragment_specs(packet) if packet else []
        active_keys = {item["fragment_key"] for item in specs}
        fragments = self.store.list_insight_fragments(research_date) if hasattr(self.store, "list_insight_fragments") else []
        fragments = [item for item in fragments if item.fragment_key in active_keys]
        jobs = self.store.list_insight_jobs(research_date) if hasattr(self.store, "list_insight_jobs") else []
        latest = self.store.latest(report_types={"insight"})
        report = None
        if latest and latest.created_at.astimezone(ZoneInfo(self.settings.timezone)).date() == research_date:
            report = {"id": latest.id, "title": latest.title, "status": latest.insight_status, "created_at": latest.created_at}
        expected = len(specs) if packet else 1
        return {
            "research_date": research_date,
            "state": "published" if report else packet.status if packet else "not_prepared",
            "topic": packet.topic if packet else "",
            "error": packet.error if packet else "",
            "warnings": packet.warnings if packet else [],
            "fragments": {
                "expected": expected,
                "complete": sum(item.status == "complete" for item in fragments),
                "failed": sum(item.status == "failed" for item in fragments),
                "items": [
                    {
                        "fragment_key": item.fragment_key,
                        "kind": item.kind,
                        "status": item.status,
                        "attempts": item.attempts,
                        "error": item.error,
                        "updated_at": item.updated_at,
                    }
                    for item in fragments
                ],
            },
            "runs": [item.model_dump(mode="json") for item in jobs],
            "report": report,
        }
