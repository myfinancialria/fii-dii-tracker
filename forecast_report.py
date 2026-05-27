"""Daily tomorrow-range forecast + yesterday-prediction backtest.

What this does:
  1. Pulls last ~60 days of daily candles from Fyers API for tracked indices
     (Nifty 50, Sensex, Bank Nifty by default).
  2. Computes tomorrow's expected price range using ATR(14)-based bands:
       - Tight  band = ±0.5 × ATR   (~40% historical coverage)
       - Normal band = ±1.0 × ATR   (~70% coverage)
       - Wide   band = ±1.5 × ATR   (~87% coverage)
  3. Adds classic pivot levels (P, R1/R2, S1/S2) projected from today's H/L/C.
  4. Backtests YESTERDAY's prediction against today's actual H/L — was today's
     real range inside our forecast? Reports tight/normal/wide hit rate.
  5. Saves today's forecast to data/forecasts/YYYY-MM-DD.json for tomorrow's
     backtest to read.
  6. Posts a formatted summary to Slack at 22:00 IST.

Environment (all required):
  FYERS_CLIENT_ID, FYERS_SECRET, FYERS_REDIRECT
  FYERS_FY_ID, FYERS_PIN, FYERS_TOTP_KEY   (for auto_login.py)
  SLACK_BOT_TOKEN, SLACK_CHANNEL           (or SLACK_WEBHOOK_URL)

Optional:
  FORECAST_SYMBOLS=NSE:NIFTY50-INDEX,BSE:SENSEX-INDEX,NSE:NIFTYBANK-INDEX
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

load_dotenv()

HERE = Path(__file__).parent
FORECAST_DIR = HERE / "data" / "forecasts"
FORECAST_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SYMBOLS = [
    ("NIFTY 50",   "NSE:NIFTY50-INDEX"),
    ("SENSEX",     "BSE:SENSEX-INDEX"),
    ("BANK NIFTY", "NSE:NIFTYBANK-INDEX"),
]


def get_symbols() -> list[tuple[str, str]]:
    """Resolve symbol list from FORECAST_SYMBOLS env (comma-separated) or default."""
    raw = os.environ.get("FORECAST_SYMBOLS", "").strip()
    if not raw:
        return DEFAULT_SYMBOLS
    out: list[tuple[str, str]] = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        # User can pass "NIFTY 50:NSE:NIFTY50-INDEX" or just "NSE:NIFTY50-INDEX"
        if s.count(":") >= 2 and not s.startswith(("NSE:", "BSE:", "MCX:")):
            name, sym = s.split(":", 1)
            out.append((name.strip(), sym.strip()))
        else:
            short = s.split(":")[-1].replace("-INDEX", "").replace("-EQ", "")
            out.append((short, s))
    return out


def get_fyers_client() -> fyersModel.FyersModel:
    """Build authenticated Fyers client, expecting access_token.txt in cwd."""
    client_id = os.environ["FYERS_CLIENT_ID"]
    token_file = HERE / "access_token.txt"
    if not token_file.exists():
        token_file = Path("access_token.txt")
    token = token_file.read_text().strip()
    return fyersModel.FyersModel(client_id=client_id, token=token, log_path="")


# ---- Indicator helpers ----
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA(20/50), RSI(14), ATR(14), MACD, daily-return std-dev."""
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss)

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    df["ret"] = df["close"].pct_change()
    df["ret_std20"] = df["ret"].rolling(20).std()
    return df


def fetch_daily_history(fyers: fyersModel.FyersModel, symbol: str,
                        days: int = 90) -> pd.DataFrame:
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    resp = fyers.history({
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"),
        "range_to":   end.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    if resp.get("s") != "ok":
        raise RuntimeError(f"history {symbol}: {resp}")
    df = pd.DataFrame(resp["candles"],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = (pd.to_datetime(df["ts"], unit="s")
                  .dt.tz_localize("UTC")
                  .dt.tz_convert("Asia/Kolkata").dt.date)
    return df[["date", "open", "high", "low", "close", "volume"]]


def classify_bias(last: pd.Series) -> str:
    """Combine RSI + MACD-hist + EMA20-vs-EMA50 into one of bullish/bearish/neutral."""
    score = 0
    rsi = float(last["rsi14"])
    if rsi >= 60: score += 1
    elif rsi <= 40: score -= 1
    if float(last["macd_hist"]) > 0: score += 1
    else: score -= 1
    if float(last["ema20"]) > float(last["ema50"]): score += 1
    else: score -= 1
    if score >= 2:  return "bullish"
    if score <= -2: return "bearish"
    return "neutral"


def pivot_levels(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3
    return {
        "P":  p,
        "R1": 2 * p - low,
        "S1": 2 * p - high,
        "R2": p + (high - low),
        "S2": p - (high - low),
        "R3": high + 2 * (p - low),
        "S3": low - 2 * (high - p),
    }


def build_forecast(df: pd.DataFrame) -> dict:
    """Project tomorrow's range from latest indicators."""
    df = add_indicators(df)
    last = df.iloc[-1]
    close = float(last["close"])
    atr   = float(last["atr14"])
    bands = {
        "tight":  {"low": close - 0.5 * atr, "high": close + 0.5 * atr},
        "normal": {"low": close - 1.0 * atr, "high": close + 1.0 * atr},
        "wide":   {"low": close - 1.5 * atr, "high": close + 1.5 * atr},
    }
    return {
        "close":        close,
        "today_open":   float(last["open"]),
        "today_high":   float(last["high"]),
        "today_low":    float(last["low"]),
        "today_date":   last["date"].isoformat(),
        "chg":          float(last["close"] - df.iloc[-2]["close"]),
        "chg_pct":      float((last["close"] / df.iloc[-2]["close"] - 1) * 100),
        "atr14":        atr,
        "rsi14":        float(last["rsi14"]),
        "macd_hist":    float(last["macd_hist"]),
        "ema20":        float(last["ema20"]),
        "ema50":        float(last["ema50"]),
        "ret_std20":    float(last["ret_std20"]) if pd.notna(last["ret_std20"]) else None,
        "bias":         classify_bias(last),
        "tomorrow":     bands,
        "pivot":        pivot_levels(float(last["high"]),
                                     float(last["low"]),
                                     float(last["close"])),
    }


def find_previous_forecast(today: dt.date) -> Optional[tuple[dt.date, dict]]:
    """Find the most recent forecast file dated BEFORE today, return (date, payload)."""
    files = sorted(FORECAST_DIR.glob("*.json"))
    for f in reversed(files):
        try:
            d = dt.date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < today:
            return d, json.loads(f.read_text())
    return None


def backtest_one(symbol_name: str, todays_forecast: dict,
                 prev: Optional[tuple[dt.date, dict]]) -> Optional[dict]:
    """Compare TODAY's actual H/L (from todays_forecast) against the previous
    saved forecast's bands."""
    if not prev:
        return None
    prev_date, prev_payload = prev
    prev_sym = prev_payload.get("symbols", {}).get(symbol_name)
    if not prev_sym:
        return None
    bands = prev_sym.get("tomorrow", {})
    if not bands:
        return None
    actual_high = todays_forecast["today_high"]
    actual_low  = todays_forecast["today_low"]
    actual_close = todays_forecast["close"]

    def inside(b: dict) -> bool:
        return actual_low >= b["low"] and actual_high <= b["high"]

    tight_hit  = inside(bands.get("tight", {"low": -1, "high": -1}))
    normal_hit = inside(bands.get("normal", {"low": -1, "high": -1}))
    wide_hit   = inside(bands.get("wide", {"low": -1, "high": -1}))

    if tight_hit:    verdict = "HIT — tight band ✓"
    elif normal_hit: verdict = "HIT — normal band ✓"
    elif wide_hit:   verdict = "HIT — wide band ✓"
    else:            verdict = "MISS — broke all bands ✗"

    return {
        "prev_date":         prev_date.isoformat(),
        "prev_forecast":     bands,
        "actual_high_today": actual_high,
        "actual_low_today":  actual_low,
        "actual_close":      actual_close,
        "inside_tight":      tight_hit,
        "inside_normal":     normal_hit,
        "inside_wide":       wide_hit,
        "verdict":           verdict,
    }


# ---- Slack formatting ----
def _bias_emoji(b: str) -> str:
    return {"bullish": ":green_circle:", "bearish": ":red_circle:"}.get(b, ":white_circle:")


def _fmt_n(x: float) -> str:
    return f"{x:,.0f}"


def build_slack_blocks(date_pretty: str, results: dict) -> list[dict]:
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"📊 Tomorrow's Range Forecast — {date_pretty}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "ATR(14)-based bands · live from Fyers · "
                               "next-trading-day projection"}]},
    ]
    for name, payload in results["symbols"].items():
        chg = payload["chg"]; chg_pct = payload["chg_pct"]
        chg_str = f"{'+' if chg >= 0 else ''}{chg:,.0f} ({chg_pct:+.2f}%)"
        tight  = payload["tomorrow"]["tight"]
        normal = payload["tomorrow"]["normal"]
        wide   = payload["tomorrow"]["wide"]
        piv    = payload["pivot"]
        bt     = results["backtest"].get(name)

        head = (f"{_bias_emoji(payload['bias'])} *{name}* — close "
                f"*{_fmt_n(payload['close'])}* ({chg_str})\n"
                f"Today: {_fmt_n(payload['today_low'])} – "
                f"{_fmt_n(payload['today_high'])} · "
                f"ATR {_fmt_n(payload['atr14'])} · "
                f"RSI {payload['rsi14']:.0f} · "
                f"Bias _{payload['bias']}_")
        body = (
            f"\n*Expected tomorrow:*\n"
            f"• Normal (~70%): `{_fmt_n(normal['low'])} – {_fmt_n(normal['high'])}`\n"
            f"• Tight  (~40%): `{_fmt_n(tight['low'])} – {_fmt_n(tight['high'])}`\n"
            f"• Wide   (~87%): `{_fmt_n(wide['low'])} – {_fmt_n(wide['high'])}`\n"
            f"*Key levels:* R2 {_fmt_n(piv['R2'])} · R1 {_fmt_n(piv['R1'])} · "
            f"P {_fmt_n(piv['P'])} · S1 {_fmt_n(piv['S1'])} · S2 {_fmt_n(piv['S2'])}"
        )
        if bt:
            head_emoji = "✅" if (bt["inside_normal"] or bt["inside_tight"]) \
                         else ("🟡" if bt["inside_wide"] else "❌")
            pf = bt["prev_forecast"].get("normal", {})
            body += (
                f"\n{head_emoji} *Yesterday's call:* {bt['verdict']}\n"
                f"   Forecast (normal): `{_fmt_n(pf.get('low',0))} – "
                f"{_fmt_n(pf.get('high',0))}`  →  "
                f"Actual: `{_fmt_n(bt['actual_low_today'])} – "
                f"{_fmt_n(bt['actual_high_today'])}`"
            )
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": head + body}})
        blocks.append({"type": "divider"})

    # Footer cumulative hit rate (rough — count today's hits)
    hits = sum(1 for bt in results["backtest"].values()
               if bt and (bt["inside_normal"] or bt["inside_tight"]))
    backtested = sum(1 for bt in results["backtest"].values() if bt)
    if backtested:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn",
                                     "text": f"_Yesterday's accuracy: "
                                             f"{hits}/{backtested} symbols inside the normal band._"}]})
    return blocks


def post_to_slack(blocks: list[dict]) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if token and channel:
        r = requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"channel": channel, "blocks": blocks,
                                "text": "Tomorrow's range forecast"},
                          timeout=30)
        ok = r.ok and r.json().get("ok")
        if not ok:
            print(f"Slack bot post failed: {r.status_code} {r.text[:300]}",
                  file=sys.stderr)
        else:
            print("Posted to Slack via bot token.")
        return
    if webhook:
        text = "\n".join(b.get("text", {}).get("text", "")
                        for b in blocks if b.get("type") in ("header", "section"))
        r = requests.post(webhook, json={"text": text}, timeout=30)
        print(f"Posted to Slack via webhook: {r.status_code}")
        return
    print("No Slack credentials set — skipping post.", file=sys.stderr)


def main() -> int:
    today = dt.date.today()
    today_pretty = today.strftime("%d %b %Y")
    prev = find_previous_forecast(today)
    fyers = get_fyers_client()

    results: dict = {
        "date": today.isoformat(),
        "symbols": {},
        "backtest": {},
    }

    for name, sym in get_symbols():
        print(f"\n=== {name}  ({sym}) ===")
        try:
            df = fetch_daily_history(fyers, sym, days=90)
            if len(df) < 30:
                print(f"  too few candles ({len(df)}) — skipping")
                continue
            fc = build_forecast(df)
            results["symbols"][name] = fc
            bt = backtest_one(name, fc, prev)
            results["backtest"][name] = bt
            print(f"  close {fc['close']:,.2f}  ATR {fc['atr14']:,.2f}  "
                  f"normal {fc['tomorrow']['normal']['low']:,.0f} – "
                  f"{fc['tomorrow']['normal']['high']:,.0f}  bias {fc['bias']}")
            if bt:
                print(f"  backtest vs {bt['prev_date']}: {bt['verdict']}")
        except Exception as e:
            print(f"  failed: {e}", file=sys.stderr)

    if not results["symbols"]:
        print("No symbols succeeded — aborting", file=sys.stderr)
        return 1

    out = FORECAST_DIR / f"{today.isoformat()}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nSaved forecast: {out}")

    blocks = build_slack_blocks(today_pretty, results)
    post_to_slack(blocks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
