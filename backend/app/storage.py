from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any, Protocol, runtime_checkable

from .models import (
    ForecastOutcome,
    InsightDraftFragment,
    InsightJobRun,
    MarketSnapshot,
    ReportHeader,
    ResearchPacket,
    ResearchReport,
)


class StorageUnavailable(RuntimeError):
    """Raised when the configured persistence service cannot complete an operation."""


@runtime_checkable
class ReportStore(Protocol):
    """Persistence boundary used by routes and research services."""

    def save_report(self, report: ResearchReport) -> None: ...
    def get_report(self, report_id: str) -> ResearchReport | None: ...
    def list_reports(self, limit: int = 30, **filters: Any) -> list[dict]: ...
    def delete_report(self, report_id: str, keep_outcomes: bool = False) -> bool: ...
    def save_job(self, job: dict[str, Any]) -> dict[str, Any]: ...
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...
    def list_jobs(self, limit: int = 20, **filters: Any) -> list[dict[str, Any]]: ...
    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid storage key component")
    return cleaned[:180]


class DocumentReportStore:
    """JSON-document implementation shared by local files and Vercel Blob."""

    _report_cache_ttl_seconds = 15.0

    def _put_bytes(
        self, path: str, body: bytes, *, content_type: str = "application/json", overwrite: bool = True
    ) -> None:
        raise NotImplementedError

    def _get_bytes(self, path: str) -> bytes | None:
        raise NotImplementedError

    def _list_paths(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def _delete_path(self, path: str) -> bool:
        raise NotImplementedError

    def _put_json(self, path: str, value: Any, *, overwrite: bool = True) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self._put_bytes(path, body, content_type="application/json", overwrite=overwrite)

    def _get_json(self, path: str) -> Any | None:
        body = self._get_bytes(path)
        if body is None:
            return None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageUnavailable(f"Stored JSON is invalid: {path}") from exc

    def _list_json(self, prefix: str) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        for path in self._list_paths(prefix):
            if path.endswith(".json"):
                value = self._get_json(path)
                if value is not None:
                    rows.append((path, value))
        return rows

    def _report_rows(self) -> list[tuple[str, Any]]:
        """Reuse a report scan briefly so one request does not list/read every report repeatedly."""
        cached = getattr(self, "_cached_report_rows", None)
        cached_at = getattr(self, "_cached_report_rows_at", 0.0)
        if cached is not None and monotonic() - cached_at < self._report_cache_ttl_seconds:
            return cached
        rows = self._list_json("reports/")
        self._cached_report_rows = rows
        self._cached_report_rows_at = monotonic()
        return rows

    def _invalidate_report_cache(self) -> None:
        self._cached_report_rows = None
        self._cached_report_rows_at = 0.0

    @staticmethod
    def _report_path(report: ResearchReport) -> str:
        if report.assets:
            symbol = report.assets[0].symbol
        elif report.tactical_monitor and report.tactical_monitor.dashboard:
            symbol = report.tactical_monitor.dashboard[0].symbol
        elif report.daily_strategy and report.daily_strategy.executive_dashboard:
            symbol = report.daily_strategy.executive_dashboard[0].symbol
        else:
            symbol = "MARKET"
        return f"reports/{_safe_component(str(symbol).upper())}/{_safe_component(report.id)}.json"

    def _find_report(self, report_id: str) -> tuple[str, ResearchReport] | None:
        safe_id = _safe_component(report_id)
        for path, payload in self._report_rows():
            if path.endswith(f"/{safe_id}.json"):
                return path, ResearchReport.model_validate(payload)
        return None

    def save_report(self, report: ResearchReport) -> None:
        self._put_json(self._report_path(report), report.model_dump(mode="json"))
        self.save_export(report.id, "md", report.markdown.encode("utf-8"), "text/markdown; charset=utf-8")
        self._invalidate_report_cache()

    def get_report(self, report_id: str) -> ResearchReport | None:
        found = self._find_report(report_id)
        return found[1] if found else None

    def list_reports(
        self,
        limit: int = 30,
        report_types: set[str] | None = None,
        exclude_types: set[str] | None = None,
        include_superseded: bool = False,
    ) -> list[dict]:
        reports = [ReportHeader.model_validate(payload) for _, payload in self._report_rows()]
        reports.sort(key=lambda item: item.published_at or item.created_at, reverse=True)
        results: list[dict] = []
        for report in reports:
            if report_types and report.report_type not in report_types:
                continue
            if exclude_types and report.report_type in exclude_types:
                continue
            if not include_superseded and report.publication_status != "CURRENT":
                continue
            retention = 7 if report.report_type == "insight" else 3
            results.append({
                "id": report.id,
                "as_of": report.as_of.isoformat(),
                "slot": report.slot,
                "title": report.title,
                "created_at": report.created_at.isoformat(),
                "published_at": (report.published_at or report.created_at).isoformat(),
                "report_type": report.report_type,
                "publication_key": report.publication_key,
                "revision": report.revision,
                "publication_status": report.publication_status,
                "is_saved": report.is_saved,
                "expires_at": None if report.is_saved else ((report.published_at or report.as_of) + timedelta(days=retention)).isoformat(),
            })
            if len(results) >= limit:
                break
        return results

    def delete_report(self, report_id: str, keep_outcomes: bool = False) -> bool:
        found = self._find_report(report_id)
        if not found:
            return False
        deleted = self._delete_path(found[0])
        for path in self._list_paths(f"exports/{_safe_component(report_id)}."):
            self._delete_path(path)
        for prefix in ([f"deliveries/{_safe_component(report_id)}/"] + ([] if keep_outcomes else [f"evaluations/{_safe_component(report_id)}/"])):
            for path in self._list_paths(prefix):
                self._delete_path(path)
        self._invalidate_report_cache()
        return deleted

    # Compatibility aliases for existing research-domain operations.
    def save(self, report: ResearchReport) -> None:
        self.save_report(report)

    def get(self, report_id: str) -> ResearchReport | None:
        return self.get_report(report_id)

    def list(self, limit: int = 30, **filters: Any) -> list[dict]:
        return self.list_reports(limit, **filters)

    def latest(
        self,
        report_types: set[str] | None = None,
        exclude_types: set[str] | None = None,
        include_superseded: bool = False,
    ) -> ResearchReport | None:
        rows = self.list_reports(1, report_types, exclude_types, include_superseded)
        return self.get_report(rows[0]["id"]) if rows else None

    def current_for_publication_key(self, publication_key: str) -> ResearchReport | None:
        for _, payload in self._report_rows():
            report = ResearchReport.model_validate(payload)
            if report.publication_key == publication_key and report.publication_status == "CURRENT":
                return report
        return None

    def publish_current(self, report: ResearchReport) -> ResearchReport:
        if not report.publication_key:
            raise ValueError("publication_key is required")
        current = self.current_for_publication_key(report.publication_key)
        if self.get_report(report.id):
            report.id = uuid.uuid4().hex[:20]
        if current:
            report.revision = current.revision + 1
            report.supersedes_id = current.id
        report.publication_status = "CURRENT"
        report.published_at = _utcnow()
        self.save_report(report)
        if current:
            current.publication_status = "SUPERSEDED"
            current.superseded_by_id = report.id
            self.save_report(current)
        return report

    def set_saved(self, report_id: str, saved: bool) -> bool:
        report = self.get_report(report_id)
        if not report:
            return False
        report.is_saved = saved
        self.save_report(report)
        return True

    def purge_old_reports(self, retention_days: int = 3, insight_retention_days: int = 7) -> int:
        cutoff = _utcnow()
        deleted = 0
        for _, payload in self._report_rows():
            report = ResearchReport.model_validate(payload)
            days = insight_retention_days if report.report_type == "insight" else retention_days
            if not report.is_saved and (report.published_at or report.as_of) < cutoff - timedelta(days=days):
                if report.publication_status == "CURRENT" and self._needs_evaluation(report):
                    self._put_json(f"pending-evaluations/{_safe_component(report.id)}.json", payload)
                deleted += int(self.delete_report(report.id, keep_outcomes=True))
        return deleted

    def save_export(self, report_id: str, extension: str, body: bytes, content_type: str) -> None:
        extension = _safe_component(extension.lower().lstrip("."))
        if extension not in {"md", "pdf"}:
            raise ValueError("Only Markdown and PDF exports are supported")
        self._put_bytes(
            f"exports/{_safe_component(report_id)}.{extension}", body, content_type=content_type, overwrite=True
        )

    def get_export(self, report_id: str, extension: str) -> bytes | None:
        return self._get_bytes(f"exports/{_safe_component(report_id)}.{_safe_component(extension.lower().lstrip('.'))}")

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow().isoformat()
        value = dict(job)
        value.setdefault("id", uuid.uuid4().hex)
        value.setdefault("status", "pending")
        value.setdefault("step", "pending")
        value.setdefault("progress", 0)
        value.setdefault("created_at", now)
        value["updated_at"] = now
        value.setdefault("error", "")
        self._put_json(f"jobs/{_safe_component(str(value['id']))}.json", value)
        return value

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._get_json(f"jobs/{_safe_component(job_id)}.json")

    def list_jobs(self, limit: int = 20, **filters: Any) -> list[dict[str, Any]]:
        jobs = [payload for _, payload in self._list_json("jobs/")]
        for key, expected in filters.items():
            jobs = [item for item in jobs if item.get(key) == expected]
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs[:limit]

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        immutable_id = job["id"]
        job.update(updates)
        job["id"] = immutable_id
        job["updated_at"] = _utcnow().isoformat()
        self._put_json(f"jobs/{_safe_component(job_id)}.json", job)
        return job

    def save_cache(self, provider: str, resource: str, payload: Any, ttl_seconds: int) -> None:
        now = _utcnow()
        self._put_json(
            f"cache/{_safe_component(provider)}/{_safe_component(resource)}.json",
            {
                "provider": provider,
                "resource": resource,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(),
                "payload": payload,
            },
        )

    def get_cache(self, provider: str, resource: str) -> Any | None:
        item = self._get_json(f"cache/{_safe_component(provider)}/{_safe_component(resource)}.json")
        if not item:
            return None
        try:
            if datetime.fromisoformat(item["expires_at"]) <= _utcnow():
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return item.get("payload")

    def save_market(self, snapshots: list[MarketSnapshot]) -> None:
        self.save_cache("market", "latest", [item.model_dump(mode="json") for item in snapshots], 24 * 60 * 60)

    def latest_market(self, max_age_minutes: int = 20) -> list[MarketSnapshot]:
        payload = self.get_cache("market", "latest") or []
        snapshots = [MarketSnapshot.model_validate(item) for item in payload]
        cutoff = _utcnow() - timedelta(minutes=max_age_minutes)
        return snapshots if len(snapshots) == 3 and min(item.as_of for item in snapshots) >= cutoff else []

    def save_context(self, category: str, payload: Any) -> None:
        self.save_cache("context", category, payload, 24 * 60 * 60)

    def latest_context(self, category: str, max_age_minutes: int = 60) -> Any | None:
        item = self._get_json(f"cache/context/{_safe_component(category)}.json")
        if not item:
            return None
        try:
            created_at = datetime.fromisoformat(item["created_at"])
        except (KeyError, TypeError, ValueError):
            return None
        return item.get("payload") if created_at >= _utcnow() - timedelta(minutes=max_age_minutes) else None

    def _needs_evaluation(self, report):
        forecasts = [(asset.symbol, "24h") for asset in report.assets]
        if report.daily_strategy:
            forecasts += [(row.symbol, "72h") for row in report.daily_strategy.executive_dashboard
                          if row.spot and row.expected_range_low is not None and row.expected_range_high is not None]
        return any(not self.outcome_exists(report.id, symbol, horizon) for symbol, horizon in forecasts)

    def reports_due_for_evaluation(self, cutoff: datetime, limit: int = 100) -> list[ResearchReport]:
        due = {}
        for path, payload in self._report_rows() + self._list_json("pending-evaluations/"):
            item = ResearchReport.model_validate(payload)
            if item.as_of > cutoff or item.publication_status != "CURRENT":
                continue
            if self._needs_evaluation(item):
                due[item.id] = item
            elif path.startswith("pending-evaluations/"):
                self._delete_path(path)
        return sorted(due.values(), key=lambda item: item.as_of, reverse=True)[:limit]

    def _outcome_path(self, report_id: str, symbol: str, horizon: str) -> str:
        return f"evaluations/{_safe_component(report_id)}/{_safe_component(symbol)}-{_safe_component(horizon)}.json"

    def outcome_exists(self, report_id: str, symbol: str, horizon: str = "24h") -> bool:
        return self._get_bytes(self._outcome_path(report_id, symbol, horizon)) is not None

    def save_outcome(self, outcome: ForecastOutcome) -> None:
        self._put_json(
            self._outcome_path(outcome.report_id, str(outcome.symbol), outcome.horizon),
            outcome.model_dump(mode="json"),
        )

    def performance(self, days: int = 90) -> dict:
        cutoff = _utcnow() - timedelta(days=days)
        outcomes = [
            ForecastOutcome.model_validate(payload)
            for _, payload in self._list_json("evaluations/")
            if datetime.fromisoformat(str(payload["evaluated_at"])) >= cutoff
        ]
        grouped: dict[tuple[str, str], list[ForecastOutcome]] = {}
        for outcome in outcomes:
            grouped.setdefault((str(outcome.symbol),outcome.horizon), []).append(outcome)
        by_asset = []
        for (symbol, horizon), rows in sorted(grouped.items()):
            count = len(rows)
            average = lambda values: round(sum(values) / count, 4)  # noqa: E731
            by_asset.append({
                "symbol": symbol, "horizon": horizon,
                "samples": count,
                "direction_accuracy": average([float(item.direction_correct) for item in rows]),
                "target_hit_rate": average([float(item.target_hit) for item in rows]),
                "avg_brier_score": (average([item.brier_score for item in rows]) if all(item.brier_score is not None for item in rows) else None),
                "avg_mfe_pct": average([item.max_favorable_excursion_pct for item in rows]),
                "avg_mae_pct": average([item.max_adverse_excursion_pct for item in rows]),
            })
        return {"window_days": days, "by_asset": by_asset, "total_samples": len(outcomes)}

    def save_delivery(self, report_id: str, channel: str, status: str, detail: str) -> None:
        created_at = _utcnow()
        payload = {
            "report_id": report_id,
            "channel": channel,
            "status": status,
            "detail": detail[:1000],
            "created_at": created_at.isoformat(),
        }
        self._put_json(f"deliveries/{_safe_component(report_id)}/{created_at.timestamp():.6f}.json", payload)

    def deliveries(self, limit: int = 50) -> list[dict]:
        rows = [payload for _, payload in self._list_json("deliveries/")]
        rows.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return rows[:limit]

    def queue_article(self, report, packet, edition=None):
        self._put_json(f"desk/ready/{_safe_component(report.id)}.json", {
            "report": report.model_dump(mode="json"), "packet": packet.model_dump(mode="json"),
            "edition": edition.isoformat() if edition else None,
        })

    def queued_articles(self):
        return [payload for _, payload in self._list_json("desk/ready/")]

    def retire_draft(self, draft_id):
        self._delete_path(f"desk/ready/{_safe_component(draft_id)}.json")

    def save_research_packet(self, packet: ResearchPacket) -> None:
        packet.updated_at = _utcnow()
        self._put_json(f"research/packets/{packet.research_date.isoformat()}.json", packet.model_dump(mode="json"))

    def get_research_packet(self, research_date: date) -> ResearchPacket | None:
        payload = self._get_json(f"research/packets/{research_date.isoformat()}.json")
        return ResearchPacket.model_validate(payload) if payload else None

    def save_insight_fragment(self, fragment: InsightDraftFragment) -> None:
        fragment.updated_at = _utcnow()
        self._put_json(
            f"research/fragments/{fragment.research_date.isoformat()}/{_safe_component(fragment.fragment_key)}.json",
            fragment.model_dump(mode="json"),
        )

    def list_insight_fragments(self, research_date: date) -> list[InsightDraftFragment]:
        prefix = f"research/fragments/{research_date.isoformat()}/"
        rows = [InsightDraftFragment.model_validate(payload) for _, payload in self._list_json(prefix)]
        return sorted(rows, key=lambda item: item.fragment_key)

    def purge_failed_insight_attempts(self, research_date: date, keep_fragment_keys: set[str]) -> dict[str, int]:
        removed_fragments = 0
        for path, payload in self._list_json(f"research/fragments/{research_date.isoformat()}/"):
            if payload.get("fragment_key") not in keep_fragment_keys:
                removed_fragments += int(self._delete_path(path))
        removed_jobs = 0
        for job in self.list_jobs(limit=1000, research_date=research_date.isoformat(), status="failed"):
            removed_jobs += int(self._delete_path(f"jobs/{_safe_component(job['id'])}.json"))
        return {"fragments": removed_fragments, "failed_runs": removed_jobs}

    def delete_insight_working_set(self, research_date: date) -> dict[str, int]:
        date_text = research_date.isoformat()
        counts = {"packets": 0, "fragments": 0, "runs": 0, "locks": 0}
        counts["packets"] += int(self._delete_path(f"research/packets/{date_text}.json"))
        for path in self._list_paths(f"research/fragments/{date_text}/"):
            counts["fragments"] += int(self._delete_path(path))
        for job in self.list_jobs(limit=1000, research_date=date_text):
            counts["runs"] += int(self._delete_path(f"jobs/{_safe_component(job['id'])}.json"))
        for lock_key in (f"insight-prepare:{date_text}", f"insight-generate:{date_text}"):
            counts["locks"] += int(self._delete_path(self._lock_path(lock_key)))
        return counts

    def start_insight_job(self, research_date: date, stage: str) -> InsightJobRun:
        now = _utcnow()
        run = InsightJobRun(id=uuid.uuid4().hex, research_date=research_date, stage=stage, started_at=now)
        self.save_job({
            **run.model_dump(mode="json"),
            "step": stage,
            "progress": 0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        })
        return run

    def finish_insight_job(
        self, run: InsightJobRun, status: str, detail: dict | None = None, error: str = ""
    ) -> None:
        finished = _utcnow()
        run.status = status
        run.finished_at = finished
        run.duration_ms = int((finished - run.started_at).total_seconds() * 1000)
        run.detail = detail or {}
        run.error = error[:2000]
        progress = 100 if status in {"succeeded", "skipped"} else 0
        self.update_job(run.id, {**run.model_dump(mode="json"), "step": "complete", "progress": progress})

    def list_insight_jobs(self, research_date: date, limit: int = 20) -> list[InsightJobRun]:
        rows = self.list_jobs(limit=limit, research_date=research_date.isoformat())
        return [
            InsightJobRun.model_validate({key: value for key, value in item.items() if key in InsightJobRun.model_fields})
            for item in rows
        ]

    def _lock_path(self, lock_key: str) -> str:
        return f"locks/{_safe_component(lock_key)}.json"

    def acquire_lock(self, lock_key: str, ttl_seconds: int = 300) -> bool:
        path = self._lock_path(lock_key)
        existing = self._get_json(path)
        now = _utcnow()
        if existing:
            try:
                if datetime.fromisoformat(existing["expires_at"]) > now:
                    return False
            except (KeyError, TypeError, ValueError):
                pass
            self._delete_path(path)
        try:
            self._put_json(
                path,
                {"id": lock_key, "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat()},
                overwrite=False,
            )
            return True
        except StorageUnavailable as exc:
            if "exist" in str(exc).lower() or "conflict" in str(exc).lower():
                return False
            raise

    def release_lock(self, lock_key: str) -> None:
        self._delete_path(self._lock_path(lock_key))
