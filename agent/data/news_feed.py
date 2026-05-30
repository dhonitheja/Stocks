"""
News Feed Module
================
Ingests news from RSS feeds, NewsAPI, and key government/regulatory sources.
Categorizes each article and maps it to affected stocks/sectors.
"""
import os
import time
import hashlib
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import feedparser
import requests
from loguru import logger

from agent.config import CONFIG


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class NewsArticle:
    """A single news article with metadata."""
    id: str                    # SHA hash of title+source
    title: str
    summary: str
    url: str
    source: str
    published_at: datetime
    sentiment_score: float = 0.0      # Filled by SentimentModel
    sentiment_label: str = "neutral"  # positive | negative | neutral
    affected_symbols: List[str] = field(default_factory=list)
    category: str = "general"         # general | government | macro | earnings | deal
    importance: str = "normal"        # high | normal | low
    raw_text: str = ""


# ─── Regulatory / Government Sources ─────────────────────────

GOVERNMENT_RSS_FEEDS = [
    {
        "name": "RBI Press Releases",
        "url": "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=rss",
        "category": "government",
        "importance": "high",
    },
    {
        "name": "SEBI Orders",
        "url": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes&sid=1&ssid=1&smid=0&type=rss",
        "category": "government",
        "importance": "high",
    },
    {
        "name": "Ministry of Finance",
        "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "category": "government",
        "importance": "high",
    },
    {
        "name": "PIB Economy",
        "url": "https://pib.gov.in/RssMain.aspx?ModId=54&Lang=1&Regid=3",
        "category": "government",
        "importance": "normal",
    },
]

GLOBAL_MACRO_RSS = [
    {
        "name": "Reuters Business",
        "url": "https://feeds.reuters.com/reuters/businessNews",
        "category": "macro",
        "importance": "normal",
    },
    {
        "name": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "category": "macro",
        "importance": "high",
    },
    {
        "name": "FT Markets",
        "url": "https://www.ft.com/markets?format=rss",
        "category": "macro",
        "importance": "normal",
    },
]

# Keywords that escalate article importance to "high"
HIGH_IMPORTANCE_KEYWORDS = [
    "RBI", "SEBI", "rate hike", "rate cut", "repo rate", "inflation",
    "recession", "budget", "GDP", "war", "sanctions", "ban", "fraud",
    "bankruptcy", "merger", "acquisition", "IPO", "FII", "FDI",
    "crude oil", "Fed", "Federal Reserve", "Modi", "Finance Minister",
    "earnings", "quarterly results", "profit", "loss",
]

# Sector keyword mapping
SECTOR_KEYWORDS = {
    "IT": ["TCS", "Infosys", "Wipro", "HCL", "Tech Mahindra", "software", "IT sector"],
    "Banking": ["HDFC", "ICICI", "SBI", "Axis Bank", "Kotak", "banking", "NPA", "RBI"],
    "Energy": ["Reliance", "ONGC", "crude oil", "petroleum", "energy", "oil"],
    "Pharma": ["Sun Pharma", "Dr Reddy", "Cipla", "pharma", "drug", "FDA"],
    "Auto": ["Maruti", "Tata Motors", "Bajaj", "automobile", "EV", "electric vehicle"],
    "Metals": ["Tata Steel", "JSW", "Hindalco", "steel", "aluminum", "metals"],
}


class NewsFeed:
    """
    Multi-source news aggregator.
    Polls RSS feeds, NewsAPI, and government portals on a schedule.
    """

    def __init__(self):
        self.config = CONFIG["sentiment"]
        self.news_api_key = os.getenv("NEWS_API_KEY", "")
        self.watchlist = CONFIG["market"]["watchlist"]

        self._articles: Dict[str, NewsArticle] = {}  # id → article
        self._subscribers = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Combine all RSS sources
        self.rss_sources = (
            CONFIG["sentiment"].get("rss_feeds", [])
            + GOVERNMENT_RSS_FEEDS
            + GLOBAL_MACRO_RSS
        )

        logger.info(f"NewsFeed initialized | {len(self.rss_sources)} RSS sources")

    # ─── Subscription API ─────────────────────────────────────

    def subscribe(self, callback):
        """Register callback for new articles."""
        self._subscribers.append(callback)

    def _notify(self, article: NewsArticle):
        for cb in self._subscribers:
            try:
                cb(article)
            except Exception as e:
                logger.error(f"News subscriber error: {e}")

    # ─── Article Access ───────────────────────────────────────

    def get_recent(self, hours: int = 4) -> List[NewsArticle]:
        """Return articles from the last N hours."""
        cutoff = datetime.now() - timedelta(hours=hours)
        return [
            a for a in self._articles.values()
            if a.published_at >= cutoff
        ]

    def get_for_symbol(self, symbol: str, hours: int = 4) -> List[NewsArticle]:
        """Return articles mentioning a specific stock symbol."""
        return [
            a for a in self.get_recent(hours)
            if symbol in a.affected_symbols or symbol.lower() in a.title.lower()
        ]

    def get_high_importance(self, hours: int = 2) -> List[NewsArticle]:
        """Return only high-importance articles from last N hours."""
        return [
            a for a in self.get_recent(hours)
            if a.importance == "high"
        ]

    # ─── Feed Runner ──────────────────────────────────────────

    def start(self):
        """Start continuous news polling."""
        if self._running:
            return
        self._running = True

        # Initial fetch
        self._fetch_all_sources()

        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="NewsFeed"
        )
        self._thread.start()
        logger.info("📰 News feed started")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        """Poll all sources every 60 seconds."""
        while self._running:
            time.sleep(60)
            try:
                self._fetch_all_sources()
            except Exception as e:
                logger.error(f"News poll error: {e}")

    def _fetch_all_sources(self):
        """Fetch from all configured RSS feeds + NewsAPI."""
        count_new = 0

        # RSS feeds
        for source in self.rss_sources:
            try:
                articles = self._fetch_rss(source)
                for a in articles:
                    if a.id not in self._articles:
                        self._articles[a.id] = a
                        self._notify(a)
                        count_new += 1
            except Exception as e:
                logger.debug(f"RSS error [{source.get('name')}]: {e}")

        # NewsAPI (if key available)
        if self.news_api_key:
            try:
                articles = self._fetch_newsapi()
                for a in articles:
                    if a.id not in self._articles:
                        self._articles[a.id] = a
                        self._notify(a)
                        count_new += 1
            except Exception as e:
                logger.debug(f"NewsAPI error: {e}")

        if count_new:
            logger.info(f"📰 {count_new} new articles ingested | Total: {len(self._articles)}")

        # Prune old articles (keep last 24 hours)
        cutoff = datetime.now() - timedelta(hours=24)
        old_keys = [k for k, a in self._articles.items() if a.published_at < cutoff]
        for k in old_keys:
            del self._articles[k]

    def _fetch_rss(self, source: dict) -> List[NewsArticle]:
        """Parse a single RSS feed."""
        feed = feedparser.parse(source["url"])
        articles = []

        for entry in feed.entries[:20]:  # Max 20 per source
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")
            url = getattr(entry, "link", "")

            # Parse published date
            published_at = datetime.now()
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    import time as _time
                    published_at = datetime.fromtimestamp(_time.mktime(entry.published_parsed))
                except Exception:
                    pass

            article_id = hashlib.md5(f"{title}{source['name']}".encode()).hexdigest()

            article = NewsArticle(
                id=article_id,
                title=title,
                summary=summary[:500],
                url=url,
                source=source["name"],
                published_at=published_at,
                category=source.get("category", "general"),
                importance=self._assess_importance(title, summary, source.get("importance", "normal")),
                affected_symbols=self._extract_symbols(title + " " + summary),
                raw_text=f"{title}. {summary}",
            )
            articles.append(article)

        return articles

    def _fetch_newsapi(self) -> List[NewsArticle]:
        """Fetch financial news from NewsAPI."""
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": "stock market OR NSE OR BSE OR RBI OR SEBI OR Nifty OR Sensex",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 30,
            "apiKey": self.news_api_key,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()

        articles = []
        for item in resp.json().get("articles", []):
            title = item.get("title", "")
            description = item.get("description", "")
            published_str = item.get("publishedAt", "")

            try:
                published_at = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                published_at = published_at.replace(tzinfo=None)
            except Exception:
                published_at = datetime.now()

            article_id = hashlib.md5(f"{title}newsapi".encode()).hexdigest()
            articles.append(NewsArticle(
                id=article_id,
                title=title,
                summary=description or "",
                url=item.get("url", ""),
                source=item.get("source", {}).get("name", "NewsAPI"),
                published_at=published_at,
                category="general",
                importance=self._assess_importance(title, description or ""),
                affected_symbols=self._extract_symbols(f"{title} {description}"),
                raw_text=f"{title}. {description}",
            ))

        return articles

    # ─── Analysis Helpers ─────────────────────────────────────

    def _assess_importance(self, title: str, summary: str, base: str = "normal") -> str:
        """Escalate importance if high-impact keywords found."""
        if base == "high":
            return "high"
        text = (title + " " + summary).upper()
        for kw in HIGH_IMPORTANCE_KEYWORDS:
            if kw.upper() in text:
                return "high"
        return "normal"

    def _extract_symbols(self, text: str) -> List[str]:
        """Find watchlist symbols mentioned in text."""
        mentioned = []
        text_upper = text.upper()
        for symbol in self.watchlist:
            if symbol.upper() in text_upper:
                mentioned.append(symbol)
        return mentioned
