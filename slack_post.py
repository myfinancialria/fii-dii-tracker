"""Post the daily Market Pulse digest + chart to Slack.

Two modes:
  - Incoming webhook (text + chart link): SLACK_WEBHOOK_URL
  - Bot token (uploads chart directly): SLACK_BOT_TOKEN + SLACK_CHANNEL
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

OUT_DIR = Path(__file__).parent / "output"
SUMMARY_JSON = OUT_DIR / "summary.json"
DASHBOARD_JPG = OUT_DIR / "dashboard.jpg"
CHART_PNG = OUT_DIR / "market_pulse.png"


def _attachment() -> Path:
    """Prefer full dashboard JPG; fall back to chart PNG."""
    return DASHBOARD_JPG if DASHBOARD_JPG.exists() else CHART_PNG


def _fmt_cr(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}₹{abs(v):,.0f} Cr"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _emoji(v: float) -> str:
    return ":chart_with_upwards_trend:" if v >= 0 else ":chart_with_downwards_trend:"


def _idx_line(name: str, d: dict) -> str:
    if not d:
        return f"*{name}*: —"
    return (f"*{name}* {d.get('last', 0):,.0f}  "
            f"({_fmt_pct(d.get('pct', 0))})")


def _global_strip(items: dict) -> str:
    keys = [("Dow Jones", "Dow"), ("Nasdaq", "Nasdaq"), ("S&P 500", "S&P"),
            ("Nikkei 225", "Nikkei"), ("Hang Seng", "HSI")]
    parts = []
    for k, label in keys:
        d = items.get(k, {})
        if d:
            parts.append(f"{label} {_fmt_pct(d.get('pct', 0))}")
    return "  •  ".join(parts) if parts else "—"


def _fx_strip(items: dict) -> str:
    parts = []
    u = items.get("USD/INR", {})
    if u:
        parts.append(f"USD/INR {u.get('last', 0):.2f} ({_fmt_pct(u.get('pct', 0))})")
    b = items.get("Brent Crude", {})
    if b:
        parts.append(f"Brent ${b.get('last', 0):.1f} ({_fmt_pct(b.get('pct', 0))})")
    g = items.get("Gold", {})
    if g:
        parts.append(f"Gold ${g.get('last', 0):,.0f} ({_fmt_pct(g.get('pct', 0))})")
    return "  •  ".join(parts) if parts else "—"


def _movers_line(rows: list[dict]) -> str:
    if not rows:
        return "—"
    return "  ".join(f"{r['symbol']} {_fmt_pct(r['pct'])}" for r in rows)


def build_blocks(s: dict, chart_url: str | None) -> list[dict]:
    fii_n = s["fii"]["net"]
    dii_n = s["dii"]["net"]
    mood = s.get("mood", "")
    mood_emoji = {"Risk-On": ":green_circle:", "Risk-Off": ":red_circle:",
                  "Mixed": ":large_yellow_circle:",
                  "Range-bound": ":white_circle:"}.get(mood, ":white_circle:")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": f"Indian Market Pulse — {s['date_pretty']}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{mood_emoji} *Mood:* {mood}  •  "
                    f"*India VIX:* {s['vix'].get('last', 0):.2f} "
                    f"({_fmt_pct(s['vix'].get('pct', 0))})"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn",
                 "text": _idx_line("Nifty 50", s["nifty"])},
                {"type": "mrkdwn",
                 "text": _idx_line("Bank Nifty", s["banknifty"])},
                {"type": "mrkdwn",
                 "text": _idx_line("Midcap 100", s["midcap"])},
                {"type": "mrkdwn",
                 "text": _idx_line("India VIX", s["vix"])},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{_emoji(fii_n)} FII / FPI (Cash)*\n"
                        f"Buy ₹{s['fii']['buy']:,.0f} Cr  •  Sell ₹{s['fii']['sell']:,.0f} Cr\n"
                        f"*Net: {_fmt_cr(fii_n)}*"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{_emoji(dii_n)} DII (Cash)*\n"
                        f"Buy ₹{s['dii']['buy']:,.0f} Cr  •  Sell ₹{s['dii']['sell']:,.0f} Cr\n"
                        f"*Net: {_fmt_cr(dii_n)}*"
                    ),
                },
            ],
        },
    ]

    gainers = s.get("movers", {}).get("gainers", [])
    losers = s.get("movers", {}).get("losers", [])
    if gainers or losers:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn",
                 "text": f"*Top Gainers*\n{_movers_line(gainers)}"},
                {"type": "mrkdwn",
                 "text": f"*Top Losers*\n{_movers_line(losers)}"},
            ],
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*Global cues*\n{_global_strip(s.get('global', {}))}\n\n"
                f"*FX / Commodities*\n{_fx_strip(s.get('fx_commodities', {}))}"
            ),
        },
    })

    if chart_url:
        blocks.append({"type": "image", "image_url": chart_url,
                       "alt_text": "Market pulse chart"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": "Source: NSE + Yahoo Finance · myfinancial.in"}],
    })
    return blocks


def post_webhook(s: dict, chart_url: str | None) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]
    payload = {
        "text": (f"Market Pulse {s['date_pretty']} — "
                 f"Nifty {_fmt_pct(s['nifty'].get('pct', 0))} · "
                 f"FII {_fmt_cr(s['fii']['net'])} / DII {_fmt_cr(s['dii']['net'])}"),
        "blocks": build_blocks(s, chart_url),
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    print(f"Posted to Slack webhook: {r.status_code}")


def post_upload(s: dict) -> None:
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL"]
    headers = {"Authorization": f"Bearer {token}"}

    attach = _attachment()
    size = attach.stat().st_size
    r = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=headers,
        params={"filename": attach.name, "length": size},
        timeout=15,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getUploadURLExternal failed: {j}")
    upload_url = j["upload_url"]
    file_id = j["file_id"]

    with attach.open("rb") as f:
        up = requests.post(upload_url, files={"file": f}, timeout=30)
        up.raise_for_status()

    initial_comment = (
        f"*Market Pulse — {s['date_pretty']}*\n"
        f"Nifty 50: *{s['nifty'].get('last', 0):,.0f}* "
        f"({_fmt_pct(s['nifty'].get('pct', 0))})  •  "
        f"Mood: *{s.get('mood', '')}*  •  "
        f"VIX: *{s['vix'].get('last', 0):.2f}*\n"
        f"FII Net: *{_fmt_cr(s['fii']['net'])}*  •  "
        f"DII Net: *{_fmt_cr(s['dii']['net'])}*\n\n"
        f"Global: {_global_strip(s.get('global', {}))}\n"
        f"FX/Comm: {_fx_strip(s.get('fx_commodities', {}))}"
    )
    r = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({
            "files": [{"id": file_id, "title": f"Market Pulse {s['date']}"}],
            "channel_id": channel,
            "initial_comment": initial_comment,
        }),
        timeout=15,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"completeUploadExternal failed: {j}")
    print(f"Uploaded chart to Slack channel {channel}")


def main() -> int:
    if not SUMMARY_JSON.exists():
        print("summary.json missing — run visualize.py first", file=sys.stderr)
        return 1
    s = json.loads(SUMMARY_JSON.read_text())

    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL"):
        post_upload(s)
    elif os.environ.get("SLACK_WEBHOOK_URL"):
        chart_url = os.environ.get("CHART_URL")
        post_webhook(s, chart_url)
    else:
        print("Set SLACK_WEBHOOK_URL (webhook) or SLACK_BOT_TOKEN+SLACK_CHANNEL (upload).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
