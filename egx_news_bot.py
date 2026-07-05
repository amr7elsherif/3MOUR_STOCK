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
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CAIRO_TZ = ZoneInfo("Africa/Cairo")

# NOTE: EGX's own "search news" page. If EGX reorganizes the site, update
# this URL. There is no official RSS/API as of writing.
EGX_NEWS_URL = "https://www.egx.com.eg/en/newssearch.aspx"

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


def fetch_egx_news():
    """Fetch and parse the EGX news search page.

    IMPORTANT: The selectors below are a best-effort guess. EGX's site
    returned a bot-protection response when we tried to inspect it while
    building this script, so we could not confirm the live markup.
    Before relying on this in production:
      1. Run this script once locally / in Actions and check the printed
         debug output.
      2. If `articles` comes back empty, open the URL above in a real
         browser, use "Inspect element" on a news row, and update the
         CSS selectors marked TODO below to match.
    """
    resp = requests.get(EGX_NEWS_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []

    # TODO: adjust this selector to match the real news list container/rows.
    candidate_rows = soup.select(
        ".newsSearchResult, .news-item, .newsRow, table tr, .grid-row"
    )

    for row in candidate_rows:
        link_el = row.select_one("a[href]")
        if not link_el:
            continue
        title = link_el.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        href = link_el["href"]
        if href.startswith("http"):
            link = href
        else:
            link = "https://www.egx.com.eg" + (href if href.startswith("/") else "/" + href)

        # TODO: adjust to the real date cell/class if present.
        date_el = row.select_one(".date, .newsDate, td:nth-of-type(1)")
        date_text = date_el.get_text(strip=True) if date_el else ""

        articles.append({"title": title, "link": link, "date": date_text})

    # De-duplicate by link, preserve order
    seen = set()
    unique_articles = []
    for a in articles:
        if a["link"] not in seen:
            seen.add(a["link"])
            unique_articles.append(a)

    return unique_articles[:MAX_ARTICLES]


def summarize_text(text: str, max_sentences: int = 2) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:max_sentences])
    # keep it short for a Telegram digest
    return summary[:400] + ("..." if len(summary) > 400 else "")


def fetch_article_summary(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # TODO: adjust to the real article body container.
        body = soup.select_one(".newsBody, .content, article, #main-content") or soup
        text = body.get_text(" ", strip=True)
        return summarize_text(text)
    except Exception as e:
        return f"(Couldn't fetch article body: {e})"


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
        if a.get("date"):
            lines.append(f"🗓 {a['date']}")
        if a.get("summary"):
            lines.append(a["summary"])
        lines.append(f'<a href="{a["link"]}">Read more</a>')
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

    print(f"Found {len(articles)} article(s). Fetching summaries...")
    for a in articles:
        a["summary"] = fetch_article_summary(a["link"])

    message = build_message(articles, now)
    print("Sending to Telegram...")
    send_long_message(message, token, chat_id)
    print("Done.")


if __name__ == "__main__":
    main()
