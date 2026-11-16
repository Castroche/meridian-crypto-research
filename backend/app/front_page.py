"""Reader projections keep the price observation, issue date and validity separate."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")


def issue_summary(report, now):
    kind = report.report_type
    local = report.as_of.astimezone(BJ)
    day = local.date()
    end = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "standard":
        monitor = report.tactical_monitor
        end = monitor.next_checkpoint if monitor else local + timedelta(hours=12)
    elif kind == "daily" and report.daily_strategy and report.daily_strategy.session_window:
        day = report.daily_strategy.session_window.session_date
        end = datetime.combine(day + timedelta(days=2), datetime.min.time(), BJ).replace(minute=10)
    elif kind == "insight":
        issued = (report.published_at or report.created_at).astimezone(BJ)
        day = issued.date()
        end = (issued + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    url = "/insights" if kind == "insight" else "/reports"
    return {"id": report.id, "kind": kind, "title": report.title, "date": day.isoformat(),
            "published_at": report.published_at or report.created_at, "as_of": report.as_of, "until": end, "current": local <= now < end and report.methodology_version != "meridian-data-notice",
            "summary": report.article.deck if report.article else report.executive_judgement,
            "url": f"{url}?id={report.id}", "method": report.editorial_audit.status}


def lead_facts(report):
    if report is None:
        return []
    if report.daily_strategy:
        divergence = [fact for fact in report.facts if fact.window == "采集时点的滚动 24 小时"]
        if report.market_regime == "市场宽度与总市值分化" and divergence:
            return divergence
        rows = [row for row in report.daily_strategy.executive_dashboard if row.return_day_pct is not None]
        for row in sorted(rows, key=lambda item: abs(item.return_day_pct), reverse=True):
            match = next((fact for fact in report.facts if fact.statement.startswith(row.symbol + " 北京时间")), None)
            if match:
                return [match]
    return report.facts[:1]


def market_overview(store, tactical):
    """Project the collector cache. Preserve the observation time of each instrument."""
    quotes = {}
    for snap in store.get_cache("market", "latest") or []:
        symbol, stamp = snap["symbol"], datetime.fromisoformat(snap["as_of"])
        recent = sorted((c for c in snap["candles"] if stamp - timedelta(hours=25)
                         < datetime.fromisoformat(c["opened_at"]) <= stamp - timedelta(hours=1)),
                        key=lambda c: c["opened_at"])[-24:]
        source = next(iter(snap.get("sources") or []), "")
        quotes[symbol] = {
            "symbol": symbol, "price": snap["price"], "change": snap["change_24h_pct"],
            "as_of": stamp, "source": source, "unit": "USDT" if source.startswith(("OKX","Binance")) else "USD",
            "volume": snap.get("volume_24h_usd"), "funding": snap.get("funding_rate"),
            "interest": snap.get("open_interest_usd"), "derivatives_as_of": snap.get("derivatives_as_of"),
            "spark": [[c["opened_at"], c["close"]] for c in recent], "origin": "collector"}
    if tactical and tactical.tactical_monitor:
        for row in tactical.tactical_monitor.dashboard:
            if row.symbol in quotes and quotes[row.symbol]["as_of"] >= tactical.as_of:
                continue
            fact = next((f for f in tactical.facts if f.category == "market" and f.statement.startswith(row.symbol+" ")), None)
            quotes[row.symbol] = {
                "symbol": row.symbol, "price": row.price, "change": row.return_24h_pct,
                "as_of": fact.as_of if fact else tactical.as_of, "source": fact.source if fact else "简报",
                "unit": fact.unit if fact else "", "volume": row.volume_24h_usd,
                "funding": row.funding_rate, "interest": row.open_interest_usd,
                "derivatives_as_of": None, "spark": [], "origin": "report"}
    return [quotes[s] for s in ("BTC","ETH","SOL") if s in quotes]


def front_page(store, now=None):
    now = now or datetime.now(BJ)
    latest = {kind: store.latest(report_types={kind}) for kind in ("daily", "standard", "insight")}
    products = {kind: issue_summary(report, now) if report else None for kind, report in latest.items()}
    hero = next((latest[k] for k in ("daily","standard") if products[k] and products[k]["current"]), None)
    chart = None
    if hero and hero.charts:
        chart = hero.charts[0].model_dump(mode="json")
    tactical = latest["standard"]
    monitor = tactical.tactical_monitor if tactical and products["standard"]["current"] else None
    return {"now": now, "products": products, "headline": issue_summary(hero, now) if hero else None,
            "chart": chart, "evidence": [f.model_dump(mode="json") for f in lead_facts(hero)],
            "assets": [{"symbol": row.symbol, "price": row.price, "change": row.return_24h_pct,
                        "view": row.current_view, "summary": row.trend_summary} for row in monitor.dashboard] if monitor else [],
            "market": market_overview(store, tactical),
            "recent": store.list_reports(8), "notice":store.get_cache("status","market-interruption")}

