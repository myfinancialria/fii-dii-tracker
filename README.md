# Indian Market Pulse — Daily Dashboard

A daily, auto-generated market dashboard for the Indian markets. Pulls everything a professional trader scans in the morning, presents it as a clean visual brief, commits to GitHub, publishes to GitHub Pages, and posts a digest to Slack.

## What's inside (every day, after market close)

| Section | Source | What you get |
|---|---|---|
| Headline indices | NSE | Nifty 50, Bank Nifty, Midcap 100, **India VIX** — last, change %, 52-week range bar |
| Sector indices | NSE | IT, Auto, Pharma, FMCG, Metal, Realty, Energy, PSU Bank, Financials |
| Sector heatmap | NSE | All sectors ranked by today's %change |
| Institutional flow | NSE | FII & DII cash market — buy / sell / net + 30-day net flow trend |
| Top movers | NSE | Top 5 gainers & losers in Nifty 50 |
| Global cues | Yahoo Finance | Dow, Nasdaq, S&P, FTSE, Nikkei, Hang Seng |
| FX & commodities | Yahoo Finance | USD/INR, DXY, Brent crude, Gold, US 10Y yield |
| Market mood | Derived | Risk-On / Risk-Off / Mixed / Range-bound (from Nifty + VIX) |
| **News catalysts** | Google News + Claude (optional) | Why each top mover & best/worst sector moved today |

## Files

| File | Purpose |
|---|---|
| `fetchers.py` | All data sources — NSE endpoints + yfinance |
| `scraper.py` | Orchestrates fetch → writes `data/snapshot.json` + FII/DII history CSV |
| `catalysts.py` | Fetches news headlines for top movers + best/worst sectors; optionally compresses to a 1-line catalyst via Claude |
| `visualize.py` | Generates `output/market_pulse.png` + `output/index.html` |
| `render_dashboard.py` | Renders the HTML dashboard to `output/dashboard.jpg` via headless Chromium |
| `slack_post.py` | Posts the daily digest + dashboard JPG to Slack |
| `run.py` | Runs the full pipeline locally |
| `.github/workflows/daily.yml` | Runs everything Mon–Fri at 19:30 IST in CI |

## Local run

```bash
cd ~/fyers-bot/fii-dii-tracker
~/fyers-bot/.venv/bin/pip install -r requirements.txt
~/fyers-bot/.venv/bin/python -m playwright install chromium   # one-time
~/fyers-bot/.venv/bin/python run.py
```

Outputs:
- `data/snapshot.json` — today's full market state
- `data/snapshots/YYYY-MM-DD.json` — daily archive
- `data/fii_dii_history.csv` — accumulating FII/DII history
- `output/market_pulse.png` — shareable image (Slack + dashboard)
- `output/index.html` — public dashboard
- `output/summary.json` — Slack-ready compact summary

## Slack setup

**Option A — Incoming webhook (text + chart link)**
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/…"
export CHART_URL="https://<user>.github.io/fii-dii-tracker/market_pulse.png"
python slack_post.py
```

**Option B — Bot token (uploads chart image inline, recommended)**
1. https://api.slack.com/apps → Create New App
2. OAuth & Permissions → Bot Token Scopes: `chat:write`, `files:write`
3. Install to workspace → copy `xoxb-…` token
4. Invite the bot to your target channel: `/invite @<bot-name>`
```bash
export SLACK_BOT_TOKEN="xoxb-…"
export SLACK_CHANNEL="C0XXXXXX"
python slack_post.py
```

## GitHub setup (one-time)

```bash
cd ~/fyers-bot/fii-dii-tracker
git commit -m "initial: market pulse dashboard"
gh repo create fii-dii-tracker --public --source=. --remote=origin --push
```

Then on GitHub:
1. **Settings → Pages** → Source: "GitHub Actions"
2. **Settings → Secrets and variables → Actions** → add either:
   - `SLACK_WEBHOOK_URL`, *or*
   - `SLACK_BOT_TOKEN` + `SLACK_CHANNEL`
3. *(Optional)* Add `ANTHROPIC_API_KEY` to get AI-condensed 1-line catalysts. Without it, catalysts fall back to the cleanest matching news headline (still useful).
4. **Actions** tab → run "Daily FII/DII tracker" manually once to seed

Dashboard goes live at `https://<user>.github.io/fii-dii-tracker/`.

## Notes

- NSE publishes FII/DII around 18:30 IST — workflow runs at 19:30 IST.
- Yahoo Finance has no rate limit for this volume; if it ever fails for a single ticker, the dashboard still renders (gracefully degrades).
- On weekends/holidays no new data is appended.
- The 30-day FII/DII trend will fill in over time as history accumulates.
