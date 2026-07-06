"""
EGX Daily News Bot
-------------------
Scrapes the news/disclosures page on the Egyptian Exchange (EGX) official
site, writes a short summary for each item, and posts a digest to a
Telegram chat/channel.

Designed to run on a schedule (see .github/workflows/egx_news_bot.yml).
It only actually sends a message on Egypt working days (Sun-Thu), close to
09:45 Africa/Cairo time -- and it re-checks the wall-clock time itself so
it keeps working correctly across Egypt's DST changes even if the
scheduler that invokes it doesn't know about them.

Environment variables required:
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    TELEGRAM_CHAT_ID     - numeric chat id or @channelusername

See README.md for full setup instructions.
"""

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CAIRO_TZ = ZoneInfo("Africa/Cairo")

# Keywords that suggest a disclosure may be price-relevant (earnings,
# dividends, capital changes, M&A). This is a simple keyword flag for
# quick scanning only - NOT financial advice or a trading signal.
SIGNIFICANT_KEYWORDS = [
    "أرباح", "خساره", "خسارة", "خسائر",
    "نتائج أعمال", "نتائج الأعمال",
    "توزيعات",
    "زيادة رأس المال", "زياده راس المال", "تخفيض رأس المال",
    "استحواذ", "اندماج",
]

TICKER_RE = re.compile(r"\(([A-Z0-9]+\.CA)\)")


def is_significant(text: str) -> bool:
    return any(kw in text for kw in SIGNIFICANT_KEYWORDS)


def extract_ticker(title: str) -> str:
    m = TICKER_RE.search(title)
    return m.group(1) if m else "-"

# NOTE: EGX's own bulletin/news page (Arabic version - this is the one
# confirmed to work; the site's robots.txt blocks automated crawling of
# some other pages). If EGX reorganizes the site, update this URL.
# There is no official RSS/API as of writing.
EGX_NEWS_URL = "https://www.egx.com.eg/ar/BulletinNews.aspx"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_ARTICLES = 50

# EGX's trading session is roughly 10:00-14:30 Cairo time, with the opening
# session from 09:30. Checks run from 09:45 through 15:00 Cairo time, every
# 15 minutes (see the workflow's cron), as requested.
MARKET_START_HOUR, MARKET_START_MINUTE = 9, 45
MARKET_END_HOUR, MARKET_END_MINUTE = 15, 0

# Where we remember which articles were already sent today, so repeated
# 15-minute checks only report genuinely new items. This file is committed
# back to the repo by the workflow after each run.
STATE_FILE = "sent_state.json"


def is_egypt_working_day(now: datetime) -> bool:
    """Egypt's work week is Sunday-Thursday; weekend is Friday/Saturday.
    Python's weekday(): Mon=0 ... Sun=6. Friday=4, Saturday=5.
    """
    return now.weekday() not in (4, 5)


def within_market_window(now: datetime) -> bool:
    minutes_now = now.hour * 60 + now.minute
    start = MARKET_START_HOUR * 60 + MARKET_START_MINUTE
    end = MARKET_END_HOUR * 60 + MARKET_END_MINUTE
    return start <= minutes_now <= end


def load_state(today_str: str):
    """Load {date, sent_links} from disk, resetting if it's a new day."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    if state.get("date") != today_str:
        state = {"date": today_str, "sent_links": []}

    return state


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_list_page(html: str):
    """Parse one page of the bulletin list into [{title, link, date}, ...]."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.select("a[href*='BulletinNews.aspx?BCODE=']"):
        title = a.get_text(strip=True)
        if not title:
            continue
        href = a["href"]
        link = href if href.startswith("http") else "https://www.egx.com.eg/ar/" + href.lstrip("/")
        date_text = ""
        table = a.find_parent("table")
        if table:
            date_span = table.select_one(".Data span")
            if date_span:
                date_text = date_span.get_text(strip=True)
        items.append({"title": title, "link": link, "date": date_text})
    return items


def parse_detail_page(html: str):
    """Parse one article's detail page into {summary, pdf_links}."""
    soup = BeautifulSoup(html, "html.parser")

    summary = ""
    details_span = soup.select_one("#ctl00_C_BulletinNews1_lblDetails")
    if details_span:
        text = details_span.get_text(separator=" ", strip=True)
        summary = text[:700] + ("..." if len(text) > 700 else "")

    pdf_links = []
    for a_tag in soup.select("a[href$='.pdf']"):
        href = a_tag["href"]
        label = a_tag.get_text(strip=True) or "PDF"
        pdf_links.append({"label": label, "url": href})

    return {"summary": summary, "pdf_links": pdf_links}


def fetch_egx_news(today_only: bool = True, max_pages: int = 10):
    """Scrape EGX's bulletin/news page end-to-end:
      1. Walk the (possibly multi-page) list, collecting today's items.
      2. Open each item's detail page for the full summary + any PDF links.

    Uses a headless Chromium browser (via Playwright) throughout, since
    EGX's site runs a JS bot-detection challenge that a plain HTTP request
    can't get past. Pagination is done by literally clicking the page
    links in the browser, the same way a human visitor would, rather than
    reverse-engineering the ASP.NET postback mechanism.
    """
    from playwright.sync_api import sync_playwright

    today_str = datetime.now(CAIRO_TZ).strftime("%d/%m/%Y")
    collected = []
    seen_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
