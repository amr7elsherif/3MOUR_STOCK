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


def fetch_egx_news(today_only: bool = True, max_pages: int = 5):
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
            user_agent=HEADERS["User-Agent"],
            locale="ar-EG",
        )
        page = context.new_page()
        page.goto(EGX_NEWS_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)

        reached_older_news = False
        for page_num in range(1, max_pages + 1):
            html = page.content()
            if page_num == 1:
                print(f"DEBUG: page 1 response length = {len(html)} chars")
                print(f"DEBUG: 'BulletinNews.aspx?BCODE=' present? "
                      f"{'BulletinNews.aspx?BCODE=' in html}")

            page_items = parse_list_page(html)
            print(f"DEBUG: page {page_num} has {len(page_items)} item(s)")

            for item in page_items:
                if item["link"] in seen_links:
                    continue
                seen_links.add(item["link"])
                if today_only and item["date"] and item["date"] != today_str:
                    reached_older_news = True
                    continue
                collected.append(item)

            # List is newest-first, so once we've seen an older item, every
            # later item on later pages will be older too - stop paginating.
            if reached_older_news or not today_only:
                if reached_older_news:
                    break

            if len(collected) >= MAX_ARTICLES:
                break

            # Best-effort: click the next page number link if one exists.
            next_page_num = str(page_num + 1)
            next_link = page.locator(f"a:text-is('{next_page_num}')").first
            if next_link.count() == 0:
                print(f"DEBUG: no link found for page {next_page_num}, stopping pagination")
                break
            try:
                next_link.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"DEBUG: couldn't click to page {next_page_num}: {e}")
                break

        collected = collected[:MAX_ARTICLES]

        # Now open each article's own page for the full detail + PDFs.
        for art in collected:
            try:
                page.goto(art["link"], wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(1500)
                detail = parse_detail_page(page.content())
                art["summary"] = detail["summary"]
                art["pdf_links"] = detail["pdf_links"]
            except Exception as e:
                art["summary"] = f"(couldn't load article details: {e})"
                art["pdf_links"] = []

        browser.close()

    return collected


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
        if a.get("summary"):
            lines.append(a["summary"])
        for pdf in a.get("pdf_links", []):
            lines.append(f'📎 <a href="{pdf["url"]}">{pdf["label"]}</a>')
        lines.append(f'<a href="{a["link"]}">رابط الصفحة / Page link</a>')
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
