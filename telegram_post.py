"""Post the daily FII/DII dashboard to Telegram in image-first brief format.

ONE message per run:
  • Dashboard image (output/dashboard.jpg, falls back to market_pulse.png).
  • Caption — one simple paragraph synopsis of the day (indices, FII/DII
    flows, top movers, sector leaders) + a link to the live dashboard.

Required env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
Optional:
  DASHBOARD_URL — overrides the default GitHub Pages URL.
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
CHART_PNG = OUT_DIR / "market_pulse.png"
SUMMARY_JSON = OUT_DIR / "summary.json"
LATEST_JSON = DATA_DIR / "latest.json"

DEFAULT_DASHBOARD_URL = "https://myfinancialria.github.io/fii-dii-tracker/"
CAPTION_LIMIT = 1024


SECTOR_DISPLAY = {
    "NIFTY IT": "IT", "NIFTY BANK": "Bank Nifty", "NIFTY AUTO": "Auto",
    "NIFTY PHARMA": "Pharma", "NIFTY FMCG": "FMCG", "NIFTY METAL": "Metal",
    "NIFTY REALTY": "Realty", "NIFTY ENERGY": "Energy",
    "NIFTY PSU BANK": "PSU Bank", "NIFTY FINANCIAL SERVICES": "Financials",
}


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attachment() -> Path | None:
    if DASHBOARD_JPG.exists():
        return DASHBOARD_JPG
    if CHART_PNG.exists():
        return CHART_PNG
    return None


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_cr(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}₹{abs(v):,.0f} Cr"


def _direction(v: float) -> str:
    if v > 0.1:
        return "gained"
    if v < -0.1:
        return "slipped"
    return "ended flat"


def _names(rows: list[dict], n: int = 3) -> str:
    return ", ".join(r["symbol"] for r in rows[:n]) if rows else ""


def _sectors(rows: list[dict], n: int = 2) -> str:
    if not rows:
        return ""
    parts = []
    for r in rows[:n]:
        nm = SECTOR_DISPLAY.get(r["name"], r["name"].replace("NIFTY ", "").title())
        parts.append(f"{nm} ({_fmt_pct(r.get('pct', 0))})")
    return ", ".join(parts)


def _from_summary(s: dict) -> tuple[str, str]:
    """Rich synopsis when output/summary.json is available."""
    nifty = s.get("nifty", {})
    bnk = s.get("banknifty", {})
    vix = s.get("vix", {})
    fii_n = (s.get("fii") or {}).get("net", 0)
    dii_n = (s.get("dii") or {}).get("net", 0)
    mood = s.get("mood", "")
    gainers = (s.get("movers") or {}).get("gainers", [])
    losers = (s.get("movers") or {}).get("losers", [])
    cs = s.get("catalyst_sectors") or {}

    parts: list[str] = []
    parts.append(
        f"Indian markets closed in a {mood.lower() or 'mixed'} mood — "
        f"the Nifty 50 {_direction(nifty.get('pct', 0))} "
        f"{_fmt_pct(nifty.get('pct', 0))} to {nifty.get('last', 0):,.0f}, "
        f"Bank Nifty {_direction(bnk.get('pct', 0))} "
        f"{_fmt_pct(bnk.get('pct', 0))}, and India VIX settled at "
        f"{vix.get('last', 0):.2f} ({_fmt_pct(vix.get('pct', 0))})."
    )
    parts.append(
        f"Institutional flow showed FIIs net {_fmt_cr(fii_n)} and "
        f"DIIs net {_fmt_cr(dii_n)} in cash."
    )
    g, l = _names(gainers), _names(losers)
    if g or l:
        bits = []
        if g: bits.append(f"top gainers were {g}")
        if l: bits.append(f"biggest drags were {l}")
        parts.append("Among the Nifty 50, " + " while ".join(bits) + ".")
    top_sec, bot_sec = _sectors(cs.get("top", [])), _sectors(cs.get("bottom", []))
    if top_sec or bot_sec:
        bits = []
        if top_sec: bits.append(f"{top_sec} led the rally")
        if bot_sec: bits.append(f"{bot_sec} lagged")
        parts.append("Sector-wise, " + ", and ".join(bits) + ".")

    date_pretty = s.get("date_pretty") or s.get("date") or ""
    return date_pretty, " ".join(parts)


def _from_latest(latest: dict) -> tuple[str, str]:
    """Flows-only synopsis when summary.json isn't available."""
    rows = latest.get("rows", [])
    fii = next((r for r in rows if r.get("category", "").startswith("FII")), {})
    dii = next((r for r in rows if r.get("category", "") == "DII"), {})
    fii_net = float(fii.get("net_value") or 0)
    dii_net = float(dii.get("net_value") or 0)
    combined = fii_net + dii_net

    sentence = (
        f"In Friday's cash market, FIIs were net {_fmt_cr(fii_net)} "
        f"(bought ₹{float(fii.get('buy_value') or 0):,.0f} Cr, sold "
        f"₹{float(fii.get('sell_value') or 0):,.0f} Cr) while DIIs were "
        f"net {_fmt_cr(dii_net)} (bought "
        f"₹{float(dii.get('buy_value') or 0):,.0f} Cr, sold "
        f"₹{float(dii.get('sell_value') or 0):,.0f} Cr), leaving combined "
        f"institutional flows at {_fmt_cr(combined)}."
    )
    return latest.get("date") or "", sentence


def build_synopsis() -> tuple[str, str]:
    """Returns (date_pretty, one-paragraph synopsis)."""
    if SUMMARY_JSON.exists():
        try:
            return _from_summary(json.loads(SUMMARY_JSON.read_text()))
        except Exception as e:
            print(f"summary.json parse failed ({e}); falling back to latest.json",
                  file=sys.stderr)
    if LATEST_JSON.exists():
        return _from_latest(json.loads(LATEST_JSON.read_text()))
    return "", "No data available for today's session."


def build_caption() -> str:
    date_pretty, synopsis = build_synopsis()
    dashboard_url = os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    title = f"📊 Indian Market Pulse — {date_pretty}" if date_pretty else "📊 Indian Market Pulse"

    lines = [
        f"<b>{_escape(title)}</b>",
        "",
        _escape(synopsis),
        "",
        f'🔗 <a href="{dashboard_url}">Full dashboard — charts, breadth & flows</a>',
    ]
    caption = "\n".join(lines)
    if len(caption) <= CAPTION_LIMIT:
        return caption

    overflow = len(caption) - CAPTION_LIMIT + 1
    lines[2] = _escape(synopsis[:-overflow].rstrip().rstrip(".") + "…")
    return "\n".join(lines)


def _send_photo(token: str, chat_id: str, photo: Path, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    mime = "image/jpeg" if photo.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    for attempt in range(3):
        try:
            with photo.open("rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption,
                          "parse_mode": "HTML"},
                    files={"photo": (photo.name, f, mime)},
                    timeout=60,
                )
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                print(f"Telegram 429 — waiting {wait}s", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            if r.ok:
                print(f"✓ photo sent (msg_id "
                      f"{r.json().get('result', {}).get('message_id')})")
                return True
            print(f"Telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Telegram error (attempt {attempt+1}): {e}", file=sys.stderr)
            time.sleep(3)
    return False


def _send_text(token: str, chat_id: str, text: str) -> bool:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    return r.ok


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping",
              file=sys.stderr)
        return 0
    caption = build_caption()
    photo = _attachment()
    ok = (_send_photo(token, chat_id, photo, caption) if photo
          else _send_text(token, chat_id, caption))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
