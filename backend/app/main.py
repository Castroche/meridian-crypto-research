from __future__ import annotations

from asyncio import to_thread
from contextlib import asynccontextmanager
import logging
import secrets
import time

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from .front_page import front_page
from .config import settings
from .scheduler import create_scheduler
from .service import ResearchService
from .storage import StorageUnavailable
from .storage_factory import create_report_store


logger = logging.getLogger(__name__)
store = create_report_store(settings)
service = ResearchService(settings, store)
scheduler = create_scheduler(service, settings.timezone)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.scheduler_enabled:
        try:
            store.purge_old_reports(retention_days=3, insight_retention_days=7)
        except StorageUnavailable as exc:
            logger.warning("Storage cleanup skipped during startup: %s", exc)
        scheduler.start()
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        app.state.candle_client = client
        try:
            yield
        finally:
            del app.state.candle_client
            if scheduler.running:
                scheduler.shutdown(wait=False)


app = FastAPI(title="Meridian Crypto Research", version="0.3.0", lifespan=lifespan)

app.mount("/web", StaticFiles(directory=settings.root / "web"), name="frontend-components")

COINBASE_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
COINBASE_GRANULARITIES = {60: "ONE_MINUTE", 300: "FIVE_MINUTE", 900: "FIFTEEN_MINUTE",
                          3600: "ONE_HOUR", 21600: "SIX_HOUR", 86400: "ONE_DAY"}


@app.middleware("http")
async def register_vercel_request_headers(request, call_next):
    """Expose Vercel's rotating OIDC request token to the Python SDK."""
    if not settings.blob_read_write_token and request.headers.get("x-vercel-oidc-token"):
        try:
            from vercel.headers import HeadersContext

            with HeadersContext(dict(request.headers)).use():
                return await call_next(request)
        except ImportError:
            pass
    return await call_next(request)


@app.exception_handler(StorageUnavailable)
async def storage_unavailable_handler(_request, exc: StorageUnavailable):
    return JSONResponse(
        status_code=503,
        content={"error": "STORAGE_UNAVAILABLE", "message": str(exc)},
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "research"}


@app.get("/api/health")
def api_health():
    return {"status": "ok", "service": "research"}


@asynccontextmanager
async def candle_connection():
    client = getattr(app.state, "candle_client", None)
    if client is not None:
        yield client
    else:
        # ASGI adapters without lifespan still keep TLS setup off the event loop.
        async with await to_thread(httpx.AsyncClient, timeout=8.0, follow_redirects=True) as scoped:
            yield scoped


@app.get("/api/market/candles")
async def market_candles(symbol: str = "BTC", granularity: int = 60):
    """Same-origin fallback for initial chart history; live trades stay browser-to-exchange."""
    symbol = symbol.upper()
    product = COINBASE_PRODUCTS.get(symbol)
    interval = COINBASE_GRANULARITIES.get(granularity)
    if not product or not interval:
        raise HTTPException(400, "Unsupported market or granularity")

    end = int(time.time())
    start = end - granularity * 180
    try:
        async with candle_connection() as client:
            response = await client.get(
                f"https://api.coinbase.com/api/v3/brokerage/market/products/{product}/candles",
                params={"start": start, "end": end, "granularity": interval, "limit": 180},
                headers={"Accept": "application/json", "User-Agent": "Meridian-Market-Terminal/1.0"},
            )
            response.raise_for_status()
            candles = response.json().get("candles", [])
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, "Coinbase candle history unavailable") from exc

    rows = [
        [item["start"], item["low"], item["high"], item["open"], item["close"], item["volume"]]
        for item in candles
        if all(key in item for key in ("start", "low", "high", "open", "close", "volume"))
    ]
    return JSONResponse(rows, headers={"Cache-Control": "public, s-maxage=15, stale-while-revalidate=60"})


@app.get("/api/reports/latest")
def latest_report(response: Response, include_superseded: bool = False, report_type: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    if report_type not in {None, "standard", "daily"}:
        raise HTTPException(400, "report_type must be standard or daily")
    report = store.latest(
        report_types={report_type} if report_type else None,
        exclude_types=None if report_type else {"insight", "midday"},
        include_superseded=include_superseded,
    )
    if not report:
        raise HTTPException(404, "No report generated yet")
    return report


@app.get("/api/reports/latest/markdown", response_class=PlainTextResponse)
def latest_report_markdown():
    report = store.latest(exclude_types={"insight", "midday"})
    if not report:
        raise HTTPException(404, "No report generated yet")
    return PlainTextResponse(report.markdown, media_type="text/markdown; charset=utf-8")


@app.get("/api/reports")
def list_reports(response: Response, limit: int = 30, include_superseded: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return store.list(max(1, min(limit, 100)), exclude_types={"insight", "midday"}, include_superseded=include_superseded)


@app.get("/api/insights")
def list_insights(response: Response, limit: int = 7, include_superseded: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return store.list(max(1, min(limit, 30)), report_types={"insight"}, include_superseded=include_superseded)


@app.get("/api/insights/status/today")
def today_insight_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return service.insight_status_today()


def get_insight_or_404(insight_id: str):
    report = store.get(insight_id)
    if not report or report.report_type != "insight":
        raise HTTPException(404, "Insight not found")
    return report


@app.get("/api/insights/{insight_id}")
def get_insight(insight_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return get_insight_or_404(insight_id)


@app.get("/api/insights/{insight_id}/export/markdown")
def export_insight_markdown(insight_id: str):
    report = get_insight_or_404(insight_id)
    filename = f"meridian-insight-{report.as_of.date().isoformat()}-{report.id}.md"
    return Response(report.markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/insights/{insight_id}/export/json")
def export_insight_json(insight_id: str):
    report = get_insight_or_404(insight_id)
    filename = f"meridian-insight-{report.as_of.date().isoformat()}-{report.id}.json"
    return Response(report.model_dump_json(indent=2), media_type="application/json; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/performance")
def performance(days: int = 90):
    return store.performance(max(7, min(days, 365)))


@app.get("/api/deliveries")
def deliveries(limit: int = 50):
    return store.deliveries(max(1, min(limit, 200)))


def require_admin(token: str | None):
    if not settings.admin_token:
        raise HTTPException(503, "Admin token is not configured")
    if not (token and secrets.compare_digest(token, settings.admin_token)):
        raise HTTPException(401, "Invalid admin token")


def require_library_access(request: Request, x_meridian_admin_token: str | None = Header(default=None)):
    preview = getattr(request.app.state, "library_preview", None)
    if preview and not settings.admin_token:
        origin, name, secret = preview
        if (request.client and request.client.host == "127.0.0.1"
                and str(request.base_url).rstrip("/") == origin
                and request.headers.get("origin") == origin
                and secrets.compare_digest(request.cookies.get(name, "").encode(), secret.encode())):
            return
        raise HTTPException(403, "请从本地预览页面刷新后再操作")
    require_admin(x_meridian_admin_token)


def require_cron(authorization: str | None):
    expected = f"Bearer {settings.cron_secret}" if settings.cron_secret else None
    if not expected or not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Invalid cron authorization")


@app.patch("/api/insights/{insight_id}/saved")
def save_insight(insight_id: str, saved: bool, _access=Depends(require_library_access)):
    get_insight_or_404(insight_id)
    store.set_saved(insight_id, saved)
    return {"id": insight_id, "is_saved": saved}


@app.delete("/api/insights/{insight_id}", status_code=204)
def delete_insight(insight_id: str, _access=Depends(require_library_access)):
    get_insight_or_404(insight_id)
    store.delete_report(insight_id)
    return Response(status_code=204)


@app.get("/api/reports/{report_id}")
def get_report(report_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    report = store.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@app.get("/api/reports/{report_id}/markdown", response_class=PlainTextResponse)
def get_report_markdown(report_id: str):
    report = store.get(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return PlainTextResponse(report.markdown, media_type="text/markdown; charset=utf-8")


@app.get("/api/reports/{report_id}/export/markdown")
def export_report_markdown(report_id: str):
    report = store.get(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    filename = f"meridian-{report.report_type}-{report.slot.replace(':', '')}-{report.as_of.date().isoformat()}-{report.id}.md"
    return Response(report.markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/reports/{report_id}/export/json")
def export_report_json(report_id: str):
    report = store.get(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    filename = f"meridian-{report.report_type}-{report.slot.replace(':', '')}-{report.as_of.date().isoformat()}-{report.id}.json"
    return Response(report.model_dump_json(indent=2), media_type="application/json; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.patch("/api/reports/{report_id}/saved")
def save_report(report_id: str, saved: bool, _access=Depends(require_library_access)):
    if not store.set_saved(report_id, saved):
        raise HTTPException(404, "Report not found")
    return {"id": report_id, "is_saved": saved}


@app.delete("/api/reports/{report_id}", status_code=204)
def delete_report(report_id: str, _access=Depends(require_library_access)):
    if not store.delete_report(report_id):
        raise HTTPException(404, "Report not found")
    return Response(status_code=204)


@app.post("/api/reports/generate")
async def generate_report(x_meridian_admin_token: str | None = Header(default=None)):
    require_admin(x_meridian_admin_token)
    return await service.generate()


@app.post("/api/reports/generate/{report_type}")
async def generate_timed_article(report_type: str, x_meridian_admin_token: str | None = Header(default=None)):
    require_admin(x_meridian_admin_token)
    if report_type != "daily":
        raise HTTPException(400, "report_type must be daily")
    return await service.generate(report_type=report_type)


@app.post("/api/insights/prepare")
async def prepare_insight(x_meridian_admin_token: str | None = Header(default=None)):
    require_admin(x_meridian_admin_token)
    return await service.prepare_insight()


@app.post("/api/insights/generate")
async def generate_insight(
    short: bool = False,
    force: bool = False,
    x_meridian_admin_token: str | None = Header(default=None),
):
    require_admin(x_meridian_admin_token)
    return await service.generate_insight(short=short, force=force)


@app.get("/api/cron/insight/prepare")
async def cron_insight_prepare(authorization: str | None = Header(default=None)):
    require_cron(authorization)
    packet = await service.prepare_insight()
    return {"ok": True, "packet_id": packet.id, "status": packet.status}


@app.get("/api/cron/insight/generate")
async def cron_insight_generate(authorization: str | None = Header(default=None)):
    require_cron(authorization)
    report = await service.generate_insight()
    return {"ok": True, "report_id": report.id, "status": report.insight_status}


@app.get("/api/cron/insight/retry")
async def cron_insight_retry(authorization: str | None = Header(default=None)):
    require_cron(authorization)
    report = await service.generate_insight(short=True)
    return {"ok": True, "report_id": report.id, "status": report.insight_status}


@app.get("/api/cron/reconcile")
async def cron_reconcile(authorization: str | None = Header(default=None)):
    require_cron(authorization)
    return await service.reconcile(force=False)


@app.get("/api/cron/reconcile/{tick}")
async def cron_reconcile_shard(tick: str, authorization: str | None = Header(default=None)):
    require_cron(authorization)
    if len(tick) != 4 or not tick.isdigit():
        raise HTTPException(404, "Unknown reconcile shard")
    return await service.reconcile(force=False)


@app.post("/api/admin/reconcile")
async def admin_reconcile(
    force: bool = True,
    include_deep: bool = True,
    x_meridian_admin_token: str | None = Header(default=None),
):
    require_admin(x_meridian_admin_token)
    return await service.reconcile(force=force, include_deep=include_deep)


@app.get("/api/cron/prepress/{product}")
async def cron_prepress(product: str, authorization: str | None = Header(default=None)):
    require_cron(authorization)
    if product == "article":
        draft = await service.prewrite_insight()
        return {"ready": draft.id}
    if product == "reserve":
        from .ready_desk import replenish
        await replenish(service)
        return {"ready": True}
    slots = {"daily":("daily","00:10"),"08":("standard","08:00"),"20":("standard","20:00")}
    if product not in slots:
        raise HTTPException(404,"Unknown edition")
    kind,slot = slots[product]
    draft = await service.generate(kind,slot,prepare_only=True)
    return {"ready":draft.id}


@app.get("/api/cron/daily")
async def cron_daily(authorization: str | None = Header(default=None)):
    require_cron(authorization)
    report = await service.generate(report_type="daily")
    deleted = service.cleanup_reports()
    return {"ok": True, "report_id": report.id, "report_type": report.report_type, "deleted": deleted}


@app.get("/api/cron/standard/{slot}")
async def cron_standard(slot: str, authorization: str | None = Header(default=None)):
    require_cron(authorization)
    if slot not in {"08", "20"}:
        raise HTTPException(404, "Unknown standard report slot")
    report = await service.generate(report_type="standard", slot_override={"08": "08:00", "20": "20:00"}[slot])
    deleted = service.cleanup_reports()
    return {"ok": True, "report_id": report.id, "report_type": report.report_type, "slot": report.slot, "deleted": deleted}


@app.get("/", include_in_schema=False)
def frontend_index():
    return FileResponse(settings.root / "index.html")


@app.get("/styles.css", include_in_schema=False)
def frontend_styles():
    return FileResponse(settings.root / "styles.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def frontend_script():
    return FileResponse(settings.root / "app.js", media_type="text/javascript")


@app.get("/reports", include_in_schema=False)
def reports_center():
    return FileResponse(settings.root / "reports.html")


@app.get("/reports.css", include_in_schema=False)
def reports_styles():
    return FileResponse(settings.root / "reports.css", media_type="text/css")


@app.get("/reports.js", include_in_schema=False)
def reports_script():
    return FileResponse(settings.root / "reports.js", media_type="text/javascript")


@app.get("/insights", include_in_schema=False)
def insights_center():
    return FileResponse(settings.root / "insights.html")


@app.get("/insights.css", include_in_schema=False)
def insights_styles():
    return FileResponse(settings.root / "insights.css", media_type="text/css")


@app.get("/insights-pipeline.css", include_in_schema=False)
def insights_pipeline_styles():
    return FileResponse(settings.root / "insights-pipeline.css", media_type="text/css")


@app.get("/insights.js", include_in_schema=False)
def insights_script():
    return FileResponse(settings.root / "insights.js", media_type="text/javascript")

@app.get("/api/home")
def research_front_page(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return front_page(store)


@app.get("/terminal", include_in_schema=False)
def terminal_page():
    return FileResponse(settings.root / "terminal.html")


@app.get("/desk.css", include_in_schema=False)
def desk_styles():
    return FileResponse(settings.root / "desk.css", media_type="text/css")


@app.get("/home.js", include_in_schema=False)
def home_script():
    return FileResponse(settings.root / "home.js", media_type="text/javascript")
