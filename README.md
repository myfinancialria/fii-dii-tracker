# FII / DII Tracker

Daily scrape of NSE cash-market FII/DII flows → polished chart + dashboard → committed to GitHub, published on GitHub Pages, and posted to Slack.

## What it does

1. **`scraper.py`** — Pulls FII/DII buy/sell/net from NSE's public JSON endpoint (with cookie warm-up). Appends new rows to `data/fii_dii_history.csv` and writes `data/latest.json`.
2. **`visualize.py`** — Renders `output/fii_dii_latest.png` (today's bars + 30-day net flows) and `output/index.html` (full dashboard).
3. **`slack_post.py`** — Posts a formatted summary + chart to Slack. Supports both incoming-webhook and bot-token (image upload) modes.
4. **GitHub Actions** — Runs Mon–Fri at 19:30 IST, commits new data, deploys the dashboard to GitHub Pages.

## Local run

```bash
cd ~/fyers-bot/fii-dii-tracker
~/fyers-bot/.venv/bin/pip install -r requirements.txt
~/fyers-bot/.venv/bin/python run.py
```

Outputs:
- `data/fii_dii_history.csv` — full history
- `output/fii_dii_latest.png` — chart
- `output/index.html` — dashboard
- `output/summary.json` — today's numbers

## Slack setup

Pick one:

**A. Incoming webhook (text + image link)** — easiest.
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/…"
export CHART_URL="https://<user>.github.io/<repo>/fii_dii_latest.png"
python slack_post.py
```

**B. Bot token (uploads chart image directly to the channel)** — nicer.
1. Create Slack app → add scopes `chat:write`, `files:write` → install to workspace.
2. Invite the bot to your channel.
3. Set:
```bash
export SLACK_BOT_TOKEN="xoxb-…"
export SLACK_CHANNEL="C0XXXXXX"   # channel ID, not name
python slack_post.py
```

## GitHub setup

```bash
cd ~/fyers-bot/fii-dii-tracker
git init -b main
gh repo create fii-dii-tracker --public --source=. --remote=origin --push
```

Then in the repo on GitHub:

1. **Settings → Pages** → Source: "GitHub Actions"
2. **Settings → Secrets and variables → Actions** → add either:
   - `SLACK_WEBHOOK_URL`, *or*
   - `SLACK_BOT_TOKEN` + `SLACK_CHANNEL`
3. **Actions** tab → run "Daily FII/DII tracker" manually once to seed.

After the first run the dashboard is live at `https://<user>.github.io/fii-dii-tracker/`.

## Schedule notes

- NSE publishes the file around **18:30 IST**; the workflow runs at **19:30 IST** to be safe.
- NSE doesn't publish on holidays — the scraper appends nothing on those days (no harm done).
- Cron in GitHub Actions can lag a few minutes; if you need exact timing, run it locally via cron and disable the GitHub schedule.
