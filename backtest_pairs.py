"""
Walk-forward backtest of the daily pairs-trading strategy (pairs_daily_signal.py),
on Fyers daily equity data. Volrix can't do this (it trades only index/commodity
F&O, not single stocks); pairs run on daily stock prices, which Fyers serves for
years.

Faithful to the live rules:
  - trailing 2y OLS log-price hedge (beta, alpha), refit each day
  - z-score of the spread over a 60-day window
  - ENTRY when a pair is cointegrated (coint p<0.05 AND ADF p<0.05), half-life in
    [5,30] days, and |z| in [2.0, 3.5)
  - direction fades the deviation (z>0 -> short spread)
  - EXIT on target |z|<=0.5, stop |z|>=3.5, or time-stop days_held > 2*half-life
  - P&L% = direction*(spread_exit - spread_entry)/(1+|beta|)  minus 0.10% round-trip
  - one open position per pair at a time; positions can re-open later

Output: output/backtest_pairs_data.json for the dashboard page.

Usage:
    python backtest_pairs.py            # fetch (cached) + backtest + write JSON
    python backtest_pairs.py --years 5  # lookback+backtest span to pull
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel
from statsmodels.tsa.stattools import adfuller, coint

HERE = Path(__file__).parent
load_dotenv("/Users/nithin/fyers-bot/.env")
CID = os.getenv("FYERS_CLIENT_ID")
CACHE = HERE / "data" / "pairs_bt_prices.csv"
OUT = HERE / "output" / "backtest_pairs_data.json"

# ---- strategy constants (mirror pairs_daily_signal.py) ----
CANDIDATE_PAIRS = [
    ("PSU Banks", "BANKBARODA", "CANBK"), ("Metals", "TATASTEEL", "JINDALSTEL"),
    ("Cement", "AMBUJACEM", "ACC"), ("IT", "TCS", "INFY"),
    ("PSU Banks", "BANKBARODA", "PNB"), ("Oil & Gas", "IOC", "BPCL"),
    ("Metals", "TATASTEEL", "SAIL"), ("Metals", "NMDC", "SAIL"),
    ("Metals", "TATASTEEL", "NATIONALUM"), ("Metals", "SAIL", "NATIONALUM"),
    ("NBFC", "BAJFINANCE", "BAJAJFINSV"), ("Oil & Gas", "BPCL", "HPCL"),
    ("Metals", "TATASTEEL", "JSWSTEEL"), ("PSU Banks", "SBIN", "BANKBARODA"),
    ("Oil & Gas", "IOC", "HPCL"), ("PSU Banks", "SBIN", "CANBK"),
    ("PSU Banks", "UNIONBANK", "BANKINDIA"), ("PSU Banks", "PNB", "CANBK"),
    ("PSU Banks", "CANBK", "UNIONBANK"),
]
TICKER_MAP = {"HPCL": "HINDPETRO", "MAXFIN": "MFSL", "TATAMOTORS": "TMPV"}
COINT_P_MAX, ADF_P_MAX = 0.05, 0.05
HL_MIN, HL_MAX = 5, 30
Z_WIN, INSAMPLE_DAYS = 60, 365 * 2
Z_ENTRY, Z_EXIT, Z_STOP = 2.0, 0.5, 3.5
COST_ONE_WAY = 0.0005


def resolve(t):
    return TICKER_MAP.get(t, t)


# ---------------- data ----------------
def fetch_daily(fy, stock, start, end):
    rows, cur = [], start
    while cur <= end:
        ce = min(cur + dt.timedelta(days=360), end)
        try:
            r = fy.history({"symbol": f"NSE:{stock}-EQ", "resolution": "D",
                            "date_format": "1", "range_from": cur.strftime("%Y-%m-%d"),
                            "range_to": ce.strftime("%Y-%m-%d"), "cont_flag": "1"})
            if r.get("s") == "ok":
                rows += r.get("candles", [])
        except Exception as e:
            print(f"  {stock}: {e}")
        cur = ce + dt.timedelta(days=1)
        time.sleep(0.35)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "close", "v"]).drop_duplicates("ts")
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    return df.set_index("date")["close"]


def load_prices(years: float, use_cache: bool) -> pd.DataFrame:
    if use_cache and CACHE.exists():
        print(f"Using cached prices: {CACHE.name}")
        df = pd.read_csv(CACHE, index_col=0)
        df.index = pd.to_datetime(df.index).date
        return df
    stocks = sorted({resolve(s) for _, a, b in CANDIDATE_PAIRS for s in (a, b)})
    print(f"Fetching {years}y daily for {len(stocks)} stocks from Fyers…")
    fy = fyersModel.FyersModel(client_id=CID,
                               token=open(HERE / "access_token.txt").read().strip(),
                               log_path="")
    end = dt.date.today()
    start = end - dt.timedelta(days=int(365 * years))
    series = {}
    for i, s in enumerate(stocks, 1):
        ser = fetch_daily(fy, s, start, end)
        if ser is not None:
            series[s] = ser
        if i % 5 == 0:
            print(f"  {i}/{len(stocks)}")
    px = pd.DataFrame(series).sort_index()
    px.to_csv(CACHE)
    print(f"Cached -> {CACHE} ({px.shape[0]} days, {px.shape[1]} stocks)")
    return px


# ---------------- math (mirror strategy) ----------------
def ols(la, lb):
    beta, alpha = np.polyfit(lb, la, 1)
    return beta, alpha


def half_life(resid: np.ndarray) -> float:
    s = resid
    ds = np.diff(s)
    lag = s[:-1]
    beta = np.polyfit(lag, ds, 1)[0]
    return (-np.log(2) / beta) if beta < 0 else np.inf


# ---------------- per-pair walk-forward ----------------
def backtest_pair(sector, A_disp, B_disp, px):
    A, B = resolve(A_disp), resolve(B_disp)
    if A not in px.columns or B not in px.columns:
        return []
    s = px[[A, B]].dropna()
    if len(s) < 300:
        return []
    dates = np.array(s.index)
    la_all = np.log(s[A].values)
    lb_all = np.log(s[B].values)
    dts64 = pd.to_datetime(s.index).values  # datetime64 for window math

    def win_start(i):
        return np.searchsorted(dts64, dts64[i] - np.timedelta64(INSAMPLE_DAYS, "D"))

    # first backtest index = first day with a full 2y lookback + z window
    start_i = None
    for i in range(len(s)):
        if (dts64[i] - dts64[0]) / np.timedelta64(1, "D") >= INSAMPLE_DAYS and i >= Z_WIN:
            start_i = i
            break
    if start_i is None:
        return []

    trades, pos = [], None
    for i in range(start_i, len(s)):
        if pos is None:
            # daily refit on trailing 2y, z on last 60
            ws = win_start(i)
            if i - ws < 250:
                continue
            beta, alpha = ols(la_all[ws:i + 1], lb_all[ws:i + 1])
            sp = la_all[i - Z_WIN + 1:i + 1] - (alpha + beta * lb_all[i - Z_WIN + 1:i + 1])
            mu, sd = sp.mean(), sp.std(ddof=1)
            if sd == 0:
                continue
            z = (sp[-1] - mu) / sd
            if not (Z_ENTRY <= abs(z) < Z_STOP):
                continue
            # confirm cointegration + half-life on the trailing 2y window
            la_w, lb_w = la_all[ws:i + 1], lb_all[ws:i + 1]
            try:
                cp = coint(la_w, lb_w)[1]
                resid = la_w - (alpha + beta * lb_w)
                ap = float(adfuller(resid, autolag="AIC")[1])
                hl = half_life(resid)
            except Exception:
                continue
            if not (cp < COINT_P_MAX and ap < ADF_P_MAX and HL_MIN <= hl <= HL_MAX):
                continue
            pos = {"sector": sector, "A": A_disp, "B": B_disp, "beta": beta, "alpha": alpha,
                   "hl": hl, "dir": -1 if z > 0 else 1, "ei": i,
                   "e_spread": float(sp[-1]), "e_z": float(z),
                   "e_pa": float(s[A].values[i]), "e_pb": float(s[B].values[i])}
        else:
            # MTM with ENTRY beta/alpha
            beta, alpha = pos["beta"], pos["alpha"]
            sp = la_all[i - Z_WIN + 1:i + 1] - (alpha + beta * lb_all[i - Z_WIN + 1:i + 1])
            mu, sd = sp.mean(), sp.std(ddof=1)
            if sd == 0:
                continue
            z = (sp[-1] - mu) / sd
            days = i - pos["ei"]
            gross = pos["dir"] * (float(sp[-1]) - pos["e_spread"]) / (1 + abs(beta))
            reason = ("target" if abs(z) <= Z_EXIT else "stop" if abs(z) >= Z_STOP
                      else "time" if (np.isfinite(pos["hl"]) and days > 2 * pos["hl"]) else None)
            if reason:
                ret = (gross - 2 * COST_ONE_WAY) * 100
                trades.append({
                    "pair": f"{pos['A']}/{pos['B']}", "sector": sector,
                    "dir": "Long spread" if pos["dir"] > 0 else "Short spread",
                    "entry": str(dates[pos["ei"]]), "exit": str(dates[i]), "days": int(days),
                    "ez": round(pos["e_z"], 2), "xz": round(float(z), 2),
                    "epa": round(pos["e_pa"], 1), "epb": round(pos["e_pb"], 1),
                    "xpa": round(float(s[A].values[i]), 1), "xpb": round(float(s[B].values[i]), 1),
                    "ret": round(ret, 2), "reason": reason,
                })
                pos = None
    return trades


def stats(trades):
    if not trades:
        return {}
    r = np.array([t["ret"] for t in trades])
    wins = r[r > 0]
    return {
        "nTrades": len(trades),
        "totalRet": round(float(r.sum()), 1),
        "winRate": round(float((r > 0).mean() * 100), 1),
        "avgRet": round(float(r.mean()), 2),
        "avgWin": round(float(wins.mean()), 2) if len(wins) else 0,
        "avgLoss": round(float(r[r <= 0].mean()), 2) if len(r[r <= 0]) else 0,
        "best": round(float(r.max()), 2), "worst": round(float(r.min()), 2),
        "avgDays": round(float(np.mean([t["days"] for t in trades])), 1),
        "byReason": {k: int(sum(1 for t in trades if t["reason"] == k))
                     for k in ("target", "stop", "time")},
        "sharpe": round(float(r.mean() / r.std(ddof=1) * np.sqrt(252 / max(1, np.mean([t["days"] for t in trades])))), 2)
                  if r.std(ddof=1) > 0 else 0,
    }


def curve(trades):
    by_exit = {}
    for t in sorted(trades, key=lambda x: x["exit"]):
        by_exit[t["exit"]] = by_exit.get(t["exit"], 0) + t["ret"]
    cum, out = 0.0, []
    for d in sorted(by_exit):
        cum += by_exit[d]
        out.append({"date": d, "cum": round(cum, 1)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--cache", action="store_true")
    args = ap.parse_args()

    px = load_prices(args.years, args.cache)
    print(f"Prices: {px.shape[0]} days {px.index.min()} -> {px.index.max()}")

    all_trades = []
    for sector, A, B in CANDIDATE_PAIRS:
        ts = backtest_pair(sector, A, B, px)
        all_trades += ts
        if ts:
            tot = sum(t["ret"] for t in ts)
            print(f"  {A}/{B:<12} {len(ts):>3} trades  {tot:+.1f}%")

    all_trades.sort(key=lambda t: t["exit"])
    bt_start = min((t["entry"] for t in all_trades), default="")
    bt_end = max((t["exit"] for t in all_trades), default="")
    payload = {
        "strategy": "Cointegration pairs — enter |z|>=2 (coint p<0.05, half-life 5-30d), "
                    "exit target z=0.5 / stop z=3.5 / time-stop 2x half-life, 0.10% round-trip cost",
        "engine": "Fyers daily equity data (walk-forward, trailing-2y hedge)",
        "period": f"{bt_start} to {bt_end}",
        "pairs_universe": len(CANDIDATE_PAIRS),
        "stats": stats(all_trades),
        "curve": curve(all_trades),
        "trades": all_trades,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    st = payload["stats"]
    print(f"\nTOTAL: {st.get('nTrades',0)} trades, {st.get('totalRet',0):+.1f}% cumulative, "
          f"win {st.get('winRate',0)}%, Sharpe {st.get('sharpe',0)}")
    print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
