from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .service import ResearchService
from .ready_desk import replenish


def create_scheduler(service: ResearchService, timezone: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        service.collect_market, IntervalTrigger(minutes=5, timezone=timezone),
        id="monitor-market", name="24/7 market snapshot collector",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        service.collect_context, IntervalTrigger(minutes=15, timezone=timezone),
        id="monitor-context", name="24/7 news, on-chain and options collector",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=180,
    )
    scheduler.add_job(
        service.reconcile, IntervalTrigger(minutes=30, timezone=timezone),
        id="reconcile-products", name="Catch up missing or contract-invalid products",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    for hour, label in ((8, "08:00"), (20, "20:00")):
        scheduler.add_job(
            service.generate,
            CronTrigger(hour=hour, minute=0, second=0, timezone=timezone),
            kwargs={"report_type": "standard", "slot_override": label},
            id=f"report-{label}", name=f"Meridian report {label}",
            replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
        )
    scheduler.add_job(
        service.generate, CronTrigger(hour=0, minute=10, second=0, timezone=timezone),
        kwargs={"report_type": "daily"}, id="daily-00:10", name="Meridian natural-day recap",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        service.prepare_insight, CronTrigger(hour=10, minute=0, second=0, timezone=timezone),
        id="insight-prepare-10:00", name="Prepare daily public-source research packet",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        service.generate_insight, CronTrigger(hour=12, minute=0, second=0, timezone=timezone),
        id="insight-pro-12:00", name="Generate daily Pro insight",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        service.generate_insight, CronTrigger(hour=14, minute=0, second=0, timezone=timezone),
        kwargs={"short": True}, id="insight-retry-14:00", name="Retry full-contract insight only after failure",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        service.cleanup_reports,
        CronTrigger(hour=0, minute=20, second=0, timezone=timezone),
        id="cleanup-reports", name="Delete unsaved reports older than three days",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    for hour, minute, kind, slot in ((7,57,"standard","08:00"), (19,57,"standard","20:00"), (0,2,"daily","00:10")):
        scheduler.add_job(service.generate, CronTrigger(hour=hour, minute=minute, timezone=timezone),
                          kwargs={"report_type":kind,"slot_override":slot,"prepare_only":True},
                          id=f"prepress-{slot}", max_instances=1, coalesce=True)
    scheduler.add_job(service.prewrite_insight, CronTrigger(hour=10, minute=15, timezone=timezone),
                      id="prewrite-insight", max_instances=1, coalesce=True)
    scheduler.add_job(replenish, CronTrigger(hour="9,13,17", minute=25, timezone=timezone),
                      args=[service], id="replenish-reserve", max_instances=1, coalesce=True)
    return scheduler
