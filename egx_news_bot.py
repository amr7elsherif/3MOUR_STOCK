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

import glob
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    IMAGE_SUPPORT = True
except ImportError:
    IMAGE_SUPPORT = False

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
    "اكتتاب", "الاكتتاب",
 "قيد أسهم زيادة",

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
# session from 09:30. Checks run from 09:30 through 15:00 Cairo time, every
# 30 minutes (see the workflow's cron / cron-job.org schedule).
MARKET_START_HOUR, MARKET_START_MINUTE = 9, 30
MARKET_END_HOUR, MARKET_END_MINUTE = 15, 0

# For the first 2 hours of the window, check every 15 minutes (news tends
# to be busiest right after open); after that, only check on the half hour.
# The external trigger fires every 15 minutes throughout the whole window
# regardless - this function is what actually decides whether a given
# 15-minute "tick" should run a real check or be a no-op.
FREQUENT_CHECK_END_HOUR, FREQUENT_CHECK_END_MINUTE = 11, 30

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
    if not (start <= minutes_now <= end):
        return False

    frequent_end = FREQUENT_CHECK_END_HOUR * 60 + FREQUENT_CHECK_END_MINUTE
    if minutes_now <= frequent_end:
        return True  # any 15-minute tick counts during the first 2 hours

    return now.minute in (0, 30)  # after that, only on the half hour


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


# Titles containing these phrases are routine auto-generated notices (e.g.
# automatic trading halts for exceeding the price-limit band), not real
# company disclosures - they're filtered out entirely.
EXCLUDE_PHRASES = [
       "ايقاف الورقة المالية",
    "إيقاف الورقة المالية",
    "سندات",
    "المحاسبات",
    "كوبون رقم",
    "الخزانة",

]


def is_excluded(title: str) -> bool:
    return any(phrase in title for phrase in EXCLUDE_PHRASES)


def parse_list_page(html: str):
    """Parse one page of the bulletin list into [{title, link, date}, ...]."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.select("a[href*='BulletinNews.aspx?BCODE=']"):
        title = a.get_text(strip=True)
        if not title:
            continue
        if is_excluded(title):
            continue
        if extract_ticker(title) == "-":
            # No (TICKER.CA) found - this is a bond/sukuk notice, a
            # market-wide administrative notice, or similar non-equity
            # item, not news about a specific listed stock.
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

            # Best-effort: find and click the next-page pager link.
            # EGX's pager is a standard ASP.NET GridView pager, so page
            # links usually fire via javascript:__doPostBack(...). We look
            # for those first, and fall back to any link whose visible
            # text matches the next page number.
            next_page_num = str(page_num + 1)
            postback_links = page.locator("a[href^='javascript:__doPostBack']")
            pcount = postback_links.count()
            pager_texts = [postback_links.nth(i).inner_text().strip() for i in range(pcount)]
            print(f"DEBUG: page {page_num}: found {pcount} postback link(s): {pager_texts}")

            next_link = None
            for i in range(pcount):
                if pager_texts[i] == next_page_num:
                    next_link = postback_links.nth(i)
                    break

            if next_link is None:
                fallback = page.locator(f"a:text-is('{next_page_num}')")
                if fallback.count() > 0:
                    next_link = fallback.first

            if next_link is None:
                print(f"DEBUG: no pager link found for page {next_page_num}, stopping pagination")
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
        # A small pause before each visit + retries handles EGX occasionally
        # resetting the connection when hit with rapid, back-to-back
        # requests (looks like basic rate-limiting on their side).
        for art in collected:
            last_error = None
            for attempt in range(1, 4):
                page.wait_for_timeout(1500)  # brief pause before each visit
                try:
                    page.goto(art["link"], wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    detail = parse_detail_page(page.content())
                    art["summary"] = detail["summary"]
                    art["pdf_links"] = detail["pdf_links"]
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    print(f"DEBUG: attempt {attempt} failed for {art['link']}: {e}")
                    page.wait_for_timeout(3000 * attempt)  # back off before retrying

            if last_error is not None:
                art["summary"] = f"(couldn't load article details after retries: {last_error})"
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
    time_str = now.strftime("%A, %d %B %Y — %H:%M")
    lines = [f"📊 <b>EGX News Update</b> — {time_str}", ""]

    if not articles:
        lines.append("No news items were found on EGX's site this run.")
        lines.append("(This may mean the page structure changed — check the scraper.)")
        return "\n".join(lines)

    # --- Summary table ---
    lines.append("<b>📋 جدول ملخص</b>")
    lines.append("<pre>")
    lines.append(f"{'#':<3}{'السهم':<10}{'':<2}")
    for i, a in enumerate(articles, 1):
        combined_text = a["title"] + " " + a.get("summary", "")
        flag = "🔥" if is_significant(combined_text) else "  "
        ticker = extract_ticker(a["title"])
        short_title = a["title"].strip()
        if len(short_title) > 28:
            short_title = short_title[:28] + "…"
        lines.append(f"{i:<3}{ticker:<10}{flag} {short_title}")
    lines.append("</pre>")
    lines.append(
        "🔥 = يحتوي على كلمات مرتبطة بالأرباح/الخسائر/التوزيعات/رأس المال/الاستحواذ "
        "(للاطلاع فقط، وليس توصية استثمارية)."
    )
    lines.append("")
    lines.append("――――――――――――――――――――")
    lines.append("")

    # --- Full details per item ---
    for i, a in enumerate(articles, 1):
        combined_text = a["title"] + " " + a.get("summary", "")
        flag_prefix = "🔥 " if is_significant(combined_text) else ""
        lines.append(f"{i}. {flag_prefix}<b>{a['title']}</b>")
        if a.get("summary"):
            lines.append(a["summary"])
        for pdf in a.get("pdf_links", []):
            lines.append(f'📎 <a href="{pdf["url"]}">{pdf["label"]}</a>')
        lines.append(f'<a href="{a["link"]}">رابط الصفحة / Page link</a>')
        lines.append("")

    return "\n".join(lines)


def find_arabic_font():
    """Look for any installed Arabic-capable font."""
    patterns = [
        "/usr/share/fonts/**/NotoNaskhArabic*.ttf",
        "/usr/share/fonts/**/NotoSansArabic*.ttf",
        "/usr/share/fonts/**/*Arabic*.ttf",
        "/usr/share/fonts/**/*arabic*.ttf",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None


def rtl(text: str) -> str:
    """Reshape + reorder Arabic text so it renders correctly in an image."""
    return get_display(arabic_reshaper.reshape(text))


def generate_summary_image(significant_articles, now: datetime, output_path="summary.png"):
    """Render a portrait (Instagram-story-sized) image of today's key news.

    Returns the output file path, or None if image generation isn't
    possible (missing libraries or no Arabic font installed) - callers
    should treat that as "skip the image, text summary still went out".
    """
    if not IMAGE_SUPPORT:
        print("DEBUG: Pillow/arabic-reshaper/python-bidi not installed, skipping image")
        return None

    font_path = find_arabic_font()
    if not font_path:
        print("DEBUG: no Arabic font found on this machine, skipping image")
        return None

    W, H = 1080, 1920
    bg_color = (13, 20, 33)
    accent = (0, 180, 120)
    text_color = (240, 240, 240)
    muted = (160, 170, 180)

    img = Image.new("RGB", (W, H), bg_color)
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(font_path, 58)
    date_font = ImageFont.truetype(font_path, 34)
    item_font = ImageFont.truetype(font_path, 38)
    footer_font = ImageFont.truetype(font_path, 30)

    margin = 60

    def draw_rtl(y, text, font, fill):
        draw.text((W - margin, y), rtl(text), font=font, fill=fill, anchor="ra")

    y = 100
    draw_rtl(y, "📌 ملخص أهم أخبار البورصة", title_font, text_color)
    y += 85
    draw_rtl(y, now.strftime("%A, %d %B %Y"), date_font, muted)
    y += 60
    draw.line([(margin, y), (W - margin, y)], fill=accent, width=4)
    y += 55

    if not significant_articles:
        draw_rtl(y, "مفيش أخبار هامة اتسجلت النهاردة", item_font, muted)
    else:
        for i, a in enumerate(significant_articles[:14], 1):
            ticker = a.get("ticker", "-")
            title = a["title"].strip()
            if len(title) > 42:
                title = title[:42] + "…"
            line = f"{i}. ({ticker}) {title}"
            draw_rtl(y, line, item_font, text_color)
            y += 62
            if y > H - 220:
                break

    footer_y = H - 110
    draw.line([(margin, footer_y - 30), (W - margin, footer_y - 30)], fill=accent, width=2)
    draw_rtl(footer_y, "شكرًا وبالتوفيق 🙏 — عمرو صلاح", footer_font, muted)

    img.save(output_path)
    return output_path


def send_telegram_photo(photo_path: str, caption: str, token: str, chat_id: str):
    url = TELEGRAM_API.format(token=token, method="sendPhoto")
    with open(photo_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def is_last_run_of_session(now: datetime) -> bool:
    minutes_now = now.hour * 60 + now.minute
    end = MARKET_END_HOUR * 60 + MARKET_END_MINUTE
    return minutes_now >= end


def build_end_of_day_summary(significant_articles, now: datetime) -> str:
    date_str = now.strftime("%A, %d %B %Y")
    lines = [f"📌 <b>ملخص أهم أخبار اليوم</b> — {date_str}", ""]

    if not significant_articles:
        lines.append(
            "مفيش أخبار هامة (زيادة رأس مال / اكتتاب / نتائج أعمال / توزيعات / "
            "استحواذ...) اتسجلت النهاردة."
        )
        lines.append("")
        lines.append("――――――――――――――――――――")
        lines.append("شكرًا وبالتوفيق 🙏 — عمرو صلاح")
        return "\n".join(lines)

    lines.append("<pre>")
    lines.append(f"{'#':<3}{'السهم':<10}{'':<2}")
    for i, a in enumerate(significant_articles, 1):
        short_title = a["title"].strip()
        if len(short_title) > 30:
            short_title = short_title[:30] + "…"
        lines.append(f"{i:<3}{a.get('ticker', '-'):<10}{short_title}")
    lines.append("</pre>")
    lines.append("")

    for i, a in enumerate(significant_articles, 1):
        lines.append(f"{i}. <b>{a['title']}</b>")
        lines.append(f'<a href="{a["link"]}">رابط الصفحة / Page link</a>')
        lines.append("")

    lines.append("――――――――――――――――――――")
    lines.append("شكرًا وبالتوفيق 🙏 — عمرو صلاح")

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
    today_str = now.strftime("%d/%m/%Y")
    print(f"Current Cairo time: {now.isoformat()}")

    if not force_run:
        if not is_egypt_working_day(now):
            print("Today is Friday or Saturday (Egypt weekend). Skipping.")
            return
        if not within_market_window(now):
            print(
                f"Current time {now.strftime('%H:%M')} is outside the "
                f"{MARKET_START_HOUR:02d}:{MARKET_START_MINUTE:02d}-"
                f"{MARKET_END_HOUR:02d}:{MARKET_END_MINUTE:02d} trading window. Skipping."
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

    print(f"Found {len(articles)} article(s) for today so far.")

    state = load_state(today_str)
    sent_links = set(state.get("sent_links", []))
    significant_articles = state.get("significant_articles", [])
    significant_links = {a["link"] for a in significant_articles}

    new_articles = [a for a in articles if a["link"] not in sent_links]
    print(f"{len(new_articles)} of those are new since the last check.")

    if new_articles:
        message = build_message(new_articles, now)
        print("Sending to Telegram...")
        send_long_message(message, token, chat_id)

        sent_links.update(a["link"] for a in new_articles)
        state["sent_links"] = sorted(sent_links)

        for a in new_articles:
            combined_text = a["title"] + " " + a.get("summary", "")
            if is_significant(combined_text) and a["link"] not in significant_links:
                significant_articles.append({
                    "title": a["title"],
                    "link": a["link"],
                    "ticker": extract_ticker(a["title"]),
                })
                significant_links.add(a["link"])
        state["significant_articles"] = significant_articles
    else:
        print("Nothing new to send.")

    if is_last_run_of_session(now):
        print("This is the last check of the session - sending end-of-day summary.")
        eod_message = build_end_of_day_summary(significant_articles, now)
        send_long_message(eod_message, token, chat_id)

        try:
            image_path = generate_summary_image(significant_articles, now)
            if image_path:
                print("Sending end-of-day summary image...")
                send_telegram_photo(
                    image_path,
                    "📌 صورة ملخص اليوم - جاهزة تتنزل وتتشارك على انستجرام 🙌",
                    token,
                    chat_id,
                )
        except Exception as e:
            print(f"DEBUG: couldn't generate/send summary image: {e}")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
