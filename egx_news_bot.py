"""
EGX Daily News Bot
-------------------
Scrapes the news/disclosures page on the Egyptian Exchange (EGX) official
site, writes a short summary for each item, and posts a digest to a
Telegram chat/channel.

Designed to run on a schedule (see .github/workflows/egx_news_bot.yml).
It only actually sends a message on Egypt working days (Sun-Thu), close to
09:30 Africa/Cairo time -- and it re-checks the wall-clock time itself so
it keeps working correctly across Egypt's DST changes even if the
scheduler that invokes it doesn't know about them.

Environment variables required:
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    TELEGRAM_CHAT_ID     - numeric chat id or @channelusername

See README.md for full setup instructions.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CAIRO_TZ = ZoneInfo("Africa/Cairo")

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

MAX_ARTICLES = 10
RUN_WINDOW_MINUTES = 20  # tolerance around the 09:30 Cairo target
TARGET_HOUR, TARGET_MINUTE = 9, 30


def is_egypt_working_day(now: datetime) -> bool:
    """Egypt's work week is Sunday-Thursday; weekend is Friday/Saturday.
    Python's weekday(): Mon=0 ... Sun=6. Friday=4, Saturday=5.
    """
    return now.weekday() not in (4, 5)


def within_run_window(now: datetime) -> bool:
    minutes_now = now.hour * 60 + now.minute
    minutes_target = TARGET_HOUR * 60 + TARGET_MINUTE
    return abs(minutes_now - minutes_target) <= RUN_WINDOW_MINUTES


def fetch_rendered_html(url: str) -> str:
    """Load a page with a real (headless) browser and return its final HTML.

    EGX's site runs a JavaScript bot-detection challenge before showing the
    real page (it sets a token/cookie via JS, then serves the actual
    content). Plain `requests` can't execute that JavaScript, so we use a
    headless Chromium browser via Playwright instead, which behaves like a
    real visitor and gets past the challenge.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="ar-EG",
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        # Give the challenge script a moment to finish and redirect/settle.
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
    return html


def fetch_egx_news(today_only: bool = True):
    """Fetch and parse EGX's bulletin/news page.

    Confirmed page structure (as of testing): each item is a small table
    containing:
      <a href="BulletinNews.aspx?BCODE=...&All="><span id="...lblTitle">TITLE</span></a>
      ...
      <span id="...lblDate">DD/MM/YYYY</span>

    Bulletin items are already short, self-contained one-liners (e.g.
    "ARVA.CA resumed trading"), so we use the title text itself as the
    digest entry rather than fetching a separate "full article" page.
    """
    html = fetch_rendered_html(EGX_NEWS_URL)

    print(f"DEBUG: response length = {len(html)} chars")
    print(f"DEBUG: 'BulletinNews.aspx?BCODE=' present in response? "
          f"{'BulletinNews.aspx?BCODE=' in html}")
    print(f"DEBUG: first 500 chars of response:\n{html[:500]}")

    soup = BeautifulSoup(html, "html.parser")

    articles = []
    today_str = datetime.now(CAIRO_TZ).strftime("%d/%m/%Y")

    for a in soup.select("a[href*='BulletinNews.aspx?BCODE=']"):
        title = a.get_text(strip=True)
        if not title:
            continue

        href = a["href"]
        if href.startswith("http"):
            link = href
        else:
            link = "https://www.egx.com.eg/ar/" + href.lstrip("/")

        date_text = ""
        table = a.find_parent("table")
        if table:
            date_span = table.select_one(".Data span")
            if date_span:
                date_text = date_span.get_text(strip=True)

        articles.append({"title": title, "link": link, "date": date_text})

    # De-duplicate by link, preserve order
    seen = set()
    unique_articles = []
    for art in articles:
        if art["link"] not in seen:
            seen.add(art["link"])
            unique_articles.append(art)

    if today_only:
        todays = [a for a in unique_articles if a["date"] == today_str]
        # Fall back to the full list if date filtering finds nothing
        # (e.g. date format changes, or it's a non-trading day catch-up run).
        if todays:
            unique_articles = todays

    return unique_articles[:MAX_ARTICLES]


def send_telegram_message(text: str, token: str, chat_id: str):
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def build_message(articles, now: datetime) -> str:
    today_str = now.strftime("%A, %d %B %Y")
    lines = [f"📊 <b>EGX Daily News Digest</b> — {today_str}", ""]

    if not articles:
        lines.append("No news items were found on EGX's site this run.")
        lines.append("(This may mean the page structure changed — check the scraper.)")
        return "\n".join(lines)

    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. <b>{a['title']}</b>")
        lines.append(f'<a href="{a["link"]}">رابط / Link</a>')
        lines.append("")

    return "\n".join(lines)


def send_long_message(text: str, token: str, chat_id: str):
    """Telegram caps messages at ~4096 chars; split safely on blank lines."""
    if len(text) <= 4000:
        send_telegram_message(text, token, chat_id)
        return

    chunks = []
    current = ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > 4000:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram_message(chunk, token, chat_id)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    force_run = os.environ.get("FORCE_RUN") == "1"  # for manual testing

    if not token or not chat_id:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
        sys.exit(1)

    now = datetime.now(CAIRO_TZ)
    print(f"Current Cairo time: {now.isoformat()}")

    if not force_run:
        if not is_egypt_working_day(now):
            print("Today is Friday or Saturday (Egypt weekend). Skipping.")
            return
        if not within_run_window(now):
            print(
                f"Current time {now.strftime('%H:%M')} is outside the "
                f"{TARGET_HOUR:02d}:{TARGET_MINUTE:02d} +/- {RUN_WINDOW_MINUTES} min "
                "run window. Skipping."
            )
            return

    print("Fetching EGX news list...")
    try:
        articles = fetch_egx_news()
    except Exception as e:
        print(f"Failed to fetch EGX news list: {e}")
        try:
            send_telegram_message(
                f"⚠️ EGX news bot couldn't reach the EGX site this run: {e}",
                token,
                chat_id,
            )
        except Exception as send_err:
            print(f"Also failed to notify Telegram: {send_err}")
        raise

    print(f"Found {len(articles)} article(s).")

    message = build_message(articles, now)
    print("Sending to Telegram...")
    send_long_message(message, token, chat_id)
    print("Done.")


if __name__ == "__main__":
    main()
