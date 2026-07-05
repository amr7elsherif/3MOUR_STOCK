EGX Daily News Bot → Telegram
Scrapes news/disclosures from the EGX (Egyptian Exchange) official site,
writes a short summary of each item, and posts a digest to Telegram every
Egypt working day (Sun–Thu) around 09:30 Cairo time.
How it works
`egx_news_bot.py` — fetches the EGX news page, summarizes each article,
and sends the result to Telegram.
`.github/workflows/egx_news_bot.yml` — runs the script automatically via
GitHub Actions, for free, with no server of your own needed.
The script checks the real Cairo time itself before sending anything,
so it stays correct across Egypt's twice-yearly clock changes (DST) even
though GitHub's scheduler only understands UTC.
⚠️ Before you rely on this: check the scraper actually works
I could not verify EGX's live page markup while building this (their site
returned a bot-protection response to my automated request). The scraper
in `fetch_egx_news()` / `fetch_article_summary()` uses reasonable guesses
for the HTML structure, marked with `# TODO` comments — but you should:
Run the script once manually (see "Test locally" below) and check
whether `articles` comes back with real titles/links.
If it comes back empty, open https://www.egx.com.eg/en/newssearch.aspx
in a browser, right-click a news item → "Inspect", and send me (or
update yourself) the actual CSS class/structure so the selectors can
be corrected.
If EGX blocks automated requests entirely (even with a browser-like
User-Agent), you may need to run this from a residential/Egyptian IP
rather than GitHub's cloud IPs, or use a headless-browser tool like
Playwright instead of plain `requests`. Let me know if you hit this
and I can adapt the script.
1. Create your Telegram bot
Open Telegram and message @BotFather.
Send `/newbot` and follow the prompts (choose a name and a username
ending in `bot`).
BotFather will reply with a token that looks like
`123456789:AAExampleTokenxxxxxxxxxxxxxxxxxxxxxxx`. Save this — it's
your `TELEGRAM_BOT_TOKEN`.
2. Get your chat ID
Option A — send to yourself / a private chat:
Start a chat with your new bot and send it any message (e.g. "hi").
Visit this URL in your browser (replace `<TOKEN>`):
`https://api.telegram.org/bot<TOKEN>/getUpdates`
Look for `"chat":{"id":123456789,...}` in the response — that number
is your `TELEGRAM_CHAT_ID`.
Option B — post to a channel:
Create a Telegram channel (public or private).
Add your bot as an administrator of the channel.
If the channel is public, `TELEGRAM_CHAT_ID` is simply `@yourchannelname`.
If private, use Option A's `getUpdates` method after posting a message
in the channel to find its numeric ID (it will look like `-100xxxxxxxxxx`).
3. Set up the GitHub repository
Create a new private GitHub repo and push these files to it.
Go to Settings → Secrets and variables → Actions → New repository secret
and add:
`TELEGRAM_BOT_TOKEN` = the token from step 1
`TELEGRAM_CHAT_ID` = the chat/channel ID from step 2
Go to the Actions tab and make sure workflows are enabled.
That's it — it will now run automatically on the schedule in
`egx_news_bot.yml`.
Test it manually (recommended before trusting the schedule)
From the Actions tab, select "EGX Daily News Bot" → Run workflow →
set `force_run` to `true`. This bypasses the day/time check so you can
confirm it sends a message right away, regardless of when you click it.
Test locally on your own machine
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export FORCE_RUN=1   # skip the day/time check for testing
python egx_news_bot.py
```
Customizing
Summary length: change `max_sentences` in `summarize_text()`.
Number of articles per digest: change `MAX_ARTICLES` at the top of
`egx_news_bot.py`.
Run time: change `TARGET_HOUR` / `TARGET_MINUTE` in the script, and
update the two `cron` times in the workflow file to match (subtract 3
hours for Egypt summer time, 2 hours for winter time, to get UTC).
Add real AI-based summarization/sentiment instead of the simple
first-two-sentences approach: swap `summarize_text()` for a call to the
Anthropic API (`api.anthropic.com`) or another LLM API — happy to wire
this up if you want richer analysis later.
