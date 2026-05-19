"""Market data fetchers — NSE (India) + yfinance (global, FX, commodities).

Each function returns a dict and never raises — failures yield an empty dict
plus a printed warning so the rest of the dashboard still renders.
"""
from __future__ import annotations

import time
from typing import Any

import requests

NSE_HOME = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

INDEX_DISPLAY_ORDER = [
    "NIFTY 50",
    "NIFTY BANK",
    "NIFTY MIDCAP 100",
    "NIFTY SMLCAP 100",
    "NIFTY IT",
    "NIFTY AUTO",
    "NIFTY PHARMA",
    "NIFTY FMCG",
    "NIFTY METAL",
    "NIFTY REALTY",
    "NIFTY ENERGY",
    "NIFTY PSU BANK",
]

GLOBAL_TICKERS = {
    "Dow Jones": "^DJI",
    "Nasdaq": "^IXIC",
    "S&P 500": "^GSPC",
    "FTSE 100": "^FTSE",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
}
FX_COMM_TICKERS = {
    "USD/INR": "INR=X",
    "Dollar Index": "DX-Y.NYB",
    "Brent Crude": "BZ=F",
    "Gold": "GC=F",
    "US 10Y Yield": "^TNX",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_HOME, timeout=15)
        time.sleep(1)
    except Exception as e:
        print(f"NSE warmup failed: {e}")
    return s


def fetch_indices() -> dict[str, dict]:
    """All NSE indices via /api/allIndices — single call, comprehensive."""
    try:
        s = _session()
        r = s.get(f"{NSE_HOME}/api/allIndices", timeout=15)
        r.raise_for_status()
        out: dict[str, dict] = {}
        for d in r.json().get("data", []):
            name = d.get("index", "")
            out[name] = {
                "last": float(d.get("last") or 0),
                "change": float(d.get("variation") or 0),
                "pct": float(d.get("percentChange") or 0),
                "year_high": float(d.get("yearHigh") or 0),
                "year_low": float(d.get("yearLow") or 0),
                "advances": int(d.get("advances") or 0) if d.get("advances") else None,
                "declines": int(d.get("declines") or 0) if d.get("declines") else None,
                "unchanged": int(d.get("unchanged") or 0) if d.get("unchanged") else None,
            }
        return out
    except Exception as e:
        print(f"fetch_indices failed: {e}")
        return {}


def fetch_breadth() -> dict[str, Any]:
    """Market breadth: advance/decline + 52-week highs/lows for NIFTY 500."""
    try:
        s = _session()
        # Top52 endpoint gives 52W highs/lows on NSE
        r = s.get(f"{NSE_HOME}/api/live-analysis-data-52weekhighstock", timeout=15)
        hl_data = r.json() if r.ok else {}
        highs = len(hl_data.get("HIGH52", {}).get("data", []))
        lows = len(hl_data.get("LOW52", {}).get("data", []))
        return {"new_52w_highs": highs, "new_52w_lows": lows}
    except Exception as e:
        print(f"fetch_breadth failed: {e}")
        return {}


def fetch_movers() -> dict[str, list]:
    """Top 5 gainers / losers in NIFTY 50."""
    try:
        s = _session()
        out = {"gainers": [], "losers": []}
        for key, group in [("gainers", "NIFTY"), ("loosers", "NIFTY")]:
            r = s.get(
                f"{NSE_HOME}/api/live-analysis-variations?index={key}",
                timeout=15,
            )
            if not r.ok:
                continue
            data = r.json().get(group, {}).get("data", [])
            sorted_data = sorted(
                data,
                key=lambda x: float(x.get("perChange") or 0),
                reverse=(key == "gainers"),
            )
            label = "gainers" if key == "gainers" else "losers"
            out[label] = [
                {
                    "symbol": d.get("symbol"),
                    "ltp": float(d.get("ltp") or 0),
                    "pct": float(d.get("perChange") or 0),
                }
                for d in sorted_data[:5]
            ]
        return out
    except Exception as e:
        print(f"fetch_movers failed: {e}")
        return {"gainers": [], "losers": []}


def fetch_yf(tickers: dict[str, str]) -> dict[str, dict]:
    """Last close + % change for a set of yfinance tickers."""
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed")
        return {}
    out: dict[str, dict] = {}
    for name, ticker in tickers.items():
        try:
            h = yf.Ticker(ticker).history(period="5d")
            if len(h) < 2:
                continue
            last = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2])
            out[name] = {
                "last": last,
                "change": last - prev,
                "pct": (last - prev) / prev * 100 if prev else 0,
            }
        except Exception as e:
            print(f"yf {name} failed: {e}")
    return out


def fetch_global() -> dict[str, dict]:
    return fetch_yf(GLOBAL_TICKERS)


def fetch_fx_comm() -> dict[str, dict]:
    return fetch_yf(FX_COMM_TICKERS)


def market_mood(indices: dict, vix_pct: float | None) -> str:
    """Simple mood label from Nifty50 + VIX move."""
    nifty = indices.get("NIFTY 50", {})
    n_pct = nifty.get("pct", 0)
    if n_pct > 0.5 and (vix_pct is None or vix_pct < 0):
        return "Risk-On"
    if n_pct < -0.5 and (vix_pct is None or vix_pct > 0):
        return "Risk-Off"
    if abs(n_pct) < 0.3:
        return "Range-bound"
    return "Mixed"
