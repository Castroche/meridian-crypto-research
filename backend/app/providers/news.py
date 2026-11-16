from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx

from ..models import NewsItem


QUERIES = {
    "BTC": 'Bitcoin when:3d',
    "ETH": 'Ethereum OR Ether crypto when:3d',
    "SOL": 'Solana crypto when:3d',
    "INDUSTRY": '(crypto regulation OR stablecoin OR crypto ETF OR crypto exchange OR tokenization) when:3d',
    "PUBLIC": '(Coinbase OR Strategy OR crypto miners OR crypto treasury company) when:3d',
    "MACRO": '(Federal Reserve OR inflation OR interest rates OR dollar liquidity) crypto when:3d',
    "MACRO_DATA": '(FOMC OR nonfarm payrolls OR CPI OR jobs report) crypto when:3d',
    "GEOPOLITICS": '(geopolitics OR war OR sanctions OR tariffs OR Middle East) crypto when:3d',
}
INSIGHT_QUERIES = {
    "WEB3": '(web3 OR stablecoin payments OR protocol governance) when:7d',
    "ONCHAIN": '(onchain OR blockchain analytics OR Ethereum research) when:7d',
    "AI": '(AI model OR inference OR AI agents OR open source AI) when:7d',
    "PEOPLE": '(AI startup founder OR Silicon Valley community OR AI business model) when:7d',
    "CAPITAL": '(global liquidity OR dollar funding OR capital flows OR central bank balance sheet) when:7d',
    "EQUITIES": '(company earnings OR IPO OR HBM OR advanced packaging OR semiconductor acquisition OR chip export controls) when:7d',
}
DIRECT_FEEDS = {
    "https://www.coindesk.com/arc/outboundfeeds/rss/": ("coindesk.com", "crypto"),
    "https://cointelegraph.com/rss": ("cointelegraph.com", "crypto"),
    "https://blockworks.co/feed": ("blockworks.co", "crypto"),
    "https://cryptoslate.com/feed/": ("cryptoslate.com", "crypto"),
    "https://www.dlnews.com/arc/outboundfeeds/rss/": ("dlnews.com", "crypto"),
    "https://crypto.news/feed/": ("crypto.news", "crypto"),
    "https://beincrypto.com/feed/": ("beincrypto.com", "crypto"),
    "https://news.bitcoin.com/feed/": ("bitcoin.com", "crypto"),
    "https://www.theblock.co/rss.xml": ("theblock.co", "crypto"),
    "https://blog.ethereum.org/feed.xml": ("ethereum.org", "official_crypto"),
    "https://www.sec.gov/news/pressreleases.rss": ("sec.gov", "official"),
    "https://www.cftc.gov/RSS/RSSGP/rssgp.xml": ("cftc.gov", "official"),
    "https://www.federalreserve.gov/feeds/press_all.xml": ("federalreserve.gov", "macro"),
    "https://www.federalreserve.gov/feeds/press_monetary.xml": ("federalreserve.gov", "macro"),
    "https://www.bls.gov/feed/bls_latest.rss": ("bls.gov", "macro_data"),
}
INSIGHT_FEEDS = {
    **DIRECT_FEEDS,
    "https://openai.com/news/rss.xml": ("openai.com", "official"),
    "https://blogs.microsoft.com/feed/": ("microsoft.com", "official"),
    "https://blog.google/rss/": ("blog.google", "official"),
    "https://ir.amd.com/news-events/press-releases/rss": ("amd.com", "official"),
    "https://techcrunch.com/category/artificial-intelligence/feed/": ("techcrunch.com", "technology"),
    "https://blogs.nvidia.com/feed/": ("nvidia.com", "official"),
    "https://www.wired.com/feed/tag/ai/latest/rss": ("wired.com", "technology"),
}
ALIASES = {
    "BTC": ("bitcoin", " btc "),
    "ETH": ("ethereum", " ether ", " eth "),
    "SOL": ("solana", " sol "),
}
POSITIVE = {"approval", "approved", "inflow", "adoption", "launch", "record", "upgrade", "rally", "surge", "growth"}
NEGATIVE = {"hack", "exploit", "lawsuit", "outflow", "ban", "fraud", "liquidation", "crash", "attack", "investigation"}
HIGH_QUALITY = {
    "sec.gov", "federalreserve.gov", "cftc.gov", "treasury.gov", "reuters.com",
    "bloomberg.com", "ft.com", "wsj.com", "coindesk.com", "theblock.co",
}
REPUTABLE = {
    "finance.yahoo.com", "investing.com", "cnbc.com", "marketwatch.com",
    "decrypt.co", "blockworks.co", "cointelegraph.com", "fortune.com",
    "cryptoslate.com", "dlnews.com", "crypto.news", "beincrypto.com", "bitcoin.com",
}
CRYPTO_TERMS = ("crypto", "bitcoin", "ethereum", "solana", "stablecoin", "token", "blockchain", "digital asset", "exchange-traded fund")
DIRECT_MACRO_TERMS = ("monetary policy", "federal funds", "interest rate", "financial stability", "bank supervision", "employment", "consumer price", "nonfarm", "inflation")
NOISE_TERMS = ("best crypto", "price prediction", "to buy", "top altcoin", "seo", "sponsored", "presale")
CATEGORY_TERMS = {
    "SECURITY": ("hack", "exploit", "vulnerability", "attack", "breach", "bug"),
    "REGULATION": ("sec", "cftc", "regulation", "court", "lawsuit", "sanction", "tax", "law"),
    "MACRO": ("federal reserve", "fomc", "inflation", "cpi", "nonfarm", "employment situation", "jobs report", "interest rate", "dollar", "liquidity", "treasury", "war", "sanction", "tariff", "geopolit"),
    "FLOW": ("etf", "inflow", "outflow", "fund", "deposit", "withdrawal"),
    "POSITIONING": ("funding", "open interest", "liquidation", "options", "basis", "positioning"),
    "FUNDAMENTAL": ("upgrade", "revenue", "fees", "tvl", "validator", "developer", "adoption"),
    "CATALYST": ("launch", "approval", "unlock", "governance", "earnings", "listing"),
}


def _sentiment(title: str) -> float:
    tokens = set(re.findall(r"[a-z]+", title.lower()))
    raw = len(tokens & POSITIVE) - len(tokens & NEGATIVE)
    return max(-1.0, min(1.0, raw / 2))


def _domain(url: str) -> str:
    query_url = parse_qs(urlparse(url).query).get("url", [url])[0]
    return urlparse(query_url).netloc.lower().removeprefix("www.") or "news.google.com"


def _signal_fields(title: str, summary: str, assets: list[str]) -> dict:
    text = f"{title} {summary}".lower()
    if any(term in text for term in NOISE_TERMS):
        return {
            "category": "NOISE", "asset_relevance": 0.1 if assets else 0,
            "price_relevance": 0, "causal_path": "", "time_horizon": "INTRADAY", "novelty": 0.1,
        }
    category = "NOISE"
    for candidate, terms in CATEGORY_TERMS.items():
        if any(term in text for term in terms):
            category = candidate
            break
    asset_relevance = 0.9 if assets else 0.45 if category in {"MACRO", "REGULATION"} else 0.25
    price_relevance = 0.85 if category in {"SECURITY", "REGULATION", "FLOW", "POSITIONING", "CATALYST"} else 0.6 if category in {"MACRO", "FUNDAMENTAL"} else 0.15
    horizon = "INTRADAY" if category in {"CATALYST", "FLOW", "POSITIONING", "SECURITY"} else "DAILY" if category in {"MACRO", "REGULATION"} else "SWING"
    causal_path = (
        f"{category} event changes expectations, flows, or risk premia for {','.join(assets) or 'the crypto market'}."
        if category != "NOISE" else ""
    )
    novelty = 0.85 if any(term in text for term in ("first", "record", "unexpected", "approve", "reject", "hack", "exploit")) else 0.55
    return {
        "category": category, "asset_relevance": asset_relevance,
        "price_relevance": price_relevance, "causal_path": causal_path,
        "time_horizon": horizon, "novelty": novelty,
    }


class NewsProvider:
    def __init__(self, client: httpx.AsyncClient, limit: int = 8, *, insight: bool = False, queries: dict | None = None):
        self.client = client
        self.limit = limit
        self.insight = insight
        self.queries = queries if queries is not None else INSIGHT_QUERIES if insight else QUERIES

    async def fetch(self) -> list[NewsItem]:
        items: dict[str, NewsItem] = {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=7 if self.insight else 3)
        for symbol, query in self.queries.items():
            try:
                response = await self.client.get(
                    "https://news.google.com/rss/search",
                    params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            feed = feedparser.parse(response.content)
            for entry in feed.entries[: self.limit]:
                try:
                    published = parsedate_to_datetime(entry.get("published")).astimezone(timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    continue
                if published < cutoff:
                    continue
                normalized = re.sub(r"\s+-\s+[^-]+$", "", entry.title).strip()
                padded_title = f" {normalized.lower()} "
                assets = [asset for asset, aliases in ALIASES.items() if any(alias in padded_title for alias in aliases)]
                if symbol in ALIASES and symbol not in assets:
                    continue
                item_id = hashlib.sha256(normalized.lower().encode()).hexdigest()[:16]
                source = entry.get("source", {})
                source_url = source.get("href", entry.link) if hasattr(source, "get") else entry.link
                domain = _domain(source_url)
                if item_id in items:
                    for asset in assets:
                        if asset not in items[item_id].assets:
                            items[item_id].assets.append(asset)
                    continue
                raw_summary = entry.get("summary", "") if hasattr(entry, "get") else ""
                summary = html.unescape(re.sub(r"<[^>]+>", " ", raw_summary))
                summary = re.sub(r"\s+", " ", summary).strip()
                items[item_id] = NewsItem(
                    id=f"news:{item_id}", title=normalized, url=entry.link,
                    source=domain, published_at=published, assets=assets,
                    sentiment=_sentiment(normalized),
                    source_score=90 if domain in HIGH_QUALITY else 72 if domain in REPUTABLE else 45,
                    summary=summary[:1600], resolved_url=source_url if source_url.startswith("http") else None,
                    **_signal_fields(normalized, summary, assets),
                )
        async def fetch_direct(url: str):
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                return url, feedparser.parse(response.content)
            except httpx.HTTPError:
                return url, None

        feeds = INSIGHT_FEEDS if self.insight else DIRECT_FEEDS
        direct_rows = await asyncio.gather(*(fetch_direct(url) for url in feeds))
        for url, feed in direct_rows:
            if not feed:
                continue
            domain, category = feeds[url]
            for entry in feed.entries[: self.limit]:
                try:
                    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
                    if parsed_time:
                        published = datetime.fromtimestamp(calendar.timegm(parsed_time), timezone.utc)
                    else:
                        published = parsedate_to_datetime(entry.get("published") or entry.get("updated")).astimezone(timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    continue
                if published < cutoff:
                    continue
                title = re.sub(r"\s+", " ", html.unescape(entry.get("title", ""))).strip()
                lowered = title.lower()
                assets = [asset for asset, aliases in ALIASES.items() if any(alias in f" {lowered} " for alias in aliases)]
                if not self.insight and category == "official" and not any(term in lowered for term in CRYPTO_TERMS):
                    continue
                if category in {"macro", "macro_data"} and not any(term in lowered for term in DIRECT_MACRO_TERMS):
                    continue
                if not title or not entry.get("link"):
                    continue
                item_id = hashlib.sha256(title.lower().encode()).hexdigest()[:16]
                if item_id in items and _domain(items[item_id].url) != "news.google.com":
                    continue
                raw_summary = entry.get("summary") or entry.get("description") or ""
                summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_summary))).strip()
                items[item_id] = NewsItem(
                    id=f"news:{item_id}", title=title, url=entry.link, resolved_url=entry.link,
                    source=domain, published_at=published, assets=assets, sentiment=_sentiment(title),
                    source_score=96 if category in {"official", "official_crypto", "macro", "macro_data"} else 90 if domain in HIGH_QUALITY else 72,
                    summary=summary[:1600], **_signal_fields(title, summary, assets),
                )
        return sorted(items.values(), key=lambda item: item.published_at, reverse=True)
