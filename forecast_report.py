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


def next_trading_day(d: dt.date) -> dt.date:
    """Return the next weekday after d (Sat/Sun rolled to Mon).

    Doesn't account for NSE/BSE holiday calendar — close enough for the
    Slack headline; the data the next run pulls is authoritative.
    """
    nd = d + dt.timedelta(days=1)
    while nd.weekday() >= 5:  # 5=Sat, 6=Sun
        nd += dt.timedelta(days=1)
    return nd


def detect_candle_patterns(df: pd.DataFrame) -> dict:
    """Inspect the last 2 daily candles and report color, body% and patterns
    (doji, hammer/inverted-hammer, shooting-star/hanging-man, engulfing)."""
    if len(df) < 2:
        return {}
    prev = df.iloc[-2]
    last = df.iloc[-1]

    def analyse(o, h, l, c, p=None):
        body = abs(c - o)
        rng = max(h - l, 1e-6)
        upper = h - max(o, c)
        lower = min(o, c) - l
        color = "green" if c >= o else "red"
        body_pct = body / rng * 100
        notes = []
        if body_pct < 10:
            notes.append("doji")
        if lower > 2 * body and upper < body:
            notes.append("hammer" if color == "green" else "hanging-man")
        if upper > 2 * body and lower < body:
            notes.append("shooting-star" if color == "red" else "inverted-hammer")
        if p is not None:
            po, pc = p["open"], p["close"]
            if c >= o and pc < po and c > po and o < pc:
                notes.append("bullish-engulfing")
            if c < o and pc > po and c < po and o > pc:
                notes.append("bearish-engulfing")
        return {"color": color, "body_pct": round(body_pct), "notes": notes}

    return {
        "today":     analyse(last["open"], last["high"], last["low"],
                             last["close"], prev),
        "yesterday": analyse(prev["open"], prev["high"], prev["low"],
                             prev["close"]),
    }


def ma_stack(close: float, ema20: float, ema50: float) -> str:
    """One-line narrative of price vs EMA20/EMA50 alignment."""
    if close > ema20 > ema50:
        return "bullish stack (price > EMA20 > EMA50)"
    if close < ema20 < ema50:
        return "bearish stack (price < EMA20 < EMA50)"
    if close > ema50 and close > ema20:
        return "above both EMAs"
    if close < ema50 and close < ema20:
        return "below both EMAs"
    if close > ema50:
        return "above EMA50, below EMA20 — pullback in uptrend"
    return "above EMA20, below EMA50 — bounce in downtrend"


def fetch_oi_signals(fyers: fyersModel.FyersModel,
                     symbol: str) -> Optional[dict]:
    """Pull option chain max-Call/max-Put OI strikes + PCR for indices that
    have F&O (Nifty, Sensex, Bank Nifty). Returns None if endpoint fails."""
    try:
        resp = fyers.optionchain(data={"symbol": symbol,
                                       "strikecount": 25,
                                       "timestamp": ""})
    except Exception as e:
        print(f"  OI fetch failed: {e}", file=sys.stderr)
        return None
    if resp.get("code") != 200:
        print(f"  OI fetch non-200: {resp.get('message')}", file=sys.stderr)
        return None
    data = resp.get("data", {})
    chain = data.get("optionsChain", [])
    opts = [r for r in chain if r.get("option_type") in ("CE", "PE")]
    if not opts:
        return None
    df = pd.DataFrame(opts)[["strike_price", "option_type", "ltp", "oi"]]
    calls = df[df.option_type == "CE"]
    puts  = df[df.option_type == "PE"]
    if calls.empty or puts.empty:
        return None
    top_c = calls.nlargest(1, "oi").iloc[0]
    top_p = puts.nlargest(1, "oi").iloc[0]
    ce_total = float(calls["oi"].sum())
    pe_total = float(puts["oi"].sum())
    pcr = round(pe_total / ce_total, 2) if ce_total > 0 else 0
    expiry = ""
    if data.get("expiryData"):
        expiry = data["expiryData"][0].get("date", "")
    if pcr >= 1.1:    pcr_read = "bullish"
    elif pcr <= 0.8:  pcr_read = "bearish"
    else:             pcr_read = "neutral"
    return {
        "expiry":            expiry,
        "resistance_strike": int(top_c["strike_price"]),
        "resistance_oi":     int(top_c["oi"]),
        "support_strike":    int(top_p["strike_price"]),
        "support_oi":        int(top_p["oi"]),
        "pcr":               pcr,
        "pcr_read":          pcr_read,
    }


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
    """Project tomorrow's range from latest indicators + technicals snapshot."""
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
        "candles":      detect_candle_patterns(df),
        "ma_stack":     ma_stack(close, float(last["ema20"]),
                                 float(last["ema50"])),
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


def _candle_line(c: dict) -> str:
    if not c:
        return ""
    color = c.get("color", "")
    body = c.get("body_pct", 0)
    notes = ", ".join(c.get("notes", [])) or "—"
    return f"{color} body {body}%, pattern: {notes}"


def build_slack_blocks(date_for_pretty: str, results: dict) -> list[dict]:
    """date_for_pretty is the date the forecast is *for* (next trading day)."""
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"📊 Range Forecast for {date_for_pretty}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "ATR(14) bands · candles · MA stack · "
                               "option-chain OI · live from Fyers"}]},
    ]
    for name, payload in results["symbols"].items():
        chg = payload["chg"]; chg_pct = payload["chg_pct"]
        chg_str = f"{'+' if chg >= 0 else ''}{chg:,.0f} ({chg_pct:+.2f}%)"
        tight  = payload["tomorrow"]["tight"]
        normal = payload["tomorrow"]["normal"]
        wide   = payload["tomorrow"]["wide"]
        piv    = payload["pivot"]
        bt     = results["backtest"].get(name)
        candles = payload.get("candles", {})
        ma_str  = payload.get("ma_stack", "")
        oi      = results.get("oi", {}).get(name)

        head = (f"{_bias_emoji(payload['bias'])} *{name}* — close "
                f"*{_fmt_n(payload['close'])}* ({chg_str})\n"
                f"Today: {_fmt_n(payload['today_low'])} – "
                f"{_fmt_n(payload['today_high'])} · "
                f"ATR {_fmt_n(payload['atr14'])} · "
                f"RSI {payload['rsi14']:.0f} · "
                f"Bias _{payload['bias']}_")

        # Technicals block: candles + MA stack + EMAs
        tech_lines = []
        if candles.get("today"):
            tech_lines.append(f"Today's candle: _{_candle_line(candles['today'])}_")
        if candles.get("yesterday"):
            tech_lines.append(f"Yesterday: _{_candle_line(candles['yesterday'])}_")
        if ma_str:
            tech_lines.append(
                f"MA stack: _{ma_str}_   ·   "
                f"EMA20 {_fmt_n(payload['ema20'])} · "
                f"EMA50 {_fmt_n(payload['ema50'])}"
            )
        tech_block = ("\n*Technicals:*\n" + "\n".join(tech_lines)) if tech_lines else ""

        # OI block (only for F&O indices that returned data)
        oi_block = ""
        if oi:
            oi_block = (
                f"\n*Options OI* (expiry {oi['expiry']}):\n"
                f"• Resistance (max Call OI): `{_fmt_n(oi['resistance_strike'])}` "
                f"({oi['resistance_oi']:,} OI)\n"
                f"• Support    (max Put  OI): `{_fmt_n(oi['support_strike'])}` "
                f"({oi['support_oi']:,} OI)\n"
                f"• PCR {oi['pcr']} → _{oi['pcr_read']}_"
            )

        body = (
            f"\n*Expected range:*\n"
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
                       "text": {"type": "mrkdwn",
                                "text": head + tech_block + oi_block + body}})
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
        try:
            j = r.json()
        except Exception:
            j = {}
        ok = r.ok and j.get("ok")
        if not ok:
            print(f"Slack post FAILED: http={r.status_code} "
                  f"error={j.get('error')} "
                  f"channel_arg={channel} "
                  f"body={r.text[:400]}", file=sys.stderr)
            return
        # Successful post — log a permalink so we can verify which channel
        # actually received it (GitHub Actions masks the raw channel id).
        ch_id = j.get("channel") or channel
        ts = j.get("ts")
        permalink = None
        try:
            pl = requests.get(
                "https://slack.com/api/chat.getPermalink",
                headers={"Authorization": f"Bearer {token}"},
                params={"channel": ch_id, "message_ts": ts}, timeout=15,
            ).json()
            if pl.get("ok"):
                permalink = pl.get("permalink")
        except Exception as e:
            print(f"  (permalink lookup failed: {e})", file=sys.stderr)
        if permalink:
            print(f"Posted to Slack: {permalink}")
        else:
            print(f"Posted to Slack (ts={ts})")
        # Also post a webhook copy if both are configured (lets you debug
        # cases where the bot landed in the wrong channel).
        if webhook:
            text = "\n".join(b.get("text", {}).get("text", "")
                             for b in blocks
                             if b.get("type") in ("header", "section"))
            try:
                rw = requests.post(webhook, json={"text": text}, timeout=30)
                print(f"  Webhook mirror: {rw.status_code}")
            except Exception as e:
                print(f"  Webhook mirror failed: {e}", file=sys.stderr)
        return
    if webhook:
        text = "\n".join(b.get("text", {}).get("text", "")
                        for b in blocks if b.get("type") in ("header", "section"))
        r = requests.post(webhook, json={"text": text}, timeout=30)
        print(f"Posted to Slack via webhook: {r.status_code}")
        return
    print("No Slack credentials set — skipping post.", file=sys.stderr)


def post_to_telegram(date_for_pretty: str, results: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("Telegram credentials not set — skipping.", file=sys.stderr)
        return
    lines = [f"📊 *Range Forecast for {date_for_pretty}*",
             "_ATR(14) bands · candles · MA stack · option-chain OI_", ""]
    for name, p in results["symbols"].items():
        chg = p["chg"]
        chg_str = f"{'+' if chg >= 0 else ''}{chg:,.0f} ({p['chg_pct']:+.2f}%)"
        n  = p["tomorrow"]["normal"]
        t_ = p["tomorrow"]["tight"]
        w  = p["tomorrow"]["wide"]
        oi = results.get("oi", {}).get(name)
        c  = p.get("candles", {}).get("today", {})
        ma = p.get("ma_stack", "")
        lines.append(f"*{name}* — close *{p['close']:,.0f}* ({chg_str})")
        lines.append(f"Today: {p['today_low']:,.0f}–{p['today_high']:,.0f}  ·  "
                     f"ATR {p['atr14']:,.0f}  ·  RSI {p['rsi14']:.0f}  ·  "
                     f"Bias _{p['bias']}_")
        if c:
            patterns = ", ".join(c.get("notes", [])) or "—"
            lines.append(f"Candle: {c.get('color','')} body {c.get('body_pct',0)}%, {patterns}")
        if ma:
            lines.append(f"MA: _{ma}_")
        if oi:
            lines.append(f"OI (exp {oi['expiry']}): R `{oi['resistance_strike']:,}` · "
                         f"S `{oi['support_strike']:,}` · PCR {oi['pcr']} "
                         f"({oi['pcr_read']})")
        lines.append(f"📈 *Tomorrow:*  Normal `{n['low']:,.0f}–{n['high']:,.0f}`")
        lines.append(f"   · Tight `{t_['low']:,.0f}–{t_['high']:,.0f}`  "
                     f"· Wide `{w['low']:,.0f}–{w['high']:,.0f}`")
        bt = results["backtest"].get(name)
        if bt:
            pf = bt["prev_forecast"].get("normal", {})
            ok = "✅" if (bt["inside_normal"] or bt["inside_tight"]) \
                 else ("🟡" if bt["inside_wide"] else "❌")
            lines.append(f"{ok} Yesterday's call: {bt['verdict']}")
            lines.append(f"   Forecast `{pf.get('low',0):,.0f}–{pf.get('high',0):,.0f}`  "
                         f"→  Actual `{bt['actual_low_today']:,.0f}–"
                         f"{bt['actual_high_today']:,.0f}`")
        lines.append("")
    text = "\n".join(lines).strip()
    # Telegram MarkdownV2 has strict escaping; use legacy 'Markdown' parse_mode
    # for simpler handling.
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        if r.ok and j.get("ok"):
            mid = j.get("result", {}).get("message_id")
            print(f"Posted to Telegram (message_id={mid})")
        else:
            print(f"Telegram post failed: {r.status_code} {r.text[:300]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"Telegram post error: {e}", file=sys.stderr)


def main() -> int:
    today = dt.date.today()
    tomorrow = next_trading_day(today)
    tomorrow_pretty = tomorrow.strftime("%d %b %Y")
    prev = find_previous_forecast(today)
    fyers = get_fyers_client()

    results: dict = {
        "date":            today.isoformat(),
        "forecast_for":    tomorrow.isoformat(),
        "symbols":         {},
        "backtest":        {},
        "oi":              {},
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
            oi = fetch_oi_signals(fyers, sym)
            if oi:
                results["oi"][name] = oi
                print(f"  OI: R {oi['resistance_strike']} ({oi['resistance_oi']:,})  "
                      f"S {oi['support_strike']} ({oi['support_oi']:,})  "
                      f"PCR {oi['pcr']} ({oi['pcr_read']})")
            print(f"  close {fc['close']:,.2f}  ATR {fc['atr14']:,.2f}  "
                  f"normal {fc['tomorrow']['normal']['low']:,.0f} – "
                  f"{fc['tomorrow']['normal']['high']:,.0f}  bias {fc['bias']}  "
                  f"MA {fc['ma_stack']}")
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

    blocks = build_slack_blocks(tomorrow_pretty, results)
    post_to_slack(blocks)
    post_to_telegram(tomorrow_pretty, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
