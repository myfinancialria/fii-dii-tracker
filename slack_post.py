"""Post the daily FII/DII summary + chart to Slack.

Two modes:
  - Incoming webhook (text + chart link only): set SLACK_WEBHOOK_URL
  - Bot token (uploads chart image directly): set SLACK_BOT_TOKEN + SLACK_CHANNEL
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

OUT_DIR = Path(__file__).parent / "output"
SUMMARY_JSON = OUT_DIR / "summary.json"
CHART_PNG = OUT_DIR / "fii_dii_latest.png"


def _fmt(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}₹{abs(v):,.0f} Cr"


def _emoji(v: float) -> str:
    return ":chart_with_upwards_trend:" if v >= 0 else ":chart_with_downwards_trend:"


def build_blocks(summary: dict, chart_url: str | None) -> list[dict]:
    fii_n = summary["fii"]["net"]
    dii_n = summary["dii"]["net"]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"FII / DII — {summary['date_pretty']}"},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{_emoji(fii_n)} FII / FPI*\n"
                        f"Buy: ₹{summary['fii']['buy']:,.0f} Cr\n"
                        f"Sell: ₹{summary['fii']['sell']:,.0f} Cr\n"
                        f"*Net: {_fmt(fii_n)}*"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{_emoji(dii_n)} DII*\n"
                        f"Buy: ₹{summary['dii']['buy']:,.0f} Cr\n"
                        f"Sell: ₹{summary['dii']['sell']:,.0f} Cr\n"
                        f"*Net: {_fmt(dii_n)}*"
                    ),
                },
            ],
        },
    ]
    if chart_url:
        blocks.append({
            "type": "image",
            "image_url": chart_url,
            "alt_text": "FII DII chart",
        })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Source: NSE • Cash market • myfinancial.in"}],
    })
    return blocks


def post_webhook(summary: dict, chart_url: str | None) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]
    payload = {
        "text": f"FII/DII {summary['date_pretty']} — "
                f"FII {_fmt(summary['fii']['net'])} / DII {_fmt(summary['dii']['net'])}",
        "blocks": build_blocks(summary, chart_url),
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    print(f"Posted to Slack webhook: {r.status_code}")


def post_upload(summary: dict) -> None:
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL"]
    headers = {"Authorization": f"Bearer {token}"}

    size = CHART_PNG.stat().st_size
    r = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=headers,
        params={"filename": CHART_PNG.name, "length": size},
        timeout=15,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getUploadURLExternal failed: {j}")
    upload_url = j["upload_url"]
    file_id = j["file_id"]

    with CHART_PNG.open("rb") as f:
        up = requests.post(upload_url, files={"file": f}, timeout=30)
        up.raise_for_status()

    initial_comment = (
        f"*FII / DII — {summary['date_pretty']}*\n"
        f"FII Net: *{_fmt(summary['fii']['net'])}*  •  "
        f"DII Net: *{_fmt(summary['dii']['net'])}*"
    )
    r = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({
            "files": [{"id": file_id, "title": f"FII/DII {summary['date']}"}],
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
    summary = json.loads(SUMMARY_JSON.read_text())

    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL"):
        post_upload(summary)
    elif os.environ.get("SLACK_WEBHOOK_URL"):
        chart_url = os.environ.get("CHART_URL")
        post_webhook(summary, chart_url)
    else:
        print("Set SLACK_WEBHOOK_URL (simple) or SLACK_BOT_TOKEN+SLACK_CHANNEL (uploads image)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
