"""Post the daily FII/DII dashboard to Telegram.

Sends the rendered dashboard.jpg (built by render_dashboard.py) as a photo
with a structured caption summarising today's net flows. Falls back to
text-only if the image isn't available.

Required env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT_DIR = HERE / "output"
DATA_DIR = HERE / "data"
DASHBOARD_JPG = OUT_DIR / "dashboard.jpg"
LATEST_JSON = DATA_DIR / "latest.json"

CAPTION_LIMIT = 1024


def _load_latest() -> dict:
    if not LATEST_JSON.exists():
        return {}
    try:
        return json.loads(LATEST_JSON.read_text())
    except Exception:
        return {}


def _fmt(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}₹{abs(v):,.0f} cr"


def _emoji(v: float) -> str:
    return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"


def build_caption(latest: dict) -> str:
    date = latest.get("date") or ""
    rows = latest.get("rows", [])
    fii = next((r for r in rows if r.get("category", "").startswith("FII")), {})
    dii = next((r for r in rows if r.get("category", "") == "DII"), {})

    if not (fii or dii):
        return f"📊 <b>FII/DII Daily</b>\n{date}\nNo data available."

    fii_net = float(fii.get("net_value") or 0)
    dii_net = float(dii.get("net_value") or 0)
    total = fii_net + dii_net

    parts = [
        "📊 <b>FII / DII — Daily Activity</b>",
        f"<i>{date}</i>",
        "",
        f"{_emoji(fii_net)} <b>FII/FPI net</b>: {_fmt(fii_net)}",
        (f"   Buy ₹{float(fii.get('buy_value') or 0):,.0f} cr · "
         f"Sell ₹{float(fii.get('sell_value') or 0):,.0f} cr"),
        "",
        f"{_emoji(dii_net)} <b>DII net</b>: {_fmt(dii_net)}",
        (f"   Buy ₹{float(dii.get('buy_value') or 0):,.0f} cr · "
         f"Sell ₹{float(dii.get('sell_value') or 0):,.0f} cr"),
        "",
        f"<b>Combined institutional net</b>: {_fmt(total)}",
        "",
        "<i>Source: NSE provisional. Cash market only.</i>",
        "<i>— myfinancial.in</i>",
    ]
    cap = "\n".join(parts)
    return cap[:CAPTION_LIMIT - 5]


def post(token: str, chat_id: str, caption: str,
          image_path: Path) -> bool:
    if image_path.exists():
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        for attempt in range(3):
            try:
                with open(image_path, "rb") as f:
                    files = {"photo": (image_path.name, f, "image/jpeg")}
                    data = {"chat_id": chat_id, "caption": caption,
                            "parse_mode": "HTML"}
                    r = requests.post(url, data=data, files=files,
                                       timeout=30)
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get(
                        "retry_after", 5)
                    print(f"Telegram 429 — waiting {wait}s", file=sys.stderr)
                    time.sleep(wait + 1)
                    continue
                if r.ok:
                    print(f"✓ photo sent (msg_id "
                          f"{r.json().get('result', {}).get('message_id')})")
                    return True
                print(f"Telegram {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
                return False
            except Exception as e:
                print(f"Telegram error (attempt {attempt+1}): {e}",
                      file=sys.stderr)
                time.sleep(3)
        return False
    # Fall back to text
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id, "text": caption, "parse_mode": "HTML",
        "disable_web_page_preview": True}, timeout=30)
    return r.ok


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping",
              file=sys.stderr)
        return 0
    latest = _load_latest()
    caption = build_caption(latest)
    ok = post(token, chat_id, caption, DASHBOARD_JPG)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
